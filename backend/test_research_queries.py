"""1D.2 queries (PLAN B8.7). Synthetic text; fake providers and sources; no network."""

import json

from research_testkit import ResearchTestCase

from app.research import llm, settings as S
from app.research.prices import PriceTable
from app.research.providers import FakeProvider
from app.research.ratelimit import Limiters
from app.research.runner import Worker
from app.research.steps import build_pipeline, queries as Q

TINNITUS = {"key": "tinnitus", "text": "Ringing in the left ear", "seg_ids": ["U2"],
            "mesh": [{"ui": "D014012", "name": "Tinnitus", "tree_numbers": [], "systems": ["ent"]}], "mesh_dropped": []}
GOOD = [
    {"source": "europepmc", "query": '(TITLE_ABS:"tinnitus" OR MESH:"Tinnitus") AND PUB_TYPE:"practice guideline"',
     "query_type": "guideline", "rationale": "guidelines"},
    {"source": "europepmc", "query": '(TITLE_ABS:"tinnitus") AND PUB_TYPE:"systematic review"',
     "query_type": "systematic_review", "rationale": "reviews"},
    {"source": "pubmed", "query": '"tinnitus"[mh] AND ("2016"[dp] : "3000"[dp])', "query_type": "other",
     "rationale": "primary"},
]


class FakeSources:
    def __init__(self):
        self.hits = {}  # query → int or Exception (or a list consumed in order)
        self.default = 120
        self.counts = []

    async def count(self, source, query):
        self.counts.append((source, query))
        value = self.hits.get(query, self.default)
        if isinstance(value, list):
            value = value.pop(0) if len(value) > 1 else value[0]
        if isinstance(value, Exception):
            raise value
        return value


class Models:
    def __init__(self):
        self.queryset = lambda request, n: {"queries": [dict(q) for q in GOOD]}
        self.refine = lambda request, n: {"query": '"tinnitus"[tiab]', "rationale": "broader"}
        self.n = {"QuerySet": 0, "QueryRefinement": 0}
        self.users = {"QuerySet": [], "QueryRefinement": []}

    def handler(self, family):
        def h(request):
            name = request.schema_name
            self.n[name] = self.n.get(name, 0) + 1
            self.users.setdefault(name, []).append(request.user)
            if name == "QuerySet":
                return self.queryset(request, self.n[name])
            if name == "QueryRefinement":
                return self.refine(request, self.n[name])
            raise AssertionError(name)
        return h


class QueryBase(ResearchTestCase):
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

    def seed(self, hyps=("tinnitus_noise",), settings=None, rnd=1):
        snap = {"search": {"sources": ["europepmc", "pubmed", "openalex"], "max_queries_per_hypothesis": 12,
                           "max_hits_per_query": 50000, "max_refinements": 3}, **(settings or {})}
        inv_id = self.db.create_investigation("Synthetic case. Ringing in my left ear.", profile="quality",
                                              settings_snapshot=snap, synthetic=True)
        with self.db.tx() as conn:
            conn.execute("INSERT INTO mesh_cache (term, descriptor_ui, name, entry_terms, fetched_at) VALUES "
                         "('tinnitus', 'D014012', 'Tinnitus', ?, '2026-11-26')",
                         (json.dumps(["Ringing-Buzzing Tinnitus", "Tinnitus, Pulsatile"]),))
            item = self.db.insert(conn, "knowledge_items", {
                "investigation_id": inv_id, "version": 1, "kind": "differential", "key": "noise", "text":
                "Noise-induced hearing loss", "attributes": {"key_features": ["noise exposure"]}, "population": "all",
                "origin": "guideline", "trust": "verified"})
            self.hyp_ids = {}
            for i, key in enumerate(hyps):
                self.hyp_ids[key] = self.db.insert(conn, "hypotheses", {
                    "investigation_id": inv_id, "key": key, "title": key.replace("_", " "), "kind": "common",
                    "item_id": item if i == 0 else None, "discriminators": ["noise exposure"], "prior": "moderate"})
            self.db.update_investigation(inv_id, conn=conn, stage="querying", knowledge_version=1, round=rnd,
                                         complaints=[TINNITUS])
        return inv_id

    async def run_inv(self, inv_id):
        ctx = self.ctx()
        pipeline, _ = build_pipeline(ctx)
        self.worker = Worker(self.db, pipeline, services=ctx, max_parallel=2)
        await self.worker.machine.advance(inv_id)
        await self.worker.run_until_idle()
        return self.db.investigation(inv_id)

    def rows(self, inv_id):
        return self.db.query("SELECT * FROM research_queries WHERE investigation_id = ? ORDER BY id", (inv_id,))

    def reason(self, inv_id):
        row = self.db.one("SELECT data FROM events WHERE investigation_id = ? AND kind = 'complete' ORDER BY id DESC",
                          (inv_id,))
        return (row["data"] or {}).get("reason", "") if row else ""


class CheckTests(QueryBase):
    def test_allowlists_and_balance(self):
        ok = [("europepmc", '(TITLE_ABS:"a b" OR MESH:"Tinnitus") AND PUB_YEAR:[2015 TO 2026] AND LANG:eng'),
              ("europepmc", 'TITLE:"x: y" AND PUB_TYPE:"randomized controlled trial"'),  # a colon inside quotes
              ("pubmed", '("a"[tiab] OR "b"[mh]) AND systematic review[pt] AND ("2016"[dp] : "3000"[dp])')]
        for source, q in ok:
            self.assertIsNone(Q.check_query(source, q), q)
        bad = [("europepmc", 'MESH_HEADING:"Tinnitus"', "fields not allowed: MESH_HEADING"),
               ("europepmc", 'TITLE_ABS:"a" AND SRC:MED', "SRC"),
               ("europepmc", 'TITLE_ABS:"a" AND PUB_TYPE:"letter"', "publication type"),
               ("europepmc", 'TITLE_ABS:"a', "quotes"),
               ("europepmc", '(TITLE_ABS:"a"', "unbalanced ()"),
               ("pubmed", '"a"[all fields]', "tags not allowed"),
               ("pubmed", '"a"[tiab]) AND (b', "unbalanced ()")]
        for source, q, msg in bad:
            self.assertIn(msg, Q.check_query(source, q) or "", q)

    def test_select_drops_other_sources_duplicates_repeats_and_extra(self):
        qs = [Q.SearchQuery(source=s, query=q, query_type="other") for s, q in [
            ("openalex", "tinnitus"), ("pubmed", '"a"[tiab]'), ("pubmed", ' "A"[TIAB] '), ("pubmed", '"b"[tiab]'),
            ("europepmc", 'TITLE:"c"'), ("pubmed", '"d"[tiab]')]]
        kept, dropped = Q.select(qs, allowed=["europepmc", "pubmed"], earlier={("pubmed", '"b"[tiab]')}, limit=2)
        self.assertEqual([q.query for q in kept], ['"a"[tiab]', 'TITLE:"c"'])
        self.assertEqual(dropped, {"source": 1, "duplicate": 2, "over_limit": 1})


class QueryFlowTests(QueryBase):
    async def test_one_task_per_hypothesis_with_vocabulary_and_hit_counts(self):
        inv_id = self.seed(hyps=("noise_damage", "ear_wax"))
        inv = await self.run_inv(inv_id)
        self.assertEqual(self.models.n["QuerySet"], 2)
        self.assertIn("1D.3", self.reason(inv_id))  # on to searching (built in 1D.3)
        first = self.models.users["QuerySet"][0] + self.models.users["QuerySet"][1]
        for term in ("Ringing-Buzzing Tinnitus", "Noise-induced hearing loss", "noise exposure",
                     "pulsatile tinnitus", "required_query_types"):  # MeSH entry terms, item, pack synonyms
            self.assertIn(term, first)
        rows = self.rows(inv_id)
        # The second hypothesis proposed the same three queries: they are repeats, dropped once committed.
        ready = [r for r in rows if r["status"] == "ready"]
        self.assertEqual(len(ready), 3)
        self.assertEqual({r["hit_count"] for r in ready}, {120})
        # Whether they are dropped before counting or at commit depends on timing; either way none runs twice.
        self.assertTrue(all(r["error"] == "repeat of a query already run" for r in rows if r["status"] != "ready"))
        self.assertTrue(all(r["purpose"] == "research" and r["round"] == 1 for r in rows))
        self.assertNotIn("SRC:", " ".join(r["query"] for r in rows))  # scope is added when searching
        note = self.db.one("SELECT * FROM note_outbox WHERE investigation_id = ? AND kind = 'search_log'", (inv_id,))
        self.assertEqual(note["artifact_key"], "search:r1")
        ev = self.db.query("SELECT data FROM events WHERE investigation_id = ? AND kind = 'queries'", (inv_id,))
        self.assertNotIn("tinnitus", json.dumps([e["data"] for e in ev]).lower())  # counts only

    async def test_a_field_outside_the_allowlist_is_repaired(self):
        def qs(request, n):
            out = [dict(q) for q in GOOD]
            if n == 1:
                out[0]["query"] = 'MESH_HEADING:"Tinnitus" AND PUB_TYPE:"guideline"'
            return {"queries": out}
        self.models.queryset = qs
        inv_id = self.seed()
        await self.run_inv(inv_id)
        self.assertEqual(self.models.n["QuerySet"], 2)
        self.assertIn("MESH_HEADING", self.models.users["QuerySet"][1])
        self.assertEqual(len([r for r in self.rows(inv_id) if r["status"] == "ready"]), 3)

    async def test_round_one_needs_guideline_and_review_queries(self):
        def qs(request, n):
            return {"queries": [dict(GOOD[2])] if n == 1 else [dict(q) for q in GOOD]}
        self.models.queryset = qs
        inv_id = self.seed()
        await self.run_inv(inv_id)
        self.assertEqual(self.models.n["QuerySet"], 2)
        self.assertIn("needs a guideline query", self.models.users["QuerySet"][1])

    async def test_later_rounds_need_no_query_types(self):
        self.models.queryset = lambda request, n: {"queries": [dict(GOOD[2])]}
        inv_id = self.seed(rnd=2)
        await self.run_inv(inv_id)
        self.assertEqual(self.models.n["QuerySet"], 1)
        self.assertEqual(self.rows(inv_id)[0]["round"], 2)

    async def test_zero_hits_is_refined_then_dropped_after_the_limit(self):
        self.sources.hits[GOOD[0]["query"]] = 0
        self.sources.hits['"tinnitus"[tiab]'] = 0  # each refinement still finds nothing
        n = {"i": 0}

        def refine(request, k):
            n["i"] += 1
            return {"query": f'TITLE_ABS:"tinnitus {n["i"]}"', "rationale": "broader"}
        self.models.refine = refine
        self.sources.hits.update({f'TITLE_ABS:"tinnitus {i}"': 0 for i in range(1, 5)})
        inv_id = self.seed(settings={"search": {"sources": ["europepmc", "pubmed"], "max_refinements": 2}})
        await self.run_inv(inv_id)
        row = next(r for r in self.rows(inv_id) if r["query_type"] == "guideline")
        self.assertEqual((row["status"], row["attempts"], row["origin"], row["error"]),
                         ("dropped", 2, "refined", "0 hits"))
        self.assertEqual(self.models.n["QueryRefinement"], 2)
        self.assertIn("0 hits", self.models.users["QueryRefinement"][0])

    async def test_refinement_fixes_zero_hits_and_too_many(self):
        self.sources.hits[GOOD[0]["query"]] = 0
        self.sources.hits[GOOD[1]["query"]] = 90000
        answers = iter([{"query": 'TITLE_ABS:"tinnitus" AND PUB_TYPE:"guideline"', "rationale": "broader"},
                        {"query": 'TITLE_ABS:"tinnitus" AND PUB_TYPE:"meta-analysis"', "rationale": "narrower"}])
        self.models.refine = lambda request, k: next(answers)
        inv_id = self.seed()
        await self.run_inv(inv_id)
        rows = {r["query_type"]: r for r in self.rows(inv_id)}
        for kind in ("guideline", "systematic_review"):
            self.assertEqual((rows[kind]["status"], rows[kind]["origin"], rows[kind]["attempts"]),
                             ("ready", "refined", 1))
        self.assertEqual(rows["other"]["origin"], "model")

    async def test_still_too_many_hits_is_kept(self):
        self.sources.default = 90000
        self.models.refine = lambda request, k: {"query": f'"tinnitus {k}"[tiab]', "rationale": "narrower"}
        inv_id = self.seed(settings={"search": {"sources": ["pubmed"], "max_refinements": 1}})
        self.models.queryset = lambda request, n: {"queries": [
            {"source": "pubmed", "query": '"tinnitus"[mh] AND practice guideline[pt]', "query_type": "guideline"},
            {"source": "pubmed", "query": '"tinnitus"[mh] AND systematic review[pt]', "query_type": "systematic_review"}]}
        await self.run_inv(inv_id)
        for r in self.rows(inv_id):
            self.assertEqual((r["status"], r["error"]), ("ready", "too many hits after refinement"))

    async def test_a_rejected_query_is_refined_and_a_network_failure_is_left_to_search(self):
        self.sources.hits[GOOD[0]["query"]] = ValueError("Search failed: bad syntax")
        self.sources.hits[GOOD[2]["query"]] = RuntimeError("timeout")
        self.models.refine = lambda request, k: {"query": 'TITLE_ABS:"tinnitus" AND PUB_TYPE:"guideline"',
                                                 "rationale": "fixed"}
        inv_id = self.seed()
        await self.run_inv(inv_id)
        self.assertIn("rejected by europepmc", self.models.users["QueryRefinement"][0])
        rows = {r["query_type"]: r for r in self.rows(inv_id)}
        self.assertEqual((rows["guideline"]["status"], rows["guideline"]["origin"]), ("ready", "refined"))
        self.assertEqual((rows["other"]["status"], rows["other"]["hit_count"], rows["other"]["error"]),
                         ("ready", None, "count failed: RuntimeError"))

    async def test_a_refinement_repeating_an_attempt_is_repaired(self):
        self.sources.hits[GOOD[0]["query"]] = 0
        answers = iter([{"query": GOOD[0]["query"], "rationale": "same"},
                        {"query": 'TITLE_ABS:"tinnitus" AND PUB_TYPE:"guideline"', "rationale": "broader"}])
        self.models.refine = lambda request, k: next(answers)
        inv_id = self.seed()
        await self.run_inv(inv_id)
        self.assertIn("repeats an earlier attempt", self.models.users["QueryRefinement"][1])

    async def test_one_hypothesis_failing_is_skipped(self):
        def qs(request, n):
            if '"title": "ear wax"' in request.user:
                raise AssertionError("model down")
            return {"queries": [dict(q) for q in GOOD]}
        self.models.queryset = qs
        inv_id = self.seed(hyps=("noise_damage", "ear_wax", "third"))
        inv = await self.run_inv(inv_id)
        tasks = {t["key"]: t["status"] for t in self.db.query(
            "SELECT key, status FROM research_tasks WHERE investigation_id = ? AND step = 'queries'", (inv_id,))}
        self.assertEqual(tasks["queries:r1:ear_wax"], "skipped")
        self.assertIn("1D.3", self.reason(inv_id))  # 1 of 3 skipped: the run goes on

    async def test_retired_hypotheses_get_no_queries(self):
        inv_id = self.seed(hyps=("noise_damage", "ear_wax"))
        with self.db.tx() as conn:
            conn.execute("UPDATE hypotheses SET status = 'retired' WHERE key = 'ear_wax'")
        await self.run_inv(inv_id)
        self.assertEqual(self.models.n["QuerySet"], 1)


class LiteratureCountTests(ResearchTestCase):
    async def test_counts_are_scoped_and_cheap(self):
        import httpx
        from app.research import transport
        from app.research.sources import LiteratureSources
        seen = []

        def handler(request):
            seen.append(request.url)
            if "ebi.ac.uk" in request.url.host:
                return httpx.Response(200, json={"hitCount": 42, "resultList": {"result": []}})
            return httpx.Response(200, json={"esearchresult": {"count": "7", "idlist": []}})
        with transport.use_transport(httpx.MockTransport(handler)):
            src = LiteratureSources(library=object())
            self.assertEqual(await src.count("europepmc", 'TITLE_ABS:"tinnitus"'), 42)
            self.assertEqual(await src.count("pubmed", '"tinnitus"[mh]'), 7)
        epmc, pubmed = seen
        self.assertIn("(SRC:MED OR SRC:PMC)", epmc.params["query"])  # B8.7 scope
        self.assertEqual((epmc.params["resultType"], epmc.params["pageSize"]), ("idlist", "1"))
        self.assertEqual(pubmed.params["retmax"], "0")
