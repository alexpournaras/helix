"""1E.2 claim verification (PLAN B8.15): panel, code rules, escalation, adjudication, re-read. Synthetic text;
fake providers; no network."""

import re

from research_testkit import ResearchTestCase

from app.literature.models import Details, Section
from app.research import llm, settings as S, verify as V
from app.research.prices import PriceTable
from app.research.providers import FakeProvider
from app.research.ratelimit import Limiters
from app.research.runner import Worker
from app.research.schemas import CaseBrief, ClaimVote
from app.research.segment import paper_snapshot, user_segments
from app.research.steps import build_pipeline, intake

TEXT = "Synthetic case. I am a 40-year-old man with ringing in my left ear for a month."
BRIEF = {"chief_complaint": {"text": "Ringing in the left ear", "seg_ids": ["U2"]},
         "case_features": [{"key": "left_tinnitus", "text": "Ringing in the left ear", "seg_ids": ["U2"]}]}
RESULTS = ("Noise exposure raised the odds of tinnitus (OR 1.50, 95% CI 1.10 to 2.05) in 1,234 adults. "
           "Ear wax removal did not change tinnitus loudness (p = 0.40). "
           "Hearing aids reduced tinnitus distress in older adults.")
SNAP = paper_snapshot("Synthetic abstract.", Details(full_text=[Section(title="Results", text=RESULTS)],
                                                     full_text_from="pmc"))
IDS = [s.seg_id for s in SNAP.segments if s.section == "Results"]
CLAIMS = {"noise": ("Noise exposure raised the odds of tinnitus", IDS[0]),
          "wax": ("Ear wax removal reduced tinnitus loudness", IDS[1]),
          "aids": ("Hearing aids reduced tinnitus distress in all adults", IDS[2])}


def vote(pair, verdict="supported", **kw):
    base = {"pair": pair, "asserts_effect": True, "direction_check": "same", "significance": "significant",
            "population_check": "same", "overgeneralization": False, "context_needed": False, "verdict": verdict,
            "reason": "r"}
    base.update(kw)
    return base


class Models:
    """`self.judge(claim key, stage, family)` → vote fields (without `pair`)."""

    def __init__(self):
        self.judge = self.default
        self.calls = []
        self.strict = lambda: []

    @staticmethod
    def default(key, stage, family):
        if key == "wax":  # an effect where the text says it isn't significant
            return {"significance": "not_significant"}
        if key == "aids":  # older adults studied, "all adults" claimed
            return {"population_check": "broader"}
        return {}

    def handler(self, family):
        def h(request):
            if request.schema_name == "ClaimSet":
                self.calls.append(("ClaimSet", family, "strict" in request.system))
                return {"claims": self.strict()}
            if request.schema_name != "ClaimVoteBatch":
                raise AssertionError(request.schema_name)
            stage = "adjudicate" if "Section:" in request.user else "escalate" if "Context:" in request.user else "verify"
            self.calls.append((stage, family))
            out = []
            for pair, text in re.findall(r"^(C\d+): (.*)$", request.user, re.M):
                key = next(k for k, (t, _) in CLAIMS.items() if text.startswith(t)) if any(
                    text.startswith(t) for t, _ in CLAIMS.values()) else "new"
                out.append(vote(pair, **self.judge(key, stage, family)))
            return {"items": out}
        return h


class VerifyBase(ResearchTestCase):
    def setUp(self):
        super().setUp()
        self.models = Models()

    def ctx(self):
        self.a = FakeProvider("openai", self.models.handler("openai"))
        self.b = FakeProvider("anthropic", self.models.handler("anthropic"))
        return llm.CallContext(db=self.db, settings=S.build("quality"),
                               providers={"openai": self.a, "anthropic": self.b},
                               limiters=Limiters({}), prices=PriceTable())

    def seed(self, keys=("noise", "wax", "aids")):
        snap = S.build("quality").model_dump(mode="json")
        inv_id = self.db.create_investigation(TEXT, profile="quality", settings_snapshot=snap, synthetic=True)
        with self.db.tx() as conn:
            intake.save_segments(conn, inv_id, user_segments(TEXT))
            intake.save_brief(self.db, conn, inv_id, CaseBrief.model_validate(BRIEF), source="intake:p1", check=None,
                              prompt_version="test")
            self.db.insert(conn, "hypotheses", {"investigation_id": inv_id, "key": "noise", "title": "noise",
                                                "kind": "common", "prior": "moderate"})
            cid = self.db.insert(conn, "candidates", {"investigation_id": inv_id, "round_found": 1, "source": "pubmed",
                                                      "source_id": "1", "pmid": "1", "status": "read"})
            self.db.insert(conn, "screenings", {"candidate_id": cid, "design": "cohort"})
            self.read_id = self.db.insert(conn, "paper_reads", {
                "investigation_id": inv_id, "candidate_id": cid, "literature_id": 9, "round": 1, "scope": "full",
                "segments": SNAP.to_dict(), "profile": {"population": {"value": "Adults", "sentence_ids": []}},
                "status": "read"})
            self.claim_ids = {}
            for k in keys:
                text, sid = CLAIMS[k]
                self.claim_ids[k] = self.db.insert(conn, "evidence_claims", {
                    "investigation_id": inv_id, "read_id": self.read_id, "literature_id": 9, "pass": "pack",
                    "extractor_model": "extractor-x", "agreement": 2, "claim": text, "sentence_ids": [sid],
                    "quote": SNAP.get(sid).text, "evidence_type": "finding", "status": "pending",
                    "checks": {"extractor": "m0", "extractor_family": "openai"}, "round": 1})
            self.db.update_investigation(inv_id, conn=conn, stage="verifying", knowledge_version=1)
        return inv_id

    async def run_inv(self, inv_id):
        ctx = self.ctx()
        pipeline, _ = build_pipeline(ctx, stop_at="mapping")
        self.worker = Worker(self.db, pipeline, services=ctx, max_parallel=2)
        await self.worker.machine.advance(inv_id)
        await self.worker.run_until_idle()
        return self.db.investigation(inv_id)

    def claim(self, key):
        return self.db.one("SELECT * FROM evidence_claims WHERE id = ?", (self.claim_ids[key],))

    def votes(self, key):
        return self.db.query("SELECT * FROM claim_votes WHERE claim_id = ? ORDER BY id", (self.claim_ids[key],))


class RuleTests(VerifyBase):
    def test_claim_rules(self):
        def v(**kw):
            return ClaimVote.model_validate(vote("C1", **kw))
        self.assertEqual(V.claim_rule(v(direction_check="opposite")), "unsupported")
        self.assertEqual(V.claim_rule(v(significance="not_significant")), "unsupported")
        self.assertEqual(V.claim_rule(v(significance="not_significant", asserts_effect=False)), "supported")
        self.assertEqual(V.claim_rule(v(population_check="different")), "unsupported")
        self.assertEqual(V.claim_rule(v(population_check="broader")), "partial")
        self.assertEqual(V.claim_rule(v(overgeneralization=True)), "partial")
        self.assertEqual(V.combine([v(), v()], V.claim_rule), "supported")
        self.assertIsNone(V.combine([v(), v(verdict="partial")], V.claim_rule))


class VerifyFlowTests(VerifyBase):
    async def test_rules_decide_and_votes_are_stored(self):
        inv_id = self.seed()
        await self.run_inv(inv_id)
        noise, wax, aids = self.claim("noise"), self.claim("wax"), self.claim("aids")
        self.assertEqual((noise["status"], noise["verification"], noise["escalated"]), ("active", "supported", 0))
        self.assertEqual((wax["status"], wax["verification"]), ("rejected", None))  # non-significant "effect"
        self.assertEqual((aids["status"], aids["verification"]), ("active", "partial"))  # broader population
        # Both supported / both unsupported settle at the panel; two "partial" votes are "anything else" (B8.15):
        # escalated, then adjudicated (the adjudicator also says partial).
        for key, c in (("noise", noise), ("wax", wax)):
            vs = self.votes(key)
            self.assertEqual([v["stage"] for v in vs], ["verify", "verify"])
            self.assertEqual({v["verdict"] for v in vs}, {c["verification"] or "unsupported"})  # match the votes
        self.assertEqual((aids["escalated"], aids["adjudicated"]), (1, 1))
        self.assertEqual([v["stage"] for v in self.votes("aids")], ["verify", "verify", "escalate", "escalate",
                                                                     "adjudicate"])
        self.assertEqual(self.votes("aids")[-1]["verdict"], aids["verification"])  # the adjudicator's is final
        note = self.db.one("SELECT * FROM note_outbox WHERE investigation_id = ? AND kind = 'paper_card'", (inv_id,))
        self.assertEqual(note["artifact_key"], f"paper:{self.read_id}")
        ev = self.db.one("SELECT data FROM events WHERE investigation_id = ? AND kind = 'verify'", (inv_id,))
        self.assertEqual((ev["data"]["active"], ev["data"]["rejected"]), (2, 1))

    async def test_opposite_direction_is_rejected(self):
        self.models.judge = lambda key, stage, family: {"direction_check": "opposite"}
        inv_id = self.seed(keys=("noise",))
        await self.run_inv(inv_id)
        self.assertEqual(self.claim("noise")["status"], "rejected")

    async def test_disagreement_escalates_then_adjudicates(self):
        def judge(key, stage, family):
            if stage == "adjudicate":
                return {"verdict": "partial"}
            return {} if family == "openai" else {"verdict": "unsupported"}  # the panel never agrees
        self.models.judge = judge
        inv_id = self.seed(keys=("noise",))
        await self.run_inv(inv_id)
        c = self.claim("noise")
        self.assertEqual((c["status"], c["verification"], c["escalated"], c["adjudicated"]), ("active", "partial", 1, 1))
        stages = [v["stage"] for v in self.votes("noise")]
        self.assertEqual(stages.count("verify"), 2)
        self.assertEqual(stages.count("escalate"), 2)
        self.assertEqual(stages.count("adjudicate"), 1)
        adj = [x for x in self.models.calls if x[0] == "adjudicate"]
        self.assertEqual(adj, [("adjudicate", "anthropic")])  # the family that didn't extract

    async def test_escalation_settles_without_adjudication(self):
        def judge(key, stage, family):
            if stage == "verify":
                return {"context_needed": True}
            return {}
        self.models.judge = judge
        inv_id = self.seed(keys=("noise",))
        await self.run_inv(inv_id)
        c = self.claim("noise")
        self.assertEqual((c["verification"], c["escalated"], c["adjudicated"]), ("supported", 1, 0))
        self.assertNotIn("adjudicate", {x[0] for x in self.models.calls})


class RereadTests(VerifyBase):
    def strict_claims(self):
        def claim(text, sid):
            return {"claim": text, "sentence_ids": [sid], "evidence_type": "finding", "population": "adults",
                    "design": "cohort", "n": None, "effect": None, "limitations": "",
                    "links": [{"hypothesis_key": "noise", "direction": "supports", "strength": "moderate",
                               "case_match": "high", "reason": "r"}]}
        return [claim("Noise exposure was associated with tinnitus in 1,234 adults", IDS[0]),
                claim("Ear wax removal did not change tinnitus loudness", IDS[1]),
                claim("Hearing aids reduced tinnitus distress in older adults", IDS[2])]

    async def test_many_rejected_triggers_a_strict_reread_that_keeps_the_better_set(self):
        def judge(key, stage, family):
            return {"verdict": "unsupported"} if key != "new" else {}  # old claims fail, strict ones pass
        self.models.judge = judge
        self.models.strict = self.strict_claims
        inv_id = self.seed()
        await self.run_inv(inv_id)
        self.assertTrue(any(c[0] == "ClaimSet" and c[2] for c in self.models.calls))  # the strict prompt
        r = self.db.one("SELECT * FROM paper_reads WHERE id = ?", (self.read_id,))
        self.assertEqual(r["reread"], 1)
        self.assertIn("reread_kept_new", r["quality_flags"])
        self.assertNotIn("reread_needed", r["quality_flags"])
        new = self.db.query("SELECT * FROM evidence_claims WHERE read_id = ? AND pass LIKE '%:reread' AND "
                            "status = 'active'", (self.read_id,))
        self.assertEqual(len(new), 3)
        self.assertTrue(all(c["agreement"] == 2 for c in new))  # both strict extractors agreed
        old = {self.claim(k)["status"] for k in ("noise", "wax", "aids")}
        self.assertEqual(old, {"rejected"})
        self.assertIn("mapping", self.db.one("SELECT data FROM events WHERE investigation_id = ? AND kind = 'complete'",
                                             (inv_id,))["data"]["reason"])

    async def test_a_worse_reread_keeps_the_old_set(self):
        def judge(key, stage, family):
            if key == "noise":
                return {}
            return {"verdict": "unsupported"}  # 2 of 3 old rejected (> 40%); the strict set is all rejected
        self.models.judge = judge
        self.models.strict = self.strict_claims
        inv_id = self.seed()
        await self.run_inv(inv_id)
        r = self.db.one("SELECT * FROM paper_reads WHERE id = ?", (self.read_id,))
        self.assertIn("reread_kept_old", r["quality_flags"])
        self.assertEqual(self.claim("noise")["status"], "active")
        new_active = self.db.one("SELECT COUNT(*) AS n FROM evidence_claims WHERE read_id = ? AND pass LIKE "
                                 "'%:reread' AND status = 'active'", (self.read_id,))["n"]
        self.assertEqual(new_active, 0)

    async def test_few_rejected_needs_no_reread(self):
        inv_id = self.seed()  # 1 of 3 rejected
        await self.run_inv(inv_id)
        self.assertFalse(any(c[0] == "ClaimSet" for c in self.models.calls))
        self.assertEqual(self.db.one("SELECT reread FROM paper_reads WHERE id = ?", (self.read_id,))["reread"], 0)
