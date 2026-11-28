"""Single-verifier mode: one verifier, a second opinion on triggers, a seeded audit (PLAN B17.5 item 5; 1R.4)."""

import re

from research_testkit import ResearchTestCase

from app.research import llm, settings as S, verify as V
from app.research.prices import PriceTable
from app.research.providers import FakeProvider
from app.research.ratelimit import Limiters
from app.research.schemas import ClaimVoteBatch
from app.research.steps import verification as VS


def vote(pair, verdict, context=False):
    return {"pair": pair, "asserts_effect": False, "direction_check": "not_applicable", "significance": "not_applicable",
            "population_check": "same", "overgeneralization": False, "context_needed": context, "verdict": verdict,
            "reason": "r"}


class SingleVerifierTests(ResearchTestCase):
    def run_verify(self, first, second=None, adjudicate=None, triggers=None, audit_rate=0.0, n=4):
        """first/second/adjudicate: pair id → verdict (or (verdict, context_needed))."""
        calls = {"verify": [], "second": [], "adjudicate": []}

        def handler(request):
            ids = re.findall(r"^(C\d+): ", request.user, re.M)
            if request.effort == "high" and "Section:" in request.user:
                table, kind = adjudicate or {}, "adjudicate"
            elif request.effort == "high":
                table, kind = second or {}, "second"
            else:
                table, kind = first, "verify"
            calls[kind].append(ids)
            out = []
            for i in ids:
                v = table.get(i, "supported")
                v, ctx = v if isinstance(v, tuple) else (v, False)
                out.append(vote(i, v, ctx))
            return {"items": out}

        st = S.build("budget", {"verification": {"audit_rate": audit_rate}})
        ctx = llm.CallContext(db=self.db, settings=st, providers={"openai": FakeProvider("openai", handler)},
                              limiters=Limiters({}), prices=PriceTable())
        pairs = [V.Pair(f"C{k}", f"claim {k}", f"quote {k}", f"context {k}", "gpt-5.4-mini", "openai",
                     triggers=(triggers or {}).get(f"C{k}", []), seed=f"claim {k}") for k in range(1, n + 1)]

        def render(ps, with_context):
            return "\n".join(f"{p.pair_id}: {p.text}" + ("\n  Context: x" if with_context else "") for p in ps)

        def render_adj(ps):
            return "\n".join(f"{p.pair_id}: {p.text}\n  Section: s" for p in ps)
        import asyncio
        asyncio.run(V.verify(ctx, "verify", pairs, system="s", prompt_version="t.v1", render=render,
                             schema=ClaimVoteBatch, rule=V.claim_rule, render_adjudicate=render_adj))
        return {p.pair_id: p for p in pairs}, calls

    def test_clean_claims_get_one_vote(self):
        pairs, calls = self.run_verify({})
        self.assertEqual(len(calls["verify"]), 1)  # four claims, one batch, one model
        self.assertEqual(calls["second"], [])
        for p in pairs.values():
            self.assertEqual((p.verdict, V.verification_mode(p.votes)), ("supported", "single"))

    def test_unsure_and_triggered_claims_get_a_second_opinion(self):
        pairs, calls = self.run_verify({"C1": "partial", "C2": ("supported", True)},
                                       triggers={"C3": ["code_flag"]})
        self.assertEqual(sorted(calls["second"][0]), ["C1", "C2", "C3"])
        self.assertEqual(pairs["C4"].votes[-1]["stage"], "verify")  # untouched
        self.assertEqual(V.verification_mode(pairs["C3"].votes), "second")

    def test_agreement_keeps_disagreement_adjudicates(self):
        pairs, calls = self.run_verify({"C1": "partial", "C2": "partial"}, second={"C1": "partial", "C2": "supported"},
                                       adjudicate={"C2": "unsupported"})
        self.assertEqual(pairs["C1"].verdict, "partial")
        self.assertEqual(calls["adjudicate"], [["C2"]])
        self.assertEqual((pairs["C2"].verdict, V.verification_mode(pairs["C2"].votes)), ("unsupported", "adjudicated"))

    def test_a_trigger_not_in_the_settings_is_ignored(self):
        st = S.build("budget")
        self.assertNotIn("vibes", st.verification["second_opinion"])
        pairs, calls = self.run_verify({}, triggers={"C1": ["vibes"]})
        self.assertEqual(calls["second"], [])

    def test_audit_is_seeded_and_counted(self):
        everyone, calls = self.run_verify({}, audit_rate=1.0)
        self.assertEqual(sorted(calls["second"][0]), ["C1", "C2", "C3", "C4"])
        self.assertTrue(all(V.verification_mode(p.votes) == "audit" for p in everyone.values()))
        picks = [V.audit_pick(f"None:claim {k}:quote {k}", 0.3) for k in range(1, 200)]
        self.assertEqual(picks, [V.audit_pick(f"None:claim {k}:quote {k}", 0.3) for k in range(1, 200)])  # stable
        self.assertTrue(30 < sum(picks) < 90)  # about 30%

    def test_claim_triggers(self):
        self.assertEqual(VS.claim_triggers({"claim": "Noise exposure causes tinnitus", "checks": {"effect_cleared": True},
                                            "strong_link": True, "red_flag_link": True,
                                            "evidence_type": "guideline_recommendation"}),
                         ["code_flag", "strong_link", "causal", "red_flag", "recommendation"])
        self.assertEqual(VS.claim_triggers({"claim": "Tinnitus was common", "checks": {}}), [])


class WriteVotesTests(ResearchTestCase):
    def test_second_and_audit_votes_are_stored_with_the_mode(self):
        inv = self.db.create_investigation("Synthetic.", profile="budget", settings_snapshot={}, synthetic=True)
        with self.db.tx() as conn:
            rid = self.db.insert(conn, "paper_reads", {"investigation_id": inv, "round": 1, "status": "read"})
            ids = [self.db.insert(conn, "evidence_claims", {"investigation_id": inv, "read_id": rid, "claim": f"c{n}",
                                                            "status": "pending", "round": 1, "checks": {"x": 1}})
                   for n in range(2)]
            for cid, stage in zip(ids, ("second", "audit")):
                p = V.Pair("C1", "t", "q", "c", "m", "openai", verdict="partial",
                           votes=[{"stage": "verify", "model": "m", "rule_verdict": "partial"},
                                  {"stage": stage, "model": "m", "rule_verdict": "partial"}])
                VS.write_votes(conn, cid, p)
        rows = {r["id"]: r for r in self.db.query("SELECT * FROM evidence_claims")}
        self.assertEqual([rows[i]["checks"]["verification_mode"] for i in ids], ["second", "audit"])
        self.assertEqual(rows[ids[0]]["checks"]["x"], 1)  # existing checks kept
        self.assertEqual(sorted(r["stage"] for r in self.db.query("SELECT stage FROM claim_votes")),
                         ["audit", "second", "verify", "verify"])
