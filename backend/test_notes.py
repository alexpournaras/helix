import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import FastAPI

from app import notes


class NoteTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_note_crud(self):
        response = await self.client.post("/api/notes", json={"title": "  First note  ", "content": "Draft", "tags": ["Idea", " idea ", "Research"], "pinned": True, "literature_ids": [7, 7, 11]})
        self.assertEqual(response.status_code, 201)
        note = response.json()
        self.assertEqual(note["title"], "First note")
        self.assertEqual(note["tags"], ["Idea", "Research"])
        self.assertTrue(note["pinned"])
        self.assertEqual(note["literature_ids"], [7, 11])
        self.assertEqual((await self.client.get(f"/api/notes/{note['id']}")).json()["content"], "Draft")

        updated = await self.client.put(f"/api/notes/{note['id']}", json={"title": "Final", "content": "Done", "tags": ["Complete"], "pinned": False, "literature_ids": [12]})
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["content"], "Done")
        self.assertEqual(updated.json()["tags"], ["Complete"])
        self.assertFalse(updated.json()["pinned"])
        self.assertEqual(updated.json()["literature_ids"], [12])
        self.assertEqual(len((await self.client.get("/api/notes")).json()), 1)

        self.assertEqual((await self.client.delete(f"/api/notes/{note['id']}")).status_code, 204)
        self.assertEqual((await self.client.get(f"/api/notes/{note['id']}")).status_code, 404)

    async def test_validation_and_missing_note(self):
        self.assertEqual((await self.client.post("/api/notes", json={"title": "  ", "content": "x"})).status_code, 422)
        self.assertEqual((await self.client.put("/api/notes/999", json={"title": "Missing", "content": ""})).status_code, 404)
        self.assertEqual((await self.client.delete("/api/notes/999")).status_code, 404)
