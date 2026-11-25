"""1C.3 grounding (PLAN B8.3a, B7.4): queries, screening, selection, reading, extraction, verification, merge,
gaps, re-grounding, reuse. Fake sources and fake models; synthetic papers; no network."""

import re

from research_testkit import ResearchTestCase

from app.literature.db import LiteratureDB
from app.literature.models import Article, Details, Section
from app.research import llm, settings as S
from app.research.knowledge import loader
from app.research.prices import PriceTable
from app.research.providers import FakeProvider, ModelError
from app.research.ratelimit import Limiters
from app.research.runner import Worker
from app.research.schemas import AttestationVote, CaseBrief
from app.research.segment import user_segments
from app.research.sources import Fetched, LiteratureSources
from app.research.steps import build_pipeline, grounding as G, intake
from app.research import verify as V

TINNITUS = {"key": "tinnitus", "text": "Ringing in the left ear", "seg_ids": ["U2"],
            "mesh": [{"ui": "D014012", "name": "Tinnitus", "tree_numbers": [], "systems": ["ent"]}], "mesh_dropped": []}
DIZZY = {"key": "dizziness", "text": "Dizziness", "seg_ids": ["U2"], "mesh": [], "mesh_dropped": []}
ADULT = {"demographics": {"age": {"text": "40", "seg_ids": ["U1"], "years": 40},
                          "sex": {"text": "Woman", "seg_ids": ["U1"], "value": "female"}}}


def art(n, *, title=None, types=("Practice Guideline",), year=2024, abstract="Synthetic abstract. It is short.",
        authors=None, pmid=None, source="europepmc", **kw):
    return Article(source=source, source_id=f"MED:{n}", pmid=pmid or str(1000 + n), title=title or f"Guideline {n}",
                   abstract=abstract, authors=authors or [f"Author{n} A"], journal="Synthetic Journal", year=year,
                   publication_types=list(types), language="eng", **kw)


GUIDE_TEXT = ("Assess people with tinnitus for sudden hearing loss. Refer people with pulsatile tinnitus for "
              "assessment.\n\nAsk about the duration and whether it affects one or both ears.")


class FakeSources:
    def __init__(self, results=None, fail=False):
        self.results = results if results is not None else {"europepmc": [art(1)], "pubmed": [art(1, source="pubmed")]}
        self.fail = fail
        self.searches, self.fetches = [], []

    async def search(self, source, query, max_results):
        self.searches.append((source, query))
        if self.fail:
            raise RuntimeError("down")
        found = self.results.get(source, [])
        return len(found), found[:max_results]

    async def fetch(self, article, investigation_id):
        self.fetches.append(article.pmid)
        details = Details(full_text=[Section(title="Recommendations", text=GUIDE_TEXT)], full_text_from="pmc")
        return Fetched(int(article.pmid), article, details)


def ids_in(text, pattern):
    return re.findall(pattern, text)


class Models:
    """Fake model answers by schema; each behaviour can be overridden per test."""

    def __init__(self):
        self.verdicts = {}  # item text fragment → (verdict A, verdict B)
        self.adjudicated = []
        self.extract_extra = []  # extra raw red flags (e.g. with IDs outside the pack)
        self.merge_words = []  # each entry: words; items containing any of them form one duplicate group

    def screen(self, request, family):
        return {"items": [{"paper_id": p, "relevance": 3, "hypothesis_keys": [], "design": "guideline",
                           "population_note": "adults", "decision": "include", "reason": "guidance"}
                          for p in ids_in(request.user, r"(?m)^(P\d+): ")]}

    def extract(self, request, family):
        segs = ids_in(request.user, r"\[(S\d+\.P\d+\.s\d+)\]")
        return {"red_flags": [{"condition": "Hearing loss that comes on within days", "features": [], "urgency": "emergency",
                               "population": "all", "sentence_ids": [segs[0]]}] + self.extract_extra,
                "differential": [{"condition": "Pulsatile tinnitus of vascular cause", "key_features": [],
                                  "typical_population": "", "sentence_ids": [segs[1]]}],
                "history_items": [{"attribute": "Duration of tinnitus", "why": "chronicity",
                                   "sentence_ids": [segs[2]]}] if family == "openai" else [],
                "evaluations": [], "management_topics": []}

    def verify(self, request, family):
        out = []
        for pair, text in re.findall(r"(?m)^(X\d+): (.*)$", request.user):
            a, b = next((v for k, v in self.verdicts.items() if k in text), ("supported", "supported"))
            verdict = a if family == "openai" else b
            out.append({"pair": pair, "verdict": verdict, "population_check": "same", "overgeneralization": False,
                        "context_needed": False, "reason": "r"})
        return {"items": out}

    def groups(self, request):
        """Group items whose text contains one of the `merge_words` sets (all words present)."""
        lines = re.findall(r"(?m)^(X\d+): \[[^\]]*\] (.*)$", request.user)
        out = []
        for words in self.merge_words:
            ids = [x for x, text in lines if any(w in text for w in words)]
            if len(ids) > 1:
                out.append({"keep": ids[0], "same": ids[1:]})
        return {"groups": out}

    def handler(self, family):
        def h(request):
            name = request.schema_name
            if name == "ScreeningBatch":
                return self.screen(request, family)
            if name == "KnowledgeExtraction":
                return self.extract(request, family)
            if name == "AttestationBatch":
                if request.model in ("claude-fable-5-2", "gpt-6.2-astra") and "Context:" in request.user:
                    self.adjudicated.append(request.model)
                return self.verify(request, family)
            if name == "MergeGroups":
                return self.groups(request)
            if name == "SafetyReview":
                return {"level": "none", "crisis": False, "flags": [], "guideline_checks": [
                    {"item": k, "state": "not_applicable", "seg_ids": []} for k in ids_in(request.user, r'"item": "(K\d+)"')],
                    "recommendation": "", "rule_out_questions": []}
            raise AssertionError(name)
        return h


class GroundingBase(ResearchTestCase):
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

    def seed(self, complaints=(TINNITUS,), settings=None):
        text = "Synthetic case. I have ringing in my left ear."
        snap = S.build("quality").model_dump(mode="json")
        snap.update(settings or {})
        inv_id = self.db.create_investigation(text, profile="quality", settings_snapshot=snap, synthetic=True)
        with self.db.tx() as conn:
            intake.save_segments(conn, inv_id, user_segments(text))
            intake.save_brief(self.db, conn, inv_id, CaseBrief.model_validate(ADULT), source="intake:p1", check=None,
                              prompt_version="test")
            self.db.update_investigation(inv_id, conn=conn, stage="safety", complaints=list(complaints))
        return inv_id

    async def run_inv(self, inv_id):
        ctx = self.ctx()
        pipeline, _ = build_pipeline(ctx)
        self.worker = Worker(self.db, pipeline, services=ctx, max_parallel=2)
        await self.worker.machine.advance(inv_id)
        await self.worker.run_until_idle()
        return self.db.investigation(inv_id)

    def items(self, inv_id, **where):
        sql = "SELECT * FROM knowledge_items WHERE investigation_id = ?"
        params = [inv_id]
        for k, v in where.items():
            sql += f" AND {k} = ?"
            params.append(v)
        return self.db.query(sql + " ORDER BY id", params)


class QueryAndSelectionTests(GroundingBase):
    def test_queries_use_mesh_entry_terms_filters_and_population(self):
        qs = G.build_queries(name="Tinnitus", entry_terms=["Ringing in the ears", "tinnitus"], text="x",
                             population="older", years=10, this_year=2026)
        epmc = qs[0]["query"]
        self.assertIn('MESH:"Tinnitus"', epmc)
        self.assertIn('TITLE_ABS:"Ringing in the ears"', epmc)
        self.assertEqual(epmc.count('TITLE_ABS:"tinnitus"'), 0)  # duplicate of the name (case-insensitive)
        self.assertIn('PUB_TYPE:"practice guideline"', epmc)
        self.assertIn("PUB_YEAR:[2016 TO 2026]", epmc)
        self.assertIn('TITLE_ABS:"elderly"', epmc)
        self.assertIn('PUB_TYPE:"systematic review"', qs[1]["query"])
        self.assertTrue(qs[2]["query"].startswith('"Tinnitus"[mh] AND (guideline[pt]'))
        self.assertIn('"aged"[mh]', qs[2]["query"])
        self.assertIn("diagnosis[sh]", qs[3]["query"])
        plain = G.build_queries(name=None, entry_terms=[], text="Head pressure", population="general", years=10,
                                this_year=2026)
        self.assertIn('TITLE_ABS:"Head pressure"', plain[0]["query"])
        self.assertNotIn("MESH:", plain[0]["query"])
        self.assertTrue(plain[2]["query"].startswith('"Head pressure"[tiab]'))

    def test_dedupe_merges_sources_and_queries(self):
        a = art(1)
        b = art(1, source="pubmed")
        c = art(2, pmid="9", title="Same title", year=2020)
        d = art(3, pmid="10", title="Same Title!", year=2020)
        cands = G.dedupe([(0, 0, a), (2, 1, b), (0, 1, c), (1, 0, d)])
        self.assertEqual(len(cands), 2)
        self.assertEqual((cands[0].sources, cands[0].queries), (["europepmc", "pubmed"], [0, 2]))
        self.assertEqual((cands[1].queries, cands[1].rank), ([0, 1], 0))

    def test_prefilter(self):
        cases = [(art(1, retracted=True), "retracted"), (art(1, title=" "), "empty title"),
                 (art(1, year=2001), "year"), (art(1, types=("Review",), abstract=""), "no abstract"),
                 (art(1, abstract=""), None), (art(1), None)]
        for a, expected in cases:
            self.assertEqual(G.prefilter(G.Cand(a, [], [], 0), years=10, this_year=2026), expected)
        lang = art(1)
        lang.language = "ger"
        self.assertEqual(G.prefilter(G.Cand(lang, [], [], 0), years=10, this_year=2026), "language")

    def test_selection_order_caps_and_gap(self):
        def cand(i, design, rel=3, year=2024, author=None):
            c = G.Cand(art(i, year=year, authors=[author or f"A{i}"]), [], [], i, local_id=f"P{i}")
            c.decision, c.relevance, c.design = "include", rel, design
            return c
        cfg = G._cfg({"grounding": {"max_sources_per_complaint": 3, "max_per_organisation": 1}})
        cands = [cand(1, "narrative_review"), cand(2, "systematic_review", year=2025), cand(3, "guideline", year=2019),
                 cand(4, "guideline", year=2023, author="NICE"), cand(5, "guideline", year=2022, author="NICE"),
                 cand(6, "guideline", rel=1), cand(7, "rct")]
        chosen, gap = G.select(cands, cfg)
        self.assertIsNone(gap)
        self.assertEqual([c.local_id for c in chosen], ["P4", "P3", "P2"])  # guidelines newest first, NICE once
        only_narrative, gap = G.select([cand(1, "narrative_review"), cand(2, "narrative_review", rel=2)], cfg)
        self.assertEqual([c.local_id for c in only_narrative], ["P1"])  # narrative needs relevance 3
        self.assertIn("No clinical guideline", gap)

    def test_verify_rules(self):
        def vote(verdict, pop="same", over=False, ctx=False):
            return AttestationVote(pair="X1", verdict=verdict, population_check=pop, overgeneralization=over,
                                   context_needed=ctx, reason="")
        self.assertEqual(V.rule_verdict(vote("supported", "different")), "unsupported")
        self.assertEqual(V.rule_verdict(vote("supported", "broader")), "partial")
        self.assertEqual(V.rule_verdict(vote("supported", over=True)), "partial")
        self.assertEqual(V.combine([vote("supported"), vote("supported")]), "supported")
        self.assertEqual(V.combine([vote("unsupported"), vote("supported", "different")]), "unsupported")
        self.assertIsNone(V.combine([vote("supported"), vote("partial")]))
        self.assertIsNone(V.combine([vote("supported"), vote("supported", ctx=True)]))


class GroundingFlowTests(GroundingBase):
    async def test_full_flow_to_knowledge_version_one(self):
        self.models.verdicts = {"Pulsatile": ("unsupported", "unsupported")}
        self.models.extract_extra = [{"condition": "Made-up flag", "features": [], "urgency": "urgent",
                                      "population": "all", "sentence_ids": ["S9.P9.s9"]},  # not in the pack
                                     {"condition": "Sudden hearing loss", "features": [], "urgency": "emergency",
                                      "population": "all", "sentence_ids": ["S1.P1.s1"]}]  # a curated floor rule
        inv_id = self.seed()
        inv = await self.run_inv(inv_id)
        self.assertEqual(inv["knowledge_version"], 1)
        # The pack's unknown rule asks a rule-out question, so the run goes on to questions (1C.4).
        self.assertIn("1C.4", self.db.one("SELECT data FROM events WHERE kind = 'complete'")["data"]["reason"])
        [run] = self.db.query("SELECT * FROM grounding_runs")
        self.assertEqual((run["status"], run["complaint_key"], run["population"], run["knowledge_version"]),
                         ("ok", "tinnitus", "general", 1))
        # Queries recorded; the duplicate paper from two sources became one candidate, selected and read once.
        self.assertEqual(len(self.db.query("SELECT * FROM research_queries WHERE purpose = 'grounding'")), 4)
        self.assertEqual(len(self.db.query("SELECT * FROM candidates")), 1)
        self.assertEqual(self.sources.fetches, ["1001"])
        read = self.db.one("SELECT * FROM paper_reads")
        self.assertEqual((read["purpose"], read["status"], read["scope"]), ("grounding", "read", "full"))
        # Attestation IDs come from the pack shown: the made-up item never became an item.
        texts = {i["text"]: i for i in self.items(inv_id, origin="guideline")}
        self.assertNotIn("Made-up flag", texts)
        flag = texts["Hearing loss that comes on within days"]
        self.assertEqual((flag["kind"], flag["trust"], flag["status"]), ("red_flag", "verified", "active"))
        self.assertEqual(flag["attributes"]["urgency"], "emergency")
        att = self.db.one("SELECT * FROM knowledge_attestations WHERE item_id = ?", (flag["id"],))
        self.assertEqual(att["quote"], "Assess people with tinnitus for sudden hearing loss.")
        self.assertEqual(att["verdict"], "supported")
        # Same name as a curated floor rule → merged into it; the attestation moves to the curated item.
        self.assertEqual(texts["Sudden hearing loss"]["status"], "superseded")
        curated = self.items(inv_id, origin="curated", key="rule:ent.sudden_hearing_loss")
        self.assertEqual(len(curated), 1)
        self.assertEqual(len(self.db.query("SELECT * FROM knowledge_attestations WHERE item_id = ?",
                                           (curated[0]["id"],))), 1)
        self.assertTrue({v["stage"] for v in att["votes"]} == {"verify"})
        # Unsupported attestations reject the item.
        self.assertEqual(texts["Pulsatile tinnitus of vascular cause"]["status"], "rejected")
        # Curated floor and pack items join at version 1; safety ran again on the new knowledge.
        self.assertGreater(len(self.items(inv_id, origin="curated")), 60)
        self.assertTrue(self.items(inv_id, key="pack:tinnitus:tinnitus.one_sided_persistent"))
        self.assertEqual([r["knowledge_version"] for r in self.db.query("SELECT * FROM safety_reviews ORDER BY id")], [0, 1])
        self.assertIn('"item": "K', self.b.requests[-1].user)  # guideline red flags reach the panel
        self.assertEqual(len(self.db.query("SELECT * FROM note_outbox WHERE kind = 'research_plan'")), 1)

    async def test_disagreement_escalates_then_adjudicates(self):
        self.models.verdicts = {"within days": ("supported", "partial")}
        inv_id = self.seed()
        await self.run_inv(inv_id)
        flag = self.items(inv_id, origin="guideline", kind="red_flag")[0]
        votes = self.db.one("SELECT votes FROM knowledge_attestations WHERE item_id = ?", (flag["id"],))["votes"]
        self.assertEqual([v["stage"] for v in votes].count("escalate"), 2)
        self.assertEqual(votes[-1]["stage"], "adjudicate")
        self.assertTrue(self.models.adjudicated)

    async def test_no_guideline_is_a_gap(self):
        self.sources.results = {"europepmc": [art(1, types=("Randomized Controlled Trial",))], "pubmed": []}
        inv_id = self.seed()
        inv = await self.run_inv(inv_id)
        run = self.db.one("SELECT * FROM grounding_runs")
        self.assertEqual(run["status"], "gap")
        self.assertIn("No clinical guideline", run["gap_reason"])
        self.assertEqual(self.sources.fetches, [])
        self.assertEqual(inv["knowledge_version"], 1)  # the merge still runs (curated items)
        self.assertEqual(self.items(inv_id, origin="guideline"), [])

    async def test_every_search_failing_is_a_gap(self):
        self.sources.fail = True
        inv_id = self.seed()
        await self.run_inv(inv_id)
        run = self.db.one("SELECT * FROM grounding_runs")
        self.assertEqual((run["status"], run["gap_reason"]), ("gap", "Every grounding search failed."))
        self.assertTrue(all(q["status"] == "failed" for q in self.db.query("SELECT * FROM research_queries")))

    async def test_failed_task_is_recorded_as_a_gap_and_the_run_continues(self):
        def broken(request):
            if request.schema_name == "ScreeningBatch":
                raise ModelError("config", "down")
            return self.models.handler("openai")(request)
        inv_id = self.seed()
        ctx = self.ctx()
        ctx.providers["openai"] = FakeProvider("openai", broken)
        ctx.providers["anthropic"] = FakeProvider("anthropic", broken)
        pipeline, _ = build_pipeline(ctx)
        worker = Worker(self.db, pipeline, services=ctx, max_parallel=2)
        await worker.machine.advance(inv_id)
        await worker.run_until_idle()
        run = self.db.one("SELECT * FROM grounding_runs")
        self.assertEqual(run["status"], "failed")
        self.assertEqual(self.db.investigation(inv_id)["knowledge_version"], 1)

    async def test_duplicates_across_complaints_merge_and_papers_are_reused(self):
        inv_id = self.seed(complaints=(TINNITUS, DIZZY))
        await self.run_inv(inv_id)
        self.assertEqual(len(self.db.query("SELECT * FROM grounding_runs")), 2)
        self.assertEqual(len(self.db.query("SELECT * FROM paper_reads")), 1)  # the second complaint reused the read
        flags = self.items(inv_id, origin="guideline", text="Hearing loss that comes on within days")
        self.assertEqual(sorted(f["status"] for f in flags), ["active", "superseded"])
        kept = next(f for f in flags if f["status"] == "active")
        self.assertEqual(len(self.db.query("SELECT * FROM knowledge_attestations WHERE item_id = ?", (kept["id"],))), 2)

    async def test_grouping_judge_merges_different_wordings(self):
        self.models.extract_extra = [{"condition": "Pulse-synchronous sound", "features": [], "urgency": "urgent",
                                      "population": "all", "sentence_ids": ["S1.P1.s2"]},
                                     {"condition": "Pulsatile tinnitus heard with the heartbeat", "features": [],
                                      "urgency": "urgent", "population": "all", "sentence_ids": ["S1.P1.s2"]}]
        self.models.merge_words = [("Pulse-synchronous", "heard with the heartbeat")]
        inv_id = self.seed()
        await self.run_inv(inv_id)
        rows = {r["text"]: r["status"] for r in self.items(inv_id, origin="guideline", kind="red_flag")}
        self.assertEqual(sorted([rows["Pulse-synchronous sound"], rows["Pulsatile tinnitus heard with the heartbeat"]]),
                         ["active", "superseded"])
        merges = self.db.one("SELECT data FROM events WHERE kind = 'knowledge'")["data"]
        self.assertGreaterEqual(merges["merges"], 1)

    def test_history_items_are_questions(self):
        self.assertEqual(G.history_kind("history_item", "Duration of tinnitus"), "history_item")
        self.assertEqual(G.history_kind("history_item", "Physical and neurological examination"), "evaluation")
        self.assertEqual(G.history_kind("history_item", "Audiological testing"), "evaluation")
        self.assertIsNone(G.history_kind("history_item", "Clinical history"))
        self.assertEqual(G.history_kind("red_flag", "Clinical history"), "red_flag")

    async def test_a_new_complaint_grounds_only_itself(self):
        inv_id = self.seed()
        await self.run_inv(inv_id)
        searches = len(self.sources.searches)
        self.db.update_investigation(inv_id, status="active", outcome=None, stage="safety",
                                     complaints=[TINNITUS, DIZZY])
        await self.worker.machine.advance(inv_id)
        await self.worker.run_until_idle()
        keys = [t["key"] for t in self.db.tasks(inv_id, step="ground")]
        self.assertEqual(keys, ["ground:tinnitus:general", "ground:dizziness:general"])
        self.assertEqual(len(self.sources.searches) - searches, 4)
        self.assertEqual(self.db.investigation(inv_id)["knowledge_version"], 2)

    async def test_population_change_grounds_again(self):
        inv_id = self.seed()
        await self.run_inv(inv_id)
        self.db.update_investigation(inv_id, status="active", outcome=None, stage="safety",
                                     special_populations=["pregnancy"])
        await self.worker.machine.advance(inv_id)
        await self.worker.run_until_idle()
        self.assertIn("ground:tinnitus:pregnancy", [t["key"] for t in self.db.tasks(inv_id, step="ground")])
        self.assertIn('TITLE_ABS:"pregnant"', self.sources.searches[-4][1] + self.sources.searches[-3][1])

    async def test_grounding_disabled_skips_the_stage(self):
        inv_id = self.seed(settings={"grounding": {**S.build("quality").grounding, "enabled": False}})
        await self.run_inv(inv_id)
        self.assertEqual(self.db.query("SELECT * FROM grounding_runs"), [])
        self.assertEqual(self.sources.searches, [])


class LibraryTests(ResearchTestCase):
    async def test_fetch_uses_the_library_first_and_tags_the_paper(self):
        lib = LiteratureDB(self.tmp / "literature.db")
        a = art(5)
        lit_id, _ = lib.save_article(a, details=Details(full_text=[Section(title="Body", text="Saved text.")]))
        fetched = await LiteratureSources(lib).fetch(a, investigation_id=7)  # network is blocked: library only
        self.assertEqual((fetched.literature_id, fetched.from_library), (lit_id, True))
        self.assertEqual(fetched.details.full_text[0].text, "Saved text.")
        self.assertIn("inv:7", [t.name for t in lib.get(lit_id).tags])


if __name__ == "__main__":
    import unittest
    unittest.main()
