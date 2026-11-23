import unittest

from pydantic import BaseModel

from research_testkit import ResearchTestCase

from app.research import ensemble as E, llm, settings as S
from app.research.prices import PriceTable
from app.research.providers import FakeProvider, ModelError
from app.research.ratelimit import Limiters


class Vote(BaseModel):
    verdict: str


class EnsembleTests(ResearchTestCase):
    def ctx(self, a=None, b=None, a_key=True, b_key=True):
        self.a = FakeProvider("openai", a, has_key=a_key)
        self.b = FakeProvider("anthropic", b, has_key=b_key)
        return llm.CallContext(db=self.db, settings=S.build("quality"), providers={"openai": self.a, "anthropic": self.b},
                               limiters=Limiters({}), prices=PriceTable(), step="t")

    async def test_panel_runs_both_families_in_parallel(self):
        ctx = self.ctx(lambda r: {"verdict": "supported"}, lambda r: {"verdict": "partial"})
        result = await E.panel(ctx, "verify", Vote, "s", "u", prompt_version="v")
        self.assertEqual(result.independence, "full")
        self.assertEqual(sorted(v.verdict for v in result.values), ["partial", "supported"])
        votes = result.votes()
        self.assertEqual({v["family"] for v in votes}, {"openai", "anthropic"})
        self.assertTrue(all(v["ok"] for v in votes))

    async def test_member_failure_continues_reduced(self):
        ctx = self.ctx(lambda r: (_ for _ in ()).throw(ModelError("config", "bad")), lambda r: {"verdict": "supported"})
        result = await E.panel(ctx, "verify", Vote, "s", "u", prompt_version="v")
        self.assertEqual(result.independence, "reduced")
        self.assertEqual(len(result.values), 1)
        self.assertFalse(result.votes()[1]["ok"])

    async def test_all_members_fail_raises(self):
        boom = lambda r: (_ for _ in ()).throw(ModelError("config", "bad"))
        ctx = self.ctx(boom, boom)
        with self.assertRaises(ModelError):
            await E.panel(ctx, "verify", Vote, "s", "u", prompt_version="v")

    async def test_single_provider_same_family(self):
        ctx = self.ctx(lambda r: {"verdict": r.model}, b_key=False)
        result = await E.dual(ctx, "screen", Vote, "s", "u", prompt_version="v")
        self.assertEqual(result.independence, "same_family")
        self.assertEqual(sorted(v.verdict for v in result.values), ["gpt-6-luna", "gpt-6.2-sol"])

    async def test_avoid_extractor_model(self):
        ctx = self.ctx(lambda r: {"verdict": r.model}, lambda r: {"verdict": r.model})
        result = await E.panel(ctx, "verify", Vote, "s", "u", prompt_version="v", avoid_models=["gpt-6.2-sol"])
        self.assertNotIn("gpt-6.2-sol", [m.model.model for m in result.members])
        self.assertTrue(any("avoid extractor" in s for s in result.substitutions))

    async def test_per_member_inputs(self):
        ctx = self.ctx(lambda r: {"verdict": r.user}, lambda r: {"verdict": r.user})
        result = await E.panel(ctx, "verify", Vote, "s", ["for-b", "for-a"], prompt_version="v")
        by_family = {m.model.family: m.value.verdict for m in result.members}
        self.assertEqual(by_family, {"anthropic": "for-b", "openai": "for-a"})

    async def test_adjudicate_uses_strongest_other_family(self):
        ctx = self.ctx(lambda r: {"verdict": r.model}, lambda r: {"verdict": r.model})
        member = await E.adjudicate(ctx, Vote, "s", "u", extractor_family="openai", prompt_version="v")
        self.assertEqual((member.value.verdict, member.model.effort), ("claude-fable-5-2", "xhigh"))
        member = await E.adjudicate(ctx, Vote, "s", "u", extractor_family="anthropic", prompt_version="v")
        self.assertEqual(member.value.verdict, "gpt-6.2-astra")

    async def test_claim_vote_recording(self):
        inv = self.db.create_investigation("x", profile="quality", settings_snapshot={}, synthetic=True)
        with self.db.tx() as conn:
            read = self.db.insert(conn, "paper_reads", {"investigation_id": inv, "round": 1})
            claim = self.db.insert(conn, "evidence_claims", {"investigation_id": inv, "read_id": read, "claim": "c",
                                                             "round": 1})
            E.record_claim_votes(self.db, conn, claim, "verify", [
                {"model": "a", "verdict": "supported", "checks": {"direction_check": "same"}},
                {"model": "b", "verdict": "partial", "reason": "broader"}])
        votes = self.db.query("SELECT * FROM claim_votes ORDER BY id")
        self.assertEqual([(v["model"], v["verdict"]) for v in votes], [("a", "supported"), ("b", "partial")])
        self.assertEqual(votes[0]["checks"], {"direction_check": "same"})


class PolicyTests(unittest.TestCase):
    def test_most_cautious(self):
        self.assertEqual(E.most_cautious(["urgent", "none", "routine_review"], E.SAFETY_ORDER), "urgent")
        self.assertEqual(E.most_cautious(["low", "high"], E.RISK_ORDER), "high")
        self.assertEqual(E.most_cautious(["supported", "partial"], E.VERDICT_CAUTION), "partial")
        self.assertEqual(E.most_cautious(["low", "weird"], E.RISK_ORDER), "weird")
        with self.assertRaises(ValueError):
            E.most_cautious([], E.RISK_ORDER)

    def test_include_and_union(self):
        self.assertEqual(E.include_if_any(["exclude", "include"]), "include")
        self.assertEqual(E.include_if_any(["exclude", "exclude"]), "exclude")
        self.assertEqual(E.union([["a", "b"], ["b", "c"]]), ["a", "b", "c"])


if __name__ == "__main__":
    unittest.main()
