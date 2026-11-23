import json
import unittest

import httpx

from research_testkit import ResearchTestCase

from app.research import llm, preflight, settings as S
from app.research.prices import PriceTable
from app.research.providers import FakeProvider, ModelError, OllamaProvider, StructuredRequest
from app.research.ratelimit import Limiters
from app.research.transport import use_transport


def req(**kw) -> StructuredRequest:
    base = dict(model="qwen3-coder:latest", system="sys", user="usr", schema={"type": "object"}, schema_name="Out",
                effort=None, max_output_tokens=8192, idle_timeout=5, deadline=10)
    return StructuredRequest(**{**base, **kw})


def ndjson(*chunks) -> bytes:
    return "".join(json.dumps(c) + "\n" for c in chunks).encode()


def reply(text, reason="stop"):
    half = len(text) // 2
    return ndjson({"message": {"content": text[:half]}, "done": False},
                  {"message": {"content": text[half:]}, "done": False},
                  {"done": True, "done_reason": reason, "prompt_eval_count": 50, "eval_count": 9})


class OllamaAdapterTests(unittest.IsolatedAsyncioTestCase):
    def handler(self, chat_body: bytes, capabilities=("completion",), status=200):
        self.bodies = []

        def handle(request):
            if request.url.path == "/api/show":
                return httpx.Response(200, json={"capabilities": list(capabilities)})
            self.bodies.append(json.loads(request.content))
            return httpx.Response(status, content=chat_body)
        return httpx.MockTransport(handle)

    async def test_body_always_sends_num_ctx_and_format(self):
        provider = OllamaProvider(base_url="http://ollama.test", num_ctx=32768)
        with use_transport(self.handler(reply('{"answer":"x"}'))):
            result = await provider.complete(req())
        body = self.bodies[0]
        self.assertEqual(body["options"]["num_ctx"], 32768)
        self.assertEqual(body["options"]["num_predict"], 8192)
        self.assertEqual(body["format"], {"type": "object"})
        self.assertTrue(body["stream"])
        self.assertNotIn("think", body)  # model lacks the thinking capability
        self.assertEqual([m["role"] for m in body["messages"]], ["system", "user"])
        self.assertEqual(result.texts, ['{"answer":"x"}'])
        self.assertEqual((result.input_tokens, result.output_tokens), (50, 9))

    async def test_think_only_with_capability(self):
        provider = OllamaProvider(base_url="http://ollama.test")
        with use_transport(self.handler(reply("{}"), capabilities=("completion", "thinking"))):
            await provider.complete(req(effort="high"))
        self.assertTrue(self.bodies[0]["think"])

    async def test_length_is_truncated(self):
        with use_transport(self.handler(reply('{"a', reason="length"))):
            result = await OllamaProvider(base_url="http://ollama.test").complete(req())
        self.assertEqual(result.stop, "truncated")

    async def test_error_line_mid_stream_and_missing_model(self):
        body = ndjson({"message": {"content": "{"}, "done": False}, {"error": "model runner crashed"})
        with use_transport(self.handler(body)):
            with self.assertRaises(ModelError) as caught:
                await OllamaProvider(base_url="http://ollama.test").complete(req())
        self.assertEqual(caught.exception.kind, "transient")
        with use_transport(self.handler(b'{"error":"model not found"}', status=404)):
            with self.assertRaises(ModelError) as caught:
                await OllamaProvider(base_url="http://ollama.test").complete(req())
        self.assertEqual(caught.exception.kind, "config")
        self.assertIn("ollama pull", caught.exception.message)

    async def test_stream_without_done_is_transient(self):
        with use_transport(self.handler(ndjson({"message": {"content": "{}"}, "done": False}))):
            with self.assertRaises(ModelError) as caught:
                await OllamaProvider(base_url="http://ollama.test").complete(req())
        self.assertEqual(caught.exception.kind, "transient")


class DevProfileTests(ResearchTestCase):
    def test_dev_profile_families(self):
        d = S.build("dev")
        self.assertEqual(d.roles["intake"].members[0].model, "gpt-5.4-mini")
        self.assertEqual([m.family for m in d.roles["verify"].members], ["ollama", "openai"])
        self.assertEqual(set(d.strongest), {"openai", "ollama"})
        self.assertEqual(d.concurrency["ollama"], 1)
        self.assertTrue(all(m.effort != "xhigh" for spec in d.roles.values() for m in spec.members))

    def test_dev_cross_family_adjudication(self):
        d = S.build("dev")
        yes = lambda ref: True
        self.assertEqual(S.strongest_other(d, "openai", yes).model, "qwen3-coder:latest")
        self.assertEqual(S.strongest_other(d, "ollama", yes).model, "gpt-5.4-mini")

    def test_dev_preflight_warns(self):
        store = preflight.ProbeStore(self.tmp / "probes.json")
        for m in ("gpt-5.4-mini", "qwen3-coder:latest"):
            store.put(m, {"ok": True, "efforts_rejected": []})
        settings = S.build("dev", {"privacy": {"openai_project_shares_data": True}})
        providers = {"openai": FakeProvider("openai"), "ollama": FakeProvider("ollama")}
        result = preflight.run(settings, providers, store, synthetic=True)
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.independence["verify"], "full")
        self.assertTrue(any("Development profile" in w for w in result.warnings))
        self.assertTrue(any("critical roles" in w for w in result.warnings))
        self.assertFalse(preflight.run(settings, providers, store, synthetic=False).ok)  # sharing project: synthetic only

    async def test_context_guard_uses_num_ctx_for_ollama(self):
        ctx = llm.CallContext(db=self.db, settings=S.build("dev"),
                              providers={"openai": FakeProvider("openai"), "ollama": FakeProvider("ollama")},
                              limiters=Limiters({}), prices=PriceTable())
        from pydantic import BaseModel

        class Out(BaseModel):
            ok: bool

        with self.assertRaises(llm.ContextOverflow):
            await llm.complete_json(ctx, "synthesis", Out, "s", "x" * 100_000, prompt_version="v")


if __name__ == "__main__":
    unittest.main()
