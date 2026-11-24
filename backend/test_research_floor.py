"""1B.1 / 1B.2: the real universal floor and packs. Every rule's own examples (its positive and negative unit
tests), B7.1 coverage, every cited source resolved at the last live check, the held-out vignette sets, and the
tinnitus pack. Synthetic text only."""

import unittest

import research_testkit  # noqa: F401  (installs the no-network guard)
from app.research.knowledge import loader as L
from app.research.knowledge import merge as M
from app.research.knowledge import validator as V
from eval.spikes import floor_vignettes as FV


class FloorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sources = L.load_sources()
        cls.lock = L.load_lock()
        cls.floor = L.load_floor()
        cls.packs = L.load_packs()

    def test_floor_is_valid_and_every_example_behaves(self):
        self.assertEqual(V.check_floor(self.floor, self.sources), [])

    def test_every_rule_has_positive_and_negative_tests(self):
        for rule in self.floor.rules:
            with self.subTest(rule=rule.id):
                self.assertGreaterEqual(len(rule.examples.fires), 1)
                self.assertGreaterEqual(len(rule.examples.no_fire), 1)

    def test_every_cited_source_resolved_and_none_unused(self):
        used = V.cited_sources(self.floor, self.packs.values())
        self.assertEqual(V.check_sources(self.sources, self.lock, used), [])

    def test_crisis_rule_is_an_emergency(self):
        crisis = [r for r in self.floor.rules if r.crisis]
        self.assertTrue(crisis)
        self.assertTrue(all(r.level == "emergency" and r.area == "mental_health" for r in crisis))

    def test_review_status_is_recorded(self):
        # Agent draft: stays unreviewed until two humans review it (PLAN Part G.6).
        self.assertIn(self.floor.review.status, ("unreviewed", "reviewed"))
        if self.floor.review.status == "reviewed":
            self.assertGreaterEqual(len(self.floor.review.reviewers), 2)

    def test_held_out_vignettes(self):
        for text, facts in FV.SET1_EMERGENCY + FV.SET2_EMERGENCY:
            with self.subTest(text=text):
                self.assertTrue(FV.emergency_caught(text, facts))
        for text, facts in FV.SET1_EVERYDAY + FV.SET2_EVERYDAY:
            with self.subTest(text=text):
                self.assertFalse(FV.false_emergency(text, facts))


class TinnitusPackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sources = L.load_sources()
        cls.lock = L.load_lock()
        cls.packs = L.load_packs()

    def test_pack_is_valid(self):
        self.assertIn("tinnitus", self.packs)
        self.assertEqual(V.check_pack(self.packs["tinnitus"], self.sources, self.lock), [])

    def test_matched_by_mesh(self):
        self.assertEqual([p.complaint for p in L.packs_for(["D014012"], self.packs)], ["tinnitus"])

    def test_pack_covers_the_goal_case_families(self):
        ids = {e.id for e in self.packs["tinnitus"].etiologies}
        for family in ("noise_exposure", "earwax_or_ear_canal", "vestibular_schwannoma", "pulsatile_vascular",
                       "somatic_jaw_neck", "stress_sleep_mood", "ototoxic_medicines"):
            self.assertIn(family, ids)

    def test_curated_items_merge_with_trust_curated(self):
        items = M.from_floor_and_packs(L.load_floor(), self.packs.values())
        result = M.merge(items)
        self.assertTrue(all(i.trust == "curated" for i in result.items))
        self.assertTrue(any(i.key == "pack:tinnitus:vestibular_schwannoma" for i in result.items))


if __name__ == "__main__":
    unittest.main()
