import json
import unittest
from unittest.mock import patch

import httpx

from app.providers import openai


class OpenAITests(unittest.IsolatedAsyncioTestCase):
    def mock_client(self, handler):
        original = httpx.AsyncClient

        def factory(**kwargs):
            return original(**kwargs, transport=httpx.MockTransport(handler))

        return patch.object(openai.httpx, "AsyncClient", factory)

    async def test_status_validates_key_and_returns_only_configured_models(self):
        def handle(request):
            self.assertEqual(request.url.path, "/v1/models")
            self.assertEqual(request.headers["authorization"], "Bearer test-key")
            return httpx.Response(200, json={"data": [{"id": "unrelated-model"}]})

        with patch.object(openai, "_key", return_value="test-key"), self.mock_client(handle):
            result = await openai.status(openai.OpenAIConfig())

        self.assertTrue(result["reachable"])
        self.assertEqual([m["name"] for m in result["models"]], list(openai.SUPPORTED_MODELS))
        self.assertNotIn("test-key", json.dumps(result))

    async def test_responses_stream_is_normalized_and_not_stored(self):
        bodies = []

        def handle(request):
            self.assertEqual(request.url.path, "/v1/responses")
            bodies.append(json.loads(request.content))
            events = [
                {"type": "response.created"},
                {"type": "response.output_text.delta", "delta": "Hello"},
                {"type": "response.output_text.delta", "delta": " world"},
                {"type": "response.completed", "response": {"usage": {"total_tokens": 12}}},
            ]
            text = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events)
            return httpx.Response(200, text=text, headers={"content-type": "text/event-stream"})

        request = openai.ChatRequest(model="gpt-5.4-mini", messages=[
            {"role": "system", "content": "Be concise."},
            {"role": "user", "content": "Hi"},
        ])
        with patch.object(openai, "_key", return_value="test-key"), self.mock_client(handle):
            events = [event async for event in openai.chat_stream(openai.OpenAIConfig(), request)]

        self.assertEqual([event["type"] for event in events], ["start", "content", "content", "done"])
        self.assertEqual(bodies[0], {
            "model": "gpt-5.4-mini",
            "input": [{"role": "user", "content": "Hi"}],
            "stream": True,
            "store": False,
            "instructions": "Be concise.",
        })

    async def test_missing_key_and_unsupported_model_fail_without_network(self):
        status = await openai.status(openai.OpenAIConfig())
        self.assertFalse(status["reachable"])
        self.assertEqual(len(status["models"]), 9)

        request = openai.ChatRequest(model="not-a-model", messages=[{"role": "user", "content": "Hi"}])
        events = [event async for event in openai.chat_stream(openai.OpenAIConfig(), request)]
        self.assertEqual(events[0]["type"], "error")
        self.assertIn("supported", events[0]["message"])


if __name__ == "__main__":
    unittest.main()
