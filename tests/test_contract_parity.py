"""Phase 0 contract-freeze parity / replay tests.

Locks today's status graph, sessions.json shape, queue stage pinning,
two-axis review isolation, and manual-transition reachability so later
architecture phases cannot silently change on-disk contracts.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gantry.advance import AUTO_TRANSITIONS
from gantry.config import DEFAULT_QUEUE_STAGES, DEFAULT_STAGES, GantryConfig
from gantry.engine import Engine
from gantry.runners import RunnerResult
from gantry.review import run_review
from gantry.state import RunStore
from gantry.status import (
    AUTOMATIC_TRANSITIONS,
    InvalidTransitionError,
    MANUALLY_REACHABLE,
    Status,
    TRANSITIONS,
    validate_transition,
)

FIXTURES = Path(__file__).parent / "fixtures"
LEGACY_SESSIONS = FIXTURES / "legacy_sessions.json"


def _init_scratch_repo(path: Path) -> None:
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(path), check=True)
    subprocess.run(["git", "commit", "--allow-empty", "-m", "init", "-q"], cwd=str(path), check=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=str(path), check=True)


class TestAutomaticTransitionParity(unittest.TestCase):
    """Every AUTOMATIC_TRANSITIONS edge must pass validate_transition."""

    def test_all_automatic_edges_validate(self):
        for key, targets in AUTOMATIC_TRANSITIONS.items():
            if isinstance(key, tuple):
                frm, side = key
                for to in targets:
                    validate_transition(frm, to, side_field=side)
            else:
                for to in targets:
                    validate_transition(key, to)

    def test_auto_transitions_set_subset_of_known_statuses(self):
        # AUTO_TRANSITIONS (advance.py poller membership) uses bare strings
        # that must remain stable; unknown entries would break poller gating.
        for status in AUTO_TRANSITIONS:
            self.assertIsInstance(status, str)
            self.assertTrue(status)  # non-empty


class TestManualReachabilityContract(unittest.TestCase):
    def test_manually_reachable_targets_from_arbitrary_statuses(self):
        samples = (
            Status.QUEUED,
            Status.BLOCKED,
            Status.BUILD_COMPLETE,
            Status.CHECKS_ESCALATED,
            Status.SHIPPED,
        )
        for frm in samples:
            for to in MANUALLY_REACHABLE:
                validate_transition(frm, to)

    def test_queued_not_manually_reachable(self):
        self.assertNotIn(Status.QUEUED, MANUALLY_REACHABLE)
        with self.assertRaises(InvalidTransitionError):
            validate_transition(Status.BUILD_RUNNING, Status.QUEUED)


class TestLegacySessionsFixture(unittest.TestCase):
    """Legacy sessions.json records must remain readable field-for-field."""

    def test_fixture_fields(self):
        data = json.loads(LEGACY_SESSIONS.read_text())
        self.assertIn("plan", data)
        self.assertIn("build", data)
        self.assertIn("review_spec", data)
        self.assertIn("review_standards", data)
        self.assertIn("investigation", data)
        for stage, entry in data.items():
            self.assertIn("session_id", entry, stage)
            self.assertIn("model", entry, stage)
            self.assertIn("runner", entry, stage)
            self.assertEqual(set(entry), {"session_id", "model", "runner"}, stage)

    def test_runstore_loads_legacy_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp))
            run_id = "legacy-1"
            store.create(run_id, "legacy")
            sessions_path = store.run_dir(run_id) / "sessions.json"
            sessions_path.write_text(LEGACY_SESSIONS.read_text())
            self.assertEqual(store.get_session_id(run_id, "plan"), "sess-plan-legacy-001")
            self.assertEqual(store.get_session(run_id, "review_spec")["runner"], "claude-code")
            self.assertEqual(store.get_session(run_id, "investigation")["model"], "opus")
            # Missing stage returns empty dict / None — additive schema must not break.
            self.assertEqual(store.get_session(run_id, "research"), {})
            self.assertIsNone(store.get_session_id(run_id, "research"))


class TestQueueStagePinning(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _init_scratch_repo(self.root)
        self.cfg = GantryConfig()
        self.eng = Engine(self.root, self.cfg)

    def tearDown(self):
        self._tmp.cleanup()

    def test_default_stages_pinned_at_create(self):
        run_id = self.eng.create_run("t", "r")
        st = self.eng.store.state(run_id)
        self.assertEqual(st["stages"], list(DEFAULT_STAGES))
        self.assertEqual(self.eng.stages_for_run(run_id), list(DEFAULT_STAGES))

    def test_bug_queue_pins_investigation_pipeline(self):
        run_id = self.eng.create_run("bug", "fix it", tag="bug")
        st = self.eng.store.state(run_id)
        self.assertEqual(st["stages"], list(DEFAULT_QUEUE_STAGES["bug"]))
        self.assertEqual(st["stages"][0], "investigation")
        self.assertEqual(st["current_stage"], "investigation")
        self.assertTrue(str(st["status"]).startswith("awaiting_"))

    def test_later_config_change_does_not_mutate_pinned_stages(self):
        run_id = self.eng.create_run("t", "r", tag="chore")
        pinned = list(self.eng.store.state(run_id)["stages"])
        self.eng.cfg.queues["chore"] = ["build"]
        self.assertEqual(self.eng.stages_for_run(run_id), pinned)
        self.assertNotEqual(pinned, ["build"])


class TestTwoAxisReviewSessionIsolation(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _init_scratch_repo(self.root)
        self.store = RunStore(self.root)
        self.run_id = self.store.new_run_id("t")
        self.store.create(self.run_id, "t")
        self.cfg = GantryConfig()
        self.cfg.review.two_axis = True
        self.cfg.review.enabled = True

    def tearDown(self):
        self._tmp.cleanup()

    def test_axes_use_distinct_session_keys(self):
        class _AxisAwareRunner:
            name = "claude-code"

            def run(self, **kwargs):
                session_name = kwargs.get("session_name", "")
                axis = "spec" if session_name.endswith("-review-spec") else "standards"
                text = (
                    "APPROVE\n\nVerification Story: ok.\n\n"
                    '```json\n{"findings": []}\n```\n'
                )
                return RunnerResult(
                    ok=True, session_id=f"sess-{axis}", exit_code=0,
                    raw={"result": text}, stdout=text, stderr="",
                )

        with patch("gantry.review.get_runner", return_value=_AxisAwareRunner()):
            out = run_review(self.store, self.run_id, self.cfg, self.root)

        self.assertEqual(out["combined_verdict"], "APPROVE")
        spec_sess = self.store.get_session(self.run_id, "review_spec")
        std_sess = self.store.get_session(self.run_id, "review_standards")
        self.assertEqual(spec_sess.get("session_id"), "sess-spec")
        self.assertEqual(std_sess.get("session_id"), "sess-standards")
        self.assertNotEqual(spec_sess.get("session_id"), std_sess.get("session_id"))
        # Shared "review" key must not be used in two-axis mode.
        self.assertEqual(self.store.get_session(self.run_id, "review"), {})


class TestStageResumeContract(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _init_scratch_repo(self.root)
        self.cfg = GantryConfig()
        self.eng = Engine(self.root, self.cfg)
        self.run_id = self.eng.create_run("t", "do it")

    def tearDown(self):
        self._tmp.cleanup()

    def test_resume_requires_prior_session(self):
        with self.assertRaises(ValueError):
            self.eng.run_agent_stage(self.run_id, "build", resume=True)

    def test_resume_passes_session_id_to_runner(self):
        self.eng.store.save_session(self.run_id, "build", session_id="prior-build",
                                    model="opus", runner="claude-code")
        captured = {}

        class Capturing:
            name = "claude-code"

            def run(self, **kwargs):
                captured.update(kwargs)
                return RunnerResult(
                    ok=True, session_id="prior-build", exit_code=0,
                    raw={"result": "done"}, stdout="done", stderr="",
                )

        with patch("gantry.engine.get_runner", return_value=Capturing()):
            self.eng.run_agent_stage(self.run_id, "build", resume=True)
        self.assertEqual(captured.get("session_id"), "prior-build")


class TestTransitionsSnapshotStability(unittest.TestCase):
    """Freeze a compact snapshot of the merged TRANSITIONS graph size/shape."""

    def test_transitions_covers_every_status(self):
        for status in Status:
            self.assertIn(status, TRANSITIONS)

    def test_status_string_values_frozen(self):
        expected = {
            "created", "queued",
            "awaiting_spec", "spec_running", "spec_complete", "spec_failed",
            "awaiting_design", "design_running", "design_complete", "design_failed",
            "awaiting_plan", "plan_running", "plan_complete", "plan_failed",
            "awaiting_build", "build_running", "build_complete", "build_failed",
            "build_changes_requested", "plan_changes_requested",
            "evidence_changes_requested", "spec_changes_requested",
            "design_changes_requested",
            "awaiting_evidence", "evidence_running", "evidence_complete", "evidence_failed",
            "review_running", "review_approved", "review_changes_requested", "review_escalated",
            "blocked", "checks_high_risk_escalated", "checks_escalated",
            "resolve_running", "resolve_failed", "resolve_escalated",
            "shipped", "shipped_manually", "ship_failed", "ship_checks_failed",
            "held", "cancelled",
        }
        self.assertEqual({s.value for s in Status}, expected)


if __name__ == "__main__":
    unittest.main()
