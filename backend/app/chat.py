"""Persistent conversations and provider-neutral streaming endpoints."""
import asyncio
import json
import re
import sqlite3
from contextlib import contextmanager
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .config import settings
from .chat_tools import TOOLS as CHAT_TOOLS, execute as execute_chat_tool
from .providers import ollama, openai

router = APIRouter(prefix="/api/chat")
# Adapters expose load_config, status, ChatRequest and normalized chat_stream events.
PROVIDERS = {"ollama": ollama, "openai": openai}
active: set[str] = set()
CONVERSATION_INSTRUCTIONS = (
    "You are a helpful assistant in Helix. Answer clearly and concisely. "
    "Use earlier messages for context when relevant. You have tools for papers saved in the Helix literature library. Reading tools are "
    "read-only. You may start a fetch/import only when the user explicitly asks to fetch, import, download, or save papers, and may cancel "
    "one or all active fetches only when explicitly asked to stop or cancel them. Requests such as 'stop Europe PMC', 'stop run 35', and "
    "'stop fetch 35' are explicit; call the cancellation tool immediately without demanding exact wording. Papers saved before cancellation are kept. Deleting all "
    "literature is destructive: on the initial request, ask for confirmation and do not call the deletion tool; call it only after a later "
    "direct confirmation. Accept natural confirmation language and never demand an exact phrase. "
    "When the user asks to list papers, show all papers, or asks what is in the library, use list_literature without demanding a query. "
    "When the user asks about saved literature, use the tools instead of guessing. Search when the paper is ambiguous and cite factual "
    "claims from a retrieved paper as [Literature #ID]. If the user mentions a tag, search with the exact tag field; do not treat a tag "
    "name as ordinary text. By default, read only abstracts with get_literature. Never access full text, figures/tables, or references/citations "
    "unless the user explicitly asks for that material. If the user says to read everything or the whole paper, use all applicable reading tools. "
    "A confirmation such as 'yes, read it' inherits the explicit scope offered or requested in the immediately preceding messages. Never claim "
    "to have read a scope unless the tool result confirms it; if a tool returns an unavailable error, report that limitation. Helix may append a "
    "trusted tool-audit note to earlier assistant messages—use it as the authoritative record of what was actually accessed. Be explicit that "
    "figure tools provide stored captions, URLs, and table data but do not visually inspect image pixels."
    " You can also list, search, and read Helix notes. Note tags are independent from literature tags; adding a tag to a note never requires "
    "a literature search or a paper with that tag. For tag-only changes, always use add_note_tag or remove_note_tag; those tools preserve the "
    "title and content automatically. Create or edit a note only when the user explicitly asks you to do so. When the user asks for two or "
    "more notes, use create_notes once instead of repeated create_note calls. If the user supplies only a topic or ambiguous acronym, do not "
    "invent facts, citations, dates, or an expanded meaning; use a simple title based on their words and leave content empty. If the user "
    "explicitly asks you to draft or improvise content, you may draft it; a later 'yes' or 'save it' confirms the most recent unresolved note "
    "create or edit request, so perform that pending mutation instead of asking them to repeat it. "
    "Delete a note only when the user explicitly asks to delete it; a direct confirmation such as 'all of them' inherits the immediately "
    "preceding note request. Use exact note IDs returned by the note tools. Before appending to or "
    "partially changing a note, read it first so existing content is preserved. Never substitute one note action for another. Report a note "
    "mutation as successful only when that exact tool returns success; do not claim a note was created, edited, or deleted otherwise."
)

NOTE_MUTATION_WORDS = {
    "create_note": r"\b(create|make|remember|jot|record|start)\b|\bsave\b.{0,30}\bnote\b|\bnote\b.{0,30}\bsave\b|\b(new|another)\s+note\b|\b(write|take)\b.{0,30}\bnote\b|\bnote down\b|δημιουργ|φτιάξ|αποθήκευσ|σημείω",
    "create_notes": r"\b(create|make|remember|jot|record|start)\b|\bsave\b.{0,30}\bnotes\b|\bnotes\b.{0,30}\bsave\b|\b(new|multiple|several|two|three|four|five)\s+notes?\b|\b(write|take)\b.{0,30}\bnotes?\b|δημιουργ|φτιάξ|αποθήκευσ|σημείω",
    "edit_note": r"\b(edit|update|change|modify|append|amend|revise|rewrite|add|pin|unpin|link|unlink)\b|\b(remove|delete|clear)\b.{0,30}\b(tag|paper|literature)\b|ενημέρωσ|επεξεργ|αλλάξ|τροποποίησ|πρόσθεσ|καρφίτσωσ|σύνδεσ|αποσύνδεσ",
    "add_note_tag": r"\b(add|apply|attach|set|give)\b.{0,30}\btag\b|\btag\b.{0,30}\b(add|apply|attach|with)\b|πρόσθεσ.{0,30}ετικέτ",
    "remove_note_tag": r"\b(remove|delete|clear|drop)\b.{0,30}\btag\b|αφαίρεσ.{0,30}ετικέτ|σβήσ.{0,30}ετικέτ",
    "delete_note": r"\b(delete|remove|trash|erase)\b|\bget rid of\b|διαγραφ|σβήσ|αφαίρεσ",
    "delete_all_notes": r"\b(delete|remove|trash|erase)\b|\bget rid of\b|διαγραφ|σβήσ|αφαίρεσ",
}
ALL_NOTES_WORDS = re.compile(r"\b(all|every|both|everything)\b|όλα|όλες", re.IGNORECASE)
NOTE_CONFIRMATION = re.compile(
    r"\s*(yes\b.*|yep\b.*|yeah\b.*|ok|okay|sure|confirm|confirmed|(?:please\s+)?(?:just\s+)?do it(?:\s+now)?|can (?:you|u) do it(?:\s+now)?|go ahead|please do|proceed|save|save it|save them|save please|try again|retry|please retry|again|use that|that works|all|all of them|them all|both|everything|"
    r"ναι|οκ|όλα|όλα τους|κάντο|κάν' το|προχώρα)[\s.!?]*",
    re.IGNORECASE,
)
NOTE_MUTATION_AUDIT_ACTIONS = {"create", "create_many", "edit", "delete", "delete_all", "add_tag", "remove_tag"}
LITERATURE_DELETE_PATTERN = re.compile(
    r"\b(delete|remove|erase|trash)\b.{0,50}\b(all|every|everything)\b.{0,30}\b(papers?|literatures?|articles?)\b|"
    r"\b(delete|remove|erase|trash)\b.{0,50}\b(papers?|literatures?|articles?)\b.{0,30}\b(all|every|everything)\b",
    re.IGNORECASE,
)
LITERATURE_FETCH_PATTERN = re.compile(
    r"\b(fetch|import|download)\b.{0,80}\b(papers?|literature|articles?)\b|"
    r"\b(save|add)\b.{0,80}\b(papers?|literature|articles?)\b",
    re.IGNORECASE,
)
LITERATURE_CANCEL_FETCH_PATTERN = re.compile(
    r"\b(cancel|stop|halt|abort)\b.{0,100}\b(fetch(?:es|ing)?|imports?|importing|downloads?|downloading|run\s*#?\s*\d+|"
    r"pubmed|pubmed\s+central|pmc|europe\s*pmc)\b",
    re.IGNORECASE,
)
LITERATURE_CANCEL_FOLLOWUP = re.compile(
    r"\s*(yes(?:\s+stop)?|stop|cancel|do it|go ahead|try again|retry|please stop)[\s.!?]*",
    re.IGNORECASE,
)
LITERATURE_REPEATED_CONFIRMATION = re.compile(
    r"\b(delete|remove|erase|trash)\b.{0,80}\b(papers?|literature|articles?|them)\b.{0,30}\b(yes|now|confirmed?)\b|"
    r"\b(yes|confirmed?)\b.{0,30}\b(delete|remove|erase|trash)\b.{0,80}\b(papers?|literature|articles?|them)\b|"
    r"\b(i\s+)?just\s+(said|told\s+you)\b.{0,50}\b(delete|remove|erase|trash)\b",
    re.IGNORECASE,
)
LITERATURE_SAFE_CONFIRMATION = re.compile(
    r"\s*(yes|yep|yeah|ok|okay|sure|confirm|confirmed|do it|go ahead|please do|proceed)[\s.!?]*",
    re.IGNORECASE,
)
LITERATURE_CONFIRMATION_SIGNAL = re.compile(
    r"\b(delete|remove|erase|trash)\b",
    re.IGNORECASE,
)
LITERATURE_CANCELLATION_SIGNAL = re.compile(
    r"\b(no|cancel|stop|abort|nevermind|never mind)\b|\b(don't|do not|dont)\b.{0,30}\b(delete|remove|erase|trash)\b",
    re.IGNORECASE,
)


def _note_intent_context(user_message: str, previous_user_message: str = "") -> str:
    """A direct confirmation inherits only the immediately preceding user's requested action."""
    if previous_user_message and NOTE_CONFIRMATION.fullmatch(user_message):
        return f"{previous_user_message}\nConfirmation: {user_message}"
    return user_message


def _pending_note_intent(user_message: str, messages: list[dict]) -> str:
    """Resolve a short approval against the latest unfinished note mutation request."""
    if not NOTE_CONFIRMATION.fullmatch(user_message):
        return user_message
    for item in reversed(messages):
        if item.get("role") == "assistant" and any(
            not activity.get("error") and (activity.get("audit") or {}).get("action") in NOTE_MUTATION_AUDIT_ACTIONS
            for activity in item.get("tool_activity") or []
        ):
            break
        if item.get("role") == "user" and any(
            _note_mutation_allowed(tool_name, item.get("content", "")) for tool_name in NOTE_MUTATION_WORDS
        ):
            return f"{item['content']}\nConfirmation: {user_message}"
    return user_message


def _literature_delete_confirmed(user_message: str, messages: list[dict]) -> bool:
    if LITERATURE_CANCELLATION_SIGNAL.search(user_message):
        return False
    if (not LITERATURE_SAFE_CONFIRMATION.fullmatch(user_message)
            and not LITERATURE_REPEATED_CONFIRMATION.search(user_message)
            and not LITERATURE_CONFIRMATION_SIGNAL.search(user_message)):
        return False
    for item in reversed(messages):
        if item.get("role") == "assistant" and any(
            not activity.get("error") and (activity.get("audit") or {}).get("action") == "delete_all_literature"
            for activity in item.get("tool_activity") or []
        ):
            return False
        if item.get("role") == "user" and LITERATURE_DELETE_PATTERN.search(item.get("content", "")):
            return True
    return False


def _pending_fetch_cancel_intent(user_message: str, messages: list[dict]) -> str:
    if LITERATURE_CANCEL_FETCH_PATTERN.search(user_message):
        return user_message
    if not LITERATURE_CANCEL_FOLLOWUP.fullmatch(user_message):
        return user_message
    for item in reversed(messages):
        if item.get("role") == "assistant" and any(
            not activity.get("error") and (activity.get("audit") or {}).get("action") in
            ("cancel_literature_fetch", "cancel_all_literature_fetches")
            for activity in item.get("tool_activity") or []
        ):
            break
        if item.get("role") == "user" and LITERATURE_CANCEL_FETCH_PATTERN.search(item.get("content", "")):
            return f"{item['content']}\nConfirmation: {user_message}"
    return user_message


def _note_mutation_allowed(tool_name: str, user_message: str) -> bool:
    pattern = NOTE_MUTATION_WORDS.get(tool_name)
    allowed = pattern is None or re.search(pattern, user_message.casefold()) is not None
    if tool_name == "delete_all_notes":
        allowed = allowed and ALL_NOTES_WORDS.search(user_message) is not None
    return allowed


def _validate_multi_note_tags(user_message: str, arguments) -> str | None:
    """Enforce an explicit 'one note has tag X' distribution before any notes are created."""
    match = re.search(r"\bone\b.{0,80}\btag(?:ged)?(?:\s+(?:with|as))?\s*[\"']([^\"']+)[\"']", user_message, re.IGNORECASE)
    if not match:
        return None
    try:
        parsed = arguments if isinstance(arguments, dict) else json.loads(arguments or "{}")
        items = parsed.get("notes") if isinstance(parsed, dict) else None
    except (TypeError, ValueError):
        items = None
    if not isinstance(items, list):
        return None
    requested_tag = match.group(1).strip().casefold()
    tagged_count = sum(
        1 for item in items if isinstance(item, dict) and
        any(str(tag).strip().casefold() == requested_tag for tag in item.get("tags", []))
    )
    if tagged_count != 1:
        return f'The user requested tag "{match.group(1).strip()}" on exactly one new note, but the tool call applied it to {tagged_count}. No notes were created.'
    return None


def _verified_audit(messages: list[dict]) -> str:
    rows = []
    for message in messages:
        for item in message.get("tool_activity") or []:
            item_audit = item.get("audit") or {}
            note_id = item_audit.get("note_id")
            note_ids = item_audit.get("note_ids") or []
            ids = (f"Note #{note_id}" if note_id else ", ".join(f"Note #{value}" for value in note_ids) if note_ids
                   else ", ".join(f"Literature #{value}" for value in item.get("source_ids", [])) or "no record")
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
    current_content = body.content.strip()
    mutation_intent = _pending_note_intent(current_content, conversation["messages"])
    literature_delete_confirmed = _literature_delete_confirmed(current_content, conversation["messages"])
    fetch_cancel_intent = _pending_fetch_cancel_intent(current_content, conversation["messages"])
    history = [{"role": m["role"], "content": m["content"]} for m in conversation["messages"]
               if m["role"] == "user" or m.get("status") == "complete"]
    message = {"role": "user", "content": current_content}
    history.append(message)
    system_messages = [{"role": "system", "content": CONVERSATION_INSTRUCTIONS}]
    audit = _verified_audit(conversation["messages"])
    if audit:
        system_messages.append({"role": "system", "content": audit})
    confirmed_literature_delete = execute_chat_tool("delete_all_literature", {}) if literature_delete_confirmed else None
    if mutation_intent != current_content:
        system_messages.append({"role": "system", "content": (
            "AUTHORITATIVE HELIX REQUEST RESOLUTION. The current short reply is a confirmation or retry of the unresolved note request below. "
            "The user does not need to repeat exact wording. Perform the matching note tool now. If you already drafted the requested content "
            "in an earlier assistant message, reuse that draft as the tool input. Do not ask for another confirmation.\n"
            f"Resolved request:\n{mutation_intent}"
        )})
    if literature_delete_confirmed:
        system_messages.append({"role": "system", "content": (
            "AUTHORITATIVE HELIX LITERATURE DELETION RESULT. Helix already executed the user's confirmed request to delete all saved "
            "literature. Report this exact backend result accurately. Do not request another confirmation and do not call the deletion tool again.\n"
            f"Result: {json.dumps(confirmed_literature_delete, ensure_ascii=False)}"
        )})
    if LITERATURE_CANCEL_FETCH_PATTERN.search(fetch_cancel_intent):
        system_messages.append({"role": "system", "content": (
            "AUTHORITATIVE HELIX FETCH CANCELLATION REQUEST. The user explicitly requested cancellation of a literature fetch. Call the "
            "matching cancel_literature_fetch or cancel_all_literature_fetches tool now. Resolve the source or run ID from the recent "
            "conversation. Do not ask the user to repeat or reword the request.\n"
            f"Resolved request:\n{fetch_cancel_intent}"
        )})
    request = adapter.ChatRequest(model=body.model, think=body.think, messages=[*system_messages, *history])
    conversation["messages"].append(message)
    if len(conversation["messages"]) == 1:
        conversation["title"] = message["content"][:80]
    reply = {"role": "assistant", "content": "", "thinking": "", "sources": [], "tool_activity": [], "provider": body.provider,
             "model": body.model, "status": "interrupted"}
    if confirmed_literature_delete is not None:
        reply["tool_activity"].append({
            "name": "delete_all_literature", "scope": confirmed_literature_delete.get("scope"),
            "error": confirmed_literature_delete.get("error"), "audit": confirmed_literature_delete.get("audit"), "source_ids": [],
        })
    conversation["messages"].append(reply)
    save(conversation)
    active.add(conversation_id)
    def execute_tool(name, arguments):
        if name == "delete_all_literature" and confirmed_literature_delete is not None:
            return confirmed_literature_delete
        if name == "delete_all_literature" and not literature_delete_confirmed:
            return {"error": "Deleting all literature requires confirmation in a separate follow-up message. No papers were deleted.",
                    "scope": "literature_delete_confirmation_required", "sources": []}
        if name == "fetch_literature" and not LITERATURE_FETCH_PATTERN.search(current_content):
            return {"error": "The current user message did not explicitly request a literature fetch or import. No fetch was started.",
                    "scope": "literature_fetch_refused", "sources": []}
        if name in ("cancel_literature_fetch", "cancel_all_literature_fetches") and not LITERATURE_CANCEL_FETCH_PATTERN.search(fetch_cancel_intent):
            return {"error": "The current user message did not explicitly request stopping a literature fetch. No fetch was cancelled.",
                    "scope": "literature_fetch_cancel_refused", "sources": []}
        if not _note_mutation_allowed(name, mutation_intent):
            action = {
                "delete_all_notes": "delete all notes", "create_notes": "create multiple notes",
                "add_note_tag": "add a note tag", "remove_note_tag": "remove a note tag",
            }.get(name, name.removesuffix("_note"))
            return {"error": f"The current user message did not explicitly request a note {action}. No note was changed.",
                    "scope": "note_action_refused", "sources": []}
        if name == "create_notes":
            error = _validate_multi_note_tags(mutation_intent, arguments)
            if error:
                return {"error": error, "scope": "note_action_refused", "sources": []}
        return execute_chat_tool(name, arguments)

    async def events():
        try:
            if confirmed_literature_delete is not None:
                yield json.dumps({"type": "tool", "name": "delete_all_literature",
                                  "scope": confirmed_literature_delete.get("scope"),
                                  "error": confirmed_literature_delete.get("error"),
                                  "audit": confirmed_literature_delete.get("audit"), "sources": []}) + "\n"
            available_tools = [tool for tool in CHAT_TOOLS if not (
                confirmed_literature_delete is not None and tool["function"]["name"] == "delete_all_literature"
            )]
            async for event in adapter.chat_stream(config, request, tools=available_tools, execute_tool=execute_tool):
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
