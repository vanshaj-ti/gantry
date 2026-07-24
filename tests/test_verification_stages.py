import tempfile
import unittest
import subprocess
from pathlib import Path
from unittest.mock import patch

from gantry.advance import advance_run
from gantry.advance_batch import AUTO_TRANSITIONS
from gantry.checks import ChecksOutcome, evaluate_checks
from gantry.config import GantryConfig, load_config
from gantry.e2e import E2eOutcome, evaluate_e2e
from gantry.engine import Engine
from gantry.herdr import _STATE_MAP
from gantry.labels import label, short_label
from gantry.state import RunStore
from gantry.status import Status


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    subprocess.run(["git", "commit", "--allow-empty", "-m", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=path, check=True)


class TestTypedVerificationOutcomes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = RunStore(self.root)
        self.run_id = self.store.create("run", "Run")
        self.store.update_state(self.run_id, status=Status.BUILD_COMPLETE)

    def tearDown(self):
        self.tmp.cleanup()

    def test_checks_outcome_does_not_change_status(self):
        cfg = GantryConfig()
        with patch("gantry.checks.run_scope_guard", return_value={"pass": True}), \
             patch("gantry.checks.run_repo_checks", return_value={"pass": True, "results": []}):
            outcome = evaluate_checks(
                self.store, self.run_id, cfg.scope, cfg.checks, self.root, "main",
            )
        self.assertIsInstance(outcome, ChecksOutcome)
        self.assertTrue(outcome.passed)
        self.assertEqual(self.store.state(self.run_id)["status"], Status.BUILD_COMPLETE)

    def test_disabled_e2e_is_explicit_skipped_outcome_without_state_change(self):
        cfg = GantryConfig()
        outcome = evaluate_e2e(
            self.store, self.run_id, cfg.e2e, self.root, "main",
        )
        self.assertIsInstance(outcome, E2eOutcome)
        self.assertEqual(outcome.status, "skipped")
        self.assertEqual(outcome.reason, "disabled")
        self.assertEqual(self.store.state(self.run_id)["status"], Status.BUILD_COMPLETE)


class TestPinnedPipelineVersion(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_default_version_one_preserves_default_stages(self):
        cfg = GantryConfig()
        eng = Engine(self.root, cfg)
        run_id = eng.create_run("legacy", "request")
        state = eng.store.state(run_id)
        self.assertEqual(state["pipeline_version"], 1)
        self.assertEqual(state["stages"], ["plan", "build", "evidence", "review"])

    def test_version_two_pins_explicit_verification_stages(self):
        cfg = GantryConfig()
        cfg.pipeline.version = 2
        eng = Engine(self.root, cfg)
        run_id = eng.create_run("v2", "request")
        state = eng.store.state(run_id)
        self.assertEqual(state["pipeline_version"], 2)
        self.assertEqual(
            state["stages"],
            ["plan", "build", "checks", "e2e", "evidence", "review"],
        )

    def test_pipeline_version_loads_from_target_config(self):
        (self.root / "gantry.toml").write_text("[pipeline]\nversion = 2\n")
        self.assertEqual(load_config(self.root).pipeline.version, 2)


class TestV2VerificationPipeline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        _init_repo(self.root)
        self.cfg = GantryConfig()
        self.cfg.pipeline.version = 2
        self.eng = Engine(self.root, self.cfg)
        self.run_id = self.eng.create_run("v2", "request")
        self.eng.store.update_state(
            self.run_id, status=Status.BUILD_COMPLETE, current_stage="build",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_checks_and_disabled_e2e_are_distinct_recorded_stages(self):
        checks = ChecksOutcome.passed_outcome(
            scope={"pass": True, "high_risk_files": []},
            checks={"pass": True, "results": []},
        )
        with patch("gantry.advance.evaluate_checks", return_value=checks):
            first = advance_run(self.eng, self.run_id)
        state = self.eng.store.state(self.run_id)
        self.assertEqual(first["action"], "checks_passed")
        self.assertEqual(state["status"], Status.CHECKS_PASSED)
        self.assertIn("checks_started_at", state)
        self.assertIn("checks_completed_at", state)
        self.assertTrue(self.eng.store.read_result(self.run_id, "checks.json")["pass"])

        second = advance_run(self.eng, self.run_id)
        state = self.eng.store.state(self.run_id)
        self.assertEqual(second["action"], "e2e_skipped")
        self.assertEqual(state["status"], Status.E2E_SKIPPED)
        report = self.eng.store.read_result(self.run_id, "e2e-report.json")
        self.assertEqual(report["status"], "skipped")
        self.assertEqual(report["reason"], "disabled")

    def test_failed_checks_use_explicit_failure_and_retry_route(self):
        failed = ChecksOutcome.failed_outcome(
            scope={"pass": True},
            checks={"pass": False, "results": []},
        )
        with patch("gantry.advance.evaluate_checks", return_value=failed):
            result = advance_run(self.eng, self.run_id)
        self.assertEqual(result["action"], "checks_failed")
        self.assertEqual(self.eng.store.state(self.run_id)["status"], Status.CHECKS_FAILED)
        self.assertIn("checks_failed", AUTO_TRANSITIONS)


class TestLegacyVerificationCompatibility(unittest.TestCase):
    def test_policy_version_bump_without_v2_stages_stays_legacy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _init_repo(root)
            cfg = GantryConfig()
            cfg.stages = ["plan", "build"]
            cfg.review.enabled = False
            eng = Engine(root, cfg)
            run_id = eng.create_run("legacy", "request")
            eng.store.update_state(
                run_id, status=Status.BUILD_COMPLETE, pipeline_version=2,
            )
            with patch.object(
                eng, "run_checks",
                return_value={"pass": True, "scope": {"high_risk_files": []}},
            ) as legacy_checks, patch(
                "gantry.advance.run_e2e_tests",
                return_value={"pass": True},
            ):
                advance_run(eng, run_id)
        legacy_checks.assert_called_once_with(run_id)


class TestVerificationPresentation(unittest.TestCase):
    def test_new_statuses_have_watch_and_herdr_labels(self):
        self.assertNotEqual(label("checks_running"), "checks_running")
        self.assertNotEqual(short_label("e2e_skipped"), "e2e_skipped")
        self.assertEqual(_STATE_MAP["checks_running"], "working")
        self.assertEqual(_STATE_MAP["checks_failed"], "blocked")
        self.assertEqual(_STATE_MAP["e2e_skipped"], "idle")


if __name__ == "__main__":
    unittest.main()
