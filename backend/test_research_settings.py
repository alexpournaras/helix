import unittest

from pydantic import ValidationError

from research_testkit import ResearchTestCase

from app.research import settings as S


def avail(*models):
    return lambda ref: ref.model in models


class ProfileTests(unittest.TestCase):
    def test_quality_defaults_match_plan(self):
        q = S.build("quality")
        self.assertEqual(q.roles["intake"].members[0].model, "gpt-6.2-astra")
        self.assertEqual(q.roles["brief_check"].members[0].model, "claude-opus-5-5")
        self.assertEqual([m.model for m in q.roles["verify"].members], ["claude-sonnet-5-5", "gpt-6.2-sol"])
        self.assertEqual(q.roles["synthesis"].members[0].effort, "xhigh")
        self.assertEqual(q.guards.max_total_papers, 120)
        self.assertEqual(q.providers.anthropic["version"], "2026-10-21")
        self.assertEqual(q.autonomy, "review_plan")

    def test_every_profile_keeps_the_safety_floor(self):
        for name in S.PROFILE_OVERLAYS:
            s = S.build(name)
            self.assertGreaterEqual(len(s.roles["safety"].members), 2, name)
            self.assertTrue({"safety", "combined"} & set(s.ensembles.critics), name)  # combined includes safety
            self.assertEqual(set(s.roles), set(S.QUALITY_ROLES), name)

    def test_profile_differences(self):
        b, e = S.build("balanced"), S.build("economy")
        self.assertFalse(b.ensembles.dual_extraction)
        self.assertEqual(b.loop["max_rounds"], 3)
        self.assertEqual(b.roles["intake"].members[0].model, "gpt-6.2-sol")
        self.assertEqual(e.roles["intake"].members[0].model, "gpt-6-luna")
        self.assertEqual(e.loop["max_rounds"], 1)
        self.assertFalse(e.snowball["enabled"])

    def test_overrides_merge_deeply(self):
        s = S.build("quality", {"guards": {"max_total_papers": 15}, "loop": {"max_rounds": 1}})
        self.assertEqual(s.guards.max_total_papers, 15)
        self.assertEqual(s.guards.max_screened, 6000)
        self.assertEqual(s.loop["max_rounds"], 1)
        self.assertEqual(s.loop["min_novelty"], 1)

    def test_role_override_replaces_role(self):
        s = S.build("quality", {"roles": {"map": {"members": [{"model": "claude-sonnet-5-5", "effort": "high"}]}}})
        self.assertEqual(s.roles["map"].members[0].provider, "anthropic")
        self.assertEqual(s.roles["map"].fallback, [])

    def test_validation_rejects_unsafe_or_bad_values(self):
        with self.assertRaises(ValidationError):
            S.build("quality", {"roles": {"safety": {"members": [{"model": "gpt-6.2-sol"}]}}})
        with self.assertRaises(ValidationError):
            S.build("quality", {"ensembles": {"critics": ["evidence"]}})
        with self.assertRaises(ValidationError):
            S.build("quality", {"plan": {"min_hypotheses": 20, "max_hypotheses": 5}})
        with self.assertRaises(ValidationError):
            S.build("quality", {"guards": {"typo_field": 1}})
        with self.assertRaises(ValueError):
            S.build("nope")


class DevOpenAITests(unittest.TestCase):
    def test_every_role_is_gpt_5_4_mini_and_panels_keep_two_members(self):
        st = S.build("dev_openai")
        self.assertEqual({m.model for r in st.roles.values() for m in r.members}, {"gpt-5.4-mini"})
        for name, role in st.roles.items():
            if len(role.members) == 2:
                res = S.resolve_role(st, name, lambda ref: True)
                self.assertEqual(len(res.members), 2, name)  # same model, two efforts → two members
                self.assertEqual(res.independence, "same_model", name)  # one model at two efforts
        self.assertEqual([m.effort for m in st.roles["safety"].members], ["high", "medium"])
        self.assertEqual(st.roles["brief_check"].members[0].effort, "medium")  # a family-B role
        self.assertFalse(st.providers.ollama["enabled"])

    def test_same_model_same_effort_still_counts_once(self):
        a = S.ModelRef(model="m", effort="high")
        self.assertTrue(S._same_member(a, S.ModelRef(model="m", effort="high")))
        self.assertFalse(S._same_member(a, S.ModelRef(model="m", effort="medium")))


class DevOllamaTests(unittest.TestCase):
    def test_every_role_is_the_local_model_and_panels_run_reduced(self):
        st = S.build("dev_ollama")
        self.assertEqual({m.model for r in st.roles.values() for m in r.members}, {S.DEV_OLLAMA})
        self.assertEqual({m.model for r in st.roles.values() for m in r.fallback}, set())
        safety = S.resolve_role(st, "safety", lambda ref: True)
        self.assertEqual((len(safety.members), safety.independence), (1, "reduced"))  # no second local member
        self.assertEqual(S.resolve_role(st, "intake", lambda ref: True).independence, "single")
        self.assertEqual(S.strongest_other(st, "ollama", lambda ref: True).model, S.DEV_OLLAMA)
        self.assertTrue(st.providers.ollama["enabled"])
        self.assertEqual(st.extraction["max_pack_tokens"], 12000)  # fits the 32k context
        self.assertEqual(st.grounding["pack_tokens"], 12000)


class LocalModelOverlayTests(unittest.TestCase):
    def test_any_local_model_fills_every_role_with_thinking_on_and_off(self):
        st = S.build("dev_ollama", S.local_model_overlay("qwen3:4b", num_ctx=24576, pack_tokens=10000))
        self.assertEqual({m.model for r in st.roles.values() for m in r.members}, {"qwen3:4b"})
        safety = S.resolve_role(st, "safety", lambda ref: True)
        self.assertEqual(([m.effort for m in safety.members], safety.independence), (["high", "none"], "same_model"))
        self.assertEqual(st.strongest["ollama"].model, "qwen3:4b")
        self.assertEqual((st.providers.ollama["num_ctx"], st.grounding["pack_tokens"], st.extraction["max_pack_tokens"]),
                         (24576, 10000, 10000))

    def test_without_thinking_two_member_roles_run_reduced(self):
        st = S.build("dev_ollama", S.local_model_overlay("qwen3-coder:latest", thinking=False))
        self.assertEqual(S.resolve_role(st, "safety", lambda ref: True).independence, "reduced")


class ResolutionTests(unittest.TestCase):
    def setUp(self):
        self.s = S.build("quality")

    def test_full_independence_when_both_families_present(self):
        r = S.resolve_role(self.s, "verify", avail("claude-sonnet-5-5", "gpt-6.2-sol"))
        self.assertEqual(r.independence, "full")
        self.assertEqual(r.substitutions, [])

    def test_single_provider_falls_back_to_same_family(self):
        r = S.resolve_role(self.s, "verify", avail("gpt-6.2-sol", "gpt-6-luna"))
        self.assertEqual([m.model for m in r.members], ["gpt-6-luna", "gpt-6.2-sol"])
        self.assertEqual(r.independence, "same_family")
        self.assertEqual(len(r.substitutions), 1)

    def test_reduced_when_member_cannot_be_replaced(self):
        r = S.resolve_role(self.s, "verify", avail("gpt-6.2-sol"))
        self.assertEqual([m.model for m in r.members], ["gpt-6.2-sol"])
        self.assertEqual(r.independence, "reduced")

    def test_single_role_fallback_chain(self):
        r = S.resolve_role(self.s, "intake", avail("gpt-6.2-sol"))
        self.assertEqual(r.members[0].model, "gpt-6.2-sol")
        self.assertEqual(r.independence, "single")

    def test_nothing_available(self):
        with self.assertRaises(LookupError):
            S.resolve_role(self.s, "intake", avail())

    def test_strongest_other_family(self):
        self.assertEqual(S.strongest_other(self.s, "openai", avail("claude-fable-5-2", "gpt-6.2-astra")).model,
                         "claude-fable-5-2")
        self.assertEqual(S.strongest_other(self.s, "openai", avail("gpt-6.2-astra")).model, "gpt-6.2-astra")


class StoreTests(ResearchTestCase):
    def test_save_load_reset(self):
        store = S.SettingsStore(self.tmp / "research.json")
        self.assertEqual(store.load().profile, "budget")  # the default (PLAN B17)
        store.save("balanced", {"guards": {"max_total_papers": 30}})
        loaded = store.load()
        self.assertEqual((loaded.profile, loaded.guards.max_total_papers), ("balanced", 30))
        with self.assertRaises(ValidationError):
            store.save("quality", {"guards": {"reserve": 2}})
        self.assertEqual(store.load().profile, "balanced")  # failed save leaves the file untouched
        self.assertEqual(store.reset().profile, "budget")

    def test_corrupt_file_falls_back_to_the_default(self):
        path = self.tmp / "research.json"
        path.write_text("{not json", "utf-8")
        self.assertEqual(S.SettingsStore(path).load().profile, "budget")


if __name__ == "__main__":
    unittest.main()
