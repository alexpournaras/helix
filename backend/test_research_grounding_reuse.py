"""Grounding reuse and the several-complaints cap (PLAN B17.5 item 10, B17.6; task 1R.2)."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.research import settings as S
from app.research.schemas import CaseBrief
from app.research.segment import user_segments
from app.research.steps import grounding as G, intake
from test_research_grounding import ADULT, DIZZY, TINNITUS, FakeSources, GroundingBase

TEXT = "Synthetic case. I have ringing in my left ear."
HEADACHE = {"key": "headache", "text": "Headache", "seg_ids": ["U2"], "mesh": [], "mesh_dropped": []}


class ReuseTests(GroundingBase):
    def seed(self, complaints=(TINNITUS,), populations=(), max_new=2, **grounding):
        snap = S.build("quality", {"grounding": {"reuse": {"enabled": True}, "max_new_complaints": max_new,
                                                 **grounding}}).model_dump(mode="json")
        inv_id = self.db.create_investigation(TEXT, profile="quality", settings_snapshot=snap, synthetic=True)
        with self.db.tx() as conn:
            intake.save_segments(conn, inv_id, user_segments(TEXT))
            intake.save_brief(self.db, conn, inv_id, CaseBrief.model_validate(ADULT), source="intake:p1", check=None,
                              prompt_version="test")
            self.db.update_investigation(inv_id, conn=conn, stage="safety", complaints=list(complaints),
                                         special_populations=list(populations))
        return inv_id

    def grounding_calls(self):
        """Extraction and verification requests sent so far (the expensive part of grounding)."""
        return [r for p in (self.a, self.b) for r in p.requests
                if r.schema_name in ("KnowledgeExtraction", "AttestationBatch")]

    def guideline_items(self, inv_id):
        return sorted((i["kind"], i["text"], i["trust"]) for i in self.items(inv_id, origin="guideline"))

    async def test_second_investigation_reuses_without_model_calls(self):
        first = self.seed()
        await self.run_inv(first)
        self.assertGreater(len(self.grounding_calls()), 0)
        [entry] = self.db.query("SELECT * FROM knowledge_cache")
        self.assertEqual((entry["complaint_key"], entry["population"], entry["status"]), ("D014012", "general", "ok"))  # its MeSH descriptor
        second = self.seed()
        await self.run_inv(second)
        self.assertEqual(len(self.grounding_calls()), 0)  # the second run's providers saw no extraction/verification
        self.assertEqual(self.guideline_items(second), self.guideline_items(first))
        run = self.db.one("SELECT * FROM grounding_runs WHERE investigation_id = ?", (second,))
        self.assertEqual((run["status"], run["reused_from"]), ("ok", entry["id"]))
        quotes = lambda inv: sorted(a["quote"] for a in self.db.query(  # noqa: E731
            "SELECT a.quote FROM knowledge_attestations a JOIN knowledge_items i ON i.id = a.item_id "
            "WHERE i.investigation_id = ?", (inv,)))
        self.assertEqual(quotes(second), quotes(first))
        read = self.db.one("SELECT * FROM paper_reads WHERE investigation_id = ?", (second,))
        self.assertEqual((read["purpose"], read["literature_id"]), ("grounding", 1001))  # fetched library-first

    async def test_misses(self):
        # A miss runs grounding afresh (reused_from stays empty); its model calls may still be answered by the
        # model-answer cache, which is a separate layer, so the check is on reuse, not on provider requests.
        def reused(inv):
            return self.db.one("SELECT reused_from FROM grounding_runs WHERE investigation_id = ?", (inv,))["reused_from"]

        await self.run_inv(self.seed())
        hit = self.seed()
        await self.run_inv(hit)
        self.assertIsNotNone(reused(hit))
        other_population = self.seed(populations=["pregnancy"])
        await self.run_inv(other_population)
        self.assertIsNone(reused(other_population))
        with patch.object(G, "EXTRACT_VERSION", "grounding.extract.v99"):  # a bumped prompt version
            bumped = self.seed()
            await self.run_inv(bumped)
            self.assertIsNone(reused(bumped))
        old = (datetime.now(timezone.utc) - timedelta(days=181)).isoformat().replace("+00:00", "Z")
        with self.db.tx() as conn:
            conn.execute("UPDATE knowledge_cache SET created_at = ?", (old,))
        stale = self.seed()  # every entry is now older than max_age_days
        await self.run_inv(stale)
        self.assertIsNone(reused(stale))

    async def test_gap_cached_only_after_a_full_search_and_expires(self):
        self.sources = FakeSources(results={"europepmc": [], "pubmed": []})  # searches succeed, nothing found
        await self.run_inv(self.seed())
        [entry] = self.db.query("SELECT * FROM knowledge_cache")
        self.assertEqual(entry["status"], "gap")
        second = self.seed()
        await self.run_inv(second)
        self.assertEqual(self.db.one("SELECT reused_from FROM grounding_runs WHERE investigation_id = ?",
                                     (second,))["reused_from"], entry["id"])
        old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat().replace("+00:00", "Z")
        with self.db.tx() as conn:
            conn.execute("UPDATE knowledge_cache SET created_at = ?", (old,))
        third = self.seed()
        await self.run_inv(third)
        self.assertIsNone(self.db.one("SELECT reused_from FROM grounding_runs WHERE investigation_id = ?",
                                      (third,))["reused_from"])

    async def test_operational_failure_is_not_cached(self):
        self.sources = FakeSources(fail=True)
        await self.run_inv(self.seed())
        self.assertEqual(self.db.one("SELECT status FROM grounding_runs")["status"], "gap")
        self.assertEqual(self.db.query("SELECT * FROM knowledge_cache"), [])

    async def test_third_new_complaint_gets_the_budget_gap(self):
        inv = self.seed(complaints=(TINNITUS, DIZZY, HEADACHE), max_new=2)
        await self.run_inv(inv)
        runs = {r["complaint_key"]: r for r in self.db.query("SELECT * FROM grounding_runs WHERE investigation_id = ?",
                                                              (inv,))}
        self.assertEqual(runs["headache"]["gap_reason"], G.BUDGET_GAP)  # the last complaint in order
        self.assertNotEqual(runs["tinnitus"]["gap_reason"], G.BUDGET_GAP)
        self.assertNotEqual(runs["dizziness"]["gap_reason"], G.BUDGET_GAP)
        broad = self.db.query("SELECT * FROM events WHERE investigation_id = ? AND kind = 'broad_case'", (inv,))
        self.assertEqual([e["message"] for e in broad], ["headache"])
        self.assertEqual(len(self.db.query("SELECT * FROM knowledge_cache")), 2)  # the budget gap isn't cached

    async def test_cached_complaints_dont_use_new_slots(self):
        await self.run_inv(self.seed(complaints=(TINNITUS,)))  # tinnitus now cached
        inv = self.seed(complaints=(TINNITUS, DIZZY, HEADACHE), max_new=2)
        await self.run_inv(inv)
        gaps = [r["complaint_key"] for r in self.db.query(
            "SELECT * FROM grounding_runs WHERE investigation_id = ? AND gap_reason = ?", (inv, G.BUDGET_GAP))]
        self.assertEqual(gaps, [])

    async def test_cache_holds_no_case_text_and_survives_deletion(self):
        inv = self.seed()
        await self.run_inv(inv)
        payload = self.db.query("SELECT payload FROM knowledge_cache")[0]["payload"]
        dumped = json.dumps(payload)
        self.assertNotIn("left ear", dumped.lower())  # the person's sentences are never cached
        self.assertNotIn("U1", json.dumps([s.get("snapshot", {}).get("segments") for s in payload["sources"]]))
        self.db.delete_investigation(inv)
        self.assertEqual(len(self.db.query("SELECT * FROM knowledge_cache")), 1)
