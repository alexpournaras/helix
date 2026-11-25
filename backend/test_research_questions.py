"""1C.4 questions and brief update (PLAN B8.4, B8.5, B4 waits). Synthetic text; fake providers; no network."""

import json
import re

from research_testkit import ResearchTestCase

from app.research import brief as B, llm, settings as S
from app.research.prices import PriceTable
from app.research.providers import FakeProvider
from app.research.ratelimit import Limiters
from app.research.runner import Worker
from app.research.schemas import CaseBrief, QuestionSet
from app.research.segment import user_segments
from app.research.steps import build_pipeline, intake, questions as Q

TEXT = "Synthetic case. I am a 27-year-old woman. Since yesterday I have pain in my lower belly."
BRIEF = {"chief_complaint": {"text": "Lower abdominal pain", "seg_ids": ["U3"]},
         "symptoms": [{"name": {"text": "Lower abdominal pain", "seg_ids": ["U3"]},
                       "onset": {"text": "Since yesterday", "seg_ids": ["U3"]}}],
         "demographics": {"age": {"text": "27", "seg_ids": ["U2"], "years": 27},
                          "sex": {"text": "Woman", "seg_ids": ["U2"], "value": "female"}},
         "unknowns": [{"key": "fever", "text": "Fever not mentioned", "priority": 1},
                      {"key": "bowel_habit", "text": "Bowel habit not mentioned", "priority": 2}]}
COMPLAINT = {"key": "abdominal_pain", "text": "Lower abdominal pain", "seg_ids": ["U3"], "mesh": [],
             "mesh_dropped": []}
RULE_OUT = "ruleout.rule:gut.abdominal_pain_possible_pregnancy"
COMPLAINT_OUT = {"key": "abdominal_pain", "text": "Lower abdominal pain", "seg_ids": ["U3"], "mesh_candidates": []}


def safe_review(request):
    return {"level": "none", "crisis": False, "flags": [], "guideline_checks": [
        {"item": k, "state": "not_applicable", "seg_ids": []} for k in re.findall(r'"item": "(K\d+)"', request.user)],
        "recommendation": "", "rule_out_questions": []}


def stated(request):
    body = request.user.split("Facts:\n", 1)[1].rsplit("</material>", 1)[0]
    return {"items": [{"path": f["path"], "verdict": "stated", "reason": "r"} for f in json.loads(body)]}


class Models:
    def __init__(self):
        self.questions = lambda request: {"questions": [
            {"key": "fever", "text": "Do you have a fever?", "why": "infection", "answer_type": "yes_no",
             "options": [], "priority": 1, "expected_impact": 0.6, "targets": ["fever"], "item_id": None}],
            "merge_rule_outs": []}
        self.update = None  # set per test
        self.seen = []

    def handler(self, family):
        def h(request):
            self.seen.append(request.schema_name)
            if request.schema_name == "SafetyReview":
                return safe_review(request)
            if request.schema_name == "QuestionSet":
                return self.questions(request)
            if request.schema_name == "BriefUpdate":
                return self.update(request)
            if request.schema_name == "BriefCheck":
                return stated(request)
            raise AssertionError(request.schema_name)
        return h


def not_pregnant_update(request):
    seg = re.findall(r'"seg_id": "(A\d+)"', request.user)
    brief = json.loads(json.dumps(BRIEF))
    brief["demographics"]["pregnancy"] = {"text": "Not pregnant", "seg_ids": [seg[0]], "status": "not_pregnant"}
    brief["unknowns"] = [{"key": "bowel_habit", "text": "Bowel habit not mentioned", "priority": 2}]
    return {"translations": [], "complaints": [dict(COMPLAINT_OUT)], "proposed_systems": ["digestive"],
            "brief": brief, "removed_features": []}


class QuestionBase(ResearchTestCase):
    def setUp(self):
        super().setUp()
        self.models = Models()

    def ctx(self):
        self.a = FakeProvider("openai", self.models.handler("openai"))
        self.b = FakeProvider("anthropic", self.models.handler("anthropic"))
        return llm.CallContext(db=self.db, settings=S.build("quality"),
                               providers={"openai": self.a, "anthropic": self.b},
                               limiters=Limiters({}), prices=PriceTable())

    def seed(self, brief=BRIEF, settings=None):
        inv_id = self.db.create_investigation(TEXT, profile="quality", settings_snapshot=settings or {}, synthetic=True)
        with self.db.tx() as conn:
            intake.save_segments(conn, inv_id, user_segments(TEXT))
            intake.save_brief(self.db, conn, inv_id, CaseBrief.model_validate(brief), source="intake:p1", check=None,
                              prompt_version="test")
            self.db.update_investigation(inv_id, conn=conn, stage="safety", complaints=[COMPLAINT])
            self.db.insert(conn, "grounding_runs", {"investigation_id": inv_id, "complaint_key": "abdominal_pain",
                                                    "population": "general", "status": "gap", "knowledge_version": 0})
        return inv_id

    async def start(self, inv_id):
        ctx = self.ctx()
        pipeline, self.resolvers = build_pipeline(ctx)
        self.worker = Worker(self.db, pipeline, services=ctx, max_parallel=2)
        await self.worker.machine.advance(inv_id)
        await self.worker.run_until_idle()
        return self.db.investigation(inv_id)

    def rows(self, inv_id):
        return {r["key"]: r for r in self.db.query("SELECT * FROM clarifying_questions WHERE investigation_id = ?",
                                                   (inv_id,))}

    async def answer(self, inv_id, answers):
        await self.worker.machine.resolve_wait(inv_id, "answers", Q.answer(answers))
        await self.worker.run_until_idle()
        return self.db.investigation(inv_id)

    def reason(self, inv_id):
        row = self.db.one("SELECT data FROM events WHERE investigation_id = ? AND kind = 'complete' ORDER BY id DESC",
                          (inv_id,))
        return (row["data"] or {}).get("reason", "") if row else ""


class QuestionStepTests(QuestionBase):
    async def test_rule_outs_first_then_proposals_and_the_answers_wait(self):
        inv_id = self.seed()
        inv = await self.start(inv_id)
        self.assertEqual((inv["status"], inv["waiting_for"]), ("waiting_user", "answers"))
        rows = self.rows(inv_id)
        self.assertEqual((rows[RULE_OUT]["qround"], rows[RULE_OUT]["priority"]), (1, 0))
        self.assertEqual((rows["fever"]["qround"], rows["fever"]["origin"]), (1, "intake"))
        asked = [r["key"] for r in self.db.query("SELECT key FROM clarifying_questions WHERE qround = 1 "
                                                 "ORDER BY priority, id")]
        self.assertEqual(asked[0], RULE_OUT)  # safety first
        self.assertEqual(len(self.db.query("SELECT * FROM note_outbox WHERE kind = 'clarifications'")), 1)

    async def test_missing_priority_one_unknown_is_added_by_code(self):
        self.models.questions = lambda r: {"questions": [], "merge_rule_outs": []}
        inv_id = self.seed()
        await self.start(inv_id)
        fever = self.rows(inv_id)["fever"]
        self.assertEqual((fever["qround"], fever["priority"]), (1, 1))
        self.assertTrue(fever["text"].endswith("?"))
        self.assertNotIn("bowel_habit", self.rows(inv_id))  # priority 2: only if the model asks it

    async def test_missing_priority_one_history_item_is_added_with_its_wording(self):
        self.models.questions = lambda r: {"questions": [], "merge_rule_outs": []}
        inv_id = self.seed()
        with self.db.tx() as conn:
            item = self.db.insert(conn, "knowledge_items", {
                "investigation_id": inv_id, "version": 0, "kind": "history_item", "key": "pack:x:onset",
                "text": "When did the pain start, and did it start suddenly?", "attributes": {"priority": "high"},
                "origin": "curated", "trust": "curated", "status": "active"})
        await self.start(inv_id)
        row = self.rows(inv_id)[f"history.k{item}"]
        self.assertEqual((row["text"], row["item_id"], row["origin"], row["qround"]),
                         ("When did the pain start, and did it start suddenly?", item, "grounding", 1))

    async def test_keys_are_never_reused(self):
        inv_id = self.seed()
        with self.db.tx() as conn:
            self.db.insert(conn, "clarifying_questions", {"investigation_id": inv_id, "origin": "intake",
                                                          "key": "fever", "text": "Old fever question?",
                                                          "status": "skipped", "qround": 1})
        await self.start(inv_id)
        self.assertEqual(self.rows(inv_id)["fever"]["text"], "Old fever question?")  # not asked again

    async def test_choice_questions_need_two_to_eight_options(self):
        calls = []

        def qs(request):
            calls.append(1)
            n = 1 if len(calls) == 1 else 3
            return {"questions": [{"key": "where", "text": "Where is the pain?", "answer_type": "choice",
                                   "options": [f"o{i}" for i in range(n)], "priority": 2, "expected_impact": 0.3,
                                   "targets": []}], "merge_rule_outs": []}
        self.models.questions = qs
        inv_id = self.seed()
        await self.start(inv_id)
        self.assertEqual(len(calls), 2)  # one repair call
        self.assertEqual(self.rows(inv_id)["where"]["options"], ["o0", "o1", "o2"])

    async def test_at_most_max_per_round_and_the_rest_waits(self):
        self.models.questions = lambda r: {"questions": [
            {"key": f"q{i}", "text": f"Question {i}?", "answer_type": "text", "priority": 2,
             "expected_impact": i / 10, "targets": []} for i in range(5)], "merge_rule_outs": []}
        inv_id = self.seed(settings={"questions": {"max_per_round": 3, "max_rounds": 2}})
        await self.start(inv_id)
        rows = self.rows(inv_id)
        asked = sorted(k for k, r in rows.items() if r["qround"] == 1)
        self.assertEqual(len(asked), 3)
        self.assertIn(RULE_OUT, asked)
        self.assertIn("fever", asked)  # priority-1 before higher impact
        self.assertEqual(rows["q4"]["qround"], 1)  # highest impact of the rest
        self.assertEqual(rows["q0"]["qround"], 0)  # deferred, still open

    async def test_duplicate_panel_rule_outs_are_merged(self):
        def panel(request):
            out = safe_review(request)
            out["rule_out_questions"] = [{"key": "fever_check", "text": "Do you have a fever?"},
                                         {"key": "temperature", "text": "Is your temperature high?"}]
            return out
        self.models.questions = lambda r: {"questions": [], "merge_rule_outs": [
            {"keep": "ruleout.panel.fever_check", "drop": ["ruleout.panel.temperature"]}]}
        inv_id = self.seed()
        ctx_models = self.models.handler
        self.models.handler = lambda family: (lambda req: panel(req) if req.schema_name == "SafetyReview"
                                              else ctx_models(family)(req))
        await self.start(inv_id)
        rows = self.rows(inv_id)
        self.assertEqual(rows["ruleout.panel.temperature"]["status"], "skipped")
        self.assertIn("merged with ruleout.panel.fever_check", rows["ruleout.panel.temperature"]["why"])
        self.assertEqual(rows["ruleout.panel.fever_check"]["qround"], 1)

    async def test_a_failed_questions_task_fails_instead_of_looping(self):
        from app.research.providers import ModelError

        def broken(request):
            raise ModelError("config", "down")
        self.models.questions = broken
        inv_id = self.seed()
        inv = await self.start(inv_id)
        self.assertEqual(inv["status"], "failed")
        # Restarting the stage without retrying the task must not spin safety <-> questions.
        self.db.update_investigation(inv_id, status="active", stage="safety", error=None)
        await self.worker.machine.advance(inv_id)  # would raise "more than 200 stage transitions"
        inv = self.db.investigation(inv_id)
        self.assertEqual(inv["status"], "failed")
        self.assertIn("did not complete", inv["error"])

    def test_rule_outs_from_rules_cannot_be_dropped(self):
        check = Q.validator({RULE_OUT, "ruleout.panel.a"})
        with self.assertRaises(ValueError):
            check(QuestionSet.model_validate({"merge_rule_outs": [{"keep": "ruleout.panel.a", "drop": [RULE_OUT]}]}))
        with self.assertRaises(ValueError):
            check(QuestionSet.model_validate({"merge_rule_outs": [{"keep": "x", "drop": []}]}))


class AnswerAndUpdateTests(QuestionBase):
    async def test_answers_update_the_brief_and_safety_reruns(self):
        self.models.update = not_pregnant_update
        inv_id = self.seed()
        await self.start(inv_id)
        rows = self.rows(inv_id)
        inv = await self.answer(inv_id, {rows[RULE_OUT]["id"]: "No, I am not pregnant.", "fever": "No fever."})
        rows = self.rows(inv_id)
        self.assertEqual((rows[RULE_OUT]["status"], rows["fever"]["status"]), ("answered", "answered"))
        seg = self.db.one("SELECT * FROM user_segments WHERE seg_id = ?", (f"A{rows[RULE_OUT]['id']}",))
        self.assertTrue(seg["text"].startswith("Q: Could you be pregnant"))
        self.assertIn("A: No, I am not pregnant.", seg["text"])
        v2 = self.db.one("SELECT * FROM case_briefs WHERE version = 2")
        self.assertTrue(v2["source"].startswith("brief_update:"))
        brief = CaseBrief.model_validate(v2["data"])
        self.assertEqual(brief.demographics.pregnancy.status, "not_pregnant")
        self.assertNotIn("fever", {u.key for u in brief.unknowns})
        # Safety ran on brief v2: the pregnancy rule is no longer unknown, so research goes on (planning: 1D).
        self.assertEqual([r["brief_version"] for r in self.db.query("SELECT * FROM safety_reviews ORDER BY id")], [1, 2])
        self.assertIn("1D", self.reason(inv_id))
        self.assertEqual(inv["outcome"], "incomplete")

    async def test_all_skipped_goes_on_without_a_brief_update(self):
        inv_id = self.seed(settings={"questions": {"max_per_round": 10, "max_rounds": 1}})
        await self.start(inv_id)
        await self.answer(inv_id, {})
        rows = self.rows(inv_id)
        self.assertTrue(all(r["status"] == "skipped" for r in rows.values() if r["qround"] == 1))
        self.assertEqual(len(self.db.query("SELECT * FROM case_briefs")), 1)
        self.assertNotIn("BriefUpdate", self.models.seen)
        self.assertIn("1D", self.reason(inv_id))  # rounds used up → return stage

    async def test_a_fact_dropped_without_a_contradicting_segment_needs_a_repair(self):
        calls = []

        def update(request):
            calls.append(1)
            out = not_pregnant_update(request)
            if len(calls) == 1:
                out["brief"]["symptoms"][0].pop("onset")  # dropped silently
            else:
                out["removed_features"] = []
            return out
        self.models.update = update
        inv_id = self.seed()
        await self.start(inv_id)
        await self.answer(inv_id, {"fever": "No fever."})
        self.assertEqual(len(calls), 2)
        self.assertIsNotNone(CaseBrief.model_validate(self.db.one(
            "SELECT data FROM case_briefs WHERE version = 2")["data"]).symptoms[0].onset)

    async def test_a_new_complaint_in_the_answers_gets_grounded(self):
        def update(request):
            out = not_pregnant_update(request)
            a = re.findall(r'"seg_id": "(A\d+)"', request.user)[0]
            out["complaints"].append({"key": "back_pain", "text": "Back pain", "seg_ids": [a], "mesh_candidates": []})
            return out
        self.models.update = update
        inv_id = self.seed()
        await self.start(inv_id)
        await self.answer(inv_id, {"fever": "No fever, but my back hurts too."})
        runs = [r["complaint_key"] for r in self.db.query("SELECT * FROM grounding_runs ORDER BY id")]
        self.assertEqual(runs, ["abdominal_pain", "back_pain"])  # only the new complaint is grounded

    async def test_eval_answers_match_by_key_or_words(self):
        self.models.update = not_pregnant_update
        inv_id = self.seed()
        await self.start(inv_id)
        case = type("Case", (), {"answers": {"pregnant": "No.", "fever": "No fever."}})()
        apply = self.resolvers["answers"](case, self.db.investigation(inv_id))
        await self.worker.machine.resolve_wait(inv_id, "answers", apply)
        await self.worker.run_until_idle()
        rows = self.rows(inv_id)
        self.assertEqual(rows[RULE_OUT]["answer"], "No.")  # matched by a word in the question
        self.assertEqual(rows["fever"]["answer"], "No fever.")  # matched by key

    def test_rules_read_only_the_answer_not_the_question(self):
        from app.research.steps import safety
        seg = {"seg_id": "A7", "origin": "answer", "text_en": None,
               "text": "Q: Could you be pregnant, for example a missed or late period or a positive test? A: No."}
        self.assertEqual(safety.english([seg]), [("A7", "No.")])
        problem = {"seg_id": "U1", "origin": "problem", "text": "I might be pregnant. A: no", "text_en": None}
        self.assertEqual(safety.english([problem])[0][1], "I might be pregnant. A: no")

    def test_removal_validator(self):
        prev = CaseBrief.model_validate(BRIEF)
        from app.research.steps.brief_update import removal_validator
        check = removal_validator(prev, {"U1", "U2", "U3", "A1"}, [])
        from app.research.schemas import BriefUpdate
        ok = {"complaints": [dict(COMPLAINT_OUT)], "brief": BRIEF}
        check(BriefUpdate.model_validate(ok))
        reworded = json.loads(json.dumps(BRIEF))
        reworded["symptoms"][0]["onset"]["text"] = "Started yesterday"  # same path: kept
        check(BriefUpdate.model_validate(ok | {"brief": reworded}))
        dropped = json.loads(json.dumps(BRIEF))
        dropped["symptoms"][0].pop("onset")  # its wording appears nowhere else
        with self.assertRaises(ValueError):
            check(BriefUpdate.model_validate(ok | {"brief": dropped}))
        check(BriefUpdate.model_validate(ok | {"brief": dropped, "removed_features": [
            {"key": "symptoms[0].onset", "seg_ids": ["A1"]}]}))
        with self.assertRaises(ValueError):
            check(BriefUpdate.model_validate(ok | {"brief": dropped, "removed_features": [
                {"key": "symptoms[0].onset", "seg_ids": ["A9"]}]}))  # not a real segment
        moved = json.loads(json.dumps(BRIEF))
        moved.pop("chief_complaint")  # same wording still in symptoms[0].name: kept
        check(BriefUpdate.model_validate(ok | {"brief": moved}))


if __name__ == "__main__":
    import unittest
    unittest.main()
