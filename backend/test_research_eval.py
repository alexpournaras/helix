"""1A.10 eval runner (Part E, G.3–4): case loading, fake / replay / live modes, waits, E1 checks, compare."""

import json
import tempfile
from pathlib import Path

import httpx

from research_testkit import ResearchTestCase
from app.research import transport
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

    def test_compare(self):
        old = {"cases": [{"id": "a", "status": "complete", "outcome": "findings", "e1_ok": True},
                         {"id": "gone", "status": "complete"}]}
        new = {"cases": [{"id": "a", "status": "complete", "outcome": "insufficient_evidence", "e1_ok": True},
                         {"id": "b", "status": "complete"}]}
        self.assertEqual(ev.compare(old, new), ["a: outcome findings → insufficient_evidence", "b: new case",
                                                "gone: no longer run"])

    def test_missing_factory_explains_itself(self):
        with self.assertRaises(SystemExit) as caught:
            ev.load_factory("app.research.steps:build_pipeline")
        self.assertIn("1C", str(caught.exception))
