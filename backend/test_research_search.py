"""1D.3 search, seed, prefilter, screening, selection and fetch (PLAN B8.8, B8.10–B8.13). Synthetic text; fake
providers and sources; no network."""

import json
import re

from research_testkit import ResearchTestCase

from app.literature.models import Article, Details, Section
from app.research import llm, settings as S
from app.research.knowledge import loader
from app.research.prices import PriceTable
from app.research.providers import FakeProvider
from app.research.ratelimit import Limiters
from app.research.runner import Worker
from app.research.schemas import CaseBrief
from app.research.segment import user_segments
from app.research.sources import Fetched
from app.research.steps import build_pipeline, intake, screen as SC, search as SE

TEXT = "Synthetic case. I am a 40-year-old man with ringing in my left ear for a month."
TINNITUS = {"key": "tinnitus", "text": "Ringing in the left ear", "seg_ids": ["U2"],
            "mesh": [{"ui": "D014012", "name": "Tinnitus", "tree_numbers": [], "systems": ["ent"]}], "mesh_dropped": []}
BRIEF = {"chief_complaint": {"text": "Ringing in the left ear", "seg_ids": ["U2"]},
         "demographics": {"age": {"text": "40", "seg_ids": ["U2"], "years": 40}},
         "case_features": [{"key": "left_tinnitus", "text": "Ringing in the left ear", "seg_ids": ["U2"]}]}
PACK_SEEDS = {s.pmid for s in loader.packs_for(["D014012"])[0].seed_literature if s.pmid}
NO_PACK = {"key": "ear_noise", "text": "Ear noise", "seg_ids": ["U2"], "mesh": [], "mesh_dropped": []}


def art(n, source="europepmc", **kw):
    base = {"source": source, "source_id": f"MED:{n}" if source == "europepmc" else str(n), "pmid": str(n),
            "title": f"Study {n} of tinnitus", "abstract": f"Abstract {n}.", "year": 2022, "language": "eng",
            "publication_types": ["Journal Article"]}
    base.update(kw)
    return Article(**base)


class FakeSources:
    def __init__(self):
        self.by_source = {"europepmc": [art(1), art(2)], "pubmed": [art(1, "pubmed"), art(3, "pubmed")]}
        self.by_query = {}
        self.fail_queries = set()
        self.fail_fetch = set()
        self.searches, self.fetches = [], []

    async def search(self, source, query, max_results):
        self.searches.append((source, query))
        if query in self.fail_queries or "*" in self.fail_queries:
            raise RuntimeError("down")
        if "[pmid]" in query:
            found = [art(int(p), "pubmed", title=f"Seed {p}") for p in re.findall(r"(\d+)\[pmid\]", query)]
        else:
            found = self.by_query.get(query, self.by_source.get(source, []))
        return len(found) * 10, found[:max_results]

    async def fetch(self, article, investigation_id):
        self.fetches.append(article.pmid)
        if article.pmid in self.fail_fetch:
            raise RuntimeError("no full text")
        details = Details(full_text=[Section(title="Results", text="Synthetic results.")], full_text_from="pmc")
        return Fetched(1000 + int(article.pmid), article, details)


class Models:
    def __init__(self):
        self.screen = self.include_all
        self.n = 0
        self.users = []
        self.fail_family = None

    @staticmethod
    def include_all(request, family):
        ids = re.findall(r"^(P\d+): ", request.user, re.M)
        return {"items": [{"paper_id": i, "relevance": 3 if family == "openai" else 2,
                           "hypothesis_keys": ["noise"] if family == "openai" else ["noise", "made_up"],
                           "design": "cohort", "population_note": "adults", "decision": "include",
                           "reason": f"r-{family}"} for i in ids]}

    def handler(self, family):
        def h(request):
            if request.schema_name != "ScreeningBatch":
                raise AssertionError(request.schema_name)
            self.n += 1
            self.users.append(request.user)
            return self.screen(request, family)
        return h


class SearchBase(ResearchTestCase):
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

    def seed(self, queries=(("europepmc", "q1"), ("pubmed", "q2")), settings=None, stage="searching",
             grounding=(), pack=True):
        snap = S.build("quality").model_dump(mode="json")
        for k, v in (settings or {}).items():
            snap[k] = {**snap.get(k, {}), **v} if isinstance(v, dict) else v
        inv_id = self.db.create_investigation(TEXT, profile="quality", settings_snapshot=snap, synthetic=True)
        with self.db.tx() as conn:
            intake.save_segments(conn, inv_id, user_segments(TEXT))
            intake.save_brief(self.db, conn, inv_id, CaseBrief.model_validate(BRIEF), source="intake:p1", check=None,
                              prompt_version="test")
            for key in ("noise", "wax"):
                self.db.insert(conn, "hypotheses", {"investigation_id": inv_id, "key": key, "title": key,
                                                    "kind": "common", "prior": "moderate"})
            for source, q in queries:
                self.db.insert(conn, "research_queries", {"investigation_id": inv_id, "round": 1, "source": source,
                                                          "query": q, "query_type": "other", "status": "ready"})
            for g in grounding:
                self.db.insert(conn, "candidates", {
                    "investigation_id": inv_id, "round_found": 1, "purpose": "grounding", "source": "pubmed",
                    "source_id": g, "pmid": g, "title": f"Guideline {g}", "abstract": "", "year": 2024,
                    "pub_types": ["Practice Guideline"], "found_via": "grounding", "status": "selected",
                    "literature_id": 77})
            self.db.update_investigation(inv_id, conn=conn, stage=stage, knowledge_version=1, complaints=[TINNITUS if pack else NO_PACK])
        return inv_id

    async def run_inv(self, inv_id, stop_at=None):
        ctx = self.ctx()
        pipeline, _ = build_pipeline(ctx, stop_at=stop_at)
        self.worker = Worker(self.db, pipeline, services=ctx, max_parallel=2)
        await self.worker.machine.advance(inv_id)
        await self.worker.run_until_idle()
        return self.db.investigation(inv_id)

    def cands(self, inv_id):
        return {r["pmid"]: r for r in self.db.query(
            "SELECT c.*, s.prefilter_reason, s.relevance, s.decision, s.votes, s.hypothesis_keys, s.design, s.score, "
            "s.score_breakdown, s.selected_round, s.selected_reason FROM candidates c LEFT JOIN screenings s "
            "ON s.candidate_id = c.id WHERE c.investigation_id = ? AND c.purpose = 'research'", (inv_id,))}

    def reason(self, inv_id):
        row = self.db.one("SELECT data FROM events WHERE investigation_id = ? AND kind = 'complete' ORDER BY id DESC",
                          (inv_id,))
        return (row["data"] or {}).get("reason", "") if row else ""


class SearchAndSeedTests(SearchBase):
    async def test_searches_dedupe_and_seed(self):
        inv_id = self.seed(grounding=("555",))
        await self.run_inv(inv_id, stop_at="prefiltering")
        c = self.cands(inv_id)
        self.assertEqual(set(c), {"1", "2", "3", "555"} | PACK_SEEDS)
        self.assertEqual(sorted(c["1"]["sources"]), ["europepmc", "pubmed"])  # same PMID from both sources
        self.assertEqual(len(c["1"]["query_ids"]), 2)
        self.assertEqual(c["1"]["first_rank"], 1)
        self.assertEqual(c["555"]["found_via"], "grounding")
        self.assertEqual(c["555"]["literature_id"], 77)
        self.assertEqual(c["42861423"]["found_via"], "seed")
        qs = self.db.query("SELECT status, hit_count FROM research_queries WHERE investigation_id = ?", (inv_id,))
        self.assertEqual({(q["status"], q["hit_count"]) for q in qs}, {("done", 20)})
        seed_search = [q for s, q in self.sources.searches if "[pmid]" in q]
        self.assertEqual(len(seed_search), 1)
        events = self.db.query("SELECT data FROM events WHERE investigation_id = ? AND kind IN ('search', 'seed')",
                               (inv_id,))
        self.assertNotIn("Study", json.dumps([e["data"] for e in events]))  # counts, never titles

    async def test_title_and_year_match_and_seed_status_is_kept(self):
        self.sources.by_source = {"europepmc": [art(42861423, title="Seed 42861423", abstract="Full abstract.")],
                                  "pubmed": [art(9, "pubmed", pmid=None, source_id="X9", title="Study 1 of tinnitus"),
                                             art(1, "pubmed", abstract="")]}
        inv_id = self.seed()
        await self.run_inv(inv_id, stop_at="prefiltering")
        c = self.cands(inv_id)
        self.assertEqual(c["42861423"]["found_via"], "seed")  # found by search and seed: seed wins
        self.assertEqual(c["42861423"]["abstract"], "Full abstract.")  # the seed record had none
        self.assertNotIn(None, c)  # no PMID, same title and year as PMID 1 → merged into it
        self.assertEqual(sorted(c["1"]["sources"]), ["pubmed"])

    async def test_one_search_failing_is_recorded_and_the_run_goes_on(self):
        self.sources.fail_queries = {"q1"}
        inv_id = self.seed()
        inv = await self.run_inv(inv_id, stop_at="prefiltering")
        q1 = self.db.one("SELECT * FROM research_queries WHERE investigation_id = ? AND query = 'q1'", (inv_id,))
        self.assertEqual(q1["status"], "failed")
        self.assertIn("prefiltering", self.reason(inv_id))

    async def test_every_search_failing_fails(self):
        self.sources.fail_queries = {"*"}
        inv_id = self.seed()
        inv = await self.run_inv(inv_id, stop_at="prefiltering")
        self.assertEqual(inv["status"], "failed")
        self.assertIn("Every literature search failed", inv["error"])


class PrefilterTests(SearchBase):
    def test_exclusion_order(self):
        f = SE._filters({"filters": {"languages": ["eng"], "year_from": 2010, "year_to": None,
                                     "exclude_pub_types": ["Editorial"]}})
        row = {"language": "eng", "year": 2020, "pub_types": [], "retracted": 0, "abstract": "a", "title": "t",
               "found_via": "search"}
        cases = [({"language": "ger", "year": 2000}, "language"), ({"year": 2000, "retracted": 1}, "year"),
                 ({"pub_types": ["Editorial"], "retracted": 1}, "publication type"), ({"retracted": 1}, "retracted"),
                 ({"abstract": ""}, "no abstract"), ({"title": " "}, "empty title"),
                 ({"abstract": "", "pub_types": ["Practice Guideline"]}, None),
                 ({"abstract": "", "found_via": "seed"}, None), ({"language": ""}, None), ({}, None)]
        for change, expected in cases:
            self.assertEqual(SE.exclusion({**row, **change}, f), expected, change)

    async def test_prefilter_writes_reasons_and_caps_round_robin(self):
        self.sources.by_source = {"europepmc": [art(1), art(2), art(3, language="fre")],
                                  "pubmed": [art(4, "pubmed"), art(5, "pubmed")]}
        inv_id = self.seed(settings={"guards": {"max_screened": 3}}, pack=False, grounding=("555",))
        await self.run_inv(inv_id, stop_at="screening")
        c = self.cands(inv_id)
        self.assertEqual((c["3"]["status"], c["3"]["prefilter_reason"]), ("excluded", "language"))
        # The grounding source first (rank 0), then rank 1 of each query, then rank 2: 3 places.
        to_screen = {p for p, r in c.items() if r["status"] == "to_screen"}
        self.assertEqual(to_screen, {"555", "1", "4"})
        self.assertEqual({c["2"]["prefilter_reason"], c["5"]["prefilter_reason"]}, {"cap"})


class ScreeningTests(SearchBase):
    async def test_dual_screening_combines_votes_and_scores(self):
        self.sources.by_source = {"europepmc": [art(1, publication_types=["Randomized Controlled Trial"]), art(2)],
                                  "pubmed": []}
        inv_id = self.seed(pack=False, queries=(("europepmc", "q1"),))
        await self.run_inv(inv_id, stop_at="selecting")
        c = self.cands(inv_id)["1"]
        self.assertEqual(c["status"], "screened")
        self.assertEqual(c["relevance"], 3)  # max of 3 and 2
        self.assertEqual(c["hypothesis_keys"], ["noise"])  # merged; unknown keys dropped
        self.assertEqual(c["design"], "rct")  # publication type beats the model's "cohort"
        self.assertEqual(c["decision"], "include")
        self.assertEqual({v["family"] for v in c["votes"]}, {"openai", "anthropic"})
        self.assertAlmostEqual(c["score"], sum(c["score_breakdown"].values()), places=3)
        self.assertAlmostEqual(c["score_breakdown"]["relevance"], 0.45)
        self.assertAlmostEqual(c["score_breakdown"]["design"], 0.17)
        self.assertIn("Abstract 1.", self.models.users[0])  # full abstracts

    async def test_a_missing_item_is_repaired_and_a_failed_batch_retried_once(self):
        calls = {"n": 0}

        def screen(request, family):
            calls["n"] += 1
            out = Models.include_all(request, family)
            if family == "openai" and calls["n"] <= 2:
                out["items"] = out["items"][:-1]  # first answer misses a paper
            return out
        self.models.screen = screen
        self.sources.by_source = {"europepmc": [art(1)], "pubmed": []}
        inv_id = self.seed(pack=False, queries=(("europepmc", "q1"),))
        await self.run_inv(inv_id, stop_at="selecting")
        self.assertTrue(any("one item per paper" in u for u in self.models.users))
        self.assertEqual(self.cands(inv_id)["1"]["status"], "screened")

    async def test_screening_failing_twice_marks_the_papers(self):
        def screen(request, family):
            raise AssertionError("down")
        self.models.screen = screen
        self.sources.by_source = {"europepmc": [art(1)], "pubmed": []}
        inv_id = self.seed(pack=False, queries=(("europepmc", "q1"),), settings={"ensembles": {"dual_screening": False}})
        await self.run_inv(inv_id, stop_at="selecting")
        keys = [t["key"] for t in self.db.query("SELECT key FROM research_tasks WHERE investigation_id = ? AND "
                                                "step = 'screen'", (inv_id,))]
        self.assertEqual(len([k for k in keys if k.endswith(":r1")]), 1)  # retried once
        self.assertEqual(self.cands(inv_id)["1"]["status"], "screen_failed")
        self.assertIn("selecting", self.reason(inv_id))

    async def test_single_screening_when_dual_is_off(self):
        self.sources.by_source = {"europepmc": [art(1)], "pubmed": []}
        inv_id = self.seed(pack=False, queries=(("europepmc", "q1"),), settings={"ensembles": {"dual_screening": False}})
        await self.run_inv(inv_id, stop_at="selecting")
        self.assertEqual(self.models.n, 1)  # one batch, one model
        self.assertEqual(len(self.cands(inv_id)["1"]["votes"]), 1)


class SelectionTests(SearchBase):
    def pool(self):
        def c(i, score, keys, ft=0, via="search"):
            return {"id": i, "score": score, "hypothesis_keys": keys, "full_text_likely": ft, "found_via": via}
        return [c(1, .90, ["a"]), c(2, .88, ["a"]), c(3, .87, ["a"]), c(4, .86, ["a"], ft=1), c(5, .60, ["b"]),
                c(6, .50, ["a", "b"], via="grounding"), c(7, .40, [])]

    def test_grounding_then_quota_round_robin_then_fill_with_full_text_tiebreak(self):
        cfg = {"min_papers_per_hypothesis": 2, "full_text_tiebreak": 0.05}
        got = SC.plan_selection(self.pool(), ["a", "b", "c"], {}, 5, cfg)
        self.assertEqual([(c["id"], r) for c, r in got],
                         [(6, "grounding source"), (1, "quota:a"), (5, "quota:b"), (4, "score (full-text tiebreak)"),
                          (2, "score")])

    def test_slots_cap_the_quota_pass(self):
        got = SC.plan_selection(self.pool(), ["a", "b"], {}, 2, {"min_papers_per_hypothesis": 3,
                                                                  "full_text_tiebreak": 0.05})
        self.assertEqual([c["id"] for c, _ in got], [6, 1])

    def test_papers_already_selected_count_towards_quotas(self):
        got = SC.plan_selection(self.pool(), ["a", "b"], {"b": 5}, 3, {"min_papers_per_hypothesis": 2,
                                                                        "full_text_tiebreak": 0.0})
        self.assertEqual([r for _, r in got], ["grounding source", "quota:a", "score"])

    async def test_select_then_fetch_with_a_replacement(self):
        self.sources.by_source = {"europepmc": [art(i) for i in range(1, 6)], "pubmed": []}  # 4 slots, 5 papers
        self.sources.fail_fetch = {"1"}
        inv_id = self.seed(pack=False, queries=(("europepmc", "q1"),),
                           settings={"selection": {"max_fulltext_papers_per_round": 4}})
        await self.run_inv(inv_id, stop_at="reading")
        c = self.cands(inv_id)
        statuses = {p: r["status"] for p, r in c.items()}
        self.assertEqual(statuses["1"], "replaced")
        self.assertEqual(sum(1 for s in statuses.values() if s == "fetched"), 4)
        replacement = [r for r in c.values() if (r["selected_reason"] or "").startswith("replacement")]
        self.assertEqual(len(replacement), 1)
        self.assertEqual(replacement[0]["literature_id"], 1000 + int(replacement[0]["pmid"]))
        self.assertIn("reading", self.reason(inv_id))
        ev = self.db.one("SELECT data FROM events WHERE investigation_id = ? AND kind = 'select'", (inv_id,))
        self.assertEqual(ev["data"]["selected"], 4)

    async def test_the_total_paper_guard_limits_selection(self):
        self.sources.by_source = {"europepmc": [art(1), art(2), art(3)], "pubmed": []}
        inv_id = self.seed(pack=False, queries=(("europepmc", "q1"),), settings={"guards": {"max_total_papers": 2}})
        await self.run_inv(inv_id, stop_at="fetching")
        self.assertEqual(sum(1 for r in self.cands(inv_id).values() if r["status"] == "selected"), 2)
