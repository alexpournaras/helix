"""Log canary (PLAN B16.4, 1A.10): logs, events, task errors and the model-call log hold IDs and counts, never
problem text, answers or prompts. A full dummy run carries a canary string through a model call, a repair,
a failing task and a code step that raises; afterwards nothing but the stored responses may contain it."""

import logging

from pydantic import BaseModel

from research_testkit import ResearchTestCase
from app.research import llm, settings as S
from app.research.pipeline import Complete, Pipeline, StageDef, StepDef
from app.research.prices import PriceTable
from app.research.providers import FakeProvider, ModelError
from app.research.ratelimit import Limiters
from app.research.runner import Worker

CANARY = "CANARY-q7x9-synthetic"
PROBLEM = f"Synthetic problem text {CANARY}. It mentions the canary twice: {CANARY}."
ANSWER = f"Q: How long? A: {CANARY} weeks"


class Echo(BaseModel):
    summary: str
    count: int


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(self.format(record))


class CanaryTests(ResearchTestCase):
    def setUp(self):
        super().setUp()
        self.capture = _Capture()
        self.capture.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
        root = logging.getLogger()
        self._level = root.level
        root.setLevel(logging.DEBUG)
        root.addHandler(self.capture)
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            if calls["n"] == 1:
                return f"not json at all {CANARY}"  # forces a repair call
            return {"summary": f"echo {CANARY}", "count": 1}

        self.provider = FakeProvider("openai", handler)
        settings = S.build("quality")
        self.ctx = llm.CallContext(db=self.db, settings=settings, providers={"openai": self.provider,
                                   "anthropic": FakeProvider("anthropic", handler)}, limiters=Limiters({}),
                                   prices=PriceTable())

    def tearDown(self):
        root = logging.getLogger()
        root.removeHandler(self.capture)
        root.setLevel(self._level)
        super().tearDown()

    def pipeline(self) -> Pipeline:
        async def model_step(tc):
            inv = tc.investigation
            ctx = tc.services.with_task(task_id=tc.task["id"], step="intake", investigation_id=inv["id"])
            out = await llm.complete_json(ctx, "intake", Echo, "Summarise.", llm.material(inv["problem_text"] + ANSWER),
                                          prompt_version="canary.v1")

            def commit(conn):
                tc.db.event(inv["id"], "summarised", f"{out.count} item(s)", data={"count": out.count}, conn=conn)
            return commit

        async def failing_step(tc):
            raise ModelError("transient", "provider unavailable")

        async def code_step(tc):
            raise RuntimeError("bug in a code step")

        stages = {
            "intake": StageDef("intake", on_enter=lambda sc: sc.queue("model", "model:1"), on_complete=lambda sc: "work"),
            "work": StageDef("work", on_enter=lambda sc: sc.queue("flaky", "flaky:1"), on_complete=lambda sc: "code"),
            "code": StageDef("code", on_enter=lambda sc: sc.queue("code", "code:1"),
                             on_complete=lambda sc: Complete("findings")),
        }
        steps = {"model": StepDef("model", model_step), "flaky": StepDef("flaky", failing_step, on_fail="skip"),
                 "code": StepDef("code", code_step, code_step=True)}
        return Pipeline(stages=stages, steps=steps)

    async def test_no_case_text_outside_stored_responses(self):
        inv = self.db.create_investigation(PROBLEM, profile="quality", settings_snapshot={"guards": {}}, synthetic=True)
        worker = Worker(self.db, self.pipeline(), services=self.ctx, max_parallel=2)
        await worker.machine.advance(inv)
        for _ in range(10):  # let the transient failure exhaust its attempts without waiting
            await worker.run_until_idle()
            with self.db.tx() as conn:
                conn.execute("UPDATE research_tasks SET not_before = NULL WHERE investigation_id = ?", (inv,))
            await worker.machine.advance(inv)
        await worker.run_until_idle()

        # The run went through every path: a repair, a failed task and a code-step error.
        calls = self.db.query("SELECT * FROM llm_calls WHERE investigation_id = ?", (inv,))
        self.assertGreaterEqual(len(calls), 2)
        self.assertIn("failed", {r["status"] for r in self.db.query("SELECT status FROM investigations")})
        self.assertTrue(any(CANARY in (c["response"] or "") for c in calls))  # responses are kept (B16.4)

        leaks = []
        for line in self.capture.lines:
            if CANARY in line:
                leaks.append(f"log: {line[:120]}")
        for row in self.db.query("SELECT * FROM events WHERE investigation_id = ?", (inv,)):
            if CANARY in f"{row['message']} {row['data']}":
                leaks.append(f"event {row['kind']}")
        for row in calls:
            for column in ("error", "request_excerpt", "cache_key", "prompt_version", "step", "role"):
                if CANARY in str(row[column] or ""):
                    leaks.append(f"llm_calls.{column}")
        for row in self.db.query("SELECT * FROM research_tasks WHERE investigation_id = ?", (inv,)):
            for column, value in row.items():
                if CANARY in str(value or ""):
                    leaks.append(f"research_tasks.{column}")
        row = self.db.investigation(inv)
        for column in ("error", "stop_reason", "title"):
            if CANARY in str(row[column] or ""):
                leaks.append(f"investigations.{column}")
        self.assertEqual(leaks, [])

    async def test_store_prompts_is_off_by_default(self):
        self.assertFalse(S.build("quality").privacy.store_prompts)
        for profile in ("balanced", "economy", "dev"):
            self.assertFalse(S.build(profile).privacy.store_prompts, profile)
