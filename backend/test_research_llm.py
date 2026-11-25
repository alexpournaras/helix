import json
import unittest

import httpx
from pydantic import BaseModel, Field

from research_testkit import ResearchTestCase

from app.research import llm, preflight, settings as S, tokens
from app.research.prices import PriceTable
from app.research.providers import AnthropicProvider, FakeProvider, ModelError, OpenAIProvider, StructuredRequest
from app.research.providers.base import ProviderResult
from app.research.ratelimit import AdaptiveLimiter, Limiters
from app.research.transport import NetworkBlocked, use_transport


class Out(BaseModel):
    answer: str = Field(max_length=50)
    count: int = 0


def sse(*events) -> bytes:
    lines = []
    for event in events:
        name = event.get("type")
        lines.append(f"event: {name}\ndata: {json.dumps(event)}\n\n")
    return "".join(lines).encode()


def req(model="gpt-6.2-sol", effort="high") -> StructuredRequest:
    return StructuredRequest(model=model, system="sys", user="usr", schema={"type": "object"}, schema_name="Out",
                             effort=effort, max_output_tokens=1000, idle_timeout=5, deadline=10)


def openai_stream(*texts, incomplete=False, usage=None):
    events = []
    for text in texts:
        events.append({"type": "response.output_item.done",
                       "item": {"type": "message", "content": [{"type": "output_text", "text": text}]}})
    final = {"model": "gpt-6.2-sol", "usage": usage or {"input_tokens": 100, "output_tokens": 20,
                                                         "input_tokens_details": {"cached_tokens": 40}}}
    events.append({"type": "response.incomplete" if incomplete else "response.completed", "response": final})
    return sse(*events)


def anthropic_stream(text, stop="end_turn", thinking=True):
    events = [{"type": "message_start", "message": {"model": "claude-opus-5-5",
                                                    "usage": {"input_tokens": 70, "cache_read_input_tokens": 30}}}]
    idx = 0
    if thinking:
        events += [{"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
                   {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "{no"}},
                   {"type": "content_block_stop", "index": 0}]
        idx = 1
    half = len(text) // 2
    events += [{"type": "content_block_start", "index": idx, "content_block": {"type": "text", "text": ""}},
               {"type": "content_block_delta", "index": idx, "delta": {"type": "text_delta", "text": text[:half]}},
               {"type": "content_block_delta", "index": idx, "delta": {"type": "text_delta", "text": text[half:]}},
               {"type": "content_block_stop", "index": idx},
               {"type": "message_delta", "delta": {"stop_reason": stop}, "usage": {"output_tokens": 12}},
               {"type": "message_stop"}]
    return sse(*events)


class OpenAIAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_body_and_stream_parsing(self):
        seen = []

        def handle(request):
            seen.append((request.url.path, request.headers["authorization"], json.loads(request.content)))
            return httpx.Response(200, content=openai_stream('{"answer":"x"}'))

        provider = OpenAIProvider(key="k1")
        with use_transport(httpx.MockTransport(handle)):
            result = await provider.complete(req())
        path, auth, body = seen[0]
        self.assertEqual((path, auth), ("/v1/responses", "Bearer k1"))
        self.assertEqual(body["stream"], True)
        self.assertEqual(body["store"], False)
        self.assertEqual(body["max_output_tokens"], 1000)
        self.assertEqual(body["reasoning"], {"effort": "high"})
        self.assertEqual(body["text"]["format"]["type"], "json_schema")
        self.assertTrue(body["text"]["format"]["strict"])
        self.assertNotIn("temperature", body)
        self.assertEqual(result.texts, ['{"answer":"x"}'])
        self.assertEqual((result.input_tokens, result.cached_input_tokens, result.output_tokens), (100, 40, 20))

    def test_effort_none_never_sent_and_probed_efforts_respected(self):
        provider = OpenAIProvider(key="k", capabilities={"gpt-6.2-sol": {"efforts": ["low", "high"]}})
        self.assertNotIn("reasoning", provider.body(req(effort="none")))
        self.assertNotIn("reasoning", provider.body(req(effort="xhigh")))
        self.assertEqual(provider.body(req(effort="low"))["reasoning"], {"effort": "low"})

    async def test_extra_message_item_keeps_all_texts(self):
        handle = lambda r: httpx.Response(200, content=openai_stream('{"answer":"first"}', '{"answer":"second"}'))
        with use_transport(httpx.MockTransport(handle)):
            result = await OpenAIProvider(key="k").complete(req())
        self.assertEqual(len(result.texts), 2)

    async def test_incomplete_is_truncated(self):
        handle = lambda r: httpx.Response(200, content=openai_stream('{"ans', incomplete=True))
        with use_transport(httpx.MockTransport(handle)):
            result = await OpenAIProvider(key="k").complete(req())
        self.assertEqual(result.stop, "truncated")

    async def test_error_classification(self):
        cases = [
            (httpx.Response(401, json={"error": {"message": "bad key"}}), "config"),
            (httpx.Response(429, json={"error": {"message": "Rate limit"}}, headers={"retry-after": "7"}), "transient"),
            (httpx.Response(429, json={"error": {"message": "You exceeded your current quota"}}), "config"),
            (httpx.Response(500, json={"error": {"message": "oops"}}), "transient"),
            (httpx.Response(400, json={"error": {"message": "Unsupported parameter: reasoning.effort"}}), "config"),
            (httpx.Response(400, json={"error": {"message": "context length exceeded the maximum"}}), "overflow"),
        ]
        for response, kind in cases:
            with use_transport(httpx.MockTransport(lambda r, resp=response: resp)):
                with self.assertRaises(ModelError) as caught:
                    await OpenAIProvider(key="k").complete(req())
            self.assertEqual(caught.exception.kind, kind, response.text)
        self.assertEqual(caught.exception.kind, "overflow")

    async def test_missing_key_is_config(self):
        with self.assertRaises(ModelError) as caught:
            await OpenAIProvider(key="").complete(req())
        self.assertEqual(caught.exception.kind, "config")

    async def test_network_guard_blocks_real_calls(self):
        with self.assertRaises((NetworkBlocked, ModelError)):
            await OpenAIProvider(key="k").complete(req())


class AnthropicAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_body_and_thinking_ignored(self):
        seen = []

        def handle(request):
            seen.append((request.headers, json.loads(request.content)))
            return httpx.Response(200, content=anthropic_stream('{"answer":"ok"}'))

        with use_transport(httpx.MockTransport(handle)):
            result = await AnthropicProvider(key="ak", version="2026-10-21").complete(req("claude-opus-5-5", "high"))
        headers, body = seen[0]
        self.assertEqual(headers["x-api-key"], "ak")
        self.assertEqual(headers["anthropic-version"], "2026-10-21")
        self.assertEqual(body["output_config"]["format"]["type"], "json_schema")
        self.assertEqual(body["output_config"]["effort"], "high")
        self.assertGreaterEqual(body["max_tokens"], 64000)  # thinking counts toward max_tokens
        self.assertTrue(body["stream"])
        self.assertEqual(result.texts, ['{"answer":"ok"}'])
        self.assertEqual((result.input_tokens, result.cached_input_tokens, result.output_tokens), (100, 30, 12))

    async def test_max_tokens_is_truncated_and_529_transient(self):
        with use_transport(httpx.MockTransport(lambda r: httpx.Response(200, content=anthropic_stream('{"a', "max_tokens")))):
            result = await AnthropicProvider(key="k").complete(req("claude-opus-5-5"))
        self.assertEqual(result.stop, "truncated")
        with use_transport(httpx.MockTransport(lambda r: httpx.Response(529, json={"error": {"type": "overloaded_error"}}))):
            with self.assertRaises(ModelError) as caught:
                await AnthropicProvider(key="k").complete(req("claude-opus-5-5"))
        self.assertEqual(caught.exception.kind, "transient")

    async def test_stream_error_event(self):
        body = sse({"type": "message_start", "message": {"usage": {}}},
                   {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}})
        with use_transport(httpx.MockTransport(lambda r: httpx.Response(200, content=body))):
            with self.assertRaises(ModelError) as caught:
                await AnthropicProvider(key="k").complete(req("claude-opus-5-5"))
        self.assertEqual(caught.exception.kind, "transient")


class LimiterTests(unittest.IsolatedAsyncioTestCase):
    async def test_aimd(self):
        clock = [0.0]
        limiter = AdaptiveLimiter(16, success_run=3, clock=lambda: clock[0])
        limiter.on_throttle(5)
        self.assertEqual(limiter.limit, 8)
        limiter.on_throttle()
        self.assertEqual(limiter.limit, 4)
        for _ in range(3):
            limiter.on_success()
        self.assertEqual(limiter.limit, 5)
        self.assertEqual(limiter._paused_until, 5)

    async def test_concurrency_never_exceeds_limit(self):
        import asyncio
        limiter = AdaptiveLimiter(2)
        peak = [0]

        async def job():
            async with limiter:
                peak[0] = max(peak[0], limiter.active)
                await asyncio.sleep(0.01)

        await asyncio.gather(*[job() for _ in range(10)])
        self.assertLessEqual(peak[0], 2)


class ModelLayerTests(ResearchTestCase):
    def ctx(self, handler_a=None, handler_b=None, **settings_overrides):
        self.a = FakeProvider("openai", handler_a)
        self.b = FakeProvider("anthropic", handler_b)
        sleeps = []
        self.sleeps = sleeps

        async def fake_sleep(s):
            sleeps.append(s)

        return llm.CallContext(db=self.db, settings=S.build("quality", settings_overrides),
                               providers={"openai": self.a, "anthropic": self.b}, limiters=Limiters({}),
                               prices=PriceTable(), investigation_id=None, step="test", sleep=fake_sleep)

    async def test_routing_validation_logging_and_cost(self):
        ctx = self.ctx(lambda r: {"answer": "hi", "count": 2})
        out = await llm.complete_json(ctx, "intake", Out, "Step prompt.", "user text", prompt_version="t.v1")
        self.assertEqual(out, Out(answer="hi", count=2))
        sent = self.a.requests[0]
        self.assertEqual(sent.model, "gpt-6.2-astra")
        self.assertIn("<material>", llm.material("x"))
        self.assertIn("Cite only IDs", sent.system)
        self.assertIn("answer: maxLength=50", sent.system)
        calls = self.db.query("SELECT * FROM llm_calls")
        self.assertEqual(len(calls), 1)
        self.assertEqual((calls[0]["status"], calls[0]["role"], calls[0]["model"]), ("ok", "intake", "gpt-6.2-astra"))
        self.assertIsNotNone(calls[0]["cost"])
        self.assertIsNone(calls[0]["request_excerpt"])  # store_prompts off

    async def test_cache_hit_skips_provider(self):
        ctx = self.ctx(lambda r: {"answer": "hi"})
        await llm.complete_json(ctx, "intake", Out, "s", "u", prompt_version="t.v1")
        await llm.complete_json(ctx, "intake", Out, "s", "u", prompt_version="t.v1")
        self.assertEqual(len(self.a.requests), 1)
        calls = self.db.query("SELECT cached, attempt_kind FROM llm_calls ORDER BY id")
        self.assertEqual([(c["cached"], c["attempt_kind"]) for c in calls], [(0, "first"), (1, "cache")])
        await llm.complete_json(ctx, "intake", Out, "s", "u", prompt_version="t.v2")
        self.assertEqual(len(self.a.requests), 2)  # prompt version is part of the key

    async def test_shared_cache_spans_databases(self):
        """Eval runs give each case its own database but share one cache (eval/results/llm_cache.sqlite3)."""
        from app.research.db import ResearchDB
        shared = ResearchDB(self.tmp / "shared.sqlite3")
        shared.init()
        first = self.ctx(lambda r: {"answer": "hi"})
        first.cache = shared
        await llm.complete_json(first, "intake", Out, "s", "u", prompt_version="t.v1")
        other_db = ResearchDB(self.tmp / "other.sqlite3")
        other_db.init()
        second = self.ctx(lambda r: {"answer": "hi"})
        second.db, second.cache = other_db, shared
        await llm.complete_json(second.with_task(task_id=None, step="t"), "intake", Out, "s", "u",
                                prompt_version="t.v1")
        self.assertEqual(len(self.a.requests), 0)  # second context's provider was never called
        self.assertEqual(other_db.one("SELECT attempt_kind FROM llm_calls")["attempt_kind"], "cache")
        self.assertIsNone(self.db.cache_get(llm.cache_key(S.ModelRef(model="gpt-6.2-astra", effort="high"),
                                                          "t.v1", "s", "u", {})))  # nothing in the case db

    async def test_repair_once_then_format_error(self):
        outputs = iter(['{"answer": 5}', '{"answer": "fixed"}'])
        ctx = self.ctx(lambda r: next(outputs))
        out = await llm.complete_json(ctx, "intake", Out, "s", "u", prompt_version="t.v1")
        self.assertEqual(out.answer, "fixed")
        self.assertIn("previous output was rejected", self.a.requests[1].user)
        ctx = self.ctx(lambda r: "not json")
        with self.assertRaises(ModelError) as caught:
            await llm.complete_json(ctx, "intake", Out, "s", "u2", prompt_version="t.v1")
        self.assertEqual(caught.exception.kind, "format")
        self.assertEqual(len(self.a.requests), 2)

    async def test_step_validate_triggers_repair(self):
        outputs = iter([{"answer": "bad"}, {"answer": "good"}])
        ctx = self.ctx(lambda r: next(outputs))

        def check(v):
            if v.answer == "bad":
                raise ValueError("answer must not be bad")

        out = await llm.complete_json(ctx, "intake", Out, "s", "u", prompt_version="t.v1", validate=check)
        self.assertEqual(out.answer, "good")
        self.assertIn("answer must not be bad", self.a.requests[1].user)

    async def test_last_valid_output_wins(self):
        ctx = self.ctx(lambda r: ProviderResult(texts=['{"answer":"a"}', '{"answer":"b"}', "garbage"]))
        out = await llm.complete_json(ctx, "intake", Out, "s", "u", prompt_version="t.v1")
        self.assertEqual(out.answer, "b")

    async def test_truncation_retries_with_larger_limit_then_overflows(self):
        ctx = self.ctx(lambda r: ProviderResult(texts=['{"ans'], stop="truncated"))
        with self.assertRaises(ModelError) as caught:
            await llm.complete_json(ctx, "intake", Out, "s", "u", prompt_version="t.v1")
        self.assertEqual(caught.exception.kind, "overflow")
        limits = [r.max_output_tokens for r in self.a.requests]
        self.assertEqual(limits, [64000, 128000])

    async def test_transient_backoff_and_adaptive_concurrency(self):
        state = {"n": 0}

        def handle(r):
            state["n"] += 1
            if state["n"] == 1:
                raise ModelError("transient", "busy", retry_after=3, status=429)
            return {"answer": "ok"}

        ctx = self.ctx(handle)
        before = ctx.limiters.get("openai").limit
        out = await llm.complete_json(ctx, "intake", Out, "s", "u", prompt_version="t.v1")
        self.assertEqual(out.answer, "ok")
        self.assertEqual(self.sleeps, [3])
        self.assertEqual(ctx.limiters.get("openai").limit, before // 2)
        statuses = [c["status"] for c in self.db.query("SELECT status FROM llm_calls ORDER BY id")]
        self.assertEqual(statuses, ["error", "ok"])

    async def test_long_retry_after_goes_to_task_level(self):
        ctx = self.ctx(lambda r: (_ for _ in ()).throw(ModelError("transient", "busy", retry_after=120, status=429)))
        with self.assertRaises(ModelError) as caught:
            await llm.complete_json(ctx, "intake", Out, "s", "u", prompt_version="t.v1")
        self.assertEqual(caught.exception.kind, "transient")
        self.assertEqual(self.sleeps, [])

    async def test_quota_and_refusal_fail(self):
        ctx = self.ctx(lambda r: (_ for _ in ()).throw(ModelError("config", "quota", status=429)))
        with self.assertRaises(ModelError) as caught:
            await llm.complete_json(ctx, "intake", Out, "s", "u", prompt_version="t.v1")
        self.assertEqual(caught.exception.kind, "config")
        ctx = self.ctx(lambda r: ProviderResult(texts=[], stop="refusal", refusal="no"))
        with self.assertRaises(ModelError) as caught:
            await llm.complete_json(ctx, "intake", Out, "s", "u", prompt_version="t.v1")
        self.assertEqual(caught.exception.kind, "refusal")

    async def test_context_overflow_before_any_call(self):
        ctx = self.ctx(lambda r: {"answer": "x"})
        huge = "α" * 1_100_000
        with self.assertRaises(llm.ContextOverflow):
            await llm.complete_json(ctx, "intake", Out, "s", huge, prompt_version="t.v1")
        self.assertEqual(self.a.requests, [])

    async def test_explicit_model_and_fallback_routing(self):
        ctx = self.ctx(handler_b=lambda r: {"answer": "from b"})
        ref = S.ModelRef(model="claude-opus-5-5", effort="high")
        self.assertEqual((await llm.complete_json(ctx, "intake", Out, "s", "u", prompt_version="v", model=ref)).answer,
                         "from b")
        self.a._has_key = False
        await llm.complete_json(ctx, "intake", Out, "s", "u2", prompt_version="v")
        self.assertEqual(self.b.requests[-1].model, "claude-opus-5-5")  # astra → opus fallback

    async def test_store_prompts_keeps_excerpt(self):
        ctx = self.ctx(lambda r: {"answer": "x"}, privacy={"store_prompts": True})
        await llm.complete_json(ctx, "intake", Out, "s", "synthetic user text", prompt_version="v")
        self.assertEqual(self.db.one("SELECT request_excerpt FROM llm_calls")["request_excerpt"], "synthetic user text")


class ProbeTests(ResearchTestCase):
    async def test_probe_records_accepted_and_rejected_efforts(self):
        def handle(r):
            if r.effort in ("none", "minimal"):
                raise ModelError("config", "Unsupported value: reasoning.effort", status=400)
            return {"ok": True, "word": "helix"}

        record = await preflight.probe(FakeProvider("openai", handle), S.ModelRef(model="gpt-6.2-astra", effort="high"))
        self.assertTrue(record["ok"])
        self.assertEqual(record["efforts_rejected"], ["none", "minimal"])
        self.assertIn("high", record["efforts_accepted"])
        store = preflight.ProbeStore(self.tmp / "probes.json")
        store.put("gpt-6.2-astra", record)
        self.assertTrue(store.accepts(S.ModelRef(model="gpt-6.2-astra", effort="high")))
        self.assertFalse(store.accepts(S.ModelRef(model="gpt-6.2-astra", effort="none")))
        self.assertFalse(store.accepts(S.ModelRef(model="gpt-6.2-sol", effort="high")))

    async def test_probe_failure(self):
        record = await preflight.probe(FakeProvider("anthropic", lambda r: "nope"),
                                       S.ModelRef(model="claude-opus-5-5", effort="high"), efforts=["high"])
        self.assertFalse(record["ok"])


class PreflightTests(ResearchTestCase):
    def setUp(self):
        super().setUp()
        self.store = preflight.ProbeStore(self.tmp / "probes.json")
        self.providers = {"openai": FakeProvider("openai"), "anthropic": FakeProvider("anthropic")}

    def probe_all(self, models):
        for m in models:
            self.store.put(m, {"ok": True, "efforts_rejected": []})

    def test_unprobed_roles_refused(self):
        result = preflight.run(S.build("quality", {"privacy": {"openai_project_shares_data": False}}),
                               self.providers, self.store, synthetic=False)
        self.assertFalse(result.ok)
        self.assertTrue(any("probed" in e for e in result.errors))

    def test_all_probed_passes_with_full_independence(self):
        self.probe_all([S.ASTRA, S.SOL, S.FABLE, S.OPUS, S.SONNET])
        result = preflight.run(S.build("quality", {"privacy": {"openai_project_shares_data": False}}),
                               self.providers, self.store, synthetic=False)
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.independence["verify"], "full")
        self.assertEqual(result.providers, ["anthropic", "openai"])

    def test_privacy_gate(self):
        self.probe_all([S.ASTRA, S.SOL, S.FABLE, S.OPUS, S.SONNET])
        unanswered = preflight.run(S.build("quality"), self.providers, self.store, synthetic=False)
        self.assertTrue(any("privacy question" in e for e in unanswered.errors))
        sharing = S.build("quality", {"privacy": {"openai_project_shares_data": True}})
        self.assertFalse(preflight.run(sharing, self.providers, self.store, synthetic=False).ok)
        self.assertTrue(preflight.run(sharing, self.providers, self.store, synthetic=True).ok)

    def test_single_provider_warns_same_family(self):
        self.providers["anthropic"] = FakeProvider("anthropic", has_key=False)
        self.probe_all([S.ASTRA, S.SOL, S.LUNA])
        result = preflight.run(S.build("quality", {"privacy": {"openai_project_shares_data": False}}),
                               self.providers, self.store, synthetic=False)
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.independence["verify"], "same_family")
        self.assertTrue(any("one model family" in w for w in result.warnings))

    def test_loopback_guard(self):
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {"HELIX_HOST": "0.0.0.0"}):
            self.assertIsNotNone(preflight.loopback_guard())
            with patch.dict(os.environ, {"HELIX_ALLOW_REMOTE": "1"}):
                self.assertIsNone(preflight.loopback_guard())
        with patch.dict(os.environ, {"HELIX_HOST": "127.0.0.1"}):
            self.assertIsNone(preflight.loopback_guard())


class TokenAndPriceTests(unittest.TestCase):
    def test_token_estimate_is_script_aware(self):
        self.assertEqual(tokens.estimate(""), 0)
        self.assertGreater(tokens.estimate("εμβοές στο αυτί"), tokens.estimate("tinnitus in ear"))
        self.assertEqual(tokens.estimate("a" * 320), int(100 * 1.1) + 1)

    def test_prices(self):
        prices = PriceTable()
        self.assertAlmostEqual(prices.cost("gpt-6.2-astra", 1_000_000, 100_000), 8 + 4)
        self.assertAlmostEqual(prices.cost("gpt-6.2-astra", 1_000_000, 0, 1_000_000), 0.8)
        self.assertIsNone(prices.cost("unknown-model", 10, 10))


if __name__ == "__main__":
    unittest.main()
