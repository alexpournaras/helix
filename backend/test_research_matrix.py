"""1E.4 evidence matrix (PLAN B8.19): aggregation, direction, certainty, case fit. Pure code; synthetic data."""

from research_testkit import ResearchTestCase

from app.research import matrix as M, settings as S, weights as W
from app.research.prices import PriceTable
from app.research.providers import FakeProvider
from app.research.ratelimit import Limiters
from app.research import llm
from app.research.runner import Worker
from app.research.steps import build_pipeline


def data(reads, studies=None, kinds=None, included=None):
    """reads: {read id: (design, risk)}; studies: {read id: study id}; kinds: {study id: kind}."""
    studies = studies or {r: r for r in reads}
    kinds = kinds or {}
    kind = {}
    for rid, sid in studies.items():
        design = reads[rid][0]
        kind[sid] = kinds.get(sid) or {"guideline": "guideline", "systematic_review": "review_sr"}.get(design, "primary")
    return M.Data(items={}, study_of=studies, kind=kind, design={r: v[0] for r, v in reads.items()},
                  risk={r: v[1] for r, v in reads.items()}, sr_of_rcts={r: False for r in reads},
                  included_in=included or {})


def item(read, direction, weight, n=300, directness=1.0, cid=None):
    item.n_ = getattr(item, "n_", 0) + 1
    return M.Item(cid or item.n_, read, direction, weight, directness, n)


class AggregationTests(ResearchTestCase):
    def test_paper_weight(self):
        self.assertEqual(M.paper_weight([1.0, 0.8, 0.8, 0.8]), 1.4)
        self.assertEqual(M.paper_weight([0.5] * 15), 0.75)  # capped at 1.5 × w1
        self.assertEqual(M.paper_weight([]), 0.0)

    def test_fifteen_claims_from_one_paper_dont_outweigh_a_strong_contradiction(self):
        d = data({1: ("rct", "low"), 2: ("rct", "low")})
        items = [item(1, "supports", 0.5) for _ in range(15)] + [item(2, "contradicts", 1.2)]
        r = M.rate(items, d)
        self.assertEqual((r["S"], r["C"]), (0.75, 1.2))
        self.assertEqual(r["direction"], "mixed")

    def test_same_cohort_counted_once(self):
        d = data({1: ("cohort", "low"), 2: ("cohort", "low")}, studies={1: 10, 2: 10})
        r = M.rate([item(1, "supports", 0.8), item(2, "supports", 0.7)], d)
        self.assertEqual(r["S"], 0.8)
        self.assertEqual(len(r["units"]["supports"]), 1)
        self.assertEqual(r["direction"], "insufficient")  # one unit, no review
        self.assertIsNone(r["certainty"])

    def test_review_overlap_at_half_weight(self):
        d = data({1: ("systematic_review", "low"), 2: ("cohort", "low")}, included={2: {1}})
        r = M.rate([item(1, "supports", 0.6), item(2, "supports", 0.8)], d)
        units = {u["study"]: u for u in r["units"]["supports"]}
        self.assertEqual((units[2]["weight"], units[2]["independent"]), (0.4, False))
        self.assertEqual(r["S"], 1.0)
        self.assertEqual(r["direction"], "supported")  # the SR unit contributes ≥ 0.5
        # A review that has no evidence on this hypothesis doesn't halve the primary study.
        r2 = M.rate([item(2, "supports", 0.8)], d)
        self.assertEqual(r2["units"]["supports"][0]["weight"], 0.8)

    def test_supported_needs_two_independent_units_or_a_strong_review(self):
        d = data({1: ("rct", "low"), 2: ("rct", "low"), 3: ("guideline", "low")})
        self.assertEqual(M.rate([item(1, "supports", 1), item(2, "supports", 1)], d)["direction"], "supported")
        self.assertEqual(M.rate([item(1, "supports", 1), item(2, "contradicts", 0.6)], d)["direction"], "mixed")
        self.assertEqual(M.rate([item(1, "contradicts", 1), item(2, "contradicts", 1)], d)["direction"],
                         "contradicted")
        self.assertEqual(M.rate([item(3, "supports", 0.5)], d)["direction"], "supported")
        self.assertEqual(M.rate([item(3, "supports", 0.4)], d)["direction"], "mixed")  # a weak guideline unit


class CertaintyTests(ResearchTestCase):
    def setUp(self):
        super().setUp()
        self.d = data({1: ("rct", "low"), 2: ("rct", "low"), 3: ("cohort", "high"), 4: ("cohort", "unclear"),
                       5: ("case_series", "low"), 6: ("rct", "low")})

    def test_clean_rct_evidence_is_high(self):
        r = M.rate([item(1, "supports", 1), item(2, "supports", 1)], self.d)
        self.assertEqual((r["start"], r["certainty"], r["downgrades"]), ("high", "high", []))

    def test_each_downgrade(self):
        bias = M.rate([item(3, "supports", 1), item(4, "supports", 1), item(1, "supports", 0.5)], self.d)
        self.assertEqual(bias["start"], "low")  # driven by a cohort (the heaviest unit)
        self.assertIn("bias", bias["downgrades"])
        # With the default dominance (S ≥ 2C) and inconsistency (C > S/2) thresholds a supported direction can never
        # be inconsistent: at the boundary C = S/2 it is supported and not downgraded. Mixed always is (below).
        boundary = M.rate([item(1, "supports", 1), item(2, "supports", 1), item(6, "contradicts", 1.0)], self.d)
        self.assertEqual((boundary["direction"], boundary["downgrades"]), ("supported", []))
        tighter = M.rate([item(1, "supports", 1), item(2, "supports", 1), item(6, "contradicts", 0.5)], self.d,
                         {"certainty": {"inconsistency_ratio": 0.2}})
        self.assertEqual(tighter["downgrades"], ["inconsistency"])  # a lower ratio makes it reachable
        indirect = M.rate([item(1, "supports", 1, directness=0.4), item(2, "supports", 1, directness=0.7)], self.d)
        self.assertEqual(indirect["downgrades"], ["indirectness"])
        imprecise = M.rate([item(1, "supports", 1, n=50), item(2, "supports", 1, n=60)], self.d)
        self.assertEqual((imprecise["downgrades"], imprecise["certainty"]), (["imprecision"], "moderate"))
        unknown_n = M.rate([item(1, "supports", 1, n=None), item(2, "supports", 1, n=None)], self.d)
        self.assertIn("imprecision", unknown_n["downgrades"])
        mixed = M.rate([item(1, "supports", 1), item(6, "contradicts", 1)], self.d)
        self.assertEqual(mixed["direction"], "mixed")
        self.assertIn("inconsistency", mixed["downgrades"])

    def test_never_below_very_low_and_other_designs_start_very_low(self):
        r = M.rate([item(5, "supports", 1, n=5, directness=0.2), item(3, "supports", 0.3, n=5)], self.d)
        self.assertEqual(r["start"], "very_low")
        self.assertEqual(r["certainty"], "very_low")

    def test_case_fit(self):
        self.assertEqual(M.case_fit(["explains", "compatible", "unknown"], 3), "good")
        self.assertEqual(M.case_fit(["against", "against", "explains", "explains"], 4), "poor")
        self.assertEqual(M.case_fit(["against", "compatible"], 2), "poor")  # A > E
        self.assertEqual(M.case_fit(["unknown", "unknown", "compatible"], 3), "unknown")
        self.assertEqual(M.case_fit(["compatible", "compatible", "compatible"], 3), "partial")

    def test_agreement_weight(self):
        claim = {"design": "rct", "evidence_type": "finding", "n": 300, "verification": "supported"}
        read = {"appraisal": {"overall_risk": "low"}, "applicability": {"overall": "high"}, "scope": "full"}
        two, _ = W.claim_weight({**claim, "agreement": 2}, {"strength": "moderate", "case_match": "high"}, read)
        one, _ = W.claim_weight({**claim, "agreement": 1}, {"strength": "moderate", "case_match": "high"}, read)
        self.assertAlmostEqual(one, two * 0.9)


class MatrixStepTests(ResearchTestCase):
    """The matrix step over the database: stored ratings, evidence versions, identical recomputes."""

    def seed(self):
        inv_id = self.db.create_investigation("Synthetic case.", profile="quality",
                                              settings_snapshot=S.build("quality").model_dump(mode="json"),
                                              synthetic=True)
        with self.db.tx() as conn:
            self.h = self.db.insert(conn, "hypotheses", {"investigation_id": inv_id, "key": "noise", "title": "noise",
                                                         "kind": "common", "prior": "moderate"})
            self.h2 = self.db.insert(conn, "hypotheses", {"investigation_id": inv_id, "key": "wax", "title": "wax",
                                                          "kind": "common", "prior": "low"})
            for n in (1, 2):
                cid = self.db.insert(conn, "candidates", {"investigation_id": inv_id, "round_found": 1,
                                                          "source": "pubmed", "source_id": str(n), "status": "read"})
                self.db.insert(conn, "screenings", {"candidate_id": cid, "design": "rct"})
                rid = self.db.insert(conn, "paper_reads", {
                    "investigation_id": inv_id, "candidate_id": cid, "round": 1, "scope": "full", "status": "read",
                    "appraisal": {"overall_risk": "low"}, "applicability": {"overall": "high"},
                    "profile": {"n": {"value": "300"}}})
                claim = self.db.insert(conn, "evidence_claims", {
                    "investigation_id": inv_id, "read_id": rid, "claim": f"Claim {n}", "sentence_ids": ["S1.P1.s1"],
                    "evidence_type": "finding", "design": "rct", "status": "active", "verification": "supported",
                    "agreement": 2, "round": 1})
                conn.execute("INSERT INTO claim_links (claim_id, hypothesis_id, origin, direction, strength, case_match) "
                             "VALUES (?, ?, 'extract', 'supports', 'moderate', 'high')", (claim, self.h))
            self.db.update_investigation(inv_id, conn=conn, stage="matrix", knowledge_version=1)
        return inv_id

    async def run_inv(self, inv_id):
        ctx = llm.CallContext(db=self.db, settings=S.build("quality"), providers={"openai": FakeProvider("openai")},
                              limiters=Limiters({}), prices=PriceTable())
        pipeline, _ = build_pipeline(ctx, stop_at="fitting")
        worker = Worker(self.db, pipeline, services=ctx, max_parallel=1)
        await worker.machine.advance(inv_id)
        await worker.run_until_idle()

    async def test_ratings_are_stored_and_a_recompute_is_identical(self):
        inv_id = self.seed()
        await self.run_inv(inv_id)
        h = self.db.one("SELECT * FROM hypotheses WHERE id = ?", (self.h,))
        self.assertEqual((h["direction"], h["certainty"], h["evidence_version"]), ("supported", "high", 1))
        self.assertEqual(len(h["ratings"]["units"]["supports"]), 2)
        h2 = self.db.one("SELECT * FROM hypotheses WHERE id = ?", (self.h2,))
        self.assertEqual((h2["direction"], h2["certainty"]), ("insufficient", None))
        note = self.db.one("SELECT * FROM note_outbox WHERE investigation_id = ? AND kind = 'evidence_matrix'",
                           (inv_id,))
        self.assertEqual(note["artifact_key"], "matrix:r1")
        # Round 2 with the same evidence: identical ratings, so no evidence version bump.
        self.db.update_investigation(inv_id, status="active", outcome=None, stage="matrix", round=2)
        await self.run_inv(inv_id)
        again = self.db.one("SELECT * FROM hypotheses WHERE id = ?", (self.h,))
        self.assertEqual((again["ratings"], again["evidence_version"]), (h["ratings"], 1))
        with self.db.read() as conn:
            d1, d2 = M.load(conn, inv_id), M.load(conn, inv_id)
        self.assertEqual(M.rate(d1.items[self.h], d1), M.rate(d2.items[self.h], d2))
