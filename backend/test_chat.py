import tempfile
import asyncio
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import FastAPI

from app import chat, notes


class ChatTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = patch.object(chat.settings, "data_dir", Path(self.temp.name))
        self.config.start()
        app = FastAPI()
        app.include_router(chat.router)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        self.config.stop()
        self.temp.cleanup()
        chat.active.clear()

    def test_note_mutations_require_matching_explicit_intent(self):
        self.assertTrue(chat._note_mutation_allowed("delete_note", "Perfect, now delete the other one"))
        self.assertFalse(chat._note_mutation_allowed("create_note", "Perfect, now delete the other one"))
        self.assertFalse(chat._note_mutation_allowed("edit_note", "Perfect, now delete the other one"))
        self.assertTrue(chat._note_mutation_allowed("create_note", "Write a note about VHL"))
        self.assertTrue(chat._note_mutation_allowed("create_notes", 'Create two about VHL, one with tag "vhl"'))
        request = 'Create two notes about VHL, and one should have tag "vhl"'
        self.assertIsNone(chat._validate_multi_note_tags(request, {"notes": [{"title": "One", "tags": ["vhl"]}, {"title": "Two"}]}))
        self.assertIn("exactly one", chat._validate_multi_note_tags(request, {"notes": [{"title": "One", "tags": ["vhl"]}, {"title": "Two", "tags": ["vhl"]}]}))
        self.assertIn("exactly one", chat._validate_multi_note_tags(request, {"notes": [{"title": "One"}, {"title": "Two"}]}))
        self.assertTrue(chat._note_mutation_allowed("edit_note", "Add this to the first note"))
        self.assertTrue(chat._note_mutation_allowed("remove_note_tag", "Remove the tag from the first"))
        self.assertTrue(chat._note_mutation_allowed("add_note_tag", 'Add tag "yo" to note 5'))
        self.assertFalse(chat._note_mutation_allowed("create_note", 'Add tag "yo" to note 5'))
        natural_pin_and_link = "pin the vhl one, and link the dna one with whatever paper u like"
        self.assertTrue(chat._note_mutation_allowed("edit_note", natural_pin_and_link))
        self.assertFalse(chat._note_mutation_allowed("create_note", natural_pin_and_link))
        self.assertFalse(chat._note_mutation_allowed("add_note_tag", natural_pin_and_link))
        literature_history = [{"role": "user", "content": "please delete all papers"},
                              {"role": "assistant", "content": "Are you sure?"}]
        self.assertTrue(chat._literature_delete_confirmed("yes", literature_history))
        self.assertTrue(chat._literature_delete_confirmed("delete all the papers yes", literature_history))
        self.assertTrue(chat._literature_delete_confirmed("i just said delete them", literature_history))
        self.assertTrue(chat._literature_delete_confirmed("yes, delete all papers", literature_history))
        self.assertFalse(chat._literature_delete_confirmed("now?", literature_history))
        self.assertTrue(chat._literature_delete_confirmed("delete bro", literature_history))
        self.assertFalse(chat._literature_delete_confirmed("can u do it now?", literature_history))
        self.assertFalse(chat._literature_delete_confirmed("no, don't delete them", literature_history))
        self.assertFalse(chat._literature_delete_confirmed("cancel", literature_history))
        self.assertFalse(chat._literature_delete_confirmed("what will happen?", literature_history))
        self.assertFalse(chat._literature_delete_confirmed("please delete all papers", []))
        combined_history = [{"role": "user", "content": "remove all notes and literatures"},
                            {"role": "assistant", "content": "Please confirm literature deletion."}]
        self.assertTrue(chat._literature_delete_confirmed("yes", combined_history))
        self.assertIsNotNone(chat.LITERATURE_FETCH_PATTERN.search("fetch 5 papers about VHL"))
        self.assertIsNotNone(chat.LITERATURE_CANCEL_FETCH_PATTERN.search("can u stop all fetching?"))
        self.assertIsNotNone(chat.LITERATURE_CANCEL_FETCH_PATTERN.search("actually stop the europe pmc i dont want from there"))
        self.assertIsNotNone(chat.LITERATURE_CANCEL_FETCH_PATTERN.search("stop run 35"))
        self.assertIsNotNone(chat.LITERATURE_CANCEL_FETCH_PATTERN.search("Stop fetch 35"))
        self.assertIsNone(chat.LITERATURE_CANCEL_FETCH_PATTERN.search("what fetches are running?"))
        cancel_history = [{"role": "user", "content": "actually stop the europe pmc i dont want from there"},
                          {"role": "assistant", "content": "Please be more explicit."}]
        self.assertIn("europe pmc", chat._pending_fetch_cancel_intent("yes stop", cancel_history))
        self.assertIn("europe pmc", chat._pending_fetch_cancel_intent("stop", cancel_history))
        inherited = chat._note_intent_context("all of them", "Please delete all notes")
        self.assertTrue(chat._note_mutation_allowed("delete_note", inherited))
        self.assertTrue(chat._note_mutation_allowed("delete_all_notes", inherited))
        self.assertFalse(chat._note_mutation_allowed("create_note", inherited))
        self.assertFalse(chat._note_mutation_allowed("delete_all_notes", "Delete one note"))
        self.assertEqual(chat._note_intent_context("all of them", "What notes exist?"), "What notes exist?\nConfirmation: all of them")
        self.assertEqual(chat._note_intent_context("Tell me about all of them", "Delete all notes"), "Tell me about all of them")
        drafting_history = [
            {"role": "user", "content": "edit content of note 6"},
            {"role": "assistant", "content": "What content?"},
            {"role": "user", "content": "about paok"},
            {"role": "assistant", "content": "Please provide exact text."},
            {"role": "user", "content": "improvise"},
            {"role": "assistant", "content": "Here is a draft."},
            {"role": "user", "content": "yes do it and save it"},
            {"role": "assistant", "content": "Please confirm."},
            {"role": "user", "content": "yes"},
            {"role": "assistant", "content": "Here is the final draft."},
        ]
        pending_edit = chat._pending_note_intent("save it", drafting_history)
        self.assertTrue(chat._note_mutation_allowed("edit_note", pending_edit))
        self.assertFalse(chat._note_mutation_allowed("create_note", pending_edit))
        retry_edit = chat._pending_note_intent("try again", drafting_history)
        self.assertTrue(chat._note_mutation_allowed("edit_note", retry_edit))
        for approval in ("can u do it now?", "JUST DO IT", "please just do it", "do it now"):
            resolved = chat._pending_note_intent(approval, drafting_history)
            self.assertTrue(chat._note_mutation_allowed("edit_note", resolved), approval)
            self.assertFalse(chat._note_mutation_allowed("create_note", resolved), approval)
        completed_history = [*drafting_history, {"role": "assistant", "content": "Saved", "tool_activity": [
            {"name": "edit_note", "error": None, "audit": {"action": "edit", "note_id": 6}}
        ]}]
        self.assertEqual(chat._pending_note_intent("yes", completed_history), "yes")

        create_history = [
            {"role": "user", "content": "create 2 notes about vhl"},
            {"role": "assistant", "content": "Send content or ask me to draft."},
            {"role": "user", "content": "draft"},
            {"role": "assistant", "content": "Here are two drafts."},
        ]
        pending_create = chat._pending_note_intent("yes save", create_history)
        self.assertTrue(chat._note_mutation_allowed("create_notes", pending_create))
        self.assertFalse(chat._note_mutation_allowed("edit_note", pending_create))

    async def test_retry_injects_pending_edit_and_can_reuse_drafted_content(self):
        note = notes.create_note(notes.NoteInput(title="Original", content="Old content"))
        conversation = chat.create()
        conversation["messages"] = [
            {"role": "user", "content": f"edit content of note {note['id']}"},
            {"role": "assistant", "content": "Draft: BRCA1 is a tumor suppressor gene involved in repairing damaged DNA.",
             "provider": "openai", "model": "test", "status": "complete"},
        ]
        chat.save(conversation)

        async def stream(config, request, **kwargs):
            system = "\n".join(message.content for message in request.messages if message.role == "system")
            self.assertIn("AUTHORITATIVE HELIX REQUEST RESOLUTION", system)
            self.assertIn(f"edit content of note {note['id']}", system)
            result = kwargs["execute_tool"]("edit_note", {
                "id": note["id"], "content": "BRCA1 is a tumor suppressor gene involved in repairing damaged DNA.",
            })
            self.assertEqual(result["scope"], "note_edited")
            yield {"type": "content", "text": "Updated note."}
            yield {"type": "done"}

        with patch.object(chat.ollama, "chat_stream", stream):
            response = await self.client.post(
                f"/api/chat/conversations/{conversation['id']}/messages",
                json={"model": "test", "content": "try again"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn("BRCA1 is a tumor suppressor gene", notes.get_note(note["id"])["content"])

    async def test_literature_delete_requires_separate_confirmation(self):
        conversation = (await self.client.post("/api/chat/conversations")).json()
        url = f"/api/chat/conversations/{conversation['id']}/messages"

        async def initial_request(config, request, **kwargs):
            refused = kwargs["execute_tool"]("delete_all_literature", {})
            self.assertEqual(refused["scope"], "literature_delete_confirmation_required")
            yield {"type": "content", "text": "Please confirm deletion of all papers."}
            yield {"type": "done"}

        with patch.object(chat.ollama, "chat_stream", initial_request):
            await self.client.post(url, json={"model": "test", "content": "delete all papers"})

        async def confirmed_request(config, request, **kwargs):
            system = "\n".join(message.content for message in request.messages if message.role == "system")
            self.assertIn("LITERATURE DELETION RESULT", system)
            self.assertNotIn("delete_all_literature", {tool["function"]["name"] for tool in kwargs["tools"]})
            yield {"type": "content", "text": "Deleted all papers."}
            yield {"type": "done"}

        result = {"deleted_count": 3, "scope": "literature_deleted",
                  "audit": {"action": "delete_all_literature", "count": 3}, "sources": []}
        with patch.object(chat.ollama, "chat_stream", confirmed_request), patch.object(chat, "execute_chat_tool", return_value=result) as execute:
            response = await self.client.post(url, json={"model": "test", "content": "yes"})
        self.assertEqual(response.status_code, 200)
        execute.assert_called_once_with("delete_all_literature", {})

    async def test_literature_fetch_requires_explicit_fetch_language(self):
        async def stream(config, request, **kwargs):
            refused = kwargs["execute_tool"]("fetch_literature", {"term": "VHL"})
            self.assertEqual(refused["scope"], "literature_fetch_refused")
            yield {"type": "done"}

        conversation = (await self.client.post("/api/chat/conversations")).json()
        url = f"/api/chat/conversations/{conversation['id']}/messages"
        with patch.object(chat.ollama, "chat_stream", stream):
            await self.client.post(url, json={"model": "test", "content": "tell me about VHL papers"})

        async def fetch(config, request, **kwargs):
            started = kwargs["execute_tool"]("fetch_literature", {"term": "VHL", "max_results": 5})
            self.assertEqual(started["scope"], "literature_fetch_started")
            yield {"type": "done"}

        started = {"run": {"id": 9}, "scope": "literature_fetch_started",
                   "audit": {"action": "fetch_literature", "run_id": 9}, "sources": []}
        with patch.object(chat.ollama, "chat_stream", fetch), patch.object(chat, "execute_chat_tool", return_value=started):
            response = await self.client.post(url, json={"model": "test", "content": "fetch 5 papers about VHL"})
        self.assertEqual(response.status_code, 200)

    async def test_cancel_all_fetches_requires_explicit_stop_request(self):
        async def stream(config, request, **kwargs):
            cancelled = kwargs["execute_tool"]("cancel_all_literature_fetches", {})
            self.assertEqual(cancelled["scope"], "literature_fetches_cancelled")
            yield {"type": "done"}

        conversation = (await self.client.post("/api/chat/conversations")).json()
        result = {"cancelled_count": 2, "cancelled_ids": [3, 4], "scope": "literature_fetches_cancelled",
                  "audit": {"action": "cancel_all_literature_fetches", "run_ids": [3, 4]}, "sources": []}
        with patch.object(chat.ollama, "chat_stream", stream), patch.object(chat, "execute_chat_tool", return_value=result):
            response = await self.client.post(
                f"/api/chat/conversations/{conversation['id']}/messages",
                json={"model": "test", "content": "can u stop all fetching?"},
            )
        self.assertEqual(response.status_code, 200)

    async def test_tag_followup_uses_dedicated_tool_and_preserves_note(self):
        note = notes.create_note(notes.NoteInput(title="VHL", content="Keep this", tags=["vhl"], pinned=True))

        async def stream(config, request, **kwargs):
            result = kwargs["execute_tool"]("remove_note_tag", {"id": note["id"], "tag": "vhl"})
            self.assertEqual(result["scope"], "note_tag_removed")
            yield {"type": "content", "text": "Removed the tag."}
            yield {"type": "done"}

        conversation = (await self.client.post("/api/chat/conversations")).json()
        with patch.object(chat.ollama, "chat_stream", stream):
            response = await self.client.post(
                f"/api/chat/conversations/{conversation['id']}/messages",
                json={"model": "test", "content": "Remove the tag from note 1"},
            )
        self.assertEqual(response.status_code, 200)
        saved = notes.get_note(note["id"])
        self.assertEqual(saved["tags"], [])
        self.assertEqual(saved["title"], "VHL")
        self.assertEqual(saved["content"], "Keep this")
        self.assertTrue(saved["pinned"])

    async def test_multiple_note_request_can_create_tagged_notes_without_literature(self):
        async def stream(config, request, **kwargs):
            result = kwargs["execute_tool"]("create_notes", {"notes": [
                {"title": "VHL one", "tags": ["vhl"]},
                {"title": "VHL two"},
            ]})
            self.assertEqual(result["scope"], "notes_created")
            yield {"type": "content", "text": "Created two notes."}
            yield {"type": "done"}

        conversation = (await self.client.post("/api/chat/conversations")).json()
        with patch.object(chat.ollama, "chat_stream", stream):
            response = await self.client.post(
                f"/api/chat/conversations/{conversation['id']}/messages",
                json={"model": "test", "content": 'Create two about VHL, one with tag "vhl"'},
            )
        self.assertEqual(response.status_code, 200)
        saved = notes.list_notes()
        self.assertEqual(len(saved), 2)
        self.assertEqual(next(note for note in saved if note["title"] == "VHL one")["tags"], ["vhl"])

    async def test_wrong_note_mutation_is_refused_while_requested_delete_succeeds(self):
        note = notes.create_note(notes.NoteInput(title="Delete me"))

        async def stream(config, request, **kwargs):
            execute = kwargs["execute_tool"]
            refused = execute("create_note", {"title": "Should not exist"})
            self.assertIn("did not explicitly request a note create", refused["error"])
            deleted = execute("delete_note", {"id": note["id"]})
            self.assertEqual(deleted["scope"], "note_deleted")
            yield {"type": "content", "text": "Deleted."}
            yield {"type": "done"}

        conversation = (await self.client.post("/api/chat/conversations")).json()
        with patch.object(chat.ollama, "chat_stream", stream):
            response = await self.client.post(
                f"/api/chat/conversations/{conversation['id']}/messages",
                json={"model": "test", "content": "Delete the other note"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(notes.list_notes(), [])

    async def test_delete_confirmation_inherits_immediately_previous_request(self):
        first = notes.create_note(notes.NoteInput(title="First"))
        second = notes.create_note(notes.NoteInput(title="Second"))
        conversation = (await self.client.post("/api/chat/conversations")).json()
        url = f"/api/chat/conversations/{conversation['id']}/messages"

        async def ask_to_confirm(config, request, **kwargs):
            yield {"type": "content", "text": "Delete all notes?"}
            yield {"type": "done"}

        with patch.object(chat.ollama, "chat_stream", ask_to_confirm):
            await self.client.post(url, json={"model": "test", "content": "Please delete all notes"})

        async def delete_confirmed(config, request, **kwargs):
            execute = kwargs["execute_tool"]
            deleted = execute("delete_all_notes", {})
            self.assertEqual(deleted["scope"], "notes_deleted")
            self.assertEqual(set(deleted["deleted_ids"]), {first["id"], second["id"]})
            self.assertIn("error", execute("create_note", {"title": "Wrong action"}))
            yield {"type": "content", "text": "Deleted both notes."}
            yield {"type": "done"}

        with patch.object(chat.ollama, "chat_stream", delete_confirmed):
            response = await self.client.post(url, json={"model": "test", "content": "all of them"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(notes.list_notes(), [])

    async def test_history_stream_and_delete(self):
        seen = []

        async def stream(config, request, **kwargs):
            self.assertFalse(request.think)
            seen.append([m.model_dump() for m in request.messages])
            yield {"type": "content", "text": "Hello!"}
            yield {"type": "done"}

        conversation = (await self.client.post("/api/chat/conversations")).json()
        url = f"/api/chat/conversations/{conversation['id']}"
        with patch.object(chat.ollama, "chat_stream", stream):
            for content in ("Hi", "Remember me?"):
                response = await self.client.post(url + "/messages", json={"model": "test", "content": content})
                self.assertEqual(response.status_code, 200)
                self.assertIn('"type": "done"', response.text)
        self.assertEqual(seen[1], [
            {"role": "system", "content": chat.CONVERSATION_INSTRUCTIONS},
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello!"},
            {"role": "user", "content": "Remember me?"},
        ])
        saved = (await self.client.get(url)).json()
        self.assertEqual(saved["title"], "Hi")
        self.assertEqual(len(saved["messages"]), 4)
        self.assertEqual(saved["messages"][-1]["status"], "complete")
        self.assertEqual((await self.client.delete(url)).status_code, 204)
        self.assertEqual((await self.client.get(url)).status_code, 404)

    async def test_failed_reply_and_validation(self):
        async def stream(config, request, **kwargs):
            yield {"type": "content", "text": "Partial"}

        item = (await self.client.post("/api/chat/conversations")).json()
        url = f"/api/chat/conversations/{item['id']}"
        for body in ({"model": "test", "content": " "}, {"model": "test", "content": "Hi", "provider": "missing"}):
            self.assertEqual((await self.client.post(url + "/messages", json=body)).status_code, 422)
        self.assertEqual((await self.client.get(url)).json()["messages"], [])
        with patch.object(chat.ollama, "chat_stream", stream):
            response = await self.client.post(url + "/messages", json={"model": "test", "content": "Hi"})
        self.assertIn('"type": "error"', response.text)
        saved = (await self.client.get(url)).json()["messages"][-1]
        self.assertEqual(saved["content"], "Partial")
        self.assertEqual(saved["status"], "error")
        self.assertNotIn(item["id"], chat.active)
        chat.active.add(item["id"])
        self.assertEqual((await self.client.delete(url)).status_code, 409)
        self.assertEqual((await self.client.post(url + "/messages", json={"model": "test", "content": "Hi"})).status_code, 409)

    async def test_saved_history_reaches_ollama_http_request(self):
        """Exercise the real adapter: assert what leaves Helix, not just its internal request."""
        import json

        payloads = []
        def handle(request):
            self.assertEqual(request.url.path, "/api/chat")
            payloads.append(json.loads(request.content))
            return httpx.Response(200, text=json.dumps({
                "message": {"role": "assistant", "content": "Hello Takis!"}, "done": True,
            }) + "\n")

        original_client = httpx.AsyncClient
        def client_factory(**kwargs):
            return original_client(**kwargs, transport=httpx.MockTransport(handle))

        item = (await self.client.post("/api/chat/conversations")).json()
        url = f"/api/chat/conversations/{item['id']}"
        with patch.object(chat.ollama.httpx, "AsyncClient", client_factory):
            response = await self.client.post(url + "/messages", json={
                "model": "qwen3.5:0.8b", "content": "My name is Takis and I am from Greece.",
            })
            self.assertEqual(response.status_code, 200)
            # Reopening reads the stored conversation; the browser sends only the new prompt.
            reopened = (await self.client.get(url)).json()
            self.assertEqual(len(reopened["messages"]), 2)
            response = await self.client.post(url + "/messages", json={
                "model": "qwen3.5:0.8b", "content": "Do you remember who I am?",
            })
            self.assertEqual(response.status_code, 200)

        self.assertEqual(payloads[1]["messages"], [
            {"role": "system", "content": chat.CONVERSATION_INSTRUCTIONS},
            {"role": "user", "content": "My name is Takis and I am from Greece."},
            {"role": "assistant", "content": "Hello Takis!"},
            {"role": "user", "content": "Do you remember who I am?"},
        ])
        self.assertFalse(payloads[1]["think"])
        self.assertTrue(payloads[1]["stream"])

    async def test_cancel_preserves_partial_reply(self):
        async def stream(config, request, **kwargs):
            yield {"type": "content", "text": "Partial reply"}
            raise asyncio.CancelledError()

        item = chat.create()
        with patch.object(chat.ollama, "chat_stream", stream):
            response = await chat.send(item["id"], chat.SendMessage(model="test", content="Hello"))
            with self.assertRaises(asyncio.CancelledError):
                async for _ in response.body_iterator:
                    pass
        reply = chat.read(item["id"])["messages"][-1]
        self.assertEqual(reply["content"], "Partial reply")
        self.assertEqual(reply["status"], "interrupted")
        self.assertNotIn(item["id"], chat.active)

    async def test_tool_scope_is_saved_and_replayed_as_verified_audit(self):
        seen = []

        async def stream(config, request, **kwargs):
            seen.append([message.model_dump() for message in request.messages])
            if len(seen) == 1:
                yield {"type": "tool", "name": "get_literature_full_text", "scope": "full_text", "error": None,
                       "audit": {"scope": "full_text", "complete": True, "sections": 12},
                       "sources": [{"id": 17, "title": "A paper"}]}
                yield {"type": "content", "text": "I read the full text."}
            else:
                yield {"type": "content", "text": "The audit confirms it."}
            yield {"type": "done"}

        item = (await self.client.post("/api/chat/conversations")).json()
        url = f"/api/chat/conversations/{item['id']}/messages"
        with patch.object(chat.ollama, "chat_stream", stream):
            await self.client.post(url, json={"model": "test", "content": "Read the full text"})
            await self.client.post(url, json={"model": "test", "content": "Did you really read it?"})

        audit_message = next(message["content"] for message in seen[1] if message["role"] == "system" and "TOOL AUDIT" in message["content"])
        self.assertIn("AUTHORITATIVE HELIX BACKEND TOOL AUDIT", audit_message)
        self.assertIn("VERIFIED SUCCESS: get_literature_full_text [full_text] on Literature #17", audit_message)
        saved = (await self.client.get(url.removesuffix("/messages"))).json()["messages"][1]
        self.assertEqual(saved["tool_activity"][0]["scope"], "full_text")
        self.assertTrue(saved["tool_activity"][0]["audit"]["complete"])


if __name__ == "__main__":
    unittest.main()
