"""1D.1 planning (PLAN B8.6, B4 plan_approval). Synthetic text; fake providers; no network."""

import json

from research_testkit import ResearchTestCase

from app.research import llm, settings as S
from app.research.prices import PriceTable
from app.research.providers import FakeProvider, ModelError
from app.research.ratelimit import Limiters
from app.research.runner import Worker
from app.research.schemas import CaseBrief, Hypothesis
from app.research.segment import user_segments
from app.research.steps import build_pipeline, intake, plan as P

TEXT = "Synthetic case. I am a 40-year-old man. I have had a headache every afternoon for two weeks."
BRIEF = {"chief_complaint": {"text": "Afternoon headache", "seg_ids": ["U3"]},
         "demographics": {"age": {"text": "40", "seg_ids": ["U2"], "years": 40},
                          "sex": {"text": "Man", "seg_ids": ["U2"], "value": "male"}},
         "case_features": [{"key": "afternoon_headache", "text": "Headache every afternoon", "seg_ids": ["U3"]},
                           {"key": "two_weeks", "text": "For two weeks", "seg_ids": ["U3"]}]}


def hyp(key, kind="common", item=None, explains=("afternoon_headache",), **kw):
    return {"key": key, "title": key.replace("_", " "), "description": "d", "mechanism": "m", "kind": kind,
            "item_id": item, "features_explained": list(explains), "features_unexplained": [],
            "discriminators": ["x"], "prior": "moderate", "prior_reason": "r", **kw}


def base_plan(n=5):
    hs = [hyp("raised_pressure", "red_flag", explains=("afternoon_headache", "two_weeks"))]
    hs += [hyp(f"cause_{i}") for i in range(1, n)]
    return {"research_questions": ["What causes afternoon headaches?"], "hypotheses": hs, "uncovered_features": []}


class Models:
    def __init__(self, items):
        self.items = items  # K refs of the differential items
        self.planner = lambda request, family: base_plan()
        self.consolidate = self.covering
        self.calls = {"Plan": 0, "ConsolidatedPlan": 0}
        self.users = {"Plan": [], "ConsolidatedPlan": []}

    def covering(self, request):
        out = base_plan()
        out["hypotheses"][1]["item_id"] = self.items[0]
        out["knowledge_coverage"] = [{"item_id": self.items[0], "decision": "hypothesis",
                                      "hypothesis_key": "cause_1", "reason": "fits", "seg_ids": []}]
        out["knowledge_coverage"] += [{"item_id": k, "decision": "excluded", "hypothesis_key": None,
                                       "reason": "He is 40", "seg_ids": ["U2"]} for k in self.items[1:]]
        return out

    def handler(self, family):
        def h(request):
            self.calls[request.schema_name] = self.calls.get(request.schema_name, 0) + 1
            self.users.setdefault(request.schema_name, []).append(request.user)
            if request.schema_name == "Plan":
                return self.planner(request, family)
            if request.schema_name == "ConsolidatedPlan":
                return self.consolidate(request)
            raise AssertionError(request.schema_name)
        return h


class PlanBase(ResearchTestCase):
    ITEMS = 2

    def setUp(self):
        super().setUp()
        self.inv_id = self.seed()
        self.models = Models([f"K{i}" for i in self.item_ids])

    def ctx(self):
        self.a = FakeProvider("openai", self.models.handler("openai"))
        self.b = FakeProvider("anthropic", self.models.handler("anthropic"))
        return llm.CallContext(db=self.db, settings=S.build("quality"),
                               providers={"openai": self.a, "anthropic": self.b},
                               limiters=Limiters({}), prices=PriceTable())

    def seed(self, settings=None):
        snap = {"plan": {"min_hypotheses": 3, "max_hypotheses": 8}, "autonomy": "review_plan",
                **(settings or {})}
        inv_id = self.db.create_investigation(TEXT, profile="quality", settings_snapshot=snap, synthetic=True)
        self.item_ids = []
        with self.db.tx() as conn:
            intake.save_segments(conn, inv_id, user_segments(TEXT))
            intake.save_brief(self.db, conn, inv_id, CaseBrief.model_validate(BRIEF), source="intake:p1", check=None,
                              prompt_version="test")
            for i in range(self.ITEMS):
                self.item_ids.append(self.db.insert(conn, "knowledge_items", {
                    "investigation_id": inv_id, "version": 1, "kind": "differential", "key": f"d{i}",
                    "text": f"Differential {i}", "attributes": {"key_features": ["f"]}, "population": "all",
                    "origin": "guideline", "trust": "verified"}))
            self.db.insert(conn, "knowledge_items", {  # a rejected item never needs coverage
                "investigation_id": inv_id, "version": 1, "kind": "differential", "key": "gone", "text": "Gone",
                "attributes": {}, "population": "all", "origin": "guideline", "trust": "verified",
                "status": "rejected"})
            self.db.update_investigation(inv_id, conn=conn, stage="planning", knowledge_version=1)
        return inv_id

    async def start(self, inv_id=None):
        inv_id = inv_id or self.inv_id
        ctx = self.ctx()
        pipeline, self.resolvers = build_pipeline(ctx, stop_at="querying")
        self.worker = Worker(self.db, pipeline, services=ctx, max_parallel=2)
        await self.worker.machine.advance(inv_id)
        await self.worker.run_until_idle()
        return self.db.investigation(inv_id)

    async def approve(self, **kw):
        ok = await self.worker.machine.resolve_wait(self.inv_id, "plan_approval", P.approve(**kw))
        await self.worker.run_until_idle()
        return ok, self.db.investigation(self.inv_id)

    def hyps(self):
        return {r["key"]: r for r in self.db.query("SELECT * FROM hypotheses WHERE investigation_id = ?",
                                                   (self.inv_id,))}

    def reason(self):
        row = self.db.one("SELECT data FROM events WHERE investigation_id = ? AND kind = 'complete' ORDER BY id DESC",
                          (self.inv_id,))
        return (row["data"] or {}).get("reason", "") if row else ""


class PlanFlowTests(PlanBase):
    async def test_two_planners_consolidation_and_coverage_then_wait_for_approval(self):
        inv = await self.start()
        self.assertEqual((inv["status"], inv["waiting_for"]), ("waiting_user", "plan_approval"))
        self.assertEqual(self.models.calls["Plan"], 2)  # A and B
        self.assertEqual(self.models.calls["ConsolidatedPlan"], 1)
        consolidation = self.models.users["ConsolidatedPlan"][0]
        self.assertEqual(consolidation.count('"research_questions"'), 2)  # both plans are shown
        self.assertNotIn('"Gone"', consolidation)  # rejected items aren't
        hs = self.hyps()
        self.assertEqual(len(hs), 5)
        self.assertEqual(hs["cause_1"]["item_id"], self.item_ids[0])
        self.assertEqual(hs["cause_1"]["origin"], "knowledge")
        self.assertEqual(hs["cause_2"]["origin"], "proposed")
        self.assertEqual(hs["raised_pressure"]["kind"], "red_flag")
        cov = {r["item_id"]: r for r in self.db.query("SELECT * FROM knowledge_coverage WHERE investigation_id = ?",
                                                      (self.inv_id,))}
        self.assertEqual(cov[self.item_ids[0]]["decision"], "hypothesis")
        self.assertEqual(cov[self.item_ids[0]]["hypothesis_id"], hs["cause_1"]["id"])
        self.assertEqual((cov[self.item_ids[1]]["decision"], cov[self.item_ids[1]]["seg_ids"]),
                         ("excluded", ["U2"]))
        note = self.db.one("SELECT * FROM note_outbox WHERE investigation_id = ? AND kind = 'research_plan'",
                           (self.inv_id,))
        self.assertEqual(note["artifact_key"], "plan:s1")
        ok, inv = await self.approve()
        self.assertTrue(ok)
        self.assertEqual(inv["outcome"], "incomplete")
        self.assertIn("querying", self.reason())

    async def test_coverage_names_the_hypothesis_without_the_hypothesis_naming_the_item(self):
        def cons(request):
            out = self.models.covering(request)
            out["hypotheses"][1]["item_id"] = None
            return out
        self.models.consolidate = cons
        await self.start()
        self.assertEqual(self.hyps()["cause_1"]["item_id"], self.item_ids[0])
        self.assertEqual(self.hyps()["cause_1"]["origin"], "knowledge")

    async def test_missing_coverage_is_repaired(self):
        def cons(request):
            out = self.models.covering(request)
            if self.models.calls["ConsolidatedPlan"] == 1:
                out["knowledge_coverage"] = out["knowledge_coverage"][:1]
            return out
        self.models.consolidate = cons
        inv = await self.start()
        self.assertEqual(self.models.calls["ConsolidatedPlan"], 2)
        self.assertIn(f"missing K{self.item_ids[1]}", self.models.users["ConsolidatedPlan"][1])
        self.assertEqual(inv["waiting_for"], "plan_approval")

    async def test_an_exclusion_without_the_persons_words_is_repaired(self):
        def cons(request):
            out = self.models.covering(request)
            if self.models.calls["ConsolidatedPlan"] == 1:
                out["knowledge_coverage"][1]["seg_ids"] = ["U99"]
            return out
        self.models.consolidate = cons
        await self.start()
        self.assertEqual(self.models.calls["ConsolidatedPlan"], 2)
        self.assertIn("must cite the person's segments", self.models.users["ConsolidatedPlan"][1])

    async def test_a_plan_without_a_red_flag_hypothesis_is_repaired(self):
        seen = {}

        def planner(request, family):  # each planner's first answer has no red-flag hypothesis
            seen[family] = seen.get(family, 0) + 1
            out = base_plan()
            if seen[family] == 1:
                out["hypotheses"][0]["kind"] = "common"
            return out
        self.models.planner = planner
        inv = await self.start()
        self.assertEqual(seen, {"openai": 2, "anthropic": 2})  # each planner repaired once
        ev = self.db.one("SELECT data FROM events WHERE investigation_id = ? AND kind = 'plan'", (self.inv_id,))
        self.assertEqual(ev["data"]["planners"], 2)
        self.assertEqual(inv["waiting_for"], "plan_approval")
        self.assertIn("red_flag hypothesis is required", self.models.users["Plan"][-1])

    async def test_one_planner_failing_reduces_independence(self):
        def planner(request, family):
            if family == "anthropic":
                raise ModelError("server", "down")
            return base_plan()
        self.models.planner = planner
        inv = await self.start()
        self.assertEqual(inv["waiting_for"], "plan_approval")
        ev = self.db.one("SELECT data FROM events WHERE investigation_id = ? AND kind = 'plan'", (self.inv_id,))
        self.assertEqual((ev["data"]["planners"], ev["data"]["independence"]), (1, "reduced"))
        self.assertNotIn("Synthetic case", json.dumps(ev["data"]))  # events carry counts, not text

    async def test_one_planner_setting_and_autonomous_mode(self):
        inv_id = self.seed({"ensembles": {**S.build("quality").model_dump(mode="json")["ensembles"], "planners": 1},
                            "autonomy": "autonomous"})
        self.inv_id = inv_id
        self.models = Models([f"K{i}" for i in self.item_ids])
        inv = await self.start(inv_id)
        self.assertEqual(self.models.calls["Plan"], 1)
        self.assertEqual(inv["outcome"], "incomplete")  # no approval wait: straight on to querying
        self.assertIn("querying", self.reason())


class ApprovalTests(PlanBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.start()

    async def test_retire_and_add(self):
        new = hyp("migraine", explains=("afternoon_headache",))
        ok, inv = await self.approve(retire={"cause_1": "not plausible"}, add=[new])
        self.assertTrue(ok)
        hs = self.hyps()
        self.assertEqual((hs["cause_1"]["status"], hs["cause_1"]["retired_reason"]), ("retired", "user: not plausible"))
        self.assertEqual((hs["migraine"]["origin"], hs["migraine"]["status"]), ("user", "active"))
        cov = self.db.one("SELECT * FROM knowledge_coverage WHERE investigation_id = ? AND item_id = ?",
                          (self.inv_id, self.item_ids[0]))
        self.assertEqual((cov["decision"], cov["hypothesis_id"]), ("excluded", None))
        self.assertEqual(self.db.investigation(self.inv_id)["stage"], "querying")

    async def test_retiring_the_last_red_flag_needs_confirmation(self):
        with self.assertRaises(P.NeedsConfirmation):
            await self.approve(retire={"raised_pressure": "no"})
        inv = self.db.investigation(self.inv_id)
        self.assertEqual(inv["waiting_for"], "plan_approval")  # nothing written
        self.assertEqual(self.hyps()["raised_pressure"]["status"], "active")
        ok, inv = await self.approve(retire={"raised_pressure": "no"}, confirm_no_red_flag=True)
        self.assertTrue(ok)
        self.assertEqual(self.hyps()["raised_pressure"]["status"], "retired")
        # It was the only hypothesis explaining `two_weeks`: the feature is now uncovered.
        row = self.db.one("SELECT * FROM plan_uncovered WHERE investigation_id = ? AND feature_key = 'two_weeks'",
                          (self.inv_id,))
        self.assertIsNotNone(row)

    async def test_edits_are_checked(self):
        with self.assertRaisesRegex(ValueError, "never reused"):
            await self.approve(add=[hyp("cause_2")])
        with self.assertRaisesRegex(ValueError, "unknown case feature"):
            await self.approve(add=[hyp("other", explains=("nope",))])
        with self.assertRaisesRegex(ValueError, "not an active hypothesis"):
            await self.approve(retire={"nope": "x"})
        with self.assertRaisesRegex(ValueError, "3 to 8 hypotheses"):
            await self.approve(retire={"cause_1": "x", "cause_2": "x", "cause_3": "x"})
        self.assertEqual(self.db.investigation(self.inv_id)["waiting_for"], "plan_approval")


class CheckTests(PlanBase):
    def test_every_feature_explained_or_uncovered(self):
        hs = [Hypothesis.model_validate(hyp("a", "red_flag")), Hypothesis.model_validate(hyp("b")),
              Hypothesis.model_validate(hyp("c"))]
        keys = {"afternoon_headache", "two_weeks"}
        with self.assertRaisesRegex(ValueError, "two_weeks"):
            P.check_hypotheses(hs, [], feature_keys=keys, item_refs=set(), lim=(3, 8))
        P.check_hypotheses(hs, ["two_weeks"], feature_keys=keys, item_refs=set(), lim=(3, 8))
        with self.assertRaisesRegex(ValueError, "not a differential"):
            P.check_hypotheses([*hs[:2], Hypothesis.model_validate(hyp("d", item="K77"))], ["two_weeks"],
                               feature_keys=keys, item_refs={"K1"}, lim=(3, 8))
        with self.assertRaisesRegex(ValueError, "unique"):
            P.check_hypotheses([hs[0], hs[1], hs[1]], ["two_weeks"], feature_keys=keys, item_refs=set(), lim=(3, 8))
