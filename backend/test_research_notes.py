"""1A.10 Notes basics (PLAN B3 "Notes columns", B12): research columns, filters, investigation link, limits,
secure_delete, detach/delete with an investigation. Synthetic text only."""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import FastAPI

import research_testkit  # noqa: F401  (installs the no-network guard)
from app import notes


class NotesBasicsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = patch.object(notes.settings, "data_dir", Path(self.temp.name))
        self.config.start()
        app = FastAPI()
        app.include_router(notes.router)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        self.config.stop()
        self.temp.cleanup()

    def ai_note(self, inv, kind, key, **extra):
        return notes.create_research_note(investigation_id=inv, kind=kind, artifact_key=key,
                                          title=f"{kind} {key}", content="Synthetic.", **extra)

    async def test_user_notes_get_default_research_columns(self):
        r = await self.client.post("/api/notes", json={"title": "Mine"})
        self.assertEqual(r.status_code, 201)
        note = r.json()
        self.assertEqual((note["origin"], note["kind"], note["investigation_id"], note["artifact_key"],
                          note["revision"], note["superseded_by"]), ("user", None, None, None, 1, None))

    async def test_note_input_accepts_an_investigation_and_200_papers(self):
        r = await self.client.post("/api/notes", json={"title": "Ctx", "investigation_id": 4,
                                                       "literature_ids": list(range(1, 201))})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["investigation_id"], 4)
        self.assertEqual(len(r.json()["literature_ids"]), 200)
        too_many = await self.client.post("/api/notes", json={"title": "x", "literature_ids": list(range(1, 202))})
        self.assertEqual(too_many.status_code, 422)
        bad_inv = await self.client.post("/api/notes", json={"title": "x", "investigation_id": 0})
        self.assertEqual(bad_inv.status_code, 422)

    async def test_research_note_and_filters(self):
        self.ai_note(1, "case_brief", "brief")
        self.ai_note(1, "paper_card", "paper:7", literature_ids=[7])
        self.ai_note(2, "case_brief", "brief")
        await self.client.post("/api/notes", json={"title": "Mine", "investigation_id": 1})

        async def titles(**params):
            r = await self.client.get("/api/notes", params=params)
            self.assertEqual(r.status_code, 200)
            return sorted(n["title"] for n in r.json())

        self.assertEqual(len(await titles()), 4)
        self.assertEqual(await titles(investigation_id=1), ["Mine", "case_brief brief", "paper_card paper:7"])
        self.assertEqual(await titles(kind="case_brief"), ["case_brief brief", "case_brief brief"])
        self.assertEqual(await titles(origin="user"), ["Mine"])
        self.assertEqual(await titles(origin="ai", investigation_id=2), ["case_brief brief"])
        bad = await self.client.get("/api/notes", params={"origin": "robot"})
        self.assertEqual(bad.status_code, 422)

    async def test_superseded_filter(self):
        old = self.ai_note(1, "final_report", "report:v1")
        new = self.ai_note(1, "final_report", "report:v2")
        notes.supersede(old["id"], new["id"])
        r = await self.client.get("/api/notes", params={"superseded": "false"})
        self.assertEqual([n["id"] for n in r.json()], [new["id"]])
        r = await self.client.get("/api/notes", params={"superseded": "true"})
        self.assertEqual([(n["id"], n["superseded_by"]) for n in r.json()], [(old["id"], new["id"])])

    async def test_research_note_fields(self):
        note = self.ai_note(3, "search_log", "search", tags=["inv:3"])
        self.assertEqual((note["origin"], note["kind"], note["artifact_key"], note["investigation_id"]),
                         ("ai", "search_log", "search", 3))
        self.assertEqual(note["tags"], ["inv:3"])
        with self.assertRaises(ValueError):
            self.ai_note(3, "not_a_kind", "x")
        with self.assertRaises(sqlite3.IntegrityError):
            self.ai_note(3, "search_log", "search")  # one note per (investigation, kind, key)

    async def test_detach_and_delete_with_an_investigation(self):
        keep = self.ai_note(5, "case_brief", "brief")
        mine = (await self.client.post("/api/notes", json={"title": "Mine", "investigation_id": 5})).json()
        other = self.ai_note(6, "case_brief", "brief")
        self.assertEqual(notes.detach_investigation(5), 2)
        self.assertIsNone(notes.get_note(keep["id"])["investigation_id"])
        self.assertIsNone(notes.get_note(mine["id"])["investigation_id"])
        self.assertEqual(notes.get_note(other["id"])["investigation_id"], 6)
        self.assertEqual(notes.delete_investigation_notes(6), [other["id"]])
        with self.assertRaises(Exception):
            notes.get_note(other["id"])

    async def test_update_keeps_the_link_unless_the_field_is_sent(self):
        note = self.ai_note(8, "case_brief", "brief")
        r = await self.client.put(f"/api/notes/{note['id']}", json={"title": "Edited"})
        self.assertEqual(r.json()["investigation_id"], 8)
        r = await self.client.put(f"/api/notes/{note['id']}", json={"title": "Edited", "investigation_id": None})
        self.assertIsNone(r.json()["investigation_id"])

    async def test_secure_delete_is_on(self):
        with notes.connect() as db:
            self.assertEqual(db.execute("PRAGMA secure_delete").fetchone()[0], 1)

    async def test_old_database_is_migrated(self):
        path = Path(self.temp.name) / "notes.sqlite3"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, "
                     "content TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
        conn.execute("INSERT INTO notes (title, created_at, updated_at) VALUES ('Old', 'x', 'x')")
        conn.commit()
        conn.close()
        r = await self.client.get("/api/notes")
        self.assertEqual(r.json()[0]["origin"], "user")
        self.assertEqual(r.json()[0]["revision"], 1)


if __name__ == "__main__":
    unittest.main()
