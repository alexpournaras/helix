import tempfile
import asyncio
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import FastAPI

from app import chat


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
