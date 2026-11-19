"""AI tools for reading and writing Helix notes."""
import json
from typing import Any

from fastapi import HTTPException
from pydantic import ValidationError

from . import notes as note_store

MAX_NOTE_CHARS = 60_000

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_notes",
            "description": "List or search notes saved in Helix. Returns note IDs, titles, tags, pinned state, linked paper IDs, and text excerpts.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Optional words to search in titles, text, and tags."},
                    "tag": {"type": "string", "description": "Optional exact note tag, matched case-insensitively."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 10},
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_note",
            "description": "Read one saved note by its exact Helix note ID, including its text, tags, pin state, and linked paper IDs.",
            "parameters": {
                "type": "object",
                "properties": {"id": {"type": "integer", "minimum": 1, "description": "The note ID returned by list_notes or create_note."}},
                "required": ["id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_note",
            "description": "Create a new note only when the user explicitly asks to save or create a new note. Never use this to edit, replace, or delete an existing note.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "minLength": 1, "maxLength": 200},
                    "content": {"type": "string", "maxLength": 100000},
                    "tags": {"type": "array", "items": {"type": "string", "maxLength": 40}, "maxItems": 20},
                    "pinned": {"type": "boolean"},
                    "literature_ids": {"type": "array", "items": {"type": "integer", "minimum": 1}, "maxItems": 50},
                },
                "required": ["title"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_note",
            "description": "Edit an existing note only when the user explicitly requests it. Fields supplied here replace the saved values; read the note first when preserving or appending to its text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "minimum": 1},
                    "title": {"type": "string", "minLength": 1, "maxLength": 200},
                    "content": {"type": "string", "maxLength": 100000},
                    "tags": {"type": "array", "items": {"type": "string", "maxLength": 40}, "maxItems": 20},
                    "pinned": {"type": "boolean"},
                    "literature_ids": {"type": "array", "items": {"type": "integer", "minimum": 1}, "maxItems": 50},
                },
                "required": ["id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_notes",
            "description": "Create two or more new notes in one action when the user explicitly asks for multiple notes. Note tags are independent from literature tags and need no paper lookup.",
            "parameters": {
                "type": "object",
                "properties": {
                    "notes": {
                        "type": "array", "minItems": 2, "maxItems": 20,
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string", "minLength": 1, "maxLength": 200},
                                "content": {"type": "string", "maxLength": 100000, "description": "Use text supplied or explicitly requested by the user. Otherwise leave empty; never invent facts."},
                                "tags": {"type": "array", "items": {"type": "string", "maxLength": 40}, "maxItems": 20},
                                "pinned": {"type": "boolean"},
                                "literature_ids": {"type": "array", "items": {"type": "integer", "minimum": 1}, "maxItems": 50},
                            },
                            "required": ["title"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["notes"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_note_tag",
            "description": "Add one tag to an existing note. Use this instead of edit_note when the user asks to add or apply a tag. Existing title and content are preserved automatically.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "minimum": 1, "description": "The exact note ID."},
                    "tag": {"type": "string", "minLength": 1, "maxLength": 40},
                },
                "required": ["id", "tag"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_note_tag",
            "description": "Remove one tag from an existing note. Use this instead of edit_note when the user asks to remove or clear a tag. Existing title and content are preserved automatically.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "minimum": 1, "description": "The exact note ID."},
                    "tag": {"type": "string", "minLength": 1, "maxLength": 40},
                },
                "required": ["id", "tag"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_note",
            "description": "Permanently delete one existing note only when the user explicitly asks to delete or remove it. Never create a replacement note.",
            "parameters": {
                "type": "object",
                "properties": {"id": {"type": "integer", "minimum": 1, "description": "The exact note ID to delete."}},
                "required": ["id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_all_notes",
            "description": "Permanently delete every saved note in one action, only when the user explicitly asks to delete all notes.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
]

NAMES = {tool["function"]["name"] for tool in TOOLS}


def _arguments(value: str | dict | None) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def _limit(value: Any) -> int:
    try:
        return min(20, max(1, int(value or 10)))
    except (TypeError, ValueError):
        return 10


def _id(args: dict[str, Any]) -> int | None:
    try:
        value = int(args.get("id"))
        return value if value > 0 else None
    except (TypeError, ValueError):
        return None


def _error(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, HTTPException):
        return {"error": str(exc.detail), "sources": []}
    if isinstance(exc, ValidationError):
        message = " ".join(str(item["msg"]).removeprefix("Value error, ") for item in exc.errors())
        return {"error": message, "sources": []}
    return {"error": str(exc), "sources": []}


def _summary(note: dict[str, Any]) -> dict[str, Any]:
    content = note["content"].strip().replace("\n", " ")
    return {
        "id": note["id"], "title": note["title"], "excerpt": content[:500], "tags": note["tags"],
        "pinned": note["pinned"], "literature_ids": note["literature_ids"], "updated_at": note["updated_at"],
    }


def execute(name: str, raw_arguments: str | dict | None) -> dict[str, Any]:
    args = _arguments(raw_arguments)
    try:
        if name == "list_notes":
            query = str(args.get("query") or "").strip().casefold()
            tag = str(args.get("tag") or "").strip().casefold()
            notes = note_store.list_notes()
            matches = [note for note in notes if
                       (not query or query in f"{note['title']} {note['content']} {' '.join(note['tags'])}".casefold()) and
                       (not tag or any(value.casefold() == tag for value in note["tags"]))]
            limited = matches[:_limit(args.get("limit"))]
            return {"total": len(matches), "notes": [_summary(note) for note in limited], "scope": "notes_list",
                    "audit": {"action": "list", "count": len(limited)}, "sources": []}

        if name == "delete_all_notes":
            ids = note_store.delete_all_notes()
            return {"deleted_count": len(ids), "deleted_ids": ids, "scope": "notes_deleted",
                    "audit": {"action": "delete_all", "note_ids": ids, "count": len(ids)}, "sources": []}

        if name == "create_notes":
            raw_notes = args.get("notes")
            if not isinstance(raw_notes, list) or not 2 <= len(raw_notes) <= 20:
                return {"error": "Provide between 2 and 20 notes.", "sources": []}
            bodies = [note_store.NoteInput(**item) for item in raw_notes]
            created = note_store.create_notes(bodies)
            ids = [note["id"] for note in created]
            return {"notes": created, "scope": "notes_created",
                    "audit": {"action": "create_many", "note_ids": ids, "count": len(ids)}, "sources": []}

        note_id = _id(args)
        if name in ("get_note", "edit_note", "delete_note", "add_note_tag", "remove_note_tag") and note_id is None:
            return {"error": "A valid note ID is required.", "sources": []}

        if name == "get_note":
            note = note_store.get_note(note_id)
            content = note["content"][:MAX_NOTE_CHARS]
            return {"note": {**note, "content": content, "content_truncated": len(note["content"]) > len(content)},
                    "scope": "note", "audit": {"action": "read", "note_id": note_id, "characters": len(content)}, "sources": []}

        if name == "create_note":
            body = note_store.NoteInput(**args)
            note = note_store.create_note(body)
            return {"note": note, "scope": "note_created",
                    "audit": {"action": "create", "note_id": note["id"]}, "sources": []}

        if name == "edit_note":
            changes = {key: value for key, value in args.items() if key != "id"}
            if not changes:
                return {"error": "Provide at least one field to edit.", "sources": []}
            current = note_store.get_note(note_id)
            body = note_store.NoteInput(**{**current, **changes})
            note = note_store.update_note(note_id, body)
            return {"note": note, "scope": "note_edited",
                    "audit": {"action": "edit", "note_id": note_id, "fields": sorted(changes)}, "sources": []}

        if name in ("add_note_tag", "remove_note_tag"):
            tag = str(args.get("tag") or "").strip()
            if not tag:
                return {"error": "A tag is required.", "sources": []}
            if len(tag) > 40:
                return {"error": "Tags must be 40 characters or fewer.", "sources": []}
            current = note_store.get_note(note_id)
            matching = next((value for value in current["tags"] if value.casefold() == tag.casefold()), None)
            if name == "add_note_tag":
                if matching:
                    return {"error": f'Note #{note_id} already has the tag "{matching}". No change was made.', "sources": []}
                tags = [*current["tags"], tag]
                action, scope = "add_tag", "note_tag_added"
            else:
                if not matching:
                    return {"error": f'Note #{note_id} does not have the tag "{tag}". No change was made.', "sources": []}
                tags = [value for value in current["tags"] if value.casefold() != tag.casefold()]
                action, scope = "remove_tag", "note_tag_removed"
            body = note_store.NoteInput(**{**current, "tags": tags})
            note = note_store.update_note(note_id, body)
            return {"note": note, "scope": scope,
                    "audit": {"action": action, "note_id": note_id, "tag": tag}, "sources": []}

        if name == "delete_note":
            current = note_store.get_note(note_id)
            note_store.delete_note(note_id)
            return {"deleted": {"id": note_id, "title": current["title"]}, "scope": "note_deleted",
                    "audit": {"action": "delete", "note_id": note_id}, "sources": []}
    except (HTTPException, ValidationError, ValueError) as exc:
        return _error(exc)

    return {"error": f"Unknown tool: {name}", "sources": []}
