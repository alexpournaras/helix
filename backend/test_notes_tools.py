import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import notes, notes_tools


class NoteToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = patch.object(notes.settings, "data_dir", Path(self.temp.name))
        self.config.start()

    def tearDown(self):
        self.config.stop()
        self.temp.cleanup()

    def test_list_read_create_and_edit(self):
        created = notes_tools.execute("create_note", {
            "title": "Experiment plan", "content": "Measure the baseline.", "tags": ["Planning"],
            "pinned": True, "literature_ids": [7],
        })
        self.assertEqual(created["scope"], "note_created")
        note_id = created["note"]["id"]

        listed = notes_tools.execute("list_notes", {"query": "baseline"})
        self.assertEqual(listed["notes"][0]["id"], note_id)
        self.assertNotIn("content", listed["notes"][0])
        self.assertEqual(notes_tools.execute("list_notes", {"tag": "planning"})["total"], 1)

        read = notes_tools.execute("get_note", {"id": note_id})
        self.assertEqual(read["note"]["content"], "Measure the baseline.")
        edited = notes_tools.execute("edit_note", {"id": note_id, "content": "Measure baseline and follow-up.", "tags": ["Done"]})
        self.assertEqual(edited["scope"], "note_edited")
        self.assertEqual(edited["note"]["title"], "Experiment plan")
        self.assertEqual(edited["note"]["tags"], ["Done"])
        self.assertEqual(edited["audit"]["fields"], ["content", "tags"])
        deleted = notes_tools.execute("delete_note", {"id": note_id})
        self.assertEqual(deleted["scope"], "note_deleted")
        self.assertEqual(deleted["deleted"]["title"], "Experiment plan")
        self.assertIn("error", notes_tools.execute("get_note", {"id": note_id}))

    def test_invalid_and_missing_inputs_are_safe(self):
        self.assertIn("error", notes_tools.execute("get_note", {"id": "bad"}))
        self.assertIn("error", notes_tools.execute("get_note", {"id": 999}))
        self.assertIn("error", notes_tools.execute("create_note", {"title": " "}))
        note_id = notes_tools.execute("create_note", {"title": "Keep"})["note"]["id"]
        self.assertIn("error", notes_tools.execute("edit_note", {"id": note_id}))
        self.assertIn("error", notes_tools.execute("delete_note", {"id": 999}))
        self.assertIn("error", notes_tools.execute("unknown", {}))

    def test_delete_all_notes(self):
        notes_tools.execute("create_note", {"title": "One"})
        notes_tools.execute("create_note", {"title": "Two"})
        result = notes_tools.execute("delete_all_notes", {})
        self.assertEqual(result["deleted_count"], 2)
        self.assertEqual(notes_tools.execute("list_notes", {})["total"], 0)

    def test_create_multiple_notes_with_independent_tags(self):
        result = notes_tools.execute("create_notes", {"notes": [
            {"title": "VHL overview", "content": "First VHL note.", "tags": ["vhl"]},
            {"title": "VHL follow-up", "content": "Second VHL note."},
        ]})
        self.assertEqual(result["scope"], "notes_created")
        self.assertEqual(result["audit"]["count"], 2)
        self.assertEqual(result["notes"][0]["tags"], ["vhl"])
        self.assertEqual(result["notes"][1]["tags"], [])
        self.assertEqual(notes_tools.execute("list_notes", {})["total"], 2)
        self.assertIn("error", notes_tools.execute("create_notes", {"notes": [{"title": "Only one"}]}))

    def test_dedicated_tag_tools_preserve_other_note_fields(self):
        note = notes_tools.execute("create_note", {
            "title": "VHL", "content": "Original text", "tags": ["vhl"], "pinned": True, "literature_ids": [9],
        })["note"]
        added = notes_tools.execute("add_note_tag", {"id": note["id"], "tag": "yo"})
        self.assertEqual(added["scope"], "note_tag_added")
        self.assertEqual(added["note"]["tags"], ["vhl", "yo"])
        self.assertEqual(added["note"]["content"], "Original text")
        self.assertTrue(added["note"]["pinned"])
        self.assertEqual(added["note"]["literature_ids"], [9])
        removed = notes_tools.execute("remove_note_tag", {"id": note["id"], "tag": "VHL"})
        self.assertEqual(removed["scope"], "note_tag_removed")
        self.assertEqual(removed["note"]["tags"], ["yo"])
        self.assertIn("error", notes_tools.execute("add_note_tag", {"id": note["id"], "tag": "YO"}))
        self.assertIn("error", notes_tools.execute("remove_note_tag", {"id": note["id"], "tag": "missing"}))


if __name__ == "__main__":
    unittest.main()
