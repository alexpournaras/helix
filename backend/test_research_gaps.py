"""1E.6 gaps (PLAN B8.22): computed gaps, panel merge, novelty, decision (M1 reports only). Synthetic text; fake
providers; no network."""

import json

from research_testkit import ResearchTestCase

from app.research import llm, settings as S
from app.research.prices import PriceTable
from app.research.providers import FakeProvider
from app.research.ratelimit import Limiters
from app.research.runner import Worker
from app.research.schemas import CaseBrief, GapReport
from app.research.segment import user_segments
from app.research.steps import build_pipeline, gaps as G, intake

TEXT = "Synthetic case. I am a 40-year-old man with ringing in my left ear."
BRIEF = {"chief_complaint": {"text": "Ringing in the left ear", "seg_ids": ["U2"]},
         "case_features": [{"key": "left_tinnitus", "text": "Ringing in the left ear", "seg_ids": ["U2"]},
                           {"key": "one_ear", "text": "Only the left ear", "seg_ids": ["U2"]}]}


def report(**kw):
    base = {"contradictions": [], "new_concepts": [], "missing_information": [], "new_hypotheses": [],
            "follow_up_queries": []}
    base.update(kw)
    return GapReport.model_validate(base)


def hyp(key, title, claims=("C1",)):
    return {"key": key, "title": title, "description": "d", "claim_ids": list(claims)}


def query(hkey, q, source="pubmed"):
    return {"hypothesis_key": hkey, "source": source, "query": q, "query_type": "other", "rationale": "r"}


class MergeTests(ResearchTestCase):
    def merge(self, reports, max_new=4, earlier=frozenset()):
        return G.merge(reports, refs={"C1": 11, "C2": 12}, hyp_keys={"noise"},
                       covered=["Noise damage", "Acoustic neuroma"], allowed_sources=["europepmc", "pubmed"],
                       earlier_queries=set(earlier), max_new=max_new)

    def test_ids_concepts_and_hypotheses(self):
        a = report(contradictions=[{"claim_ids": ["C1", "C2"], "note": "different ages"},
                                   {"claim_ids": ["C1", "C9"], "note": "bad"}],
                   new_concepts=[{"term": "acoustic neuroma", "reason": "covered"},
                                 {"term": "jaw clenching", "reason": "new", "claim_ids": ["C2"]}],
                   new_hypotheses=[hyp("tmj", "Jaw joint disorder"), hyp("drug", "Medicine side effect", ()),
                                   hyp("noise", "Noise damage again")],
                   missing_information=[{"text": "Whether he takes aspirin", "reason": "ototoxic"}])
        b = report(contradictions=[{"claim_ids": ["C2", "C1"], "note": "same pair"}],
                   new_hypotheses=[hyp("jaw_joint", "Jaw joint disorder (TMD)", ("C2",)), hyp("wax", "Ear wax")],
                   missing_information=[{"text": "Whether he takes aspirin daily", "reason": "dup"}])
        got = self.merge([a, b], max_new=1)
        self.assertEqual([c["claim_ids"] for c in got["contradictions"]], [[11, 12]])  # bad IDs, repeats dropped
        self.assertEqual([c["term"] for c in got["new_concepts"]], ["jaw clenching"])
        self.assertEqual(len(got["missing_information"]), 1)
        # Both members proposed the jaw-joint hypothesis: it wins the one place; its claims are pooled.
        self.assertEqual([(h["key"], h["proposed_by"], h["claim_ids"]) for h in got["new_hypotheses"]],
                         [("tmj", 2, [11, 12])])
        d = got["dropped"]
        self.assertEqual((d["bad_ids"], d["hypothesis_without_claim"], d["hypothesis_over_limit"]), (1, 1, 1))
        self.assertEqual(d["concept_covered"], 2)  # acoustic neuroma; the noise hypothesis again

    def test_queries_go_through_the_query_checks(self):
        a = report(new_hypotheses=[hyp("tmj", "Jaw joint disorder")],
                   follow_up_queries=[query("noise", '"tinnitus"[tiab] AND "noise"[mh]'),
                                      query("tmj", 'TITLE_ABS:"jaw" AND TITLE_ABS:"tinnitus"', "europepmc"),
                                      query("noise", 'MESH_HEADING:"x"', "europepmc"),
                                      query("noise", '"x"[tiab]', "openalex"),
                                      query("ghost", '"y"[tiab]'),
                                      query("noise", '"old"[tiab]')])
        got = self.merge([a], earlier={("pubmed", '"old"[tiab]')})
        self.assertEqual([q["hypothesis_key"] for q in got["follow_up_queries"]], ["noise", "tmj"])
        d = got["dropped"]
        self.assertEqual((d["query_checks"], d["query_unknown_hypothesis"], d["query_repeat"]), (2, 1, 1))

    def test_decision(self):
        self.assertFalse(G.decide(rnd=1, first=1, max_rounds=1, acquisition_stopped=False, novelty=5, min_novelty=1,
                                  new_work=3))  # M1: one round
        self.assertTrue(G.decide(rnd=1, first=1, max_rounds=5, acquisition_stopped=False, novelty=1, min_novelty=1,
                                 new_work=1))
        self.assertFalse(G.decide(rnd=1, first=1, max_rounds=5, acquisition_stopped=True, novelty=1, min_novelty=1,
                                  new_work=1))
        self.assertFalse(G.decide(rnd=1, first=1, max_rounds=5, acquisition_stopped=False, novelty=0, min_novelty=1,
                                  new_work=1))
        self.assertFalse(G.decide(rnd=1, first=1, max_rounds=5, acquisition_stopped=False, novelty=2, min_novelty=1,
                                  new_work=0))


class GapsFlowTests(ResearchTestCase):
    def setUp(self):
        super().setUp()
        self.calls = []

    def handler(self, family):
        def h(request):
            self.calls.append((request.schema_name, family, request.user))
            assert request.schema_name == "GapReport", request.schema_name
            return report(new_hypotheses=[hyp("tmj", "Jaw joint disorder")],
                          follow_up_queries=[query("tmj", '"jaw"[tiab] AND "tinnitus"[tiab]')]).model_dump(mode="json")
        return h

    def seed(self):
        inv_id = self.db.create_investigation(TEXT, profile="quality",
                                              settings_snapshot=S.build("quality").model_dump(mode="json"),
                                              synthetic=True)
        with self.db.tx() as conn:
            intake.save_segments(conn, inv_id, user_segments(TEXT))
            intake.save_brief(self.db, conn, inv_id, CaseBrief.model_validate(BRIEF), source="intake:p1", check=None,
                              prompt_version="test")
            self.h = {k: self.db.insert(conn, "hypotheses", {"investigation_id": inv_id, "key": k, "title": t,
                                                             "kind": "common", "prior": "moderate"})
                      for k, t in (("noise", "Noise damage"), ("wax", "Ear wax"))}
            claims = []
            for n, (direction, strength) in enumerate((("supports", "weak"), ("contradicts", "moderate")), 1):
                cid = self.db.insert(conn, "candidates", {"investigation_id": inv_id, "round_found": 1,
                                                          "source": "pubmed", "source_id": str(n), "status": "read"})
                rid = self.db.insert(conn, "paper_reads", {"investigation_id": inv_id, "candidate_id": cid, "round": 1,
                                                           "scope": "full", "status": "read"})
                claim = self.db.insert(conn, "evidence_claims", {
                    "investigation_id": inv_id, "read_id": rid, "claim": f"Claim {n} about noise",
                    "sentence_ids": ["S1.P1.s1"], "evidence_type": "finding", "status": "active",
                    "verification": "supported", "agreement": 2, "round": 1})
                conn.execute("INSERT INTO claim_links (claim_id, hypothesis_id, origin, direction, strength, "
                             "case_match) VALUES (?, ?, 'extract', ?, ?, 'high')", (claim, self.h["noise"], direction,
                                                                                    strength))
                claims.append(claim)
            self.claims = claims
            self.db.insert(conn, "research_queries", {"investigation_id": inv_id, "round": 1, "source": "pubmed",
                                                      "query": '"x"[tiab]', "status": "dropped"})
            conn.execute("INSERT INTO plan_uncovered (investigation_id, feature_key, reason, round) VALUES "
                         "(?, 'one_ear', 'no hypothesis', 1)", (inv_id,))
            self.db.insert(conn, "grounding_runs", {"investigation_id": inv_id, "complaint_key": "tinnitus",
                                                    "population": "general", "status": "gap", "knowledge_version": 1})
            for k in ("noise", "wax"):
                conn.execute("INSERT INTO feature_fit (investigation_id, round, feature_key, hypothesis_id, fit) VALUES "
                             "(?, 1, 'one_ear', ?, 'unknown')", (inv_id, self.h[k]))
                conn.execute("INSERT INTO feature_fit (investigation_id, round, feature_key, hypothesis_id, fit) VALUES "
                             "(?, 1, 'left_tinnitus', ?, ?)", (inv_id, self.h[k], "explains" if k == "noise" else
                                                               "unknown"))
            self.db.update_investigation(inv_id, conn=conn, stage="gaps", knowledge_version=1)
        return inv_id

    async def test_gaps_round_is_reported_and_m1_stops(self):
        inv_id = self.seed()
        ctx = llm.CallContext(db=self.db, settings=S.build("quality"),
                              providers={"openai": FakeProvider("openai", self.handler("openai")),
                                         "anthropic": FakeProvider("anthropic", self.handler("anthropic"))},
                              limiters=Limiters({}), prices=PriceTable())
        pipeline, _ = build_pipeline(ctx)
        worker = Worker(self.db, pipeline, services=ctx, max_parallel=2)
        await worker.machine.advance(inv_id)
        await worker.run_until_idle()
        self.assertEqual(len(self.calls), 2)  # the panel: A and B
        self.assertIn("Claim 1 about noise", self.calls[0][2])
        g = self.db.one("SELECT * FROM gap_reports WHERE investigation_id = ?", (inv_id,))
        comp = g["computed"]
        self.assertEqual(comp["weak_only"], [self.h["noise"]])
        self.assertEqual(comp["features_unknown_everywhere"], ["one_ear"])
        self.assertEqual(len(comp["abandoned_queries"]), 1)
        self.assertEqual(comp["uncovered_features"], ["one_ear"])
        self.assertEqual(comp["grounding_gaps"], ["tinnitus"])
        self.assertEqual(comp["contradiction_pairs"], [{"hypothesis_id": self.h["noise"], "supports": self.claims[0],
                                                        "contradicts": self.claims[1]}])
        self.assertEqual(set(comp["unmet_quotas"]), set(self.h.values()))
        merged = g["panel"]["merged"]
        self.assertEqual([h["proposed_by"] for h in merged["new_hypotheses"]], [2])
        self.assertEqual(len(merged["follow_up_queries"]), 1)  # the second member's copy is a repeat
        self.assertEqual(g["decision"], "stop")
        self.assertIn("M1 reports gaps only", g["reason"])
        self.assertEqual(g["novelty"]["hypotheses"], 1)
        inv = self.db.investigation(inv_id)
        reason = self.db.one("SELECT data FROM events WHERE investigation_id = ? AND kind = 'complete'",
                             (inv_id,))["data"]["reason"]
        self.assertIn("1F", reason)  # synthesis is next
        ev = self.db.one("SELECT data FROM events WHERE investigation_id = ? AND kind = 'gaps'", (inv_id,))
        self.assertNotIn("Jaw", json.dumps(ev["data"]))  # counts only
