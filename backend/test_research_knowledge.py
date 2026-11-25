"""1B.3 knowledge loader, validator and merge: conditions (three-state, negation), populations, schema,
validation, duplicate merging, trust levels, disagreement flags. Synthetic text only."""

import json
import tempfile
import unittest
from pathlib import Path

import yaml

import research_testkit  # noqa: F401  (installs the no-network guard)
from app.research.knowledge import conditions as C
from app.research.knowledge import loader as L
from app.research.knowledge import merge as M
from app.research.knowledge import validator as V


def case(*texts, **facts):
    return C.Case(segments=[(f"U{i}", t) for i, t in enumerate(texts, 1)], facts=facts)


def ev(node, *texts, **facts):
    return C.evaluate(C.compile_condition(node), case(*texts, **facts))


class ConditionTests(unittest.TestCase):
    def test_text_mentions_and_segment_ids(self):
        r = ev({"text": [r"chest pain"]}, "I have had chest pain since noon.", "Other thing.")
        self.assertEqual((r.state, r.seg_ids), ("true", ["U1"]))
        self.assertEqual(ev({"text": [r"chest pain"]}, "My knee hurts.").state, "false")

    def test_whole_words_and_case(self):
        self.assertEqual(ev({"text": ["rash"]}, "A RASH appeared.").state, "true")
        self.assertEqual(ev({"text": ["rash"]}, "I was rashly brave.").state, "false")

    def test_negation(self):
        node = {"text": [r"fever"]}
        for text in ("No fever.", "I don't have a fever.", "I do not have any fever.", "Denies fever.",
                     "There was never a fever.", "Without fever.", "I haven’t had a fever."):
            with self.subTest(text=text):
                self.assertEqual(ev(node, text).state, "false")
        for text in ("I have a fever.", "No cough but a high fever.", "Not sure, maybe a fever.",
                     "No headache. Fever since Monday.", "I had no idea the fever would last this long and"):
            with self.subTest(text=text):
                self.assertEqual(ev(node, text).state, "true")

    def test_negation_window(self):
        far = "No, " + " ".join(["word"] * 8) + " fever."
        self.assertEqual(ev({"text": ["fever"]}, far).state, "true")

    def test_mention_is_unknown_when_never_mentioned(self):
        node = {"mention": [r"short of breath", r"breathless"]}
        self.assertEqual(ev(node, "I feel breathless.").state, "true")
        self.assertEqual(ev(node, "I am not short of breath.").state, "false")
        self.assertEqual(ev(node, "My chest hurts.").state, "unknown")

    def test_all_any_not(self):
        node = {"all": [{"text": ["chest pain"]}, {"any": [{"mention": ["breathless"]}, {"mention": ["sweat\\w*"]}]}]}
        self.assertEqual(ev(node, "Chest pain and sweating.").state, "true")
        self.assertEqual(ev(node, "Chest pain.").state, "unknown")
        self.assertEqual(ev(node, "Chest pain, not breathless and not sweating.").state, "false")
        self.assertEqual(ev(node, "Breathless and sweating.").state, "false")
        r = ev(node, "Chest pain.", "Very sweaty.")
        self.assertEqual(r.state, "true")
        self.assertEqual(r.seg_ids, ["U1", "U2"])
        self.assertEqual(ev({"not": {"text": ["fever"]}}, "No fever.").state, "true")

    def test_facts(self):
        node = {"fact": "age_months", "lt": 3}
        self.assertEqual(ev(node, "x", age_months=2).state, "true")
        self.assertEqual(ev(node, "x", age_months=5).state, "false")
        r = ev(node, "x")
        self.assertEqual((r.state, r.unknown_facts), ("unknown", ["age_months"]))
        self.assertEqual(ev({"fact": "sex", "in": ["female"]}, "x", sex="female").state, "true")

    def test_fact_missing_default(self):
        node = {"fact": "diabetes", "eq": True, "missing": False}
        self.assertEqual(ev(node, "x").state, "false")
        self.assertEqual(ev(node, "x", diabetes=True).state, "true")
        with self.assertRaises(C.ConditionError):
            C.compile_condition({"fact": "diabetes", "eq": True, "missing": "no"})

    def test_rule_derives_ages(self):
        r = L.Rule.model_validate({**RULE, "fires": {"fact": "age_months", "lt": 3}, "unknown": "age",
                                   "examples": {"fires": [{"text": "x", "facts": {"age_years": 0.1}}],
                                                "no_fire": [{"text": "x", "facts": {"age_years": 2}}]}})
        self.assertEqual(r.check(case("x", age_years=0.1)).state, "true")

    def test_bad_conditions(self):
        for node in ({}, {"all": []}, {"text": []}, {"fact": "shoe_size", "gt": 3}, {"fact": "age_years"},
                     {"frobnicate": 1}, {"text": ["(unclosed"]}, "chest pain"):
            with self.subTest(node=node):
                with self.assertRaises(C.ConditionError):
                    C.compile_condition(node)


class PopulationTests(unittest.TestCase):
    def test_populations(self):
        P = L.population_state
        self.assertEqual(P("all", {}), "true")
        self.assertEqual(P("infant", {"age_months": 2}), "true")
        self.assertEqual(P("infant", {"age_years": 3}), "false")
        self.assertEqual(P("child", {"age_years": 3}), "true")
        self.assertEqual(P("child", {}), "unknown")
        self.assertEqual(P("older", {"age_years": 70}), "true")
        self.assertEqual(P("pregnancy", {"pregnant": True}), "true")
        self.assertEqual(P("pregnancy", {"postpartum": True}), "true")
        self.assertEqual(P("pregnancy", {"sex": "male"}), "false")
        self.assertEqual(P("pregnancy", {"sex": "female", "age_years": 30}), "unknown")
        self.assertEqual(P("pregnancy", {"pregnant": False}), "false")
        self.assertEqual(P("immunocompromised", {"chemotherapy": True}), "true")
        self.assertEqual(P("immunocompromised", {}), "unknown")


RULE = {
    "id": "test.chest_pain", "area": "heart_lungs_allergy", "level": "emergency", "population": "all",
    "title": "Chest pain with breathlessness",
    "fires": {"all": [{"text": ["chest pain"]}, {"mention": ["breathless"]}]},
    "unknown": "breathlessness", "rule_out_question": "Are you breathless?", "sources": ["src_a"],
    "examples": {"fires": [{"text": "Chest pain and breathless."}], "no_fire": [{"text": "Knee pain."}],
                 "unknown": [{"text": "Chest pain."}]},
}
SOURCES = {"version": 1, "sources": [{"id": "src_a", "title": "Source A", "publisher": "Pub", "url": "https://a.test/",
                                      "kind": "triage"}]}


class SchemaAndValidatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "sources.yaml").write_text(yaml.safe_dump(SOURCES), "utf-8")
        self.sources = L.load_sources(self.tmp / "sources.yaml")

    def rule(self, **changes):
        return L.Rule.model_validate({**RULE, **changes})

    def test_rule_checks_population_and_conditions(self):
        r = self.rule(population="child", unknown="age")
        self.assertEqual(r.check(case("Chest pain, breathless.", age_years=5)).state, "true")
        self.assertEqual(r.check(case("Chest pain, breathless.", age_years=40)).state, "false")
        res = r.check(case("Chest pain, breathless."))
        self.assertEqual((res.state, res.unknown_facts), ("unknown", ["population:child"]))

    def test_valid_rule_has_no_problems(self):
        self.assertEqual(V.check_rule(self.rule(), self.sources), [])

    def test_examples_are_the_unit_tests(self):
        bad = self.rule(examples={"fires": [{"text": "Knee pain."}], "no_fire": [{"text": "Chest pain, breathless."}]})
        problems = V.check_rule(bad, self.sources)
        self.assertEqual(len(problems), 2, problems)

    def test_schema_errors(self):
        for change in ({"level": "soonish"}, {"area": "elbows"}, {"rule_out_question": "Tell me."},
                       {"sources": []}, {"id": "nodots"}, {"fires": {"text": []}}, {"extra": 1},
                       {"examples": {"fires": [{"text": "x"}], "no_fire": []}}):
            with self.subTest(change=change):
                with self.assertRaises(Exception):
                    self.rule(**change)

    def test_unknown_source_and_missing_unknown_attribute(self):
        problems = V.check_rule(self.rule(sources=["nope"], unknown=None), self.sources)
        self.assertTrue(any("unknown source nope" in p for p in problems))
        self.assertTrue(any("needs an `unknown` attribute" in p for p in problems))

    def test_crisis_only_in_mental_health(self):
        self.assertTrue(any("crisis" in p for p in V.check_rule(self.rule(crisis=True), self.sources)))

    def test_floor_coverage(self):
        floor = L.Floor.model_validate({"version": 1, "review": {}, "rules": [RULE]})
        problems = V.check_floor(floor, self.sources)
        self.assertTrue(any("area mental_health" in p for p in problems))
        self.assertTrue(any("population infant" in p for p in problems))
        self.assertTrue(any("crisis" in p for p in problems))
        self.assertTrue(any("no urgent rule" in p for p in problems))

    def test_duplicate_ids(self):
        self.assertTrue(any("duplicate" in p for p in V.check_rules([self.rule(), self.rule()], self.sources)))

    def test_sources_lock(self):
        lock = {"sources": {"src_a": {"url": "https://a.test/", "status": "ok"}}}
        self.assertEqual(V.check_sources(self.sources, lock), [])
        self.assertIn("never checked", V.check_sources(self.sources, {"sources": {}})[0])
        self.assertIn("URL changed", V.check_sources(self.sources, {"sources": {"src_a": {"url": "https://b/", "status": "ok"}}})[0])
        self.assertIn("did not resolve", V.check_sources(self.sources, {"sources": {"src_a": {"url": "https://a.test/", "status": "404"}}})[0])
        self.assertIn("not cited", V.check_sources(self.sources, lock, used=set())[0])

    def test_duplicate_sources_rejected(self):
        dup = {"version": 1, "sources": SOURCES["sources"] * 2}
        (self.tmp / "dup.yaml").write_text(yaml.safe_dump(dup), "utf-8")
        with self.assertRaises(ValueError):
            L.load_sources(self.tmp / "dup.yaml")

    def pack(self, **changes):
        data = {"version": 1, "complaint": "testpain", "mesh": ["D000001"], "review": {},
                "red_flags": [RULE],
                "etiologies": [{"id": "e1", "name": "Cause one", "sources": ["src_a"]}],
                "history_checklist": [{"id": "h1", "attribute": "onset", "priority": "high", "question": "When?",
                                       "sources": ["src_a"]}],
                "evaluations": [{"id": "v1", "test": "Exam", "indication": "always", "sources": ["src_a"]}],
                "management_topics": [{"id": "m1", "topic": "Sound therapy", "context": "options", "sources": ["src_a"]}],
                "seed_literature": [{"pmid": "123", "kind": "guideline"}]}
        data.update(changes)
        return L.Pack.model_validate(data)

    def test_pack_checks(self):
        lock = {"papers": {"123": {"status": "ok"}}}
        self.assertEqual(V.check_pack(self.pack(), self.sources, lock), [])
        self.assertTrue(any("seed 123" in p for p in V.check_pack(self.pack(), self.sources, {"papers": {}})))
        dosed = self.pack(management_topics=[{"id": "m1", "topic": "A drug", "context": "take 20 mg twice a day",
                                              "sources": ["src_a"]}])
        self.assertTrue(any("doses" in p for p in V.check_pack(dosed, self.sources, lock)))
        dup = self.pack(evaluations=[{"id": "e1", "test": "x", "indication": "y", "sources": ["src_a"]}])
        self.assertTrue(any("duplicate item id e1" in p for p in V.check_pack(dup, self.sources, lock)))
        with self.assertRaises(Exception):
            self.pack(mesh=["tinnitus"])

    def test_pack_file_name_must_match(self):
        folder = self.tmp / "packs"
        folder.mkdir()
        (folder / "other.yaml").write_text(yaml.safe_dump(self.pack().model_dump(exclude_none=True)), "utf-8")
        with self.assertRaises(ValueError):
            L.load_packs(folder)

    def test_packs_for_mesh(self):
        packs = {"testpain": self.pack()}
        self.assertEqual([p.complaint for p in L.packs_for(["D000001", "D9"], packs)], ["testpain"])
        self.assertEqual(L.packs_for(["D9"], packs), [])


def item(kind="differential", key="k", text="Somatic tinnitus", origin="guideline", verdicts=("supported",), **kw):
    atts = [M.Attestation(literature_id=i, sentence_ids=[f"S1.P1.s{i}"], quote="q", verdict=v)
            for i, v in enumerate(verdicts, 1)]
    return M.Item(kind, key, text, origin, attestations=atts, **kw)


class MergeTests(unittest.TestCase):
    def test_trust_levels(self):
        self.assertEqual(item(origin="curated", verdicts=()).trust, "curated")
        self.assertEqual(item(verdicts=("partial", "supported")).trust, "verified")
        self.assertEqual(item(verdicts=("partial",)).trust, "partial")
        self.assertEqual(item(origin="proposed", verdicts=()).trust, "proposed")

    def test_unsupported_attestations_are_dropped_and_empty_guideline_items_rejected(self):
        res = M.merge([item(key="a", verdicts=("unsupported",)), item(key="b", text="Other", verdicts=("unsupported", "partial"))])
        by_key = {i.key: i for i in res.items}
        self.assertEqual(by_key["a"].status, "rejected")
        self.assertEqual(res.rejected, ["a"])
        self.assertEqual((by_key["b"].status, by_key["b"].trust, len(by_key["b"].attestations)), ("active", "partial", 1))

    def test_duplicates_by_normalised_name_keep_the_most_trusted(self):
        res = M.merge([item(key="g1", text="The somatic tinnitus"), item(key="c1", text="Somatic Tinnitus",
                                                                        origin="curated", verdicts=(), sources=["nice"])])
        self.assertEqual(len(res.items), 1)
        kept = res.items[0]
        self.assertEqual((kept.key, kept.trust, kept.merged_from, kept.sources), ("c1", "curated", ["g1"], ["nice"]))
        self.assertEqual(len(kept.attestations), 1)  # the guideline's attestation joins the curated item
        self.assertEqual(res.merges, [("c1", "g1")])

    def test_duplicates_by_mesh_descriptor(self):
        res = M.merge([item(key="a", text="Meniere disease", mesh_ui="D008575"),
                       item(key="b", text="Endolymphatic hydrops", mesh_ui="D008575")])
        self.assertEqual(len(res.items), 1)

    def test_kind_and_population_separate_items(self):
        res = M.merge([item(key="a"), item(key="b", kind="evaluation"), item(key="c", population="child")])
        self.assertEqual(len(res.items), 3)

    def test_borderline_pairs_go_to_the_judge(self):
        a, b = item(key="a", text="Noise induced hearing loss"), item(key="b", text="Noise induced hearing damage")
        self.assertEqual(len(M.merge([a, b]).items), 2)  # no judge → apart
        self.assertEqual(len(M.merge([a, b], judge=lambda x, y: True).items), 1)
        self.assertEqual(len(M.merge([a, b], judge=lambda x, y: False).items), 2)
        calls = []
        M.merge([a, item(key="c", text="Pulsatile tinnitus")], judge=lambda x, y: calls.append(1) or True)
        self.assertEqual(calls, [])  # not borderline → never asked

    def test_disagreements_are_kept_and_flagged(self):
        a = item(key="a", text="Refer for imaging", kind="evaluation", attributes={"age_threshold": 50})
        b = item(key="b", text="Refer for imaging", kind="evaluation", attributes={"age_threshold": 55})
        res = M.merge([a, b])
        self.assertEqual(len(res.items), 2)
        self.assertEqual({i.disagreement for i in res.items}, {"evaluation:imaging refer"})
        self.assertEqual(res.disagreements, ["evaluation:imaging refer"])

    def test_wording_differences_are_not_disagreements(self):
        a = item(key="a", text="Pulsatile tinnitus", kind="red_flag", attributes={"urgency": "urgent", "question": "A?"})
        b = item(key="b", text="Pulsatile tinnitus", kind="red_flag", attributes={"urgency": "urgent", "question": "B?"})
        res = M.merge([a, b])
        self.assertEqual((len(res.items), res.disagreements), (1, []))
        c = item(key="c", text="Pulsatile tinnitus", kind="red_flag", attributes={"level": "routine_review"})
        d = item(key="d", text="Pulsatile tinnitus", kind="red_flag", attributes={"urgency": "urgent"})
        self.assertEqual(len(M.merge([c, d]).disagreements), 1)  # different urgency is a real disagreement
        e = item(key="e", text="Pulsatile tinnitus", kind="red_flag", attributes={"urgency": "routine"})
        self.assertEqual(M.merge([c, e]).disagreements, [])  # routine = routine_review

    def test_judged_pairs_match_any_group_member(self):
        a = item(key="a", text="Pulsatile tinnitus", kind="red_flag")
        b = item(key="b", text="pulsatile  tinnitus", kind="red_flag")  # same as a by name
        c = item(key="c", text="Pulse-synchronous sound", kind="red_flag")  # judged the same as b only
        res = M.merge([a, b, c], same={frozenset(("b", "c"))})
        self.assertEqual(len(res.items), 1)
        self.assertEqual(sorted(res.items[0].merged_from), ["b", "c"])

    def test_curated_items_from_floor_and_packs(self):
        floor = L.Floor.model_validate({"version": 1, "review": {}, "rules": [RULE]})
        items = M.from_floor_and_packs(floor, [])
        self.assertEqual([(i.kind, i.key, i.trust) for i in items], [("red_flag", "rule:test.chest_pain", "curated")])


if __name__ == "__main__":
    unittest.main()
