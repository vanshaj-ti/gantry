import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gantry.config import E2eConfig
from gantry.e2e import _has_specs, _touched_apps, run_e2e_tests
from gantry.state import RunStore


def _init_scratch_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(path), check=True)
    subprocess.run(["git", "commit", "--allow-empty", "-m", "init", "-q"], cwd=str(path), check=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=str(path), check=True)


class TestTouchedApps(unittest.TestCase):
    def test_matches_files_under_app_prefix(self):
        apps = {"web": "npm test", "api": "npm test"}
        files = ["apps/web/src/index.ts", "README.md"]
        self.assertEqual(_touched_apps(files, apps), ["web"])

    def test_no_false_positive_on_partial_name_match(self):
        # "webapp" must not match a file under "apps/web/" (prefix, not substring)
        apps = {"webapp": "npm test"}
        files = ["apps/web/src/index.ts"]
        self.assertEqual(_touched_apps(files, apps), [])

    def test_multiple_apps_touched(self):
        apps = {"web": "npm test", "api": "npm test", "mobile": "npm test"}
        files = ["apps/web/a.ts", "apps/api/b.ts"]
        self.assertEqual(sorted(_touched_apps(files, apps)), ["api", "web"])

    def test_no_apps_touched(self):
        apps = {"web": "npm test"}
        files = ["README.md"]
        self.assertEqual(_touched_apps(files, apps), [])


class TestHasSpecs(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cwd = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_false_when_app_dir_missing(self):
        self.assertFalse(_has_specs(self.cwd, "web", "tests/e2e/*.spec.ts"))

    def test_false_when_dir_exists_but_no_matching_specs(self):
        (self.cwd / "apps" / "web").mkdir(parents=True)
        self.assertFalse(_has_specs(self.cwd, "web", "tests/e2e/*.spec.ts"))

    def test_true_when_matching_spec_exists(self):
        spec_dir = self.cwd / "apps" / "web" / "tests" / "e2e"
        spec_dir.mkdir(parents=True)
        (spec_dir / "smoke.spec.ts").write_text("test('x', () => {})")
        self.assertTrue(_has_specs(self.cwd, "web", "tests/e2e/*.spec.ts"))


class TestRunE2eTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cwd = Path(self._tmp.name)
        _init_scratch_repo(self.cwd)
        self.store = RunStore(self.cwd)
        self.run_id = self.store.new_run_id("t")
        self.store.create(self.run_id, "t")

    def tearDown(self):
        self._tmp.cleanup()

    def test_noop_when_disabled(self):
        cfg = E2eConfig(enabled=False)
        out = run_e2e_tests(self.store, self.run_id, cfg, self.cwd, "main")
        self.assertEqual(out, {"enabled": False, "pass": True, "apps": []})
        self.assertEqual(self.store.read_result(self.run_id, "e2e-report.json"), out)

    def test_noop_when_no_apps_configured(self):
        cfg = E2eConfig(enabled=True, apps={})
        out = run_e2e_tests(self.store, self.run_id, cfg, self.cwd, "main")
        self.assertEqual(out, {"enabled": False, "pass": True, "apps": []})

    def test_skips_app_with_no_specs(self):
        (self.cwd / "apps" / "web").mkdir(parents=True)
        cfg = E2eConfig(enabled=True, apps={"web": "echo hi"})
        with patch("gantry.e2e._changed_files", return_value=["apps/web/src/a.ts"]), \
             patch("gantry.e2e._merge_base", return_value="abc123"):
            out = run_e2e_tests(self.store, self.run_id, cfg, self.cwd, "main")
        self.assertEqual(out["apps"], [{"app": "web", "skipped": True, "reason": "no e2e specs found"}])
        self.assertTrue(out["pass"])

    def test_passing_app_reports_pass(self):
        spec_dir = self.cwd / "apps" / "web" / "tests" / "e2e"
        spec_dir.mkdir(parents=True)
        (spec_dir / "smoke.spec.ts").write_text("x")
        cfg = E2eConfig(enabled=True, apps={"web": "true"})
        with patch("gantry.e2e._changed_files", return_value=["apps/web/src/a.ts"]), \
             patch("gantry.e2e._merge_base", return_value="abc123"):
            out = run_e2e_tests(self.store, self.run_id, cfg, self.cwd, "main")
        self.assertTrue(out["pass"])
        self.assertEqual(out["apps"][0]["app"], "web")
        self.assertTrue(out["apps"][0]["pass"])

    def test_failing_app_reports_fail(self):
        spec_dir = self.cwd / "apps" / "web" / "tests" / "e2e"
        spec_dir.mkdir(parents=True)
        (spec_dir / "smoke.spec.ts").write_text("x")
        cfg = E2eConfig(enabled=True, apps={"web": "false"})
        with patch("gantry.e2e._changed_files", return_value=["apps/web/src/a.ts"]), \
             patch("gantry.e2e._merge_base", return_value="abc123"):
            out = run_e2e_tests(self.store, self.run_id, cfg, self.cwd, "main")
        self.assertFalse(out["pass"])
        self.assertFalse(out["apps"][0]["pass"])

    def test_table_app_config_with_retry_used_on_failure(self):
        spec_dir = self.cwd / "apps" / "web" / "tests" / "e2e"
        spec_dir.mkdir(parents=True)
        (spec_dir / "smoke.spec.ts").write_text("x")
        cfg = E2eConfig(enabled=True, apps={"web": {"command": "false", "retry": 2}})
        with patch("gantry.e2e._changed_files", return_value=["apps/web/src/a.ts"]), \
             patch("gantry.e2e._merge_base", return_value="abc123"), \
             patch("gantry.e2e.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = ""
            mock_run.return_value.stderr = ""
            out = run_e2e_tests(self.store, self.run_id, cfg, self.cwd, "main")
        # 1 initial attempt + 2 retries = 3 subprocess calls
        self.assertEqual(mock_run.call_count, 3)
        self.assertFalse(out["apps"][0]["pass"])
        self.assertEqual(out["apps"][0]["retries_used"], 2)
        self.assertEqual(out["apps"][0]["retry_cap"], 2)

    def test_retry_stops_early_once_a_retry_passes(self):
        spec_dir = self.cwd / "apps" / "web" / "tests" / "e2e"
        spec_dir.mkdir(parents=True)
        (spec_dir / "smoke.spec.ts").write_text("x")
        cfg = E2eConfig(enabled=True, apps={"web": {"command": "flaky", "retry": 3}})
        results_iter = iter([1, 1, 0])  # fails twice, passes on 3rd (2nd retry)

        class _FakeProc:
            def __init__(self, rc):
                self.returncode = rc
                self.stdout = ""
                self.stderr = ""

        with patch("gantry.e2e._changed_files", return_value=["apps/web/src/a.ts"]), \
             patch("gantry.e2e._merge_base", return_value="abc123"), \
             patch("gantry.e2e.subprocess.run", side_effect=lambda *a, **k: _FakeProc(next(results_iter))):
            out = run_e2e_tests(self.store, self.run_id, cfg, self.cwd, "main")
        self.assertTrue(out["pass"])
        self.assertTrue(out["apps"][0]["pass"])
        self.assertEqual(out["apps"][0]["retries_used"], 2)

    def test_no_retry_by_default_bare_string_command(self):
        spec_dir = self.cwd / "apps" / "web" / "tests" / "e2e"
        spec_dir.mkdir(parents=True)
        (spec_dir / "smoke.spec.ts").write_text("x")
        cfg = E2eConfig(enabled=True, apps={"web": "false"})
        with patch("gantry.e2e._changed_files", return_value=["apps/web/src/a.ts"]), \
             patch("gantry.e2e._merge_base", return_value="abc123"), \
             patch("gantry.e2e.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = ""
            mock_run.return_value.stderr = ""
            out = run_e2e_tests(self.store, self.run_id, cfg, self.cwd, "main")
        self.assertEqual(mock_run.call_count, 1)
        self.assertNotIn("retries_used", out["apps"][0])

    def test_per_app_spec_glob_override(self):
        spec_dir = self.cwd / "apps" / "api" / "custom" / "e2e"
        spec_dir.mkdir(parents=True)
        (spec_dir / "smoke.e2e.ts").write_text("x")
        cfg = E2eConfig(enabled=True, apps={
            "api": {"command": "true", "spec_glob": "custom/e2e/*.e2e.ts"}
        }, spec_glob="tests/e2e/*.spec.ts")  # global glob would NOT match
        with patch("gantry.e2e._changed_files", return_value=["apps/api/src/a.ts"]), \
             patch("gantry.e2e._merge_base", return_value="abc123"):
            out = run_e2e_tests(self.store, self.run_id, cfg, self.cwd, "main")
        self.assertNotEqual(out["apps"][0].get("skipped"), True)
        self.assertTrue(out["apps"][0]["pass"])

    def test_timeout_reports_fail_without_raising(self):
        spec_dir = self.cwd / "apps" / "web" / "tests" / "e2e"
        spec_dir.mkdir(parents=True)
        (spec_dir / "smoke.spec.ts").write_text("x")
        cfg = E2eConfig(enabled=True, apps={"web": "sleep 10"}, timeout=1800)
        timeout_exc = subprocess.TimeoutExpired(cmd="sleep 10", timeout=1800)
        with patch("gantry.e2e._changed_files", return_value=["apps/web/src/a.ts"]), \
             patch("gantry.e2e._merge_base", return_value="abc123"), \
             patch("gantry.e2e.subprocess.run", side_effect=timeout_exc):
            out = run_e2e_tests(self.store, self.run_id, cfg, self.cwd, "main")
        self.assertFalse(out["pass"])
        self.assertFalse(out["apps"][0]["pass"])
        self.assertIn("Timed out", out["apps"][0]["stderr_tail"])


if __name__ == "__main__":
    unittest.main()
