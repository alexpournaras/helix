"""1C.2 safety (PLAN B8.2, B8.3, B4 safety rule, B7.1). Synthetic text only; fake providers; no network."""

import json

from research_testkit import ResearchTestCase

from app.research import brief as B, llm, safety_rules as R, settings as S
from app.research.knowledge import loader
from app.research.knowledge.conditions import Case
from app.research.prices import PriceTable
from app.research.providers import FakeProvider
from app.research.ratelimit import Limiters
from app.research.runner import Worker
from app.research.schemas import CaseBrief, SafetyReview
from app.research.segment import user_segments
from app.research.steps import build_pipeline, intake, safety

ADULT = {"demographics": {"age": {"text": "40", "seg_ids": ["U1"], "years": 40}}}


def review(level="none", **over):
    out = {"level": level, "crisis": False, "flags": [], "guideline_checks": [], "recommendation": "See a GP.",
           "rule_out_questions": []}
    out.update(over)
    return out


def rule(rule_id):
    return next(r for r in loader.floor().rules if r.id == rule_id)


class SafetyRuleTests(ResearchTestCase):
    def test_every_floor_rule_through_the_engine(self):
        """Each floor rule fires on its positive examples and not on its negative ones, via R.evaluate."""
        for r in loader.floor().rules:
            for ex in r.examples.fires:
                with self.subTest(rule=r.id, example="fires"):
                    out = R.evaluate([(f"U{i}", s) for i, s in enumerate([ex.text], 1)], ex.facts)
                    self.assertIn(f"rule:{r.id}", [h.ref for h in out.fired])
            for ex in r.examples.no_fire:
                with self.subTest(rule=r.id, example="no_fire"):
                    out = R.evaluate([("U1", ex.text)], ex.facts)
                    self.assertNotIn(f"rule:{r.id}", [h.ref for h in out.fired])

    def test_population_rules_only_apply_to_their_population(self):
        r = next(x for x in loader.floor().rules if x.population == "infant")
        ex = r.examples.fires[0]
        ref = f"rule:{r.id}"
        self.assertIn(ref, [h.ref for h in R.evaluate([("U1", ex.text)], ex.facts).fired])
        adult = {k: v for k, v in ex.facts.items() if not k.startswith("age")} | {"age_years": 40}
        out = R.evaluate([("U1", ex.text)], adult)
        self.assertNotIn(ref, [h.ref for h in out.fired + out.unknown])

    def test_unknown_rule_carries_its_question(self):
        out = R.evaluate([("U1", "I have had abdominal pain for two days.")], {})
        hit = next(h for h in out.unknown if h.ref == "rule:gut.abdominal_pain_possible_pregnancy")
        self.assertTrue(hit.question.endswith("?"))
        self.assertEqual(out.level, "none")  # unknown doesn't set a level

    def test_pack_red_flags_join_when_the_descriptor_matches(self):
        text = [("U1", "I have ringing in my left ear and sudden weakness on one side of my face.")]
        with_pack = R.evaluate(text, {"age_years": 50}, ["D014012"])
        without = R.evaluate(text, {"age_years": 50}, [])
        self.assertIn("pack:tinnitus:tinnitus.with_neurological_or_vestibular_symptoms",
                      [h.ref for h in with_pack.fired])
        self.assertTrue(any(ref.startswith("pack:tinnitus:") for ref in with_pack.evaluated))
        self.assertFalse(any(ref.startswith("pack:") for ref in without.evaluated))

    def test_display_text(self):
        self.assertEqual(R.display_text("emergency", ["Rest at home."]), R.TEMPLATES["emergency"])  # fixed text only
        urgent = R.display_text("urgent", ["See your GP this week.", "See your GP this week."])
        self.assertTrue(urgent.startswith(R.TEMPLATES["urgent"]))
        self.assertEqual(urgent.count("See your GP"), 1)
        self.assertTrue(R.display_text("none", [], crisis=True).startswith(R.CRISIS_RESOURCES))
        self.assertEqual(R.most_cautious(["none", "urgent", "routine_review"]), "urgent")
        self.assertEqual(R.most_cautious([]), "none")


def no_questions(handler):
    """The questions step (1C.4) that follows safety proposes nothing; code adds the required rule-outs."""
    def wrapped(request):
        if request.schema_name == "QuestionSet":
            return {"questions": [], "merge_rule_outs": []}
        return handler(request)
    return wrapped


class SafetyStepBase(ResearchTestCase):
    def ctx(self, a, b):
        self.a = FakeProvider("openai", no_questions(a))
        self.b = FakeProvider("anthropic", no_questions(b))
        return llm.CallContext(db=self.db, settings=S.build("quality"),
                               providers={"openai": self.a, "anthropic": self.b},
                               limiters=Limiters({}), prices=PriceTable())

    def seed(self, text, brief=None, *, complaints=None, settings=None, grounded=True):
        """An investigation at the safety stage with segments and brief v1 (intake already done)."""
        inv_id = self.db.create_investigation(text, profile="quality", settings_snapshot=settings or {},
                                              synthetic=True)
        complaints = complaints if complaints is not None else [{"key": "c", "text": "c", "seg_ids": ["U1"],
                                                                 "mesh": [], "mesh_dropped": []}]
        pops = B.populations(CaseBrief.model_validate(brief or ADULT))
        with self.db.tx() as conn:
            intake.save_segments(conn, inv_id, user_segments(text))
            intake.save_brief(self.db, conn, inv_id, CaseBrief.model_validate(brief or ADULT), source="intake:p1",
                              check=None, prompt_version="test")
            self.db.update_investigation(inv_id, conn=conn, stage="safety", complaints=complaints,
                                         special_populations=pops)
            if grounded:
                for c in complaints:
                    self.ground(conn, inv_id, c["key"], B.population_key(pops))
        return inv_id

    def ground(self, conn, inv_id, key, pop="general"):
        self.db.insert(conn, "grounding_runs", {"investigation_id": inv_id, "complaint_key": key, "population": pop,
                                                "status": "ok", "knowledge_version": 0})

    async def run_inv(self, inv_id, a=lambda r: review(), b=lambda r: review()):
        ctx = self.ctx(a, b)
        pipeline, self.resolvers = build_pipeline(ctx, stop_at="planning")
        self.worker = Worker(self.db, pipeline, services=ctx, max_parallel=2)
        await self.worker.machine.advance(inv_id)
        await self.worker.run_until_idle()
        return self.db.investigation(inv_id)

    def reason(self, inv_id):
        row = self.db.one("SELECT data FROM events WHERE investigation_id = ? AND kind = 'complete' ORDER BY id DESC",
                          (inv_id,))
        return (row["data"] or {}).get("reason", "")

    def reviews(self, inv_id):
        return self.db.query("SELECT * FROM safety_reviews WHERE investigation_id = ? ORDER BY id", (inv_id,))


class SafetyStepTests(SafetyStepBase):
    async def test_nothing_found_goes_on_to_questions_then_planning(self):
        inv_id = self.seed("Synthetic case. A mild itch on my arm for a week.")
        inv = await self.run_inv(inv_id)
        self.assertEqual(inv["safety_level"], "none")
        self.assertIn("planning", self.reason(inv_id))  # no priority-1 unknowns or rule-outs → return stage
        [rev] = self.reviews(inv_id)
        self.assertEqual((rev["level"], rev["rule_level"], rev["brief_version"]), ("none", "none", 1))
        self.assertEqual(len(rev["panel"]["members"]), 2)
        self.assertEqual(len(self.db.query("SELECT * FROM note_outbox WHERE kind = 'safety_review'")), 1)

    async def test_grounding_comes_first_when_a_complaint_is_not_grounded(self):
        inv_id = self.seed("Synthetic case. A mild itch on my arm for a week.", grounded=False)
        await self.run_inv(inv_id)
        # Grounding ran for the complaint (a gap here: no network in tests), then safety again on k1.
        self.assertEqual([r["complaint_key"] for r in self.db.query("SELECT * FROM grounding_runs")], ["c"])
        self.assertEqual([r["knowledge_version"] for r in self.reviews(inv_id)], [0, 1])
        self.assertIn("planning", self.reason(inv_id))

    async def test_unknown_rule_becomes_a_priority_zero_rule_out_question(self):
        inv_id = self.seed("Synthetic case. I have had abdominal pain for two days.")
        await self.run_inv(inv_id)
        q = self.db.one("SELECT * FROM clarifying_questions WHERE key = 'ruleout.rule:gut.abdominal_pain_possible_pregnancy'")
        self.assertEqual((q["priority"], q["origin"], q["status"], q["qround"]), (0, "safety", "open", 1))
        self.assertEqual(self.db.investigation(inv_id)["waiting_for"], "answers")  # unanswered rule-out → asked

    async def test_priority_one_unknown_leads_to_questions(self):
        brief = {**ADULT, "unknowns": [{"key": "onset", "text": "When it started", "priority": 1}]}
        inv_id = self.seed("Synthetic case. A mild itch on my arm.", brief)
        await self.run_inv(inv_id)
        self.assertEqual(self.db.investigation(inv_id)["waiting_for"], "answers")
        self.assertEqual(self.db.one("SELECT qround FROM clarifying_questions WHERE key = 'onset'")["qround"], 1)

    async def test_most_cautious_level_wins_and_panel_cannot_lower_rules(self):
        inv_id = self.seed("Synthetic case. A mild itch on my arm for a week.")
        inv = await self.run_inv(inv_id, lambda r: review("urgent"), lambda r: review("none"))
        self.assertEqual(inv["safety_level"], "urgent")
        self.assertEqual(inv["waiting_for"], "safety_ack")

        r = rule("mh.suicide_or_self_harm")
        inv_id = self.seed("Synthetic case. " + r.examples.fires[0].text)
        inv = await self.run_inv(inv_id, lambda r: review("none"), lambda r: review("none"))
        self.assertEqual(self.reviews(inv_id)[0]["rule_level"], "emergency")
        self.assertEqual(inv["safety_level"], "emergency")

    async def test_emergency_stops_by_default_with_a_safety_report(self):
        r = next(x for x in loader.floor().rules if x.level == "emergency" and not x.crisis and x.population == "all")
        ex = r.examples.fires[0]
        inv_id = self.seed("Synthetic case. " + ex.text)
        inv = await self.run_inv(inv_id)
        self.assertEqual((inv["status"], inv["outcome"]), ("complete", "safety_stop"))
        rep = self.db.one("SELECT * FROM reports WHERE investigation_id = ?", (inv_id,))
        self.assertEqual((rep["kind"], rep["status"]), ("safety", "final"))
        self.assertIn(R.TEMPLATES["emergency"], rep["markdown"])
        self.assertIn(R.STOPPED, rep["markdown"])
        self.assertIn(r.title, rep["markdown"])
        self.assertIn("you wrote “", rep["markdown"])  # the user's own sentences
        self.assertNotIn(R.CRISIS_RESOURCES, rep["markdown"])
        self.assertEqual(len(self.db.query("SELECT * FROM note_outbox WHERE kind = 'final_report'")), 1)

    async def test_emergency_can_continue_after_acknowledgement_when_allowed(self):
        r = next(x for x in loader.floor().rules if x.level == "emergency" and not x.crisis and x.population == "all")
        inv_id = self.seed("Synthetic case. " + r.examples.fires[0].text,
                           settings={"safety": {"allow_continue_after_emergency": True}})
        inv = await self.run_inv(inv_id)
        self.assertEqual((inv["status"], inv["waiting_for"]), ("waiting_user", "safety_ack"))
        await self.worker.machine.resolve_wait(inv_id, "safety_ack", safety.acknowledge())
        await self.worker.run_until_idle()
        inv = self.db.investigation(inv_id)
        self.assertEqual(inv["outcome"], "incomplete")  # went on (to a stage not built yet)
        self.assertIsNotNone(self.reviews(inv_id)[0]["acknowledged_at"])

    async def test_urgent_without_continue_goes_to_safety_finalize(self):
        inv_id = self.seed("Synthetic case. A mild itch on my arm for a week.",
                           settings={"safety": {"allow_continue_after_urgent": False}})
        inv = await self.run_inv(inv_id, lambda r: review("urgent"), lambda r: review("none"))
        self.assertEqual(inv["outcome"], "safety_stop")
        self.assertIn(R.TEMPLATES["urgent"], self.db.one("SELECT markdown FROM reports")["markdown"])

    async def test_urgent_acknowledgement_is_per_brief_and_knowledge_version(self):
        inv_id = self.seed("Synthetic case. A mild itch on my arm for a week.")
        urgent = lambda r: review("urgent")
        inv = await self.run_inv(inv_id, urgent, urgent)
        self.assertEqual(inv["waiting_for"], "safety_ack")
        await self.worker.machine.resolve_wait(inv_id, "safety_ack", safety.acknowledge())
        await self.worker.run_until_idle()
        self.assertEqual(self.db.investigation(inv_id)["status"], "complete")
        # New knowledge (grounding) → a new review → a new acknowledgement is needed.
        self.db.update_investigation(inv_id, status="active", outcome=None, stage="safety", knowledge_version=1)
        await self.worker.machine.advance(inv_id)
        await self.worker.run_until_idle()
        inv = self.db.investigation(inv_id)
        self.assertEqual(inv["waiting_for"], "safety_ack")
        self.assertEqual([r["knowledge_version"] for r in self.reviews(inv_id)], [0, 1])
        # Same brief and knowledge version again → no new review, no new wait after acknowledgement.
        await self.worker.machine.resolve_wait(inv_id, "safety_ack", safety.acknowledge())
        await self.worker.run_until_idle()
        self.db.update_investigation(inv_id, status="active", outcome=None, stage="safety")
        await self.worker.machine.advance(inv_id)
        await self.worker.run_until_idle()
        self.assertEqual(self.db.investigation(inv_id)["status"], "complete")
        self.assertEqual(len(self.reviews(inv_id)), 2)

    async def test_self_harm_shows_crisis_resources_immediately(self):
        r = rule("mh.suicide_or_self_harm")
        inv_id = self.seed("Synthetic case. " + r.examples.fires[0].text)
        seen = {}

        def slow_panel(request):
            # While the panel runs, the crisis is already recorded.
            seen["crisis"] = self.db.one("SELECT data FROM events WHERE investigation_id = ? AND kind = 'crisis'",
                                         (inv_id,))
            seen["level"] = self.db.investigation(inv_id)["safety_level"]
            return review("emergency", crisis=True)
        inv = await self.run_inv(inv_id, slow_panel, slow_panel)
        self.assertEqual(seen["crisis"]["data"]["rules"], ["rule:mh.suicide_or_self_harm"])
        self.assertEqual(seen["level"], "emergency")
        self.assertEqual(inv["outcome"], "safety_stop")
        md = self.db.one("SELECT markdown FROM reports WHERE investigation_id = ?", (inv_id,))["markdown"]
        self.assertTrue(md.startswith("# Safety\n\n" + R.CRISIS_RESOURCES))
        events = json.dumps(self.db.query("SELECT message, data FROM events WHERE investigation_id = ?", (inv_id,)))
        self.assertNotIn(r.examples.fires[0].text[:20], events)  # IDs and counts only

    async def test_panel_crisis_alone_adds_crisis_resources(self):
        inv_id = self.seed("Synthetic case. Everything feels pointless lately.")
        await self.run_inv(inv_id, lambda r: review("emergency", crisis=True), lambda r: review("urgent"))
        self.assertIn(R.CRISIS_RESOURCES, self.db.one("SELECT markdown FROM reports")["markdown"])

    async def test_flags_with_invalid_segments_are_unverified(self):
        flags = [{"flag": "a", "rule_ref": None, "reason": "r", "seg_ids": ["U1"]},
                 {"flag": "b", "rule_ref": None, "reason": "r", "seg_ids": ["U1", "U99"]},
                 {"flag": "c", "rule_ref": None, "reason": "r", "seg_ids": []}]
        inv_id = self.seed("Synthetic case. A mild itch on my arm for a week.")
        await self.run_inv(inv_id, lambda r: review("routine_review", flags=flags), lambda r: review())
        got = {f["flag"]: f["verified"] for f in self.reviews(inv_id)[0]["panel"]["flags"]}
        self.assertEqual(got, {"a": True, "b": False, "c": False})

    async def test_guideline_red_flags_are_checked_by_the_panel(self):
        inv_id = self.seed("Synthetic case. A mild itch on my arm for a week.")
        with self.db.tx() as conn:
            fire = self.db.insert(conn, "knowledge_items", {
                "investigation_id": inv_id, "version": 0, "kind": "red_flag", "key": "spreading", "text": "Rapidly "
                "spreading rash", "attributes": {"urgency": "urgent"}, "population": "all", "origin": "guideline",
                "trust": "verified", "status": "active"})
            unk = self.db.insert(conn, "knowledge_items", {
                "investigation_id": inv_id, "version": 0, "kind": "red_flag", "key": "fever", "text": "Fever with "
                "the rash", "attributes": {"urgency": "emergency", "question": "Do you have a fever with the rash?"},
                "population": "all", "origin": "guideline", "trust": "verified", "status": "active"})
            vague = self.db.insert(conn, "knowledge_items", {
                "investigation_id": inv_id, "version": 0, "kind": "red_flag", "key": "other", "text": "Other signs",
                "attributes": {"urgency": "urgent"}, "population": "all", "origin": "guideline",
                "trust": "verified", "status": "active"})
            pregnant = self.db.insert(conn, "knowledge_items", {
                "investigation_id": inv_id, "version": 0, "kind": "red_flag", "key": "preg", "text": "Rash in "
                "pregnancy", "attributes": {"urgency": "urgent"}, "population": "pregnancy", "origin": "guideline",
                "trust": "verified", "status": "active"})
        calls = []

        def a(request):
            calls.append(1)
            checks = [{"item": f"K{fire}", "state": "fires", "seg_ids": ["U2"]},
                      {"item": f"K{vague}", "state": "unknown", "seg_ids": []}]
            if len(calls) > 1:  # the first answer misses an item → one repair call
                checks.append({"item": f"K{unk}", "state": "not_applicable", "seg_ids": []})
            return review("none", guideline_checks=checks)

        def b(request):
            return review("none", guideline_checks=[{"item": f"K{fire}", "state": "not_applicable", "seg_ids": []},
                                                    {"item": f"K{unk}", "state": "unknown", "seg_ids": []},
                                                    {"item": f"K{vague}", "state": "unknown", "seg_ids": []}])
        inv = await self.run_inv(inv_id, a, b)
        self.assertEqual(len(calls), 2)
        self.assertIn(f"K{fire}", self.a.requests[0].user)
        self.assertEqual(inv["safety_level"], "urgent")  # the item that fires sets the level
        q = self.db.one("SELECT * FROM clarifying_questions WHERE key = ?", (f"ruleout.K{unk}",))
        self.assertEqual((q["item_id"], q["priority"], q["text"]), (unk, 0, "Do you have a fever with the rash?"))
        # Without a plain question from the guideline, an unknown item asks nothing; another population's flag
        # isn't shown to the panel at all.
        self.assertIsNone(self.db.one("SELECT * FROM clarifying_questions WHERE key = ?", (f"ruleout.K{vague}",)))
        self.assertNotIn(f'"K{pregnant}"', self.a.requests[0].user)

    async def test_panel_rule_out_questions_are_saved(self):
        inv_id = self.seed("Synthetic case. A mild itch on my arm for a week.")
        qs = [{"key": "fever", "text": "Do you have a fever?"}]
        await self.run_inv(inv_id, lambda r: review(rule_out_questions=qs), lambda r: review(rule_out_questions=qs))
        rows = self.db.query("SELECT key, priority FROM clarifying_questions WHERE investigation_id = ?", (inv_id,))
        self.assertEqual(rows, [{"key": "ruleout.panel.fever", "priority": 0}])

    async def test_one_member_failing_continues_reduced(self):
        from app.research.providers import ModelError
        inv_id = self.seed("Synthetic case. A mild itch on my arm for a week.")

        def broken(request):
            raise ModelError("config", "down")
        inv = await self.run_inv(inv_id, lambda r: review("routine_review"), broken)
        self.assertEqual(inv["safety_level"], "routine_review")
        self.assertEqual(self.reviews(inv_id)[0]["panel"]["independence"], "reduced")


class CombineTests(SafetyStepBase):
    def test_follow_up_cycle_asks_only_for_pending_rule_outs(self):
        inv_id = self.seed("Synthetic case. A mild itch.",
                           {**ADULT, "unknowns": [{"key": "onset", "text": "When", "priority": 1}]})
        self.db.update_investigation(inv_id, segment_no=1)
        from app.research.pipeline import StageContext
        from app.research import budgets
        with self.db.tx() as conn:
            inv = self.db.investigation(inv_id)
            sc = StageContext(self.db, conn, inv, {}, budgets.GuardState(None, None, {}))
            self.assertFalse(safety.questions_needed(sc, {"panel": {"questions": []}}))
            self.db.insert(conn, "clarifying_questions", {"investigation_id": inv_id, "origin": "safety",
                                                          "key": "ruleout.x", "text": "?", "priority": 0})
            self.assertTrue(safety.questions_needed(sc, {"panel": {"questions": ["ruleout.x"]}}))

    def test_rounds_are_limited(self):
        inv_id = self.seed("Synthetic case. A mild itch.",
                           {**ADULT, "unknowns": [{"key": "onset", "text": "When", "priority": 1}]})
        from app.research.pipeline import StageContext
        from app.research import budgets
        with self.db.tx() as conn:
            inv = self.db.investigation(inv_id)
            sc = StageContext(self.db, conn, inv, {"questions": {"max_rounds": 1}}, budgets.GuardState(None, None, {}))
            self.assertTrue(safety.questions_needed(sc, None))
            self.db.queue_task(conn, inv_id, "questions", "questions:r1:q1", None)
            self.assertFalse(safety.questions_needed(sc, None))


if __name__ == "__main__":
    import unittest
    unittest.main()


class ClearanceTests(SafetyStepBase):
    """1R.7 (PLAN B17.5 item 11): an unknown emergency-level rule-out is unresolved, never ruled out."""

    async def test_unanswered_emergency_rule_out_is_unresolved_and_blocks_ranking(self):
        inv_id = self.seed("Synthetic case. I have had abdominal pain for two days.")
        await self.run_inv(inv_id)
        c = self.reviews(inv_id)[-1]["panel"]["clearance"]
        self.assertEqual(c["state"], "unresolved")
        self.assertIn("rule:gut.abdominal_pain_possible_pregnancy", c["open_emergency"])
        with self.db.read() as conn:
            self.assertFalse(safety.ranking_allowed(conn, inv_id))

    async def test_nothing_open_passes(self):
        inv_id = self.seed("Synthetic case. A mild itch on my arm for a week.")
        await self.run_inv(inv_id)
        self.assertEqual(self.reviews(inv_id)[-1]["panel"]["clearance"]["state"], "passed")
        with self.db.read() as conn:
            self.assertTrue(safety.ranking_allowed(conn, inv_id))

    def test_levels(self):
        from app.research.safety_rules import RuleHit
        urgent = RuleHit("rule:x", "urgent", "x", "unknown", "?")
        emergency = RuleHit("rule:y", "emergency", "y", "unknown", "?")
        self.assertEqual(safety.clearance("none", [urgent], [])["state"], "passed")  # a banner only
        self.assertEqual(safety.clearance("none", [urgent], [])["open_urgent"], ["rule:x"])
        self.assertEqual(safety.clearance("urgent", [], [])["state"], "urgent")
        self.assertEqual(safety.clearance("urgent", [emergency], [])["state"], "unresolved")
        self.assertEqual(safety.clearance("emergency", [emergency], [])["state"], "emergency")
        self.assertEqual(safety.clearance("none", [], [{"ref": "K1", "level": "emergency"}])["state"], "unresolved")
