"""1A.10 eval runner (Part E, G.3–4): case loading, fake / replay / live modes, waits, E1 checks, compare."""

import asyncio
import json
import tempfile
from pathlib import Path

import httpx

from research_testkit import ResearchTestCase
from app.research import settings as S, transport
from app.research.pipeline import Complete, Pipeline, StageDef, StepDef, Wait
from eval import run as ev


def _write(folder: Path, name: str, data: dict) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    import yaml
    (folder / name).write_text(yaml.safe_dump(data), "utf-8")


CASE = {"id": "ent.synthetic-1", "system": "ent", "synthetic": True, "problem_text": "Synthetic problem.",
        "profile": "quality", "answers": {"q1": "Synthetic answer."}}


def factory(fetch_url: str | None = None):
    """intake (optionally one HTTP call) → wait for answers → done."""
    seen = {"answers": None}

    def build(ctx):
        async def intake(tc):
            if fetch_url:
                async with transport.client() as client:
                    (await client.get(fetch_url)).raise_for_status()
            return None

        def intake_done(sc):
            return Wait("answers") if seen["answers"] is None else "done"

        stages = {"intake": StageDef("intake", on_enter=lambda sc: sc.queue("intake", "intake:1"),
                                     on_complete=intake_done),
                  "done": StageDef("done", on_complete=lambda sc: Complete("findings"))}
        steps = {"intake": StepDef("intake", intake)}

        def answer(case, inv):
            def apply(sc):
                seen["answers"] = dict(case.answers)
                return "done"
            return apply

        return Pipeline(stages=stages, steps=steps), {"answers": answer}

    build.seen = seen
    return build


class CaseLoadingTests(ResearchTestCase):
    def test_load_filter_and_examples(self):
        folder = self.tmp / "cases"
        _write(folder / "ent", "a.yaml", CASE)
        _write(folder / "ent", "_example.yaml", {**CASE, "id": "example"})
        _write(folder / "skin", "b.yaml", {**CASE, "id": "skin.synthetic-1", "system": "skin"})
        self.assertEqual([c.id for c in ev.load_cases(folder)], ["ent.synthetic-1", "skin.synthetic-1"])
        self.assertEqual([c.id for c in ev.load_cases(folder, "skin.synthetic-1")], ["skin.synthetic-1"])

    def test_real_cases_are_refused(self):
        folder = self.tmp / "cases"
        _write(folder, "real.yaml", {**CASE, "synthetic": False})
        with self.assertRaises(ValueError):
            ev.load_cases(folder)

    def test_duplicate_ids_are_refused(self):
        folder = self.tmp / "cases"
        _write(folder, "a.yaml", CASE)
        _write(folder, "b.yaml", CASE)
        with self.assertRaises(ValueError):
            ev.load_cases(folder)

    def test_repo_example_case_is_valid(self):
        example = Path(ev.CASES_DIR) / "_example.yaml"
        import yaml
        ev.EvalCase.model_validate(yaml.safe_load(example.read_text("utf-8")))


class RunTests(ResearchTestCase):
    async def test_fake_mode_answers_waits_and_writes_results(self):
        build = factory()
        report = await ev.run([ev.EvalCase(**CASE)], mode="fake", factory=build, name="t",
                              recordings_root=self.tmp / "rec", results_dir=self.tmp / "results")
        case = report["cases"][0]
        self.assertEqual((case["status"], case["outcome"], case["waits"]), ("complete", "findings", ["answers"]))
        self.assertTrue(case["e1_ok"])
        self.assertEqual(build.seen["answers"], {"q1": "Synthetic answer."})
        saved = json.loads((self.tmp / "results" / "t-fake.json").read_text("utf-8"))
        self.assertEqual(saved["summary"], {"cases": 1, "complete": 1, "e1_failures": 0})

    async def test_fake_mode_blocks_the_network(self):
        report = await ev.run([ev.EvalCase(**CASE)], mode="fake", factory=factory("https://example.test/x"), name="t",
                              recordings_root=self.tmp / "rec", results_dir=self.tmp / "results")
        self.assertEqual(report["cases"][0]["status"], "failed")

    async def test_replay_serves_recordings_and_fails_on_unknown_requests(self):
        rec = self.tmp / "rec" / "t"

        def handler(request):
            return httpx.Response(200, json={"ok": True})
        async with httpx.AsyncClient(transport=transport.RecordTransport(rec, inner=httpx.MockTransport(handler))) as c:
            await c.get("https://example.test/x")

        report = await ev.run([ev.EvalCase(**CASE)], mode="replay", factory=factory("https://example.test/x"),
                              name="t", recordings_root=self.tmp / "rec", results_dir=self.tmp / "results")
        self.assertEqual(report["cases"][0]["status"], "complete")
        report = await ev.run([ev.EvalCase(**CASE)], mode="replay", factory=factory("https://example.test/other"),
                              name="t", recordings_root=self.tmp / "rec", results_dir=self.tmp / "results")
        self.assertEqual(report["cases"][0]["status"], "failed")

    async def test_replay_without_recordings_is_an_error(self):
        with self.assertRaises(FileNotFoundError):
            await ev.run([ev.EvalCase(**CASE)], mode="replay", factory=factory(), name="missing",
                         recordings_root=self.tmp / "rec", results_dir=self.tmp / "results")

    async def test_live_mode_stops_at_preflight_without_the_privacy_answer(self):
        report = await ev.run([ev.EvalCase(**CASE)], mode="live", factory=factory(), name="t",
                              recordings_root=self.tmp / "rec", results_dir=self.tmp / "results")
        case = report["cases"][0]
        self.assertEqual(case["status"], "preflight_failed")
        self.assertTrue(any("privacy question" in e for e in case["errors"]))

    async def test_live_mode_uses_the_saved_privacy_answer(self):
        S.SettingsStore(self.tmp / "research.json").save("dev", {"privacy": {"openai_project_shares_data": True}})
        self.assertEqual(ev.local_overrides(), {"privacy": {"openai_project_shares_data": True}})
        report = await ev.run([ev.EvalCase(**CASE)], mode="live", factory=factory(), name="t",
                              recordings_root=self.tmp / "rec", results_dir=self.tmp / "results")
        self.assertNotEqual(report["cases"][0]["status"], "preflight_failed")

    async def test_a_slow_case_is_reported_as_a_timeout(self):
        def slow_factory(ctx):
            async def step(tc):
                await asyncio.sleep(5)
            stages = {"intake": StageDef("intake", on_enter=lambda sc: sc.queue("intake", "intake:1"),
                                         on_complete=lambda sc: Complete("findings"))}
            return Pipeline(stages=stages, steps={"intake": StepDef("intake", step)}), {}
        report = await ev.run([ev.EvalCase(**CASE)], mode="fake", factory=slow_factory, name="t",
                              recordings_root=self.tmp / "rec", results_dir=self.tmp / "results", timeout=0.3)
        self.assertEqual(report["cases"][0]["status"], "timeout")
        self.assertTrue((self.tmp / "results" / "t-fake.json").exists())

    async def test_settings_overlays_reach_the_run(self):
        seen = {}

        def capture(ctx):
            seen["settings"] = ctx.settings
            return factory()(ctx)
        case = ev.EvalCase(**{**CASE, "settings": {"guards": {"max_screened": 200}, "loop": {"max_rounds": 2}}})
        report = await ev.run([case], mode="fake", factory=capture, name="t", recordings_root=self.tmp / "rec",
                              results_dir=self.tmp / "results", settings={"loop": {"max_rounds": 1}})
        s = seen["settings"].model_dump()
        self.assertEqual(s["guards"]["max_screened"], 200)  # the case's overlay
        self.assertEqual(s["loop"]["max_rounds"], 1)  # the command line's overlay wins over the case's
        self.assertEqual(s["guards"]["max_total_papers"], 120)  # untouched keys keep the profile's value
        self.assertEqual(report["cases"][0]["settings_overlay"],
                         {"guards": {"max_screened": 200}, "loop": {"max_rounds": 1}})

    def test_overlays_cant_change_privacy_or_profile(self):
        for bad in ({"privacy": {"store_prompts": True}}, {"profile": "quality"}):
            with self.assertRaises(ValueError):
                ev.EvalCase(**{**CASE, "settings": bad})
            with self.assertRaises(ValueError):
                ev.check_overlay(bad)

    def test_unknown_setting_is_an_error(self):
        with self.assertRaises(ValueError):
            ev.EvalCase(**{**CASE, "settings": {"guards": {"max_screened": "many"}}})

    def test_set_arguments_and_settings_files(self):
        self.assertEqual(ev.parse_sets(["guards.max_tokens=2000000", "snowball.enabled=false", "search.sources=[pubmed]"]),
                         {"guards": {"max_tokens": 2000000}, "snowball": {"enabled": False},
                          "search": {"sources": ["pubmed"]}})
        with self.assertRaises(ValueError):
            ev.parse_sets(["no-equals-sign"])
        path = self.tmp / "limits.yaml"
        path.write_text("guards: {max_screened: 50}\n", "utf-8")
        self.assertEqual(ev.overlay_from(str(path), ["guards.max_tokens=10"]),
                         {"guards": {"max_screened": 50, "max_tokens": 10}})

    def test_profile_option_replaces_each_cases_profile(self):
        cases = ev.with_profile([ev.EvalCase(**CASE)], "dev_ollama")
        self.assertEqual(cases[0].profile, "dev_ollama")
        self.assertEqual(ev.with_profile([ev.EvalCase(**CASE)], None)[0].profile, "quality")
        with self.assertRaises(ValueError):
            ev.with_profile([ev.EvalCase(**CASE)], "no_such_profile")

    def test_repo_limit_files_are_valid(self):
        files = sorted(Path(ev.LIMITS_DIR).glob("*.yaml"))
        self.assertTrue(files)
        for path in files:
            overlay = ev.overlay_from(str(path), [])
            S.build("dev_openai", overlay)  # validates

    def test_compare(self):
        old = {"cases": [{"id": "a", "status": "complete", "outcome": "findings", "e1_ok": True},
                         {"id": "gone", "status": "complete"}]}
        new = {"cases": [{"id": "a", "status": "complete", "outcome": "insufficient_evidence", "e1_ok": True},
                         {"id": "b", "status": "complete"}]}
        self.assertEqual(ev.compare(old, new), ["a: outcome findings → insufficient_evidence", "b: new case",
                                                "gone: no longer run"])

    def test_missing_factory_explains_itself(self):
        with self.assertRaises(SystemExit) as caught:
            ev.load_factory("app.research.steps:no_such_factory")
        self.assertIn("isn't available", str(caught.exception))

    def test_default_factory_loads(self):
        pipeline, resolvers = ev.load_factory(ev.DEFAULT_FACTORY)(None)
        self.assertEqual(pipeline.first_stage, "intake")
        self.assertIn("problem_rewrite", resolvers)
