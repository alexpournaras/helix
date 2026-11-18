import json
import unittest
from unittest.mock import patch

import httpx

from app.providers import ollama


class OllamaToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_call_continues_to_final_answer(self):
        bodies = []

        def handle(request):
            body = json.loads(request.content)
            bodies.append(body)
            if len(bodies) == 1:
                message = {"role": "assistant", "content": "", "tool_calls": [{
                    "function": {"name": "search_literature", "arguments": {"query": "TP53"}}
                }]}
            else:
                message = {"role": "assistant", "content": "I read the saved paper [Literature #3]."}
            return httpx.Response(200, text=json.dumps({"message": message, "done": True}) + "\n")

        original = httpx.AsyncClient
        def factory(**kwargs):
            return original(**kwargs, transport=httpx.MockTransport(handle))

        tools = [{"type": "function", "function": {"name": "search_literature", "description": "Search", "parameters": {"type": "object"}}}]
        execute = lambda name, args: {"papers": [{"id": 3}], "sources": [{"id": 3, "title": "TP53"}]}
        request = ollama.ChatRequest(model="test", messages=[{"role": "user", "content": "Find TP53"}])
        with patch.object(ollama.httpx, "AsyncClient", factory):
            events = [event async for event in ollama.chat_stream(
                ollama.OllamaConfig(), request, tools=tools, execute_tool=execute
            )]

        self.assertEqual([event["type"] for event in events], ["start", "tool", "content", "done"])
        self.assertEqual(bodies[0]["tools"], tools)
        self.assertEqual(bodies[1]["messages"][-1]["role"], "tool")
        self.assertEqual(bodies[1]["messages"][-1]["tool_name"], "search_literature")


if __name__ == "__main__":
    unittest.main()
