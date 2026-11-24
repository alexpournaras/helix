"""1A.10 doctor (`npm run research:doctor`): keys, profile, pre-flight, privacy, database, knowledge, git hygiene.
Never prints key values. Offline unless --probe."""

import json
import os
import sqlite3
from unittest.mock import patch

from research_testkit import FAKE_KEYS, ResearchTestCase
from app.research import doctor
from app.research.db import SCHEMA_VERSION


def _by_name(checks):
    out = {}
    for c in checks:
        out.setdefault(c.name, []).append(c)
    return out


class DoctorTests(ResearchTestCase):
    def run_checks(self, **kwargs):
        return doctor.checks(data_dir=self.tmp, git_tracked=lambda paths: [], **kwargs)

    def probes(self, *models, ok=True):
        (self.tmp / "research_probes.json").write_text(json.dumps({m: {"ok": ok} for m in models}), "utf-8")

    def settings(self, profile="quality", **overrides):
        (self.tmp / "research.json").write_text(json.dumps({"profile": profile, "overrides": overrides}), "utf-8")

    def test_key_values_are_never_printed(self):
        lines = doctor.render(self.run_checks())
        for value in ("sk-test-openai", "sk-test-anthropic"):
            self.assertNotIn(value, lines)
        self.assertIn("OPENAI_API_KEY", lines)

    def test_keys(self):
        checks = _by_name(self.run_checks())
        keys = {c.detail.split()[0]: c.status for c in checks["keys"]}
        self.assertEqual(keys["OPENAI_API_KEY"], "ok")
        self.assertEqual(keys["ANTHROPIC_API_KEY"], "ok")
        self.assertEqual(keys["NCBI_API_KEY"], "warn")  # recommended, not required

    def test_unprobed_models_fail_preflight(self):
        checks = _by_name(self.run_checks())
        self.assertIn("fail", {c.status for c in checks["preflight"]})
        self.assertEqual(doctor.exit_code(self.run_checks()), 1)

    def test_probed_dev_profile_passes_preflight_with_warnings(self):
        self.settings("dev", privacy={"openai_project_shares_data": False})
        self.probes("gpt-5.4-mini", "qwen3-coder:latest")
        with patch("app.research.providers.ollama.OllamaProvider.has_key", lambda self: True):
            checks = _by_name(self.run_checks())
        self.assertNotIn("fail", {c.status for c in checks["preflight"]}, [c.detail for c in checks["preflight"]])
        self.assertTrue(any("Development profile" in c.detail for c in checks["preflight"]))

    def test_privacy_question_unanswered_warns(self):
        checks = _by_name(self.run_checks())
        self.assertTrue(any(c.status == "warn" and "privacy question" in c.detail for c in checks["privacy"]))

    def test_data_sharing_warns_but_does_not_fail(self):
        self.settings("dev", privacy={"openai_project_shares_data": True})
        self.probes("gpt-5.4-mini", "qwen3-coder:latest")
        with patch("app.research.providers.ollama.OllamaProvider.has_key", lambda self: True):
            checks = self.run_checks()
        self.assertEqual(doctor.exit_code(checks), 0, [c.detail for c in checks if c.status == "fail"])
        self.assertTrue(any(c.status == "warn" and "only synthetic" in c.detail for c in _by_name(checks)["privacy"]))

    def test_store_prompts_on_warns(self):
        self.settings(privacy={"openai_project_shares_data": False, "store_prompts": True})
        checks = _by_name(self.run_checks())
        self.assertTrue(any(c.status == "warn" and "store_prompts" in c.detail for c in checks["privacy"]))

    def test_remote_bind_fails(self):
        with patch.dict(os.environ, {"HELIX_HOST": "0.0.0.0", "HELIX_ALLOW_REMOTE": ""}):
            checks = _by_name(self.run_checks())
        self.assertIn("fail", {c.status for c in checks["privacy"]})

    def test_database(self):
        checks = _by_name(self.run_checks())
        self.assertEqual(checks["database"][0].status, "ok")  # the test kit created it
        self.assertIn(f"schema {SCHEMA_VERSION}", checks["database"][0].detail)

    def test_missing_database_is_fine_and_newer_schema_fails(self):
        (self.tmp / "research.sqlite3").unlink()
        self.assertEqual(_by_name(self.run_checks())["database"][0].status, "ok")
        conn = sqlite3.connect(self.tmp / "research.sqlite3")
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION + 1),))
        conn.commit()
        conn.close()
        self.assertEqual(_by_name(self.run_checks())["database"][0].status, "fail")

    def test_database_check_does_not_create_or_change_files(self):
        (self.tmp / "research.sqlite3").unlink()
        self.run_checks()
        self.assertFalse((self.tmp / "research.sqlite3").exists())

    def test_knowledge(self):
        checks = _by_name(self.run_checks())
        statuses = {c.detail.split(":")[0]: c.status for c in checks["knowledge"]}
        self.assertEqual(statuses["mesh_systems.yaml"], "ok")
        self.assertIn(statuses["universal/red_flags.yaml"], ("ok", "warn"))

    def test_git_tracked_secrets_fail(self):
        checks = _by_name(doctor.checks(data_dir=self.tmp, git_tracked=lambda paths: ["backend/.env"]))
        self.assertEqual(checks["git"][0].status, "fail")
        clean = _by_name(self.run_checks())
        self.assertEqual(clean["git"][0].status, "ok")

    def test_render_and_exit_code(self):
        checks = [doctor.Check("a", "ok", "fine"), doctor.Check("b", "warn", "hmm")]
        self.assertEqual(doctor.exit_code(checks), 0)
        self.assertIn("WARN", doctor.render(checks))
        self.assertEqual(doctor.exit_code(checks + [doctor.Check("c", "fail", "no")]), 1)

    def test_fake_keys_are_the_test_kit_values(self):
        self.assertEqual(os.environ["OPENAI_API_KEY"], FAKE_KEYS["OPENAI_API_KEY"])
