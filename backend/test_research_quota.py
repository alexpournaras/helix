"""Daily quota ledger (PLAN B17.1, B17.5 item 1, task 1R.0)."""

import asyncio
import threading
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel
from research_testkit import ResearchTestCase

from app.research import ensemble, llm, quota, settings as S, transport
from app.research.db import now
from app.research.prices import PriceTable
from app.research.providers import FakeProvider, ModelError
from app.research.quota import Ledger, QuotaExceeded
from app.research.ratelimit import Limiters
from app.research.runner import Worker
from test_research_worker import Dummy


class Out(BaseModel):
    answer: str


class Clock:
    def __init__(self, t: datetime):
        self.t = t

    def __call__(self) -> datetime:
        return self.t


class CountedFake(FakeProvider):
    """A fake that the ledger treats like a real adapter."""
    quota_counted = True


class LedgerTests(ResearchTestCase):
    def setUp(self):
        super().setUp()
        self.clock = Clock(datetime(2026, 11, 28, 21, 30, tzinfo=timezone.utc))
        self.ledger = Ledger(self.tmp / "quota.sqlite3", clock=self.clock)

    def test_reserve_within_ceiling_and_refuse_beyond(self):
        a = self.ledger.reserve("openai", 600, 1000)
        with self.assertRaises(QuotaExceeded) as cm:
            self.ledger.reserve("openai", 500, 1000)  # 600 held + 500 > 1000
        self.assertEqual(cm.exception.kind, "quota")
        self.assertEqual(cm.exception.reset_at, "2026-11-29T00:00:00Z")
        self.assertAlmostEqual(cm.exception.retry_after, 2.5 * 3600 + 5, delta=1)
        self.ledger.settle(a, 100, 50)  # actual usage replaces the reservation
        b = self.ledger.reserve("openai", 800, 1000)  # 150 used + 800 ≤ 1000
        st = self.ledger.status("openai", 1000)
        self.assertEqual((st.used, st.reserved, st.remaining), (150, 800, 50))
        self.ledger.release(b)
        self.assertEqual(self.ledger.status("openai", 1000).reserved, 0)

    def test_keep_books_the_full_reservation(self):
        r = self.ledger.reserve("openai", 700, 1000)
        self.ledger.keep(r)
        st = self.ledger.status("openai", 1000)
        self.assertEqual((st.used, st.reserved), (700, 0))
        self.ledger.settle(r, 1, 1)  # a later settle of a finished reservation changes nothing
        self.assertEqual(self.ledger.status("openai", 1000).used, 700)

    def test_no_ceiling_counts_but_never_refuses(self):
        self.ledger.settle(self.ledger.reserve("anthropic", 10**9, None), 5, 5)
        self.assertEqual(self.ledger.status("anthropic", None).used, 10)

    def test_external_adjustment_counts(self):
        self.ledger.add_external("openai", 900)
        with self.assertRaises(QuotaExceeded):
            self.ledger.reserve("openai", 200, 1000)
        self.assertEqual(self.ledger.status("openai", 1000).remaining, 100)

    def test_stale_held_reservation_is_booked_in_full(self):
        self.ledger.reserve("openai", 400, 1000)  # its process died before settling
        self.clock.t += timedelta(seconds=quota.STALE_SECONDS + 1)
        st = self.ledger.status("openai", 1000)
        self.assertEqual((st.used, st.reserved), (400, 0))

    def test_utc_day_boundary(self):
        r = self.ledger.reserve("openai", 900, 1000)  # 21:30 UTC on the 28th
        self.clock.t = datetime(2026, 11, 29, 0, 1, tzinfo=timezone.utc)
        self.ledger.settle(r, 300, 300)  # booked to the day the call started
        today = self.ledger.status("openai", 1000)
        self.assertEqual((today.day_utc, today.used, today.reserved), ("2026-11-29", 0, 0))
        self.ledger.reserve("openai", 1000, 1000)  # a fresh day has the whole ceiling
        self.clock.t = datetime(2026, 11, 28, 23, 0, tzinfo=timezone.utc)
        self.assertEqual(self.ledger.status("openai", 1000).used, 600)

    def test_parallel_reservations_never_pass_the_ceiling(self):
        """Many threads, each with its own Ledger on the same file (like separate processes)."""
        ok, refused = [], []

        def worker():
            mine = Ledger(self.tmp / "quota.sqlite3", clock=self.clock)
            try:
                ok.append(mine.reserve("openai", 100, 1000))
            except QuotaExceeded:
                refused.append(1)

        threads = [threading.Thread(target=worker) for _ in range(25)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual((len(ok), len(refused)), (10, 15))
        self.assertEqual(self.ledger.status("openai", 1000).reserved, 1000)

    def test_case_can_start(self):
        st = self.ledger.status("openai", 2_400_000)
        self.assertTrue(st.case_can_start())
        self.ledger.add_external("openai", 500_000)
        self.assertFalse(self.ledger.status("openai", 2_400_000).case_can_start())


class CallLayerTests(ResearchTestCase):
    def setUp(self):
        super().setUp()
        self.ledger = Ledger(self.tmp / "quota.sqlite3")

    def ctx(self, provider, ceiling=100_000):
        st = S.build("quality", {"quota": {"daily_ceiling_tokens": ceiling},
                                 "providers": {"openai": {"max_output_tokens": 1000}}})
        return llm.CallContext(db=self.db, settings=st, providers={"openai": provider, "anthropic": FakeProvider("anthropic")},
                               limiters=Limiters({}), prices=PriceTable(), step="test", ledger=self.ledger)

    def status(self):
        return self.ledger.status("openai", 100_000)

    async def test_call_reserves_then_settles_actual_usage(self):
        p = CountedFake("openai", lambda r: {"answer": "hi"})
        await llm.complete_json(self.ctx(p), "intake", Out, "", "user text", prompt_version="t.v1")
        call = self.db.query("SELECT input_tokens, output_tokens FROM llm_calls")[0]
        st = self.status()
        self.assertEqual((st.used, st.reserved), (call["input_tokens"] + call["output_tokens"], 0))
        # the cached repeat reaches no provider and counts nothing
        await llm.complete_json(self.ctx(p), "intake", Out, "", "user text", prompt_version="t.v1")
        self.assertEqual(self.status().used, st.used)

    async def test_over_the_ceiling_the_provider_is_not_called(self):
        p = CountedFake("openai", lambda r: {"answer": "hi"})
        with self.assertRaises(QuotaExceeded):
            await llm.complete_json(self.ctx(p, ceiling=500), "intake", Out, "", "x", prompt_version="t.v1")
        self.assertEqual(p.requests, [])

    async def test_uncounted_providers_and_transports(self):
        plain = FakeProvider("openai", lambda r: {"answer": "hi"})  # fake: never counted
        await llm.complete_json(self.ctx(plain), "intake", Out, "", "a", prompt_version="t.v1")
        counted = CountedFake("openai", lambda r: {"answer": "hi"})
        with transport.use_transport(transport.BlockedTransport()):  # replay / fake modes
            await llm.complete_json(self.ctx(counted), "intake", Out, "", "b", prompt_version="t.v1")
        self.assertEqual(self.status().used, 0)

    async def test_http_error_releases_and_timeout_keeps(self):
        def fail_with(exc):
            def handler(r):
                raise exc
            return handler
        http = CountedFake("openai", fail_with(ModelError("config", "bad request", status=400)))
        with self.assertRaises(ModelError):
            await llm.complete_json(self.ctx(http), "intake", Out, "", "a", prompt_version="t.v1")
        self.assertEqual((self.status().used, self.status().reserved), (0, 0))
        timeout = CountedFake("openai", fail_with(ModelError("config", "stream dropped")))  # no status
        with self.assertRaises(ModelError):
            await llm.complete_json(self.ctx(timeout), "intake", Out, "", "b", prompt_version="t.v1")
        self.assertGreater(self.status().used, 1000)  # booked at its full reservation (input + 1000 output)

    async def test_quota_error_is_not_a_failed_panel_member(self):
        p = CountedFake("openai", lambda r: {"answer": "hi"})
        ctx = self.ctx(p, ceiling=500)
        with self.assertRaises(QuotaExceeded):
            await ensemble.panel(ctx, "safety", Out, "", "x", prompt_version="t.v1",
                                 members=[S.ModelRef(model="gpt-5.4-mini", effort="high"),
                                          S.ModelRef(model="gpt-5.4-mini", effort="medium")])


class WorkerQuotaTests(ResearchTestCase):
    async def test_task_waits_for_the_reset_without_using_an_attempt(self):
        dummy = Dummy()
        worker = Worker(self.db, dummy.pipeline(), max_parallel=4)
        inv = self.db.create_investigation("Synthetic.", profile="quality",
                                           settings_snapshot={"review": {"max_iterations": 1}, "guards": {}},
                                           synthetic=True)
        dummy.fail_first["a:1"] = QuotaExceeded("ceiling", retry_after=3 * 3600, reset_at="2026-11-29T00:00:00Z")
        before = datetime.now(timezone.utc)
        await worker.machine.advance(inv)
        await worker.run_until_idle()
        task = [t for t in self.db.tasks(inv) if t["key"] == "a:1"][0]
        self.assertEqual((task["status"], task["attempts"], task["error_kind"]), ("queued", 0, "quota"))
        wait = (datetime.fromisoformat(task["not_before"].replace("Z", "+00:00")) - before).total_seconds()
        self.assertAlmostEqual(wait, 3 * 3600, delta=10)
        row = self.db.investigation(inv)
        self.assertEqual((row["status"], row["stage"]), ("active", "work"))
        self.assertEqual(len(self.db.query("SELECT * FROM events WHERE investigation_id = ? AND kind = 'quota_wait'",
                                           (inv,))), 1)
        with self.db.tx() as conn:  # the reset arrives
            conn.execute("UPDATE research_tasks SET not_before = ? WHERE id = ?", (now(), task["id"]))
        await worker.run_until_idle()
        self.assertEqual(self.db.investigation(inv)["status"], "complete")


if __name__ == "__main__":
    import unittest
    unittest.main()
