import sqlite3
import threading
import unittest

from research_testkit import ResearchTestCase

from app.research import db as dbmod
from app.research.db import ResearchDB
from app.research.ids import answer_seg, batch_key, is_paper_segment, is_user_segment, row_of, slug


def make_investigation(db: ResearchDB, **kw) -> int:
    return db.create_investigation("Synthetic test problem.", profile="quality", settings_snapshot={"profile": "quality"},
                                   synthetic=True, **kw)


class SchemaTests(ResearchTestCase):
    def test_every_plan_table_exists(self):
        names = {r["name"] for r in self.db.query("SELECT name FROM sqlite_master WHERE type='table'")}
        expected = {
            "investigations", "user_segments", "case_briefs", "mesh_cache", "grounding_runs", "knowledge_items",
            "knowledge_attestations", "knowledge_coverage", "clarifying_questions", "safety_reviews", "hypotheses",
            "plan_uncovered", "research_queries", "candidates", "snowball_edges", "screenings", "paper_reads",
            "studies", "study_members", "review_inclusions", "evidence_claims", "claim_votes", "claim_links",
            "rejected_items", "feature_fit", "hypothesis_analyses", "gap_reports", "reports", "followups",
            "research_tasks", "llm_calls", "events", "note_outbox", "meta",
        }
        self.assertTrue(expected <= names, expected - names)

    def test_pragmas(self):
        with self.db.read() as conn:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(conn.execute("PRAGMA secure_delete").fetchone()[0], 1)
            self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 10000)

    def test_migration_is_idempotent_and_versioned(self):
        self.assertEqual(self.db.schema_version(), dbmod.SCHEMA_VERSION)
        again = ResearchDB(self.db.path)
        again.init()
        self.assertEqual(again.schema_version(), dbmod.SCHEMA_VERSION)

    def test_newer_schema_is_refused(self):
        with self.db.tx() as conn:
            conn.execute("UPDATE meta SET value = '999' WHERE key = 'schema_version'")
        with self.assertRaises(RuntimeError):
            ResearchDB(self.db.path).init()

    def test_registered_migration_runs_once(self):
        nxt = dbmod.SCHEMA_VERSION + 1  # a hypothetical next migration on top of the real ones
        with unittest.mock.patch.dict(dbmod.MIGRATIONS, {nxt: ["ALTER TABLE meta ADD COLUMN extra TEXT"]}), \
                unittest.mock.patch.object(dbmod, "SCHEMA_VERSION", nxt):
            fresh = ResearchDB(self.db.path)
            fresh.init()
            self.assertEqual(fresh.schema_version(), nxt)
            cols = {r["name"] for r in fresh.query("PRAGMA table_info(meta)")}
            self.assertIn("extra", cols)
            ResearchDB(self.db.path).init()  # does not re-run (would fail with duplicate column)


class DataTests(ResearchTestCase):
    def test_json_round_trip_and_status_check(self):
        inv = make_investigation(self.db)
        self.db.update_investigation(inv, complaints=[{"key": "tinnitus"}], systems=["ent"])
        row = self.db.investigation(inv)
        self.assertEqual(row["complaints"], [{"key": "tinnitus"}])
        self.assertEqual(row["systems"], ["ent"])
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.update_investigation(inv, status="bogus")

    def test_task_keys_are_unique_per_investigation(self):
        a, b = make_investigation(self.db), make_investigation(self.db)
        with self.db.tx() as conn:
            self.assertTrue(self.db.queue_task(conn, a, "intake", "intake:p1"))
            self.assertFalse(self.db.queue_task(conn, a, "intake", "intake:p1"))
            self.assertTrue(self.db.queue_task(conn, b, "intake", "intake:p1"))
        self.assertEqual(len(self.db.tasks(a)), 1)

    def test_claims_are_atomic_across_threads(self):
        inv = make_investigation(self.db)
        with self.db.tx() as conn:
            for i in range(30):
                self.db.queue_task(conn, inv, "x", f"x:{i}")
        claimed: list[int] = []
        lock = threading.Lock()

        def worker():
            while True:
                task = self.db.claim_task()
                if task is None:
                    return
                with lock:
                    claimed.append(task["id"])

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(claimed), 30)
        self.assertEqual(len(set(claimed)), 30)

    def test_claim_respects_priority_not_before_and_status(self):
        inv = make_investigation(self.db)
        with self.db.tx() as conn:
            self.db.queue_task(conn, inv, "x", "late", priority=5)
            conn.execute("UPDATE research_tasks SET not_before = '2999-01-01T00:00:00Z' WHERE key = 'late'")
            self.db.queue_task(conn, inv, "x", "low", priority=200)
            self.db.queue_task(conn, inv, "x", "high", priority=10)
        self.assertEqual(self.db.claim_task()["key"], "high")
        self.db.update_investigation(inv, status="paused")
        self.assertIsNone(self.db.claim_task())
        self.db.update_investigation(inv, status="active")
        self.assertEqual(self.db.claim_task()["key"], "low")
        self.assertIsNone(self.db.claim_task())

    def test_transaction_rolls_back(self):
        inv = make_investigation(self.db)
        with self.assertRaises(ZeroDivisionError):
            with self.db.tx() as conn:
                self.db.queue_task(conn, inv, "x", "x:1")
                self.db.event(inv, "test", conn=conn)
                1 / 0
        self.assertEqual(self.db.tasks(inv), [])
        self.assertEqual(self.db.query("SELECT * FROM events WHERE investigation_id = ?", (inv,)), [])

    def test_compare_and_set_stage(self):
        inv = make_investigation(self.db)
        self.assertTrue(self.db.set_stage(inv, "intake", "safety"))
        self.assertFalse(self.db.set_stage(inv, "intake", "planning"))
        self.assertEqual(self.db.investigation(inv)["stage"], "safety")

    def test_requeue_running_counts_restarts_only_on_recovery(self):
        inv = make_investigation(self.db)
        with self.db.tx() as conn:
            self.db.queue_task(conn, inv, "x", "x:1")
        self.db.claim_task()
        self.db.requeue_running(count_restart=True)
        task = self.db.tasks(inv)[0]
        self.assertEqual((task["status"], task["restarts"], task["attempts"]), ("queued", 1, 0))
        self.db.claim_task()
        self.db.requeue_running(count_restart=False)
        task = self.db.tasks(inv)[0]
        self.assertEqual((task["status"], task["restarts"]), ("queued", 1))

    def test_delete_cascades(self):
        inv = make_investigation(self.db)
        with self.db.tx() as conn:
            self.db.queue_task(conn, inv, "x", "x:1")
            self.db.event(inv, "e", conn=conn)
            cid = self.db.insert(conn, "candidates", {"investigation_id": inv, "round_found": 1, "source": "pubmed",
                                                      "source_id": "1", "title": "t"})
            self.db.insert(conn, "screenings", {"candidate_id": cid, "decision": "include"})
        self.db.log_call({"investigation_id": inv, "provider": "fake", "model": "m", "status": "ok"})
        self.assertTrue(self.db.delete_investigation(inv))
        for table in ("research_tasks", "events", "candidates", "llm_calls"):
            self.assertEqual(self.db.query(f"SELECT * FROM {table}"), [], table)
        self.assertEqual(self.db.query("SELECT * FROM screenings"), [])

    def test_usage_per_segment(self):
        inv = make_investigation(self.db)
        self.db.log_call({"investigation_id": inv, "provider": "openai", "model": "a", "status": "ok",
                          "input_tokens": 100, "output_tokens": 10, "cost": 0.5, "created_at": "x"})
        self.db.log_call({"investigation_id": inv, "provider": "anthropic", "model": "b", "status": "ok",
                          "input_tokens": 50, "cached_input_tokens": 20, "output_tokens": 5, "cost": 0.25, "cached": 1})
        usage = self.db.usage(inv)
        self.assertEqual(usage["calls"], 2)
        self.assertEqual(usage["tokens"], 165)
        self.assertEqual(usage["cached_input_tokens"], 20)
        self.assertAlmostEqual(usage["cost"], 0.75)
        self.assertEqual(len(usage["by_model"]), 2)
        self.assertEqual(self.db.usage(inv, segment_started_at="2999-01-01")["calls"], 0)


class IdTests(unittest.TestCase):
    def test_formats(self):
        for good in ("S2.P3.s1", "AB.P1.s4", "T1.cap", "T2.r3.c4", "T2.r3", "T1.foot", "F3.cap"):
            self.assertTrue(is_paper_segment(good), good)
        for bad in ("S0.P1.s1", "s1", "T1.r1.c", "AB.s1", "U1"):
            self.assertFalse(is_paper_segment(bad), bad)
        for good in ("U1", "A12", "FU3", "N4.s2"):
            self.assertTrue(is_user_segment(good), good)
        self.assertEqual(answer_seg(7), "A7")
        self.assertEqual(row_of("T2.r3.c4"), "T2.r3")
        self.assertIsNone(row_of("T2.cap"))

    def test_batch_key_is_order_independent(self):
        self.assertEqual(batch_key("screen", [3, 1, 2]), batch_key("screen", [2, 3, 1]))
        self.assertNotEqual(batch_key("screen", [1, 2]), batch_key("screen", [1, 3]))

    def test_slug(self):
        self.assertEqual(slug("Somatic (somatosensory) tinnitus!"), "somatic_somatosensory_tinnitus")


if __name__ == "__main__":
    unittest.main()
