"""1E.1 reading (PLAN B8.14): stage A, dual extraction, whole-paper pass, reconciliation, stage C. Synthetic text;
fake providers and sources; no network."""

import json
import re

from research_testkit import ResearchTestCase

from app.literature.models import Article, Details, Section
from app.research import llm, settings as S
from app.research.prices import PriceTable
from app.research.providers import FakeProvider
from app.research.ratelimit import Limiters
from app.research.runner import Worker
from app.research.schemas import CaseBrief
from app.research.segment import paper_snapshot, user_segments
from app.research.sources import Fetched
from app.research.steps import build_pipeline, intake, read as R

TEXT = "Synthetic case. I am a 40-year-old man with ringing in my left ear for a month."
BRIEF = {"chief_complaint": {"text": "Ringing in the left ear", "seg_ids": ["U2"]},
         "demographics": {"age": {"text": "40", "seg_ids": ["U2"], "years": 40}},
         "case_features": [{"key": "left_tinnitus", "text": "Ringing in the left ear", "seg_ids": ["U2"]}]}
ABSTRACT = "Synthetic abstract about noise and tinnitus."
RESULTS = ("Adults were enrolled in the HELIX cohort (NCT01234567). "
           "Noise exposure raised the odds of tinnitus (OR 1.50, 95% CI 1.10 to 2.05) in 1,234 adults. "
           "Ear wax was not associated with tinnitus (p = 0.40). "
           "Hearing was tested in every participant.")
DETAILS = Details(full_text=[Section(title="Results", text=RESULTS)], full_text_from="pmc")
SNAP = paper_snapshot(ABSTRACT, DETAILS)
IDS = [s.seg_id for s in SNAP.segments if s.section == "Results"]


def field(value, ids=()):
    return {"value": value, "sentence_ids": list(ids)}


def profile(request, family):
    keys = ["question", "design", "setting", "population", "inclusion_criteria", "n", "exposure", "comparator",
            "outcomes", "follow_up", "funding", "conflicts", "review_included_designs", "dataset_or_cohort",
            "secondary_analysis"]
    out = {k: field(None) for k in keys}
    out.update(population=field("Adults", [IDS[0]]), n=field("1,234 adults", [IDS[1]]),
               dataset_or_cohort=field("HELIX cohort", [IDS[0], "S9.P9.s9"]), secondary_analysis=field("no"))
    return out


def appraisal(request, family):
    names = re.search(r"Criteria: (.*)", request.user).group(1).split(", ")
    rating = "low" if family == "openai" else "some"
    crit = [{"criterion": n, "rating": rating, "reason": "r", "sentence_ids": [IDS[0]]} for n in names]
    if family == "anthropic":
        crit[0]["rating"] = "high"  # one criterion high → the more cautious rating and overall high
    return {"criteria": crit, "overall_risk": "low" if family == "openai" else "some"}


def link(key="noise", direction="supports"):
    return {"hypothesis_key": key, "direction": direction, "strength": "moderate", "case_match": "high", "reason": "r"}


def claim(text, ids, **kw):
    base = {"claim": text, "sentence_ids": ids, "evidence_type": "finding", "population": "adults",
            "design": "cohort", "n": None, "effect": None, "limitations": "", "links": [link()]}
    base.update(kw)
    return base


class Models:
    def __init__(self):
        self.calls = {}
        self.users = {}
        self.claims_a = lambda r: [
            claim("Noise exposure raised the odds of tinnitus in adults", [IDS[1]], n=1234,
                  effect={"measure": "OR", "value": 1.5, "ci_low": 1.1, "ci_high": 2.05, "p": None}),
            claim("Hearing loss was the main finding", ["S9.P9.s9"])]  # an ID that doesn't exist → rejected
        self.claims_b = lambda r: [
            claim("Noise exposure raised the odds of tinnitus in adults with OR 1.5", [IDS[1], IDS[0]],
                  effect={"measure": "OR", "value": 1.7, "ci_low": None, "ci_high": None, "p": None}),
            claim("Ear wax was not associated with tinnitus", [IDS[2]], links=[link("wax", "contradicts"),
                                                                               link("made_up")])]
        self.whole = lambda r: [claim("Every participant had a hearing test in the cohort", [IDS[3]], links=[])]
        self.same = lambda r: {"pairs": [{"pair": p, "same": True, "reason": "same"}
                                         for p in re.findall(r"^(M\d+):", r.user, re.M)]}
        self.applicability = lambda r: {"population_match": "high", "symptom_match": "high", "setting_match": "partial",
                                        "special_population_fit": "not_applicable", "overall": "high", "reason": "fits"}

    def handler(self, family):
        def h(request):
            name = request.schema_name
            if name == "ClaimSet" and "whole-paper" in request.system:
                name = "WholePaper"
            self.calls[name] = self.calls.get(name, 0) + 1
            self.users.setdefault(name, []).append(request.user)
            if name == "PaperProfile":
                return profile(request, family)
            if name == "Appraisal":
                return appraisal(request, family)
            if name == "ClaimSet":
                return {"claims": (self.claims_a if family == "openai" else self.claims_b)(request)}
            if name == "WholePaper":
                return {"claims": self.whole(request)}
            if name == "SameClaims":
                return self.same(request)
            if name == "Applicability":
                return self.applicability(request)
            raise AssertionError(name)
        return h


class FakeSources:
    def __init__(self):
        self.loads = []
        self.fail = False

    async def load(self, literature_id):
        self.loads.append(literature_id)
        if self.fail:
            raise LookupError("gone")
        a = Article(source="pubmed", source_id="11", pmid="11", title="Noise and tinnitus", abstract=ABSTRACT)
        return Fetched(literature_id, a, DETAILS, from_library=True)


class ReadBase(ResearchTestCase):
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

    def seed(self, settings=None, populations=(), shared_read=False):
        snap = S.build("quality").model_dump(mode="json")
        for k, v in (settings or {}).items():
            snap[k] = {**snap.get(k, {}), **v}
        inv_id = self.db.create_investigation(TEXT, profile="quality", settings_snapshot=snap, synthetic=True)
        with self.db.tx() as conn:
            intake.save_segments(conn, inv_id, user_segments(TEXT))
            intake.save_brief(self.db, conn, inv_id, CaseBrief.model_validate(BRIEF), source="intake:p1", check=None,
                              prompt_version="test")
            self.hyps = {k: self.db.insert(conn, "hypotheses", {"investigation_id": inv_id, "key": k, "title": k,
                                                                "kind": "common", "prior": "moderate"})
                         for k in ("noise", "wax")}
            self.cid = self.db.insert(conn, "candidates", {
                "investigation_id": inv_id, "round_found": 1, "source": "pubmed", "source_id": "11", "pmid": "11",
                "title": "Noise and tinnitus", "abstract": ABSTRACT, "year": 2022, "status": "fetched",
                "literature_id": 501})
            self.db.insert(conn, "screenings", {"candidate_id": self.cid, "design": "cohort", "decision": "include",
                                                "relevance": 3})
            if shared_read:
                self.shared = self.db.insert(conn, "paper_reads", {
                    "investigation_id": inv_id, "literature_id": 501, "round": 1, "purpose": "grounding",
                    "scope": "full", "segments": SNAP.to_dict(), "status": "read"})
            self.db.update_investigation(inv_id, conn=conn, stage="reading", knowledge_version=1,
                                         special_populations=list(populations))
        return inv_id

    async def run_inv(self, inv_id):
        ctx = self.ctx()
        pipeline, _ = build_pipeline(ctx, stop_at="verifying")
        self.worker = Worker(self.db, pipeline, services=ctx, max_parallel=2)
        await self.worker.machine.advance(inv_id)
        await self.worker.run_until_idle()
        return self.db.investigation(inv_id)

    def claims(self, inv_id):
        return self.db.query("SELECT * FROM evidence_claims WHERE investigation_id = ? ORDER BY id", (inv_id,))

    def read_row(self, inv_id):
        return self.db.one("SELECT * FROM paper_reads WHERE investigation_id = ? ORDER BY id DESC", (inv_id,))

    def reason(self, inv_id):
        row = self.db.one("SELECT data FROM events WHERE investigation_id = ? AND kind = 'complete' ORDER BY id DESC",
                          (inv_id,))
        return (row["data"] or {}).get("reason", "") if row else ""


class ReadFlowTests(ReadBase):
    async def test_full_read(self):
        inv_id = self.seed()
        await self.run_inv(inv_id)
        self.assertIn("verifying", self.reason(inv_id))
        self.assertEqual(self.models.calls, {"PaperProfile": 1, "Appraisal": 2, "ClaimSet": 2, "WholePaper": 1,
                                             "Applicability": 1})
        r = self.read_row(inv_id)
        self.assertEqual((r["purpose"], r["scope"], r["status"]), ("research", "full", "read"))
        self.assertEqual(r["profile"]["dataset_or_cohort"]["sentence_ids"], [IDS[0]])  # unknown ID dropped
        self.assertEqual(r["study_identity"]["registry_ids"][0]["id"], "NCT01234567")
        self.assertEqual(r["appraisal"]["overall_risk"], "high")  # one criterion high
        self.assertEqual(r["appraisal"]["criteria"][1]["rating"], "some")  # the more cautious of low / some
        self.assertEqual(len(r["appraisal_votes"]), 2)
        self.assertEqual(r["applicability"]["overall"], "high")
        cl = {c["claim"]: c for c in self.claims(inv_id)}
        kept = cl["Noise exposure raised the odds of tinnitus in adults"]
        member = cl["Noise exposure raised the odds of tinnitus in adults with OR 1.5"]
        self.assertEqual((kept["status"], kept["agreement"], kept["cluster_id"]), ("pending", 2, kept["id"]))
        self.assertEqual((member["status"], member["cluster_id"]), ("superseded", kept["id"]))
        self.assertEqual(kept["effect"]["value"], 1.5)
        self.assertEqual(kept["n"], 1234)
        self.assertIsNone(member["effect"])  # 1.7 isn't in the cited text
        self.assertTrue(member["checks"]["effect_cleared"])
        self.assertTrue(member["quote"].startswith("Adults were enrolled"))  # document order
        wax = cl["Ear wax was not associated with tinnitus"]
        self.assertEqual(wax["agreement"], 1)
        links = self.db.query("SELECT * FROM claim_links WHERE claim_id = ?", (wax["id"],))
        self.assertEqual([(l["hypothesis_id"], l["direction"], l["origin"]) for l in links],
                         [(self.hyps["wax"], "contradicts", "extract")])  # the unknown key is dropped
        whole = cl["Every participant had a hearing test in the cohort"]
        self.assertEqual((whole["pass"], whole["agreement"], whole["checks"]["neutral"]), ("whole", 1, True))
        self.assertEqual(whole["section_title"], "Results")
        self.assertIn("Noise exposure raised", self.models.users["WholePaper"][0])  # sees what was extracted
        rej = self.db.query("SELECT * FROM rejected_items WHERE investigation_id = ?", (inv_id,))
        self.assertEqual([x["reason"] for x in rej], ["no valid sentence IDs"])
        cand = self.db.one("SELECT status FROM candidates WHERE id = ?", (self.cid,))
        self.assertEqual(cand["status"], "read")
        ev = self.db.one("SELECT data FROM events WHERE investigation_id = ? AND kind = 'read'", (inv_id,))
        self.assertEqual((ev["data"]["claims"], ev["data"]["agreement_2"], ev["data"]["rejected"]), (3, 1, 1))
        self.assertNotIn("Noise", json.dumps(ev["data"]))  # counts, never paper or case text

    async def test_borderline_pairs_go_to_the_judge(self):
        self.models.claims_a = lambda r: [claim("Noise exposure raised the odds of tinnitus", [IDS[1]])]
        self.models.claims_b = lambda r: [claim("Noise exposure was linked with more tinnitus in adults", [IDS[1]])]
        self.models.whole = lambda r: []
        inv_id = self.seed()
        await self.run_inv(inv_id)
        self.assertEqual(self.models.calls["SameClaims"], 1)
        self.assertIn("M1: A: Noise exposure raised", self.models.users["SameClaims"][0])
        statuses = sorted(c["status"] for c in self.claims(inv_id))
        self.assertEqual(statuses, ["pending", "superseded"])

    async def test_judge_says_different(self):
        self.models.claims_a = lambda r: [claim("Noise exposure raised the odds of tinnitus", [IDS[1]])]
        self.models.claims_b = lambda r: [claim("Noise exposure was linked with more tinnitus in adults", [IDS[1]])]
        self.models.whole = lambda r: []
        self.models.same = lambda r: {"pairs": [{"pair": "M1", "same": False, "reason": "different"}]}
        inv_id = self.seed()
        await self.run_inv(inv_id)
        self.assertEqual([c["agreement"] for c in self.claims(inv_id)], [1, 1])

    async def test_adult_evidence_for_a_child_is_low_applicability(self):
        self.models.applicability = lambda r: {"population_match": "low", "symptom_match": "high",
                                               "setting_match": "high", "special_population_fit": "mismatch",
                                               "overall": "high", "reason": "adults only"}
        inv_id = self.seed(populations=("child",))
        await self.run_inv(inv_id)
        app = self.read_row(inv_id)["applicability"]
        self.assertEqual((app["overall"], app["capped_by"]), ("low", "special_population"))
        self.assertIn('"child"', self.models.users["Applicability"][0])

    async def test_no_special_population_means_not_applicable(self):
        self.models.applicability = lambda r: {"population_match": "high", "symptom_match": "high",
                                               "setting_match": "high", "special_population_fit": "mismatch",
                                               "overall": "high", "reason": "x"}
        inv_id = self.seed()
        await self.run_inv(inv_id)
        app = self.read_row(inv_id)["applicability"]
        self.assertEqual((app["special_population_fit"], app["overall"]), ("not_applicable", "high"))

    async def test_single_models_when_ensembles_are_off(self):
        inv_id = self.seed(settings={"ensembles": {"dual_extraction": False, "whole_paper_pass": False,
                                                   "appraisal_panel": False}})
        await self.run_inv(inv_id)
        self.assertEqual((self.models.calls["ClaimSet"], self.models.calls["Appraisal"]), (1, 1))
        self.assertNotIn("WholePaper", self.models.calls)
        self.assertTrue(all(c["agreement"] == 1 for c in self.claims(inv_id)))

    async def test_a_missing_criterion_is_repaired(self):
        n = {"i": 0}

        def partial_first(request, family):
            out = appraisal(request, family)
            n["i"] += 1
            if family == "openai" and n["i"] <= 2:
                out["criteria"] = out["criteria"][1:]
            return out
        original = self.models.handler

        def handler(family):
            base = original(family)

            def h(request):
                if request.schema_name == "Appraisal":
                    self.models.users.setdefault("Appraisal", []).append(request.user)
                    return partial_first(request, family)
                return base(request)
            return h
        self.models.handler = handler
        inv_id = self.seed()
        await self.run_inv(inv_id)
        self.assertTrue(any("rate every criterion" in u for u in self.models.users["Appraisal"]))
        self.assertEqual(len(self.read_row(inv_id)["appraisal"]["criteria"]), 5)

    async def test_a_paper_grounding_read_is_shared(self):
        inv_id = self.seed(shared_read=True)
        await self.run_inv(inv_id)
        self.assertEqual(self.sources.loads, [])  # the grounding snapshot is reused
        r = self.db.one("SELECT * FROM paper_reads WHERE id = ?", (self.shared,))
        self.assertEqual((r["purpose"], r["candidate_id"]), ("both", self.cid))
        self.assertEqual(self.db.one("SELECT COUNT(*) AS n FROM paper_reads WHERE investigation_id = ?",
                                     (inv_id,))["n"], 1)
        self.assertTrue(all(c["read_id"] == self.shared for c in self.claims(inv_id)))

    async def test_a_paper_that_cannot_be_loaded_is_skipped(self):
        self.sources.fail = True
        inv_id = self.seed()
        inv = await self.run_inv(inv_id)
        task = self.db.one("SELECT status FROM research_tasks WHERE investigation_id = ? AND step = 'read'", (inv_id,))
        self.assertEqual(task["status"], "skipped")
        self.assertEqual(inv["status"], "failed")  # 1 of 1 skipped: more than half

    def test_appraisal_combination_rules(self):
        from app.research.schemas import Appraisal
        a = Appraisal.model_validate({"criteria": [{"criterion": "sampling", "rating": "low"}], "overall_risk": "low"})
        b = Appraisal.model_validate({"criteria": [{"criterion": "sampling", "rating": "unclear"}],
                                      "overall_risk": "some"})
        got = R.combine_appraisals([a, b], "cross_sectional", SNAP)
        self.assertEqual(got["overall_risk"], "some")
        self.assertEqual([c["rating"] for c in got["criteria"]], ["unclear", "unclear", "unclear", "unclear"])
