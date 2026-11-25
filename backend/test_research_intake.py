"""1C.1 intake (PLAN B8.1, B4 intake row, B0 populations): generate → code checks → brief check → MeSH.

Fake providers: family A (openai) answers the intake call, family B (anthropic) the brief check. MeSH lookups
come from the cache (seeded here), so no network. All case text is synthetic.
"""

import json

from research_testkit import ResearchTestCase

from app.research import brief as B, llm, mesh, settings as S
from app.research.prices import PriceTable
from app.research.providers import FakeProvider
from app.research.ratelimit import Limiters
from app.research.runner import Worker
from app.research.schemas import BriefCheck, CaseBrief, CheckItem, IntakeResult
from app.research.steps import build_pipeline, intake

EN_TEXT = ("Synthetic case. I have had ringing in my left ear for three weeks. "
           "It is worse at night. I take methotrexate for arthritis.")
# U1 "Synthetic case." U2 ringing… U3 worse at night. U4 methotrexate…

TINNITUS = mesh.Descriptor("D014012", "Tinnitus", ["Ringing in the ears"], ["C09.218.458.341.887.886",
                                                                             "C10.597.751.418.341.887.886",
                                                                             "C23.888.592.763.393.341.887.886"])


def fact(text, *segs, **extra):
    return {"text": text, "seg_ids": list(segs), **extra}


def intake_output(**over):
    out = {
        "is_health_problem": True, "is_human": True, "not_accepted_reason": None, "language": "en",
        "translations": [], "title": "Ringing in the left ear",
        "complaints": [{"key": "tinnitus", "text": "Ringing in the left ear", "seg_ids": ["U2"],
                        "mesh_candidates": ["Tinnitus"]}],
        "proposed_systems": ["ent"],
        "brief": {
            "chief_complaint": fact("Ringing in the left ear", "U2"),
            "symptoms": [{"name": fact("Ringing", "U2"), "laterality": fact("Left", "U2"),
                          "duration": fact("Three weeks", "U2"), "pattern": fact("Worse at night", "U3")}],
            "medications": [fact("Methotrexate", "U4")],
            "chronic_conditions": [fact("Arthritis", "U4")],
            "unknowns": [{"key": "hearing_loss", "text": "Hearing loss not mentioned", "priority": 1}],
        },
    }
    out.update(over)
    return out


def all_stated(request):
    """A brief check that confirms every fact it is shown."""
    body = request.user.split("Facts:\n", 1)[1].rsplit("</material>", 1)[0]
    return {"items": [{"path": f["path"], "verdict": "stated", "reason": "said"} for f in json.loads(body)]}


SAFE = {"level": "none", "crisis": False, "flags": [], "guideline_checks": [], "recommendation": "",
        "rule_out_questions": []}


def with_safety(handler):
    """Intake tests: the safety panel (1C.2) that follows intake answers `none`."""
    def wrapped(request):
        if request.schema_name == "SafetyReview":
            return SAFE
        if request.schema_name == "QuestionSet":  # questions (1C.4): none proposed; code adds the required ones
            return {"questions": [], "merge_rule_outs": []}
        return handler(request)
    return wrapped


class IntakeBase(ResearchTestCase):
    def setUp(self):
        super().setUp()
        mesh._store(self.db, "Tinnitus", TINNITUS)

    def ctx(self, a, b=all_stated):
        self.a = FakeProvider("openai", with_safety(a))
        self.b = FakeProvider("anthropic", with_safety(b))
        return llm.CallContext(db=self.db, settings=S.build("quality"),
                               providers={"openai": self.a, "anthropic": self.b},
                               limiters=Limiters({}), prices=PriceTable())

    async def run_case(self, text, a, b=all_stated):
        ctx = self.ctx(a, b)
        pipeline, self.resolvers = build_pipeline(ctx)
        self.worker = Worker(self.db, pipeline, services=ctx, max_parallel=2)
        inv_id = self.db.create_investigation(text, profile="quality", settings_snapshot={}, synthetic=True)
        await self.worker.machine.advance(inv_id)
        await self.worker.run_until_idle()
        return inv_id

    def brief(self, inv_id, version=1) -> CaseBrief:
        row = self.db.one("SELECT data FROM case_briefs WHERE investigation_id = ? AND version = ?", (inv_id, version))
        return CaseBrief.model_validate(row["data"])


class IntakeStepTests(IntakeBase):
    async def test_accepted_case_saves_brief_complaints_systems(self):
        inv_id = await self.run_case(EN_TEXT, lambda r: intake_output(proposed_systems=["ent", "musculoskeletal"]))
        inv = self.db.investigation(inv_id)
        # Intake → safety → grounding (a gap: no network in tests) → safety → questions → wait for answers.
        self.assertEqual((inv["status"], inv["waiting_for"]), ("waiting_user", "answers"))
        self.assertEqual(self.db.one("SELECT status FROM grounding_runs")["status"], "gap")
        asked = {r["key"] for r in self.db.query("SELECT key FROM clarifying_questions WHERE qround = 1")}
        self.assertIn("age", asked)  # a priority-1 unknown added by code
        segs = self.db.query("SELECT seg_id, origin FROM user_segments WHERE investigation_id = ? ORDER BY seg_id",
                             (inv_id,))
        self.assertEqual([s["seg_id"] for s in segs], ["U1", "U2", "U3", "U4"])
        [complaint] = inv["complaints"]
        self.assertEqual((complaint["key"], [m["ui"] for m in complaint["mesh"]]), ("tinnitus", ["D014012"]))
        # Systems are the union of the MeSH mapping and the model's proposals.
        self.assertEqual(inv["systems"], ["ent", "neurology", "musculoskeletal"])
        self.assertEqual(inv["special_populations"], ["immunocompromised"])  # methotrexate (keyword backstop)
        self.assertEqual(inv["title"], "Ringing in the left ear")
        brief = self.brief(inv_id)
        self.assertEqual({u.key for u in brief.unknowns}, {"hearing_loss", "age", "sex"})
        row = self.db.one("SELECT * FROM case_briefs WHERE investigation_id = ?", (inv_id,))
        self.assertEqual((row["source"], row["prompt_version"]), ("intake:p1", "intake.generate.v1+intake.check.v1"))
        self.assertEqual(row["check_results"]["counts"]["not_stated"], 0)
        self.assertEqual(len(self.db.query("SELECT * FROM note_outbox WHERE kind = 'case_brief'")), 1)
        # The brief check saw the cited segments.
        self.assertIn("U2: I have had ringing", next(r.user for r in self.b.requests if r.schema_name == "BriefCheck"))

    async def test_invalid_segment_ids_become_unknowns(self):
        out = intake_output()
        out["brief"]["symptoms"][0]["duration"] = fact("Three weeks", "U9")  # no such segment
        out["brief"]["symptoms"][0]["pattern"] = fact("Worse at night", "U3", "U77")  # one good ID
        out["brief"]["medications"] = [fact("Methotrexate", "X1")]
        inv_id = await self.run_case(EN_TEXT, lambda r: out)
        brief = self.brief(inv_id)
        self.assertIsNone(brief.symptoms[0].duration)
        self.assertEqual(brief.symptoms[0].pattern.seg_ids, ["U3"])
        self.assertEqual(brief.medications, [])
        keys = {u.key for u in brief.unknowns}
        self.assertTrue({"duration", "medications"} <= keys)
        stats = self.db.one("SELECT data FROM events WHERE investigation_id = ? AND kind = 'intake'", (inv_id,))["data"]
        self.assertEqual((stats["invalid_seg_ids"], stats["facts_removed_uncited"]), (3, 2))

    async def test_symptom_without_valid_name_is_removed_whole(self):
        out = intake_output()
        out["brief"]["symptoms"][0]["name"] = fact("Ringing", "U99")
        inv_id = await self.run_case(EN_TEXT, lambda r: out)
        self.assertEqual(self.brief(inv_id).symptoms, [])

    async def test_complaint_without_valid_segments_is_dropped(self):
        out = intake_output()
        out["complaints"].append({"key": "dizziness", "text": "Dizziness", "seg_ids": ["U12"],
                                  "mesh_candidates": ["Dizziness"]})
        out["complaints"].append({"key": "tinnitus", "text": "Again", "seg_ids": ["U3"], "mesh_candidates": []})
        inv_id = await self.run_case(EN_TEXT, lambda r: out)
        self.assertEqual([c["key"] for c in self.db.investigation(inv_id)["complaints"]], ["tinnitus", "tinnitus_2"])

    async def test_unverified_mesh_descriptor_is_dropped(self):
        mesh._store(self.db, "Ear ringing syndrome", None)  # a cached miss: unverified
        out = intake_output(proposed_systems=[])
        out["complaints"][0]["mesh_candidates"] = ["Ear ringing syndrome"]
        inv_id = await self.run_case(EN_TEXT, lambda r: out)
        inv = self.db.investigation(inv_id)
        self.assertEqual(inv["complaints"][0]["mesh"], [])
        self.assertEqual(inv["complaints"][0]["mesh_dropped"], ["Ear ringing syndrome"])
        self.assertEqual(inv["systems"], [])

    async def test_brief_check_removes_unstated_and_marks_implied(self):
        def check(request):
            items = all_stated(request)["items"]
            for item in items:
                if item["path"] == "symptoms[0].laterality":
                    item["verdict"] = "not_stated"
                if item["path"] == "chronic_conditions[0]":
                    item["verdict"] = "implied"
            return {"items": items}
        inv_id = await self.run_case(EN_TEXT, lambda r: intake_output(), check)
        brief = self.brief(inv_id)
        self.assertIsNone(brief.symptoms[0].laterality)
        self.assertIn("laterality", {u.key for u in brief.unknowns})
        self.assertTrue(brief.chronic_conditions[0].implied)
        self.assertFalse(brief.symptoms[0].duration.implied)

    async def test_brief_check_must_cover_every_fact(self):
        calls = []

        def check(request):
            calls.append(1)
            items = all_stated(request)["items"]
            return {"items": items[1:] if len(calls) == 1 else items}  # first answer misses one path
        inv_id = await self.run_case(EN_TEXT, lambda r: intake_output(), check)
        self.assertEqual(len(calls), 2)  # one repair call
        self.assertEqual(self.db.investigation(inv_id)["waiting_for"], "answers")  # the run went on
        self.assertIsNotNone(self.brief(inv_id).chief_complaint)

    async def test_greek_translations_are_required_and_stored(self):
        text = "Συνθετική περίπτωση. Έχω βουητό στο αριστερό αυτί εδώ και τρεις εβδομάδες."
        calls = []

        def generate(request):
            calls.append(1)
            translations = [{"seg_id": "U1", "text_en": "Synthetic case."}]
            if len(calls) > 1:
                translations.append({"seg_id": "U2", "text_en": "I have had ringing in my left ear for three weeks."})
            out = intake_output(language="el", translations=translations)
            out["brief"]["symptoms"][0]["pattern"] = None
            out["brief"]["medications"] = []
            out["brief"]["chronic_conditions"] = []
            return out
        inv_id = await self.run_case(text, generate)
        self.assertEqual(len(calls), 2)  # the incomplete translation triggered one repair
        inv = self.db.investigation(inv_id)
        self.assertEqual(inv["problem_lang"], "el")
        self.assertEqual(inv["problem_text_en"], "Synthetic case. I have had ringing in my left ear for three weeks.")
        row = self.db.one("SELECT text, text_en FROM user_segments WHERE investigation_id = ? AND seg_id = 'U2'",
                          (inv_id,))
        self.assertTrue(row["text"].startswith("Έχω"))
        self.assertTrue(row["text_en"].startswith("I have had"))
        self.assertIn("U2 (English): I have had", next(r.user for r in self.b.requests if r.schema_name == "BriefCheck"))

    async def test_not_human_waits_for_rewrite_then_restarts_intake(self):
        answers = [intake_output(is_human=False, not_accepted_reason="not_human", complaints=[], brief={}),
                   intake_output()]
        inv_id = await self.run_case("Synthetic case. My dog has itchy ears.", lambda r: answers.pop(0))
        inv = self.db.investigation(inv_id)
        self.assertEqual((inv["status"], inv["waiting_for"]), ("waiting_user", "problem_rewrite"))
        self.assertEqual(self.db.query("SELECT * FROM case_briefs"), [])
        waiting = self.db.one("SELECT data FROM events WHERE investigation_id = ? AND kind = 'waiting'", (inv_id,))
        self.assertIn("vet", waiting["data"]["message"])
        self.assertIsNone(self.resolvers["problem_rewrite"](type("C", (), {"rewrite": None})(), inv))

        await self.worker.machine.resolve_wait(inv_id, "problem_rewrite", intake.rewrite(EN_TEXT))
        await self.worker.run_until_idle()
        inv = self.db.investigation(inv_id)
        self.assertEqual((inv["problem_version"], inv["waiting_for"]), (2, "answers"))
        self.assertEqual(self.db.one("SELECT source FROM case_briefs")["source"], "intake:p2")
        self.assertEqual(len(self.db.tasks(inv_id, step="intake")), 2)

    async def test_not_health_and_dose_requests_wait_for_rewrite(self):
        for reason, word in (("not_health", "health problem"), ("prescription_or_dose", "dose")):
            with self.subTest(reason):
                out = intake_output(is_health_problem=reason != "not_health", not_accepted_reason=reason,
                                    complaints=[], brief={})
                inv_id = await self.run_case(f"Synthetic case. Some text about {reason}.", lambda r: out)
                inv = self.db.investigation(inv_id)
                self.assertEqual(inv["waiting_for"], "problem_rewrite")
                msg = self.db.one("SELECT data FROM events WHERE investigation_id = ? AND kind = 'waiting'",
                                  (inv_id,))["data"]["message"]
                self.assertIn(word, msg)

    async def test_rewrite_rejects_empty_text(self):
        with self.assertRaises(ValueError):
            intake.rewrite("   ")

    async def test_events_hold_no_problem_text(self):
        inv_id = await self.run_case(EN_TEXT, lambda r: intake_output())
        events = json.dumps(self.db.query("SELECT message, data FROM events WHERE investigation_id = ?", (inv_id,)))
        for phrase in ("ringing", "methotrexate", "arthritis", "night"):
            self.assertNotIn(phrase, events.lower())


class BriefCodeTests(IntakeBase):
    def make(self, **demo) -> CaseBrief:
        return CaseBrief.model_validate({"demographics": demo})

    def test_populations_from_age_pregnancy_and_medications(self):
        cases = [
            ({"age": fact("6 months", "U1", years=0.5)}, ["infant"]),
            ({"age": fact("10", "U1", years=10)}, ["child"]),
            ({"age": fact("17", "U1", years=17)}, ["child"]),
            ({"age": fact("40", "U1", years=40)}, []),
            ({"age": fact("70", "U1", years=70)}, ["older"]),
            ({"pregnancy": fact("Pregnant", "U1", status="pregnant", weeks=20)}, ["pregnancy"]),
            ({"pregnancy": fact("Gave birth", "U1", status="postpartum")}, ["pregnancy"]),
            ({"pregnancy": fact("Not pregnant", "U1", status="not_pregnant")}, []),
        ]
        for demo, expected in cases:
            with self.subTest(demo=demo):
                self.assertEqual(B.populations(self.make(**demo)), expected)
        meds = CaseBrief.model_validate({"medications": [fact("Tacrolimus after a kidney transplant", "U1")],
                                         "chronic_conditions": [fact("Heart failure", "U2",
                                                                     flags=["serious_chronic"])]})
        self.assertEqual(B.populations(meds), ["immunocompromised", "serious_chronic"])
        flagged = CaseBrief.model_validate({"medications": [fact("A daily tablet", "U1", flags=["immunosuppressant"])]})
        self.assertEqual(B.populations(flagged), ["immunocompromised"])
        self.assertEqual(B.populations(CaseBrief.model_validate({"medications": [fact("Paracetamol", "U1")]})), [])

    def test_case_facts_for_rules(self):
        brief = CaseBrief.model_validate({
            "demographics": {"age": fact("30", "U1", years=30), "sex": fact("Woman", "U1", value="female"),
                             "pregnancy": fact("Pregnant", "U1", status="pregnant", weeks=12)},
            "medications": [fact("Warfarin", "U2")], "chronic_conditions": [fact("Type 2 diabetes", "U2")]})
        self.assertEqual(B.case_facts(brief), {"age_years": 30, "age_months": 360, "sex": "female", "pregnant": True,
                                               "postpartum": False, "weeks_pregnant": 12, "on_anticoagulants": True,
                                               "diabetes": True})
        self.assertEqual(B.case_facts(CaseBrief()), {})  # missing → absent → unknown for the rules

    def test_keyword_flags_avoid_common_words(self):
        brief = CaseBrief.model_validate({"medications": [fact("I accept any concept except this one", "U1")]})
        self.assertEqual(B.flags(brief), set())

    def test_population_key(self):
        self.assertEqual(B.population_key([]), "general")
        self.assertEqual(B.population_key(["older", "immunocompromised"]), "immunocompromised+older")

    def test_paths_and_check_cover(self):
        brief = IntakeResult.model_validate(intake_output()).brief
        paths = B.fact_paths(brief)
        self.assertEqual(paths[:3], ["chief_complaint", "symptoms[0].name", "symptoms[0].laterality"])
        self.assertIn("medications[0]", paths)
        self.assertNotIn("unknowns[0]", paths)
        B.check_covers(BriefCheck(items=[CheckItem(path=p, verdict="stated", reason="") for p in paths]), paths)
        with self.assertRaises(ValueError):
            B.check_covers(BriefCheck(items=[]), paths)
        with self.assertRaises(ValueError):
            B.parse_path("symptoms[0]..name")

    def test_missing_verdict_does_not_survive(self):
        brief = IntakeResult.model_validate(intake_output()).brief
        out, counts = B.apply_check(brief, BriefCheck(items=[]))
        self.assertIsNone(out.chief_complaint)
        self.assertEqual(out.symptoms, [])
        self.assertEqual(counts["stated"], 0)

    def test_validator_requires_reason_and_complaints(self):
        check = intake.validator(["U1", "U2"])
        with self.assertRaises(ValueError):
            check(IntakeResult.model_validate(intake_output(is_human=False, complaints=[], brief={})))
        with self.assertRaises(ValueError):
            check(IntakeResult.model_validate(intake_output(complaints=[])))
        with self.assertRaises(ValueError):
            check(IntakeResult.model_validate(intake_output(translations=[{"seg_id": "U5", "text_en": "x"}])))
        check(IntakeResult.model_validate(intake_output()))


if __name__ == "__main__":
    import unittest
    unittest.main()
