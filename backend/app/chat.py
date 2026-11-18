"""Persistent conversations and provider-neutral streaming endpoints."""
import asyncio
import json
import sqlite3
from contextlib import contextmanager
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .config import settings
from .providers import ollama, openai

router = APIRouter(prefix="/api/chat")
# Adapters expose load_config, status, ChatRequest and normalized chat_stream events.
PROVIDERS = {"ollama": ollama, "openai": openai}
active: set[str] = set()
CONVERSATION_INSTRUCTIONS = (
    "You are a helpful assistant in Helix. Answer clearly and concisely. "
    "Use earlier messages for context when relevant."
)


@contextmanager
def connect():
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(settings.data_dir / "chat.sqlite3")
    try:
        db.row_factory = sqlite3.Row
        with db:
            db.execute("CREATE TABLE IF NOT EXISTS conversations (id TEXT PRIMARY KEY, title TEXT NOT NULL, messages TEXT NOT NULL, updated_at TEXT NOT NULL)")
            yield db
    finally:
        db.close()


def read(conversation_id):
    with connect() as db:
        row = db.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Conversation not found.")
    return {**dict(row), "messages": json.loads(row["messages"])}


def save(conversation):
    with connect() as db:
        db.execute("INSERT OR REPLACE INTO conversations VALUES (?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))",
                   (conversation["id"], conversation["title"], json.dumps(conversation["messages"])))


@router.get("/providers")
async def providers():
    result = []
    for name, adapter in PROVIDERS.items():
        config = adapter.load_config()
        status = await adapter.status(config)
        display_name = "OpenAI" if name == "openai" else name.title()
        result.append({"id": name, "name": display_name, "default_model": config.default_model,
                       "models": status["models"], "reachable": status["reachable"], "error": status["error"]})
    return result


@router.get("/conversations")
def conversations():
    with connect() as db:
        return [dict(row) for row in db.execute("SELECT id, title, updated_at FROM conversations ORDER BY updated_at DESC")]


@router.post("/conversations", status_code=201)
def create():
    conversation = {"id": uuid4().hex, "title": "New conversation", "messages": []}
    save(conversation)
    return read(conversation["id"])


@router.get("/conversations/{conversation_id}")
def get(conversation_id: str):
    return read(conversation_id)


@router.delete("/conversations/{conversation_id}", status_code=204)
async def delete(conversation_id: str):
    if conversation_id in active:
        raise HTTPException(409, "Stop the reply before deleting this conversation.")
    read(conversation_id)
    with connect() as db:
        db.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))


class SendMessage(BaseModel):
    provider: str = "ollama"
    think: bool = False
    model: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=100_000)


@router.post("/conversations/{conversation_id}/messages")
async def send(conversation_id: str, body: SendMessage):
    adapter = PROVIDERS.get(body.provider)
    if adapter is None:
        raise HTTPException(422, "Unsupported AI provider.")
    if not body.content.strip():
        raise HTTPException(422, "Enter a message first.")
    if conversation_id in active:
        raise HTTPException(409, "This conversation already has a reply in progress.")
    conversation = read(conversation_id)
    config = adapter.load_config()
    history = [{"role": m["role"], "content": m["content"]} for m in conversation["messages"]
               if m["role"] == "user" or m.get("status") == "complete"]
    message = {"role": "user", "content": body.content.strip()}
    history.append(message)
    request = adapter.ChatRequest(model=body.model, think=body.think, messages=[
        {"role": "system", "content": CONVERSATION_INSTRUCTIONS}, *history,
    ])
    conversation["messages"].append(message)
    if len(conversation["messages"]) == 1:
        conversation["title"] = message["content"][:80]
    reply = {"role": "assistant", "content": "", "thinking": "", "provider": body.provider,
             "model": body.model, "status": "interrupted"}
    conversation["messages"].append(reply)
    save(conversation)
    active.add(conversation_id)

    async def events():
        try:
            async for event in adapter.chat_stream(config, request):
                kind = event["type"]
                if kind in ("content", "thinking"):
                    reply[kind] += event["text"]
                elif kind == "done":
                    reply["status"] = "complete"
                elif kind == "error":
                    reply.update(status="error", error=event["message"])
                yield json.dumps(event) + "\n"
            if reply["status"] == "interrupted":
                reply.update(status="error", error="The connection ended before the reply finished.")
                yield json.dumps({"type": "error", "message": reply["error"]}) + "\n"
        except asyncio.CancelledError:
            raise
        except Exception:
            reply.update(status="error", error="The reply could not be completed. Try again.")
            yield json.dumps({"type": "error", "message": reply["error"]}) + "\n"
        finally:
            try:
                save(conversation)
            finally:
                active.discard(conversation_id)

    return StreamingResponse(events(), media_type="application/x-ndjson", headers={"Cache-Control": "no-cache"})
