"""The `budget` profile and the case budget (PLAN B17.2–B17.4, task 1R.1)."""

import unittest

from pydantic import BaseModel, ValidationError
from research_testkit import ResearchTestCase

from app.research import budgets, llm, settings as S
from app.research.pipeline import Complete, Pipeline, StageDef, StepDef
from app.research.prices import PriceTable
from app.research.providers import FakeProvider
from app.research.ratelimit import Limiters
from app.research.runner import Worker
from app.research.steps import build_pipeline


class Out(BaseModel):
    answer: str


class ProfileTests(unittest.TestCase):
    def test_budget_is_the_default_and_matches_b17(self):
        b = S.build()
        self.assertEqual(b.profile, "budget")
        self.assertEqual({m.model for r in b.roles.values() for m in r.members}, {"gpt-5.4-mini"})
        self.assertEqual([m.effort for m in b.roles["safety"].members], ["high", "medium"])
        self.assertEqual(S.resolve_role(b, "safety", lambda r: True).independence, "same_model")
        for role, (effort, max_out) in S.BUDGET_ROLES.items():
            ref = b.roles[role].members[0]
            self.assertEqual((ref.effort, ref.max_output_tokens), (effort, max_out), role)
        e = b.ensembles
        self.assertEqual((e.dual_screening, e.dual_extraction, e.whole_paper_pass, e.verify_panel, e.planners,
                          e.gaps_panel, e.critics, e.appraisal_panel), (False, False, False, 1, 1, False, ["combined"], False))
        self.assertEqual((b.guards.max_tokens, b.guards.reserve, b.guards.max_total_papers, b.guards.max_screened),
                         (2_400_000, 0, 16, 400))
        self.assertEqual(sum(b.budget["targets"].values()), 1_480_000)  # round-1 targets (B17.4)
        self.assertEqual(b.budget["protected"], {"finalization": 650_000, "retry": 150_000})
        self.assertEqual((b.selection["max_fulltext_papers_per_round"], b.selection["round2_max_papers"]), (10, 6))
        self.assertEqual((b.loop["max_rounds"], b.loop["round2_min_pool"]), (2, 260_000))
        self.assertEqual((b.extraction["max_pack_tokens"], b.extraction["max_packs_per_paper"]), (16_000, 2))
        self.assertEqual(b.verification["audit_rate"], 0.05)
        self.assertEqual(set(b.verification["second_opinion"]), set(S.SECOND_OPINION_TRIGGERS))
        self.assertEqual(b.privacy.mode, "shared_synthetic")
        # the pool: 2.4M − round-1 targets − protected reserves
        self.assertEqual(b.guards.max_tokens - 1_480_000 - 800_000, 120_000)

    def test_validation(self):
        with self.assertRaises(ValidationError):
            S.build("budget", {"budget": {"targets": {"reading": 1}}})  # unknown group
        with self.assertRaises(ValidationError):
            S.build("budget", {"budget": {"targets": {"read": -5}}})
        with self.assertRaises(ValidationError):
            S.build("budget", {"budget": {"protected": {"other": 5}}})
        with self.assertRaises(ValidationError):
            S.build("budget", {"verification": {"second_opinion": ["vibes"]}})
        with self.assertRaises(ValidationError):
            S.build("budget", {"ensembles": {"critics": ["evidence"]}})  # no safety and no combined critic

    def test_every_model_calling_step_has_a_stage_group(self):
        pipeline, _ = build_pipeline()
        model_steps = {name for name, step in pipeline.steps.items() if not step.code_step}
        missing = model_steps - set(budgets.STAGE_GROUPS) - {"search", "seed", "snowball", "fetch", "group", "safety_finalize"}  # no model calls
        self.assertEqual(missing, set())


class UsageTests(ResearchTestCase):
    def setUp(self):
        super().setUp()
        self.inv = self.db.create_investigation("Synthetic.", profile="budget", settings_snapshot={}, synthetic=True)

    def log(self, step, tokens, *, cached=False, attempt="first"):
        self.db.log_call({"investigation_id": self.inv, "step": step, "role": "r", "provider": "openai",
                          "model": "gpt-5.4-mini", "status": "ok", "cached": cached, "attempt_kind": attempt,
                          "input_tokens": tokens // 2, "output_tokens": tokens - tokens // 2})

    def test_group_usage_counts_provider_tokens_only(self):
        self.log("read", 1000)
        self.log("verify", 500)
        self.log("synthesis", 300, attempt="repair")
        self.log("read", 9999, cached=True)  # a cache hit reached no provider (and logs 0 in practice)
        u = budgets.group_usage(self.db, self.inv)
        self.assertEqual((u["read"], u["report"], u["retry"], u["total"]), (1500, 300, 300, 1800))

    def test_acquisition_rule_keeps_the_protected_reserves(self):
        st = S.build("budget").model_dump(mode="json")
        self.assertIsNone(budgets.acquisition_refusal(self.db, self.inv, "read", st))
        self.log("read", 1_600_000 - 80_000)  # read estimate is 80k: exactly at 2.4M − 800k
        self.assertIsNone(budgets.acquisition_refusal(self.db, self.inv, "read", st))
        self.log("screen", 1)
        self.assertEqual(budgets.acquisition_refusal(self.db, self.inv, "read", st), "budget:read")
        self.assertIsNone(budgets.acquisition_refusal(self.db, self.inv, "fit", st))  # not acquisition
        # spent finalization tokens are no longer reserved
        self.log("fit", 100_000)
        self.assertEqual(budgets.protected_unspent(budgets.group_usage(self.db, self.inv), st["budget"]), 700_000)
        # quality has no case budget: the rule is off
        self.assertIsNone(budgets.acquisition_refusal(self.db, self.inv, "read", S.build("quality").model_dump(mode="json")))

    def test_over_targets(self):
        st = S.build("budget")
        self.assertEqual(budgets.over_targets({"read": 650_001, "assess": 400_000, "report": 300_000}, st.budget),
                         ["read", "finalization"])


class WorkerBudgetTests(ResearchTestCase):
    """A pipeline whose `read` tasks each spend 100k tokens: the rule skips the ones that don't fit."""

    def pipeline(self) -> Pipeline:
        async def read(tc):
            tc.db.log_call({"investigation_id": tc.task["investigation_id"], "step": "read", "role": "read.claims",
                            "provider": "openai", "model": "gpt-5.4-mini", "status": "ok", "cached": False,
                            "attempt_kind": "first", "input_tokens": 60_000, "output_tokens": 40_000})
            return None

        def work_enter(sc):
            for n in range(6):
                sc.queue("read", f"read:{n}")

        stages = {"intake": StageDef("intake", on_complete=lambda sc: "work"),
                  "work": StageDef("work", on_enter=work_enter, on_complete=lambda sc: "done",
                                   skip_check_steps=("read",)),
                  "done": StageDef("done", on_complete=lambda sc: Complete("findings"))}
        return Pipeline(stages=stages, steps={"read": StepDef("read", read, on_fail="skip")})

    async def test_reads_past_the_budget_are_skipped_and_the_case_completes(self):
        snap = S.build("budget", {"guards": {"max_tokens": 1_200_000},
                                  "budget": {"targets": {"read": 200_000}}}).model_dump(mode="json")
        # room for reads = 1.2M − 800k protected = 400k; each start needs used + 80k ≤ 400k → 4 run, 2 skipped
        inv = self.db.create_investigation("Synthetic.", profile="budget", settings_snapshot=snap, synthetic=True)
        worker = Worker(self.db, self.pipeline(), max_parallel=1)
        await worker.machine.advance(inv)
        await worker.run_until_idle()
        tasks = self.db.tasks(inv)
        self.assertEqual(sum(t["status"] == "done" for t in tasks), 4)
        skipped = [t for t in tasks if t["status"] == "skipped"]
        self.assertEqual({t["skip_reason"] for t in skipped}, {"budget:read"})
        self.assertEqual(len(skipped), 2)
        row = self.db.investigation(inv)
        self.assertEqual((row["status"], row["outcome"]), ("complete", "findings"))  # B10 rule ignores budget skips
        kinds = [e["kind"] for e in self.db.query("SELECT kind FROM events WHERE investigation_id = ?", (inv,))]
        self.assertEqual(kinds.count("budget_skip"), 2)
        self.assertEqual(kinds.count("budget_over"), 1)  # read passed its 200k target once


class RoleOutputCapTests(ResearchTestCase):
    async def test_the_roles_max_output_reaches_the_provider(self):
        p = FakeProvider("openai", lambda r: {"answer": "x"})
        ctx = llm.CallContext(db=self.db, settings=S.build("budget"), providers={"openai": p}, limiters=Limiters({}),
                              prices=PriceTable(), step="test")
        await llm.complete_json(ctx, "verify", Out, "", "a", prompt_version="t.v1")
        await llm.complete_json(ctx, "synthesis", Out, "", "b", prompt_version="t.v1")
        self.assertEqual([r.max_output_tokens for r in p.requests], [8000, 32000])
        self.assertEqual([r.effort for r in p.requests], ["medium", "high"])


if __name__ == "__main__":
    unittest.main()
