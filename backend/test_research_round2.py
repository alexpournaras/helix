"""The second round from the pool (PLAN B17.5 item 9, task 1R.6): the decision, the carry-over, queries for the new
hypotheses only, targeted citation chasing, and selection sized to the pool."""

from app.literature.models import Article, Ref
from app.research import budgets, llm, settings as S
from app.research.prices import PriceTable
from app.research.providers import FakeProvider
from app.research.ratelimit import Limiters
from app.research.runner import Worker
from app.research.steps import build_pipeline, search as SE
from test_research_gaps import GapsFlowTests, hyp, query, report


class Round2Base(GapsFlowTests):
    test_gaps_round_is_reported_and_m1_stops = None  # the inherited quality test isn't run here

    def seed(self, used=0):
        inv_id = super().seed()
        snap = S.build("budget").model_dump(mode="json")
        with self.db.tx() as conn:
            conn.execute("UPDATE investigations SET settings_snapshot = ?, profile = 'budget' WHERE id = ?",
                         (__import__("json").dumps(snap), inv_id))
        if used:
            self.db.log_call({"investigation_id": inv_id, "step": "read", "role": "read.claims", "provider": "openai",
                              "model": "gpt-5.4-mini", "status": "ok", "cached": False, "attempt_kind": "first",
                              "input_tokens": used, "output_tokens": 0})
        return inv_id

    async def run_gaps(self, inv_id):
        ctx = llm.CallContext(db=self.db, settings=S.build("budget"),
                              providers={"openai": FakeProvider("openai", self.handler("openai")),
                                         "anthropic": FakeProvider("anthropic", self.handler("anthropic"))},
                              limiters=Limiters({}), prices=PriceTable())
        pipeline, _ = build_pipeline(ctx, stop_at="querying")
        worker = Worker(self.db, pipeline, services=ctx, max_parallel=2)
        await worker.machine.advance(inv_id)
        await worker.run_until_idle()
        return self.db.investigation(inv_id)


class Round2DecisionTests(Round2Base):
    async def test_new_work_and_a_pool_start_round_two(self):
        inv_id = self.seed()
        inv = await self.run_gaps(inv_id)
        g = self.db.one("SELECT * FROM gap_reports WHERE investigation_id = ? AND round = 1", (inv_id,))
        self.assertEqual(g["decision"], "continue")
        self.assertEqual((inv["round"], inv["stage"]), (2, "querying"))
        new = self.db.one("SELECT * FROM hypotheses WHERE investigation_id = ? AND key = 'tmj'", (inv_id,))
        self.assertEqual((new["round_added"], new["origin"], new["status"]), (2, "gaps", "active"))
        [q] = self.db.query("SELECT * FROM research_queries WHERE investigation_id = ? AND round = 2", (inv_id,))
        self.assertEqual((q["status"], q["origin"], q["hypothesis_id"]), ("ready", "gaps", new["id"]))
        self.assertEqual(len(self.calls), 1)  # one gaps member in budget

    async def test_a_small_pool_stops_with_the_reason(self):
        inv_id = self.seed(used=1_400_000)  # 2.4M − 1.4M − 800k protected = 200k < 260k
        inv = await self.run_gaps(inv_id)
        g = self.db.one("SELECT * FROM gap_reports WHERE investigation_id = ? AND round = 1", (inv_id,))
        self.assertEqual(g["decision"], "stop")
        self.assertIn("token pool", g["reason"])
        self.assertEqual(inv["round"], 1)
        self.assertEqual(self.db.query("SELECT * FROM hypotheses WHERE investigation_id = ? AND key = 'tmj'",
                                       (inv_id,)), [])

    def test_pool_left(self):
        inv_id = self.seed(used=500_000)
        settings = S.build("budget").model_dump(mode="json")
        self.assertEqual(budgets.pool_left(self.db, inv_id, settings), 2_400_000 - 500_000 - 800_000)
        self.assertIsNone(budgets.pool_left(self.db, inv_id, S.build("quality").model_dump(mode="json")))


class Round2QueriesTests(Round2Base):
    async def test_round_two_queries_only_the_new_hypotheses(self):
        inv_id = self.seed()
        await self.run_gaps(inv_id)
        with self.db.tx() as conn:
            from app.research.pipeline import StageContext, budgets as B
            from app.research.steps import queries as Q
            inv = self.db.investigation(inv_id)
            sc = StageContext(self.db, conn, inv, inv["settings_snapshot"], B.GuardState(None, None, {}))
            Q.stage().on_enter(sc)
            keys = [t["key"] for t in sc.tasks("queries")]
        self.assertEqual(keys, ["queries:r2:tmj"])  # not noise or wax


class FakeSnowSources:
    def __init__(self):
        self.searches = []

    async def references(self, literature_id):
        return [Ref(pmid="900"), Ref(pmid="901"), Ref(doi="10.1/x")]  # no PMID → skipped

    async def citing(self, literature_id, limit):
        return [Ref(pmid="901"), Ref(pmid="902")][:limit]

    async def search(self, source, q, n):
        self.searches.append((source, q))
        pmids = [p.split("[")[0] for p in q.split(" OR ")]
        return len(pmids), [Article(source="pubmed", source_id=p, pmid=p, title=f"Paper {p}", abstract="A.")
                            for p in pmids]


class SnowballTests(Round2Base):
    async def test_targeted_chasing_adds_snowball_candidates_shared_first(self):
        inv_id = self.seed()
        with self.db.tx() as conn:
            conn.execute("UPDATE paper_reads SET literature_id = 70 + id WHERE investigation_id = ?", (inv_id,))
            self.db.update_investigation(inv_id, conn=conn, round=2)
        src = FakeSnowSources()

        class TC:
            investigation = self.db.investigation(inv_id)
            settings = S.build("budget").model_dump(mode="json")
            db = self.db
            services = type("S", (), {"sources": src})()
            task = {"id": 1}
            payload = {}
        commit = await SE.run_snowball(TC)
        with self.db.tx() as conn:
            commit(conn)
        rows = self.db.query("SELECT pmid, found_via, first_rank, round_found FROM candidates WHERE investigation_id = ? "
                             "AND found_via = 'snowball' ORDER BY first_rank", (inv_id,))
        # 901 is linked by every seed (reference + citing, twice) → first; the DOI-only reference is skipped
        self.assertEqual([r["pmid"] for r in rows][0], "901")
        self.assertEqual({r["pmid"] for r in rows}, {"900", "901", "902"})
        self.assertTrue(all(r["round_found"] == 2 for r in rows))
        self.assertEqual(len(src.searches), 1)  # one batched abstract lookup
