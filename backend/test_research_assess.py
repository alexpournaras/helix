"""1E.5 fit and analysis (PLAN B8.20, B8.21). Synthetic text; fake providers; no network."""

from research_testkit import ResearchTestCase

from app.research import llm, settings as S
from app.research.prices import PriceTable
from app.research.providers import FakeProvider
from app.research.ratelimit import Limiters
from app.research.runner import Worker
from app.research.schemas import CaseBrief
from app.research.segment import user_segments
from app.research.steps import assess as A, build_pipeline, intake

TEXT = "Synthetic case. I am a 40-year-old man. The ringing in my left ear started after a loud concert."
BRIEF = {"chief_complaint": {"text": "Ringing in the left ear", "seg_ids": ["U3"]},
         "case_features": [{"key": "left_tinnitus", "text": "Ringing in the left ear", "seg_ids": ["U3"]},
                           {"key": "after_concert", "text": "Started after a loud concert", "seg_ids": ["U3"]},
                           {"key": "age_40", "text": "Age 40", "seg_ids": ["U2"]}]}


def stmt(text, kind, claims=(), segs=()):
    return {"text": text, "kind": kind, "claim_ids": list(claims), "seg_ids": list(segs)}


class Models:
    def __init__(self):
        self.calls = []
        self.fit = lambda r, n: {"items": [
            {"feature_key": "left_tinnitus", "fit": "explains", "claim_ids": ["C1"], "reason": "r"},
            {"feature_key": "after_concert", "fit": "explains", "claim_ids": [], "reason": "no claim"},  # downgraded
            {"feature_key": "left_tinnitus", "fit": "against", "claim_ids": ["C1"], "reason": "repeat"},  # dropped
            {"feature_key": "invented", "fit": "explains", "claim_ids": ["C1"], "reason": "x"}]}  # age_40 missing
        self.analysis = self.default_analysis

    @staticmethod
    def default_analysis(r, n):
        statements = [stmt("Noise exposure was linked with tinnitus in adults.", "evidence", ["C1"]),
                      stmt("He reported the ringing began after a concert.", "case_fact", segs=["U3"]),
                      stmt("The studies were small.", "caveat")]
        if n == 1:
            statements.append(stmt("Noise is the cause.", "inference"))  # no claim → repair
        else:
            statements.append(stmt("A hearing test is worth raising.", "recommendation", ["C9"]))  # still bad → dropped
        return {"statements": statements, "strongest_support": ["C1", "C7"], "strongest_contra": [],
                "open_questions": ["Whether one concert is enough."]}

    def handler(self, family):
        def h(request):
            self.calls.append(request.schema_name)
            n = self.calls.count(request.schema_name)
            if request.schema_name == "FitSet":
                return self.fit(request, n)
            if request.schema_name == "HypothesisAnalysis":
                return self.analysis(request, n)
            raise AssertionError(request.schema_name)
        return h


class AssessBase(ResearchTestCase):
    def setUp(self):
        super().setUp()
        self.models = Models()

    def ctx(self):
        self.a = FakeProvider("openai", self.models.handler("openai"))
        self.b = FakeProvider("anthropic", self.models.handler("anthropic"))
        return llm.CallContext(db=self.db, settings=S.build("quality"),
                               providers={"openai": self.a, "anthropic": self.b},
                               limiters=Limiters({}), prices=PriceTable())

    def seed(self):
        inv_id = self.db.create_investigation(TEXT, profile="quality",
                                              settings_snapshot=S.build("quality").model_dump(mode="json"),
                                              synthetic=True)
        with self.db.tx() as conn:
            intake.save_segments(conn, inv_id, user_segments(TEXT))
            intake.save_brief(self.db, conn, inv_id, CaseBrief.model_validate(BRIEF), source="intake:p1", check=None,
                              prompt_version="test")
            self.h = self.db.insert(conn, "hypotheses", {
                "investigation_id": inv_id, "key": "noise", "title": "Noise damage", "kind": "common",
                "prior": "moderate", "direction": "supported", "certainty": "low", "evidence_version": 1,
                "ratings": {"S": 1.2, "C": 0, "downgrades": ["imprecision"]}})
            cid = self.db.insert(conn, "candidates", {"investigation_id": inv_id, "round_found": 1, "source": "pubmed",
                                                      "source_id": "1", "status": "read"})
            rid = self.db.insert(conn, "paper_reads", {"investigation_id": inv_id, "candidate_id": cid, "round": 1,
                                                       "scope": "full", "status": "read",
                                                       "appraisal": {"overall_risk": "low"},
                                                       "applicability": {"overall": "high"}})
            self.claim = self.db.insert(conn, "evidence_claims", {
                "investigation_id": inv_id, "read_id": rid, "claim": "Noise exposure was linked with tinnitus",
                "sentence_ids": ["S1.P1.s1"], "evidence_type": "finding", "design": "cohort", "status": "active",
                "verification": "supported", "agreement": 2, "round": 1})
            conn.execute("INSERT INTO claim_links (claim_id, hypothesis_id, origin, direction, strength, case_match) "
                         "VALUES (?, ?, 'extract', 'supports', 'moderate', 'high')", (self.claim, self.h))
            self.db.update_investigation(inv_id, conn=conn, stage="fitting", knowledge_version=1)
        return inv_id

    async def run_inv(self, inv_id):
        ctx = self.ctx()
        pipeline, _ = build_pipeline(ctx, stop_at="gaps")
        self.worker = Worker(self.db, pipeline, services=ctx, max_parallel=2)
        await self.worker.machine.advance(inv_id)
        await self.worker.run_until_idle()
        return self.db.investigation(inv_id)

    def fits(self, inv_id, rnd=1):
        return {r["feature_key"]: r for r in self.db.query(
            "SELECT * FROM feature_fit WHERE investigation_id = ? AND round = ?", (inv_id, rnd))}

    def reason(self, inv_id):
        row = self.db.one("SELECT data FROM events WHERE investigation_id = ? AND kind = 'complete' ORDER BY id DESC",
                          (inv_id,))
        return (row["data"] or {}).get("reason", "") if row else ""


class FitAndAnalysisTests(AssessBase):
    async def test_fit_then_analysis(self):
        inv_id = self.seed()
        await self.run_inv(inv_id)
        self.assertIn("gaps", self.reason(inv_id))
        f = self.fits(inv_id)
        self.assertEqual(set(f), {"left_tinnitus", "after_concert", "age_40"})  # exactly one per feature
        self.assertEqual((f["left_tinnitus"]["fit"], f["left_tinnitus"]["claim_ids"]), ("explains", [self.claim]))
        self.assertEqual(f["after_concert"]["fit"], "compatible")  # explains without a claim → downgraded
        self.assertEqual(f["age_40"]["fit"], "unknown")  # missing → unknown
        h = self.db.one("SELECT case_fit FROM hypotheses WHERE id = ?", (self.h,))
        self.assertEqual(h["case_fit"], "good")  # E = 1 ≥ max(1, 3/3), A = 0
        ev = self.db.one("SELECT data FROM events WHERE investigation_id = ? AND kind = 'fit'", (inv_id,))
        self.assertEqual((ev["data"]["dropped"], ev["data"]["downgraded"], ev["data"]["missing"]), (2, 1, 1))
        a = self.db.one("SELECT * FROM hypothesis_analyses WHERE investigation_id = ? AND hypothesis_id = ?",
                        (inv_id, self.h))
        kinds = [s["kind"] for s in a["data"]["statements"]]
        self.assertEqual(kinds, ["evidence", "case_fact", "caveat"])  # the bad one is repaired, then dropped
        self.assertEqual(a["data"]["dropped"], 1)
        self.assertEqual(a["data"]["statements"][0]["claim_ids"], [self.claim])
        self.assertEqual(a["data"]["strongest_support"], [self.claim])  # C7 doesn't exist
        self.assertEqual(self.models.calls.count("HypothesisAnalysis"), 2)  # one repair
        self.assertEqual(a["data"]["basis"], {"evidence_version": 1,
                                              "features": ["after_concert", "age_40", "left_tinnitus"]})
        note = self.db.one("SELECT * FROM note_outbox WHERE investigation_id = ? AND kind = 'hypothesis_analysis'",
                           (inv_id,))
        self.assertEqual(note["artifact_key"], f"analysis:r1:h{self.h}")

    async def test_unchanged_hypotheses_are_copied_forward(self):
        inv_id = self.seed()
        await self.run_inv(inv_id)
        calls = len(self.models.calls)
        self.db.update_investigation(inv_id, status="active", outcome=None, stage="fitting", round=2)
        await self.run_inv(inv_id)
        self.assertEqual(len(self.models.calls), calls)  # nothing rerun
        f2 = self.fits(inv_id, 2)
        self.assertEqual(len(f2), 3)
        self.assertTrue(all(r["copied"] == 1 for r in f2.values()))
        a2 = self.db.one("SELECT copied FROM hypothesis_analyses WHERE investigation_id = ? AND round = 2", (inv_id,))
        self.assertEqual(a2["copied"], 1)

    async def test_new_evidence_reruns(self):
        inv_id = self.seed()
        await self.run_inv(inv_id)
        calls = len(self.models.calls)
        with self.db.tx() as conn:
            conn.execute("UPDATE hypotheses SET evidence_version = 2 WHERE id = ?", (self.h,))
        self.db.update_investigation(inv_id, status="active", outcome=None, stage="fitting", round=2)
        await self.run_inv(inv_id)
        self.assertGreater(len(self.models.calls), calls)
        self.assertTrue(all(r["copied"] == 0 for r in self.fits(inv_id, 2).values()))

    def test_statement_rules(self):
        from app.research.schemas import AnalysisStatement as St
        refs, segs = {"C1"}, {"U1"}
        cases = [(St(text="Found it.", kind="evidence", claim_ids=["C1"]), None),
                 (St(text="Found it.", kind="evidence"), "must cite a claim"),
                 (St(text="He said so.", kind="case_fact"), "segments"),
                 (St(text="He said so.", kind="case_fact", seg_ids=["U9"]), "unknown IDs"),
                 (St(text="Generally true.", kind="general"), "belong in the report"),
                 (St(text="Small studies.", kind="caveat"), None)]
        for s, expected in cases:
            got = A.statement_problem(s, refs, segs)
            if expected is None:
                self.assertIsNone(got)
            else:
                self.assertIn(expected, got or "")
