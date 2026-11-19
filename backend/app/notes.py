"""Persistent workspace notes."""
import json
import sqlite3
from contextlib import contextmanager

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from .config import settings

router = APIRouter(prefix="/api/notes", tags=["notes"])


class NoteInput(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(default="", max_length=100_000)
    tags: list[str] = Field(default_factory=list, max_length=20)
    pinned: bool = False
    literature_ids: list[int] = Field(default_factory=list, max_length=50)

    @field_validator("title")
    @classmethod
    def clean_title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Enter a title.")
        return value

    @field_validator("tags")
    @classmethod
    def clean_tags(cls, values: list[str]) -> list[str]:
        cleaned = []
        seen = set()
        for value in values:
            tag = value.strip()
            if not tag:
                continue
            if len(tag) > 40:
                raise ValueError("Tags must be 40 characters or fewer.")
            key = tag.casefold()
            if key not in seen:
                seen.add(key)
                cleaned.append(tag)
        return cleaned

    @field_validator("literature_ids")
    @classmethod
    def clean_literature_ids(cls, values: list[int]) -> list[int]:
        if any(value < 1 for value in values):
            raise ValueError("Paper IDs must be positive.")
        return list(dict.fromkeys(values))


@contextmanager
def connect():
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(settings.data_dir / "notes.sqlite3")
    try:
        db.row_factory = sqlite3.Row
        with db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL DEFAULT '',
                    tags TEXT NOT NULL DEFAULT '[]',
                    pinned INTEGER NOT NULL DEFAULT 0,
                    literature_ids TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(notes)")}
            if "tags" not in columns:
                db.execute("ALTER TABLE notes ADD COLUMN tags TEXT NOT NULL DEFAULT '[]'")
            if "pinned" not in columns:
                db.execute("ALTER TABLE notes ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
            if "literature_ids" not in columns:
                db.execute("ALTER TABLE notes ADD COLUMN literature_ids TEXT NOT NULL DEFAULT '[]'")
            yield db
    finally:
        db.close()


def _read(db: sqlite3.Connection, note_id: int):
    row = db.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Note not found.")
    return _note(row)


def _note(row: sqlite3.Row) -> dict:
    item = dict(row)
    item["tags"] = json.loads(item["tags"] or "[]")
    item["pinned"] = bool(item["pinned"])
    item["literature_ids"] = json.loads(item["literature_ids"] or "[]")
    return item


@router.get("")
def list_notes():
    with connect() as db:
        return [_note(row) for row in db.execute("SELECT * FROM notes ORDER BY pinned DESC, updated_at DESC, id DESC")]


@router.post("", status_code=201)
def create_note(body: NoteInput):
    with connect() as db:
        cursor = db.execute(
            """INSERT INTO notes (title, content, tags, pinned, literature_ids, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))""",
            (body.title, body.content, json.dumps(body.tags), body.pinned, json.dumps(body.literature_ids)),
        )
        return _read(db, cursor.lastrowid)


def create_notes(bodies: list[NoteInput]) -> list[dict]:
    """Create several notes in one database transaction."""
    created = []
    with connect() as db:
        for body in bodies:
            cursor = db.execute(
                """INSERT INTO notes (title, content, tags, pinned, literature_ids, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))""",
                (body.title, body.content, json.dumps(body.tags), body.pinned, json.dumps(body.literature_ids)),
            )
            created.append(_read(db, cursor.lastrowid))
    return created


@router.get("/{note_id}")
def get_note(note_id: int):
    with connect() as db:
        return _read(db, note_id)


@router.put("/{note_id}")
def update_note(note_id: int, body: NoteInput):
    with connect() as db:
        _read(db, note_id)
        db.execute(
            """UPDATE notes SET title = ?, content = ?, tags = ?, pinned = ?, literature_ids = ?,
               updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?""",
            (body.title, body.content, json.dumps(body.tags), body.pinned, json.dumps(body.literature_ids), note_id),
        )
        return _read(db, note_id)


@router.delete("/{note_id}", status_code=204)
def delete_note(note_id: int):
    with connect() as db:
        _read(db, note_id)
        db.execute("DELETE FROM notes WHERE id = ?", (note_id,))


def delete_all_notes() -> list[int]:
    """Delete every note in one database transaction and return the deleted IDs."""
    with connect() as db:
        ids = [row["id"] for row in db.execute("SELECT id FROM notes ORDER BY id")]
        db.execute("DELETE FROM notes")
        return ids
