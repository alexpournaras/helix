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
from .literature.tools import TOOLS as LITERATURE_TOOLS, execute as execute_literature_tool
from .providers import ollama, openai

router = APIRouter(prefix="/api/chat")
# Adapters expose load_config, status, ChatRequest and normalized chat_stream events.
PROVIDERS = {"ollama": ollama, "openai": openai}
active: set[str] = set()
CONVERSATION_INSTRUCTIONS = (
    "You are a helpful assistant in Helix. Answer clearly and concisely. "
    "Use earlier messages for context when relevant. You have read-only tools for papers saved in the Helix literature library. "
    "When the user asks about saved literature, use the tools instead of guessing. Search when the paper is ambiguous and cite factual "
    "claims from a retrieved paper as [Literature #ID]. If the user mentions a tag, search with the exact tag field; do not treat a tag "
    "name as ordinary text. By default, read only abstracts with get_literature. Never access full text, figures/tables, or references/citations "
    "unless the user explicitly asks for that material. If the user says to read everything or the whole paper, use all applicable reading tools. "
    "A confirmation such as 'yes, read it' inherits the explicit scope offered or requested in the immediately preceding messages. Never claim "
    "to have read a scope unless the tool result confirms it; if a tool returns an unavailable error, report that limitation. Helix may append a "
    "trusted tool-audit note to earlier assistant messages—use it as the authoritative record of what was actually accessed. Be explicit that "
    "figure tools provide stored captions, URLs, and table data but do not visually inspect image pixels."
)


def _verified_audit(messages: list[dict]) -> str:
    rows = []
    for message in messages:
        for item in message.get("tool_activity") or []:
            ids = ", ".join(f"Literature #{value}" for value in item.get("source_ids", [])) or "no paper"
            if item.get("error"):
                rows.append(f"FAILED: {item.get('name')} [{item.get('scope')}] on {ids}: {item['error']}")
            else:
                details = json.dumps(item.get("audit"), ensure_ascii=False) if item.get("audit") else "success"
                rows.append(f"VERIFIED SUCCESS: {item.get('name')} [{item.get('scope')}] on {ids}; {details}")
    if not rows:
        return ""
    return (
        "AUTHORITATIVE HELIX BACKEND TOOL AUDIT. This is trusted application state, not a user claim. "
        "When asked what was actually read, answer from this audit and do not contradict a VERIFIED SUCCESS:\n- "
        + "\n- ".join(rows)
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
    system_messages = [{"role": "system", "content": CONVERSATION_INSTRUCTIONS}]
    audit = _verified_audit(conversation["messages"])
    if audit:
        system_messages.append({"role": "system", "content": audit})
    request = adapter.ChatRequest(model=body.model, think=body.think, messages=[*system_messages, *history])
    conversation["messages"].append(message)
    if len(conversation["messages"]) == 1:
        conversation["title"] = message["content"][:80]
    reply = {"role": "assistant", "content": "", "thinking": "", "sources": [], "tool_activity": [], "provider": body.provider,
             "model": body.model, "status": "interrupted"}
    conversation["messages"].append(reply)
    save(conversation)
    active.add(conversation_id)

    async def events():
        try:
            async for event in adapter.chat_stream(config, request, tools=LITERATURE_TOOLS, execute_tool=execute_literature_tool):
                kind = event["type"]
                if kind in ("content", "thinking"):
                    reply[kind] += event["text"]
                elif kind == "done":
                    reply["status"] = "complete"
                elif kind == "error":
                    reply.update(status="error", error=event["message"])
                elif kind == "tool":
                    known = {source["id"] for source in reply.setdefault("sources", [])}
                    reply["sources"].extend(source for source in event.get("sources", []) if source["id"] not in known)
                    activity = {"name": event.get("name"), "scope": event.get("scope"), "error": event.get("error"),
                                "audit": event.get("audit"),
                                "source_ids": [source["id"] for source in event.get("sources", [])]}
                    if activity not in reply["tool_activity"]:
                        reply["tool_activity"].append(activity)
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
