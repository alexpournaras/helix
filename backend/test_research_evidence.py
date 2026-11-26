"""1E.3 map, group, confirm (PLAN B8.16–B8.18) and the claim weight (B8.19). Synthetic text; fake providers and
sources; no network."""

import re

from research_testkit import ResearchTestCase

from app.literature.models import Article, Details, Ref, Section
from app.research import llm, settings as S, weights as W
from app.research.prices import PriceTable
from app.research.providers import FakeProvider
from app.research.ratelimit import Limiters
from app.research.runner import Worker
from app.research.schemas import CaseBrief
from app.research.segment import paper_snapshot, user_segments
from app.research.sources import Fetched
from app.research.steps import build_pipeline, evidence as E, intake

TEXT = "Synthetic case. I am a 40-year-old man with ringing in my left ear for a month."
BRIEF = {"chief_complaint": {"text": "Ringing in the left ear", "seg_ids": ["U2"]},
         "case_features": [{"key": "left_tinnitus", "text": "Ringing in the left ear", "seg_ids": ["U2"]}]}
SNAP = paper_snapshot("Synthetic abstract.", Details(full_text=[
    Section(title="Methods", text="Adults were studied."),
    Section(title="Results", text="Noise exposure raised the odds of tinnitus. Ear wax was not associated."),
    Section(title="Discussion", text="These results agree with earlier work.")], full_text_from="pmc"))
RES = [s.seg_id for s in SNAP.segments if s.section == "Results"]


class Models:
    def __init__(self):
        self.map = lambda request: {"items": [{"claim": c, "links": []}
                                              for c in re.findall(r"^(C\d+): ", request.user, re.M)]}
        self.confirm = lambda pair, text: "supported"
        self.calls = []

    def handler(self, family):
        def h(request):
            self.calls.append((request.schema_name, family, request.model))
            if request.schema_name == "MapBatch":
                return self.map(request)
            if request.schema_name == "ClaimVoteBatch":
                items = []
                for pair, text in re.findall(r"^(C\d+): (.*)$", request.user, re.M):
                    items.append({"pair": pair, "asserts_effect": True, "direction_check": "same",
                                  "significance": "significant", "population_check": "same",
                                  "overgeneralization": False, "context_needed": False,
                                  "verdict": self.confirm(pair, text), "reason": "r"})
                return {"items": items}
            raise AssertionError(request.schema_name)
        return h


class FakeSources:
    def __init__(self):
        self.refs = {}

    async def load(self, literature_id):
        details = Details(references=self.refs.get(literature_id, []))
        return Fetched(literature_id, Article(source="pubmed", source_id=str(literature_id)), details, True)


class EvidenceBase(ResearchTestCase):
    def setUp(self):
        super().setUp()
        self.models = Models()
        self.sources = FakeSources()

    def ctx(self):
        self.a = FakeProvider("openai", self.models.handler("openai"))
        self.b = FakeProvider("anthropic", self.models.handler("anthropic"))
        return llm.CallContext(db=self.db, settings=S.build("quality"),
                               providers={"openai": self.a, "anthropic": self.b},
                               limiters=Limiters({}), prices=PriceTable(), sources=self.sources)

    def seed(self, stage="mapping", papers=None, settings=None):
        """papers: [(pmid, design, registry ids, dataset)]."""
        papers = papers or [("1", "cohort", [], None)]
        snap = S.build("quality").model_dump(mode="json")
        for k, v in (settings or {}).items():
            snap[k] = {**snap.get(k, {}), **v}
        inv_id = self.db.create_investigation(TEXT, profile="quality", settings_snapshot=snap, synthetic=True)
        with self.db.tx() as conn:
            intake.save_segments(conn, inv_id, user_segments(TEXT))
            intake.save_brief(self.db, conn, inv_id, CaseBrief.model_validate(BRIEF), source="intake:p1", check=None,
                              prompt_version="test")
            self.h = {k: self.db.insert(conn, "hypotheses", {"investigation_id": inv_id, "key": k, "title": k,
                                                             "kind": "common", "prior": "moderate"})
                      for k in ("noise", "wax")}
            self.reads = {}
            for pmid, design, registry, dataset in papers:
                cid = self.db.insert(conn, "candidates", {"investigation_id": inv_id, "round_found": 1,
                                                          "source": "pubmed", "source_id": pmid, "pmid": pmid,
                                                          "status": "read", "literature_id": 100 + int(pmid)})
                self.db.insert(conn, "screenings", {"candidate_id": cid, "design": design})
                self.reads[pmid] = self.db.insert(conn, "paper_reads", {
                    "investigation_id": inv_id, "candidate_id": cid, "literature_id": 100 + int(pmid), "round": 1,
                    "scope": "full", "segments": SNAP.to_dict(), "status": "read",
                    "appraisal": {"overall_risk": "low"}, "applicability": {"overall": "high"},
                    "profile": {"n": {"value": "250 adults", "sentence_ids": []}},
                    "study_identity": {"registry_ids": [{"id": r, "sentence_ids": []} for r in registry],
                                       "dataset_or_cohort": {"value": dataset, "sentence_ids": []}}})
            self.db.update_investigation(inv_id, conn=conn, stage=stage, knowledge_version=1)
        return inv_id

    def add_claim(self, inv_id, pmid="1", text="Noise exposure raised the odds of tinnitus", links=(), family="openai",
                  **kw):
        with self.db.tx() as conn:
            cid = self.db.insert(conn, "evidence_claims", {
                "investigation_id": inv_id, "read_id": self.reads[pmid], "literature_id": 100 + int(pmid),
                "pass": "pack", "extractor_model": "x", "agreement": 2, "claim": text, "sentence_ids": [RES[0]],
                "quote": SNAP.get(RES[0]).text, "evidence_type": "finding", "design": "cohort", "status": "active",
                "verification": kw.pop("verification", "supported"), "round": 1,
                "checks": {"extractor_family": family, **kw.pop("checks", {})}, **kw})
            for hyp, direction, strength in links:
                conn.execute("INSERT INTO claim_links (claim_id, hypothesis_id, origin, direction, strength, case_match) "
                             "VALUES (?, ?, 'extract', ?, ?, 'high')", (cid, self.h[hyp], direction, strength))
        return cid

    async def run_inv(self, inv_id, stop_at):
        ctx = self.ctx()
        pipeline, _ = build_pipeline(ctx, stop_at=stop_at)
        self.worker = Worker(self.db, pipeline, services=ctx, max_parallel=2)
        await self.worker.machine.advance(inv_id)
        await self.worker.run_until_idle()
        return self.db.investigation(inv_id)


class WeightTests(EvidenceBase):
    def test_claim_weight(self):
        claim = {"design": "cohort", "evidence_type": "finding", "n": None, "verification": "partial", "agreement": 1}
        read = {"appraisal": {"overall_risk": "some"}, "applicability": {"overall": "partial"}, "scope": "abstract_only",
                "profile": {"n": {"value": "45 adults"}}}
        w, parts = W.claim_weight(claim, {"strength": "strong", "case_match": "unknown"}, read, concern=True)
        expected = 1.5 * 0.7 * 0.75 * 0.7 * 0.6 * 0.85 * 1 * 0.6 * 0.6 * 0.9 * 0.8
        self.assertAlmostEqual(w, round(expected, 4))
        self.assertEqual(parts["size"], 0.85)  # 45 from the profile → 30–99
        w2, p2 = W.claim_weight({**claim, "evidence_type": "background"}, {"strength": "weak"}, read)
        self.assertEqual((p2["type"], p2["size"]), (0.3, 1.0))  # background: size n/a


class MapTests(EvidenceBase):
    async def test_mapping_overrides_extraction_and_neutral_cancels(self):
        inv_id = self.seed()
        c1 = self.add_claim(inv_id, links=[("noise", "supports", "moderate")])
        c2 = self.add_claim(inv_id, text="Ear wax was not associated with tinnitus",
                            links=[("wax", "supports", "weak")])

        def mapping(request):
            return {"items": [
                {"claim": "C1", "links": [{"hypothesis_key": "noise", "direction": "contradicts", "strength": "strong",
                                           "case_match": "high", "reason": "r"}]},
                {"claim": "C2", "links": [{"hypothesis_key": "wax", "direction": "neutral", "strength": "weak",
                                           "case_match": "high", "reason": "r"},
                                          {"hypothesis_key": "invented", "direction": "supports", "strength": "weak",
                                           "case_match": "high", "reason": "r"}]}]}
        self.models.map = mapping
        await self.run_inv(inv_id, stop_at="grouping")
        with self.db.read() as conn:
            eff = W.effective_links(conn, inv_id)
        self.assertEqual(eff[c1][self.h["noise"]]["direction"], "contradicts")  # the mapping wins
        self.assertNotIn(c2, eff)  # neutral cancels the extraction link
        ev = self.db.one("SELECT data FROM events WHERE investigation_id = ? AND kind = 'map'", (inv_id,))
        self.assertEqual((ev["data"]["disagree"], ev["data"]["unknown_keys"]), (2, 1))
        c = self.db.one("SELECT checks FROM evidence_claims WHERE id = ?", (c1,))
        self.assertEqual(c["checks"]["mapped"], sorted(self.h.values()))

    async def test_older_claims_meet_only_new_hypotheses(self):
        inv_id = self.seed()
        self.add_claim(inv_id, checks={"mapped": [self.h["noise"]]})
        await self.run_inv(inv_id, stop_at="grouping")
        maps = [c for c in self.models.calls if c[0] == "MapBatch"]
        self.assertEqual(len(maps), 1)
        user = self.a.requests[0].user if self.a.requests else self.b.requests[0].user
        self.assertIn("- wax: wax", user)
        self.assertNotIn("- noise: noise", user)

    async def test_nothing_to_map(self):
        inv_id = self.seed()
        self.add_claim(inv_id, checks={"mapped": list(self.h.values())})
        await self.run_inv(inv_id, stop_at="grouping")
        self.assertEqual([c for c in self.models.calls if c[0] == "MapBatch"], [])


class GroupTests(EvidenceBase):
    async def test_same_registry_or_dataset_is_one_study_and_review_inclusion(self):
        inv_id = self.seed(stage="grouping", papers=[
            ("1", "rct", ["NCT01234567"], None), ("2", "cohort", ["NCT01234567"], "HELIX cohort"),
            ("3", "cohort", [], "The HELIX Cohort Study"), ("4", "cohort", [], "Other data"),
            ("5", "systematic_review", [], None), ("6", "case_report", [], "not reported"),
            ("7", "case_report", [], "not reported")])
        self.sources.refs = {105: [Ref(pmid="4"), Ref(doi="10.1/none"), Ref(pmid="99")]}
        await self.run_inv(inv_id, stop_at="confirming")
        members = self.db.query("SELECT s.id, s.kind, s.label, s.registry_ids, m.read_id FROM studies s JOIN "
                                "study_members m ON m.study_id = s.id WHERE s.investigation_id = ?", (inv_id,))
        by_read = {m["read_id"]: m for m in members}
        r = self.reads
        self.assertEqual(len({by_read[r[p]]["id"] for p in ("1", "2", "3")}), 1)  # registry + dataset chain
        self.assertNotEqual(by_read[r["4"]]["id"], by_read[r["1"]]["id"])
        self.assertNotEqual(by_read[r["6"]]["id"], by_read[r["7"]]["id"])  # "not reported" is no dataset
        self.assertEqual(by_read[r["5"]]["kind"], "review_sr")
        self.assertEqual(by_read[r["1"]]["registry_ids"], ["NCT01234567"])
        inc = self.db.query("SELECT * FROM review_inclusions", ())
        self.assertEqual([(i["review_study_id"], i["included_study_id"], i["matched_by"]) for i in inc],
                         [(by_read[r["5"]]["id"], by_read[r["4"]]["id"], "pmid")])
        ev = self.db.one("SELECT data FROM events WHERE investigation_id = ? AND kind = 'group'", (inv_id,))
        self.assertEqual((ev["data"]["papers"], ev["data"]["studies"]), (7, 5))


class ConfirmTests(EvidenceBase):
    def test_selection(self):
        prov = [{"claim_id": i, "hypothesis_id": 1, "direction": "supports", "strength": "moderate", "weight": w}
                for i, w in [(1, .9), (2, .8), (3, .7)]]
        prov += [{"claim_id": 4, "hypothesis_id": 1, "direction": "contradicts", "strength": "moderate", "weight": .2},
                 {"claim_id": 5, "hypothesis_id": 2, "direction": "supports", "strength": "strong", "weight": .1},
                 {"claim_id": 6, "hypothesis_id": 2, "direction": "supports", "strength": "weak", "weight": .05}]
        self.assertEqual(E.choose(prov, 2, 100, set()), [1, 2, 4, 5, 6])  # top 2 per direction; 3 is third
        self.assertEqual(E.choose(prov, 2, 100, {1}), [2, 3, 4, 5, 6])
        self.assertEqual(E.choose(prov, 1, 2, set()), [1, 4])
        prov.append({"claim_id": 7, "hypothesis_id": 2, "direction": "supports", "strength": "strong", "weight": .01})
        self.assertIn(7, E.choose(prov, 1, 100, set()))  # every strong link

    async def test_most_cautious_verdict_wins(self):
        inv_id = self.seed(stage="confirming")
        keep = self.add_claim(inv_id, links=[("noise", "supports", "strong")])
        down = self.add_claim(inv_id, text="Noise exposure doubled tinnitus", links=[("noise", "supports", "moderate")])
        drop = self.add_claim(inv_id, text="Noise exposure caused tinnitus", links=[("noise", "contradicts", "weak")],
                              family="anthropic")
        partial = self.add_claim(inv_id, text="Ear wax mattered", links=[("wax", "supports", "weak")],
                                 verification="partial")
        none = self.add_claim(inv_id, text="Unlinked claim")
        verdicts = {"Noise exposure raised": "supported", "Noise exposure doubled": "partial",
                    "Noise exposure caused": "unsupported", "Ear wax mattered": "supported"}
        self.models.confirm = lambda pair, text: next(v for k, v in verdicts.items() if text.startswith(k))
        await self.run_inv(inv_id, stop_at="matrix")
        got = {r["id"]: r for r in self.db.query("SELECT * FROM evidence_claims WHERE investigation_id = ?", (inv_id,))}
        self.assertEqual((got[keep]["status"], got[keep]["verification"], got[keep]["consequential"]),
                         ("active", "supported", 1))
        self.assertEqual(got[down]["verification"], "partial")
        self.assertEqual(got[drop]["status"], "rejected")
        self.assertEqual(got[partial]["verification"], "partial")  # confirm says supported; partial stays
        self.assertIsNone(got[none]["confirmation"])  # no link: not consequential
        fams = {(c[1], c[2]) for c in self.models.calls if c[0] == "ClaimVoteBatch"}
        quality = S.build("quality")
        self.assertEqual(fams, {("anthropic", quality.strongest["anthropic"].model),
                                ("openai", quality.strongest["openai"].model)})  # the other family's strongest
        votes = self.db.query("SELECT stage FROM claim_votes WHERE claim_id = ?", (drop,))
        self.assertEqual([v["stage"] for v in votes], ["confirm"])
