"""Persistent workspace notes.

Research columns (PLAN B3, B12): `investigation_id`, `kind` (null for the user's own notes), `origin`
(user/ai), `artifact_key`, `revision`, `superseded_by`. In M1 research Notes are written once, at the end.
`secure_delete` is on, so deleted text is overwritten on disk.
"""
import json
import sqlite3
from contextlib import contextmanager
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from .config import settings

router = APIRouter(prefix="/api/notes", tags=["notes"])

MAX_LITERATURE_IDS = 200
# B12 Note kinds (key in brackets): case_brief (brief), clarifications (questions), safety_review (safety),
# research_plan (plan), search_log (search), paper_card (paper:{id}), evidence_matrix (matrix),
# hypothesis_analysis (hyp:{key}), gaps (gaps), critic_review (critic:v{n}), final_report (report:v{n}).
NOTE_KINDS = ("case_brief", "clarifications", "safety_review", "research_plan", "search_log", "paper_card",
              "evidence_matrix", "hypothesis_analysis", "gaps", "critic_review", "final_report")
Origin = Literal["user", "ai"]


class NoteInput(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(default="", max_length=100_000)
    tags: list[str] = Field(default_factory=list, max_length=20)
    pinned: bool = False
    literature_ids: list[int] = Field(default_factory=list, max_length=MAX_LITERATURE_IDS)
    investigation_id: int | None = Field(default=None, ge=1)

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
        db.execute("PRAGMA secure_delete = ON")
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
            for column, ddl in (
                ("investigation_id", "INTEGER"),
                ("kind", "TEXT"),
                ("origin", "TEXT NOT NULL DEFAULT 'user'"),
                ("artifact_key", "TEXT"),
                ("revision", "INTEGER NOT NULL DEFAULT 1"),
                ("superseded_by", "INTEGER"),
            ):
                if column not in columns:
                    db.execute(f"ALTER TABLE notes ADD COLUMN {column} {ddl}")
            db.execute("CREATE INDEX IF NOT EXISTS notes_investigation ON notes (investigation_id, kind)")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS notes_artifact ON notes (investigation_id, kind, artifact_key) "
                       "WHERE artifact_key IS NOT NULL")
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


_INSERT = """INSERT INTO notes (title, content, tags, pinned, literature_ids, investigation_id, kind, origin,
                   artifact_key, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"""


def _insert(db: sqlite3.Connection, body: NoteInput, *, kind: str | None = None, origin: str = "user",
            artifact_key: str | None = None) -> int:
    cursor = db.execute(_INSERT, (body.title, body.content, json.dumps(body.tags), body.pinned,
                                  json.dumps(body.literature_ids), body.investigation_id, kind, origin, artifact_key))
    return cursor.lastrowid


@router.get("")
def list_notes(investigation_id: int | None = None, kind: str | None = None, origin: Origin | None = None,
               superseded: bool | None = None):
    """All notes, or those matching every filter given."""
    where, params = [], []
    for column, value in (("investigation_id", investigation_id), ("kind", kind), ("origin", origin)):
        if value is not None:
            where.append(f"{column} = ?")
            params.append(value)
    if superseded is not None:
        where.append("superseded_by IS NOT NULL" if superseded else "superseded_by IS NULL")
    sql = "SELECT * FROM notes" + (f" WHERE {' AND '.join(where)}" if where else "")
    with connect() as db:
        return [_note(row) for row in db.execute(sql + " ORDER BY pinned DESC, updated_at DESC, id DESC", params)]


@router.post("", status_code=201)
def create_note(body: NoteInput):
    with connect() as db:
        return _read(db, _insert(db, body))


def create_research_note(*, investigation_id: int, kind: str, artifact_key: str, title: str, content: str,
                         literature_ids: list[int] = (), tags: list[str] = ()) -> dict:
    """An AI Note for one investigation artifact (B12). One per (investigation, kind, key)."""
    if kind not in NOTE_KINDS:
        raise ValueError(f"Unknown note kind {kind!r}.")
    body = NoteInput(title=title[:200], content=content, tags=list(tags), literature_ids=list(literature_ids),
                     investigation_id=investigation_id)
    with connect() as db:
        return _read(db, _insert(db, body, kind=kind, origin="ai", artifact_key=artifact_key))


def supersede(old_id: int, new_id: int) -> None:
    """Mark `old_id` as replaced by `new_id` (a newer report version)."""
    with connect() as db:
        _read(db, old_id)
        _read(db, new_id)
        db.execute("UPDATE notes SET superseded_by = ? WHERE id = ?", (new_id, old_id))


def detach_investigation(investigation_id: int) -> int:
    """Keep an investigation's Notes when it is deleted (B12): unlink them. Returns how many."""
    with connect() as db:
        return db.execute("UPDATE notes SET investigation_id = NULL WHERE investigation_id = ?",
                          (investigation_id,)).rowcount


def delete_investigation_notes(investigation_id: int) -> list[int]:
    """Delete every Note linked to an investigation; returns the deleted IDs."""
    with connect() as db:
        ids = [r["id"] for r in db.execute("SELECT id FROM notes WHERE investigation_id = ? ORDER BY id",
                                           (investigation_id,))]
        db.execute("DELETE FROM notes WHERE investigation_id = ?", (investigation_id,))
        return ids


def create_notes(bodies: list[NoteInput]) -> list[dict]:
    """Create several notes in one database transaction."""
    created = []
    with connect() as db:
        for body in bodies:
            created.append(_read(db, _insert(db, body)))
    return created


@router.get("/{note_id}")
def get_note(note_id: int):
    with connect() as db:
        return _read(db, note_id)


@router.put("/{note_id}")
def update_note(note_id: int, body: NoteInput):
    with connect() as db:
        current = _read(db, note_id)
        # The link is only changed when the client sends the field (older clients don't know it).
        investigation_id = body.investigation_id if "investigation_id" in body.model_fields_set else current["investigation_id"]
        db.execute(
            """UPDATE notes SET title = ?, content = ?, tags = ?, pinned = ?, literature_ids = ?,
               investigation_id = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?""",
            (body.title, body.content, json.dumps(body.tags), body.pinned, json.dumps(body.literature_ids),
             investigation_id, note_id),
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
