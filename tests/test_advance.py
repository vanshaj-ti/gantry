import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gantry.config import GantryConfig
from gantry.engine import Engine
from gantry.advance import advance_run, notify_message, _checks_failure_detail


def _init_scratch_repo(path: Path) -> None:
    """Init a throwaway git repo for a test. Sets repo-local user.name/email
    so this works on a fresh CI runner with no global git config (`git commit`
    fails with exit 128 there otherwise — global config can't be assumed)."""
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(path), check=True)
    subprocess.run(["git", "commit", "--allow-empty", "-m", "init", "-q"], cwd=str(path), check=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=str(path), check=True)


def _fake_run_agent_stage(self, run_id, stage, resume=False):
    self.store.update_state(run_id, status=f"{stage}_complete")
    return {"stage": stage, "ok": True}


class TestChecksRetry(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.target = Path(self._tmp.name)
        _init_scratch_repo(self.target)

    def tearDown(self):
        self._tmp.cleanup()

    def _make_engine(self, retry_checks=2):
        cfg = GantryConfig()
        cfg.checks.retry_checks = retry_checks
        cfg.checks.commands = ["false"]
        return Engine(self.target, cfg)

    def test_retries_up_to_cap_then_escalates(self):
        eng = self._make_engine(retry_checks=2)
        run_id = eng.create_run("t", "test")
        eng.store.update_state(run_id, status="build_complete")

        with patch.object(Engine, "run_agent_stage", _fake_run_agent_stage):
            for _ in range(6):
                status = eng.store.state(run_id).get("status")
                if status == "checks_escalated":
                    break
                advance_run(eng, run_id)

        self.assertEqual(eng.store.state(run_id).get("status"), "checks_escalated")
        self.assertEqual(eng.store.state(run_id).get("checks_retry_count"), 2)

    def test_writes_failure_detail_to_answers_build_md(self):
        eng = self._make_engine(retry_checks=2)
        run_id = eng.create_run("t", "test")
        eng.store.update_state(run_id, status="build_complete")

        with patch.object(Engine, "run_agent_stage", _fake_run_agent_stage):
            advance_run(eng, run_id)  # build_complete -> blocked (checks fail)
            advance_run(eng, run_id)  # blocked -> retry build

        answer = eng.store.read_artifact(run_id, "answers/build.md")
        self.assertIsNotNone(answer)
        self.assertIn("false", answer)

    def test_unrelated_blocked_reason_is_not_auto_retried(self):
        eng = self._make_engine()
        run_id = eng.create_run("t", "test")
        eng.store.update_state(run_id, status="blocked", blocked_on="human_question")

        with patch.object(Engine, "run_agent_stage", _fake_run_agent_stage) as _:
            result = advance_run(eng, run_id)
        self.assertEqual(result["action"], "no_auto_transition")


class TestChecksFailureDetail(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.target = Path(self._tmp.name)
        _init_scratch_repo(self.target)
        self.cfg = GantryConfig()
        self.eng = Engine(self.target, self.cfg)
        self.run_id = self.eng.create_run("t", "test")

    def tearDown(self):
        self._tmp.cleanup()

    def test_scope_violation_detail(self):
        self.eng.store.write_result(self.run_id, "checks.json", {
            "pass": False,
            "scope": {"unexpected_files": ["apps/other/leaked.ts"]},
        })
        detail = _checks_failure_detail(self.eng.store, self.run_id)
        self.assertIn("apps/other/leaked.ts", detail)
        self.assertIn("Scope violation", detail)

    def test_failing_command_detail(self):
        self.eng.store.write_result(self.run_id, "checks.json", {
            "pass": False,
            "checks": {"results": [{"command": "npm run lint", "pass": False}]},
        })
        detail = _checks_failure_detail(self.eng.store, self.run_id)
        self.assertIn("npm run lint", detail)

    def test_checks_escalated_notify_message_includes_detail(self):
        self.eng.store.write_result(self.run_id, "checks.json", {
            "pass": False,
            "checks": {"results": [{"command": "npm run build", "pass": False}]},
        })
        self.eng.store.update_state(self.run_id, checks_retry_count=3)
        msg = notify_message(self.eng.store, self.run_id, "checks_escalated")
        self.assertIn("npm run build", msg)
        self.assertIn("3 attempt", msg)


class TestAutoShip(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.target = Path(self._tmp.name)
        _init_scratch_repo(self.target)

    def tearDown(self):
        self._tmp.cleanup()

    def test_auto_ship_disabled_by_default(self):
        cfg = GantryConfig()
        self.assertFalse(cfg.git.auto_ship)
        eng = Engine(self.target, cfg)
        run_id = eng.create_run("t", "test")
        eng.store.update_state(run_id, status="review_approved")

        with patch("gantry.ship.ship_run") as mock_ship:
            result = advance_run(eng, run_id)

        self.assertFalse(mock_ship.called)
        self.assertEqual(result["action"], "no_auto_transition")

    def test_auto_ship_enabled_calls_ship_run(self):
        cfg = GantryConfig()
        cfg.git.auto_ship = True
        eng = Engine(self.target, cfg)
        run_id = eng.create_run("t", "test")
        eng.store.update_state(run_id, status="review_approved")

        with patch("gantry.ship.ship_run") as mock_ship:
            mock_ship.return_value = {"ok": True, "pr": {"url": "https://example.com/pr/1"}}
            result = advance_run(eng, run_id)

        self.assertTrue(mock_ship.called)
        self.assertEqual(result["action"], "auto_shipped")
        self.assertEqual(result["pr_url"], "https://example.com/pr/1")

    def test_auto_ship_failure_reports_action(self):
        cfg = GantryConfig()
        cfg.git.auto_ship = True
        eng = Engine(self.target, cfg)
        run_id = eng.create_run("t", "test")
        eng.store.update_state(run_id, status="review_approved")

        with patch("gantry.ship.ship_run") as mock_ship:
            mock_ship.return_value = {"ok": False, "stage": "push"}
            result = advance_run(eng, run_id)

        self.assertEqual(result["action"], "auto_ship_failed")


class TestRepairStaleRunning(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.target = Path(self._tmp.name)
        _init_scratch_repo(self.target)
        self.cfg = GantryConfig()
        self.eng = Engine(self.target, self.cfg)
        self.run_id = self.eng.create_run("t", "test")

    def tearDown(self):
        self._tmp.cleanup()

    def test_stale_heartbeat_marks_stage_failed(self):
        from gantry.advance import _repair_stale_running
        from gantry.state import now_iso
        import time
        self.eng.store.update_state(self.run_id, status="build_running",
                                    heartbeat_at=now_iso())
        # Backdate the heartbeat past the grace window (3x HEARTBEAT_INTERVAL)
        # without sleeping in the test.
        from gantry.engine import HEARTBEAT_INTERVAL
        stale_ts = time.time() - (HEARTBEAT_INTERVAL * 3 + 5)
        from datetime import datetime, timezone
        stale_iso = datetime.fromtimestamp(stale_ts, tz=timezone.utc).isoformat()
        self.eng.store.update_state(self.run_id, heartbeat_at=stale_iso)
        run = {"id": self.run_id, "status": "build_running", "mtime": time.time()}

        result = _repair_stale_running(self.eng, run)
        self.assertIsNotNone(result)
        self.assertEqual(result["action"], "repaired_stale_running")
        self.assertEqual(self.eng.store.state(self.run_id)["status"], "build_failed")

    def test_fresh_heartbeat_is_not_repaired(self):
        from gantry.advance import _repair_stale_running
        from gantry.state import now_iso
        import time
        self.eng.store.update_state(self.run_id, status="build_running", heartbeat_at=now_iso())
        run = {"id": self.run_id, "status": "build_running", "mtime": time.time()}

        result = _repair_stale_running(self.eng, run)
        self.assertIsNone(result)
        self.assertEqual(self.eng.store.state(self.run_id)["status"], "build_running")

    def test_no_heartbeat_falls_back_to_stage_timeout(self):
        from gantry.advance import _repair_stale_running
        import time
        self.eng.store.update_state(self.run_id, status="build_running")
        old_mtime = time.time() - self.cfg.model_for("build").timeout - 200
        run = {"id": self.run_id, "status": "build_running", "mtime": old_mtime}

        result = _repair_stale_running(self.eng, run)
        self.assertIsNotNone(result)
        self.assertEqual(self.eng.store.state(self.run_id)["status"], "build_failed")


class TestRunDependencies(unittest.TestCase):
    """Queueing/prerequisite runs: create_run(depends_on=[...]) parks a run at
    status="queued" until every listed run reaches review_approved; the
    poller (advance_run/advance_all) auto-transitions it to awaiting_{stage}
    once prereqs clear, same as any other AUTO_TRANSITIONS status."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.target = Path(self._tmp.name)
        _init_scratch_repo(self.target)
        self.cfg = GantryConfig()
        self.eng = Engine(self.target, self.cfg)

    def tearDown(self):
        self._tmp.cleanup()

    def test_create_run_without_depends_on_starts_immediately(self):
        rid = self.eng.create_run("t", "test")
        self.assertEqual(self.eng.store.state(rid)["status"], f"awaiting_{self.cfg.stages[0]}")

    def test_create_run_with_depends_on_is_queued(self):
        prereq = self.eng.create_run("prereq", "test")
        rid = self.eng.create_run("dependent", "test", depends_on=[prereq])
        st = self.eng.store.state(rid)
        self.assertEqual(st["status"], "queued")
        self.assertEqual(st["depends_on"], [prereq])

    def test_create_run_depends_on_unknown_run_raises(self):
        with self.assertRaises(ValueError):
            self.eng.create_run("dependent", "test", depends_on=["does-not-exist"])

    def test_advance_run_leaves_queued_run_parked_while_prereq_incomplete(self):
        prereq = self.eng.create_run("prereq", "test")
        rid = self.eng.create_run("dependent", "test", depends_on=[prereq])

        result = advance_run(self.eng, rid)
        self.assertFalse(result["advanced"])
        self.assertEqual(result["action"], "waiting_on_prereqs")
        self.assertEqual(self.eng.store.state(rid)["status"], "queued")

    def test_advance_run_starts_queued_run_once_prereq_approved(self):
        prereq = self.eng.create_run("prereq", "test")
        rid = self.eng.create_run("dependent", "test", depends_on=[prereq])
        self.eng.store.update_state(prereq, status="review_approved")

        result = advance_run(self.eng, rid)
        self.assertTrue(result["advanced"])
        self.assertEqual(self.eng.store.state(rid)["status"], f"awaiting_{self.cfg.stages[0]}")

    def test_prereqs_met_false_if_any_dependency_unapproved(self):
        p1 = self.eng.create_run("p1", "test")
        p2 = self.eng.create_run("p2", "test")
        rid = self.eng.create_run("dependent", "test", depends_on=[p1, p2])
        self.eng.store.update_state(p1, status="review_approved")
        # p2 still awaiting_plan — not approved
        self.assertFalse(self.eng._prereqs_met(rid))

        self.eng.store.update_state(p2, status="review_approved")
        self.assertTrue(self.eng._prereqs_met(rid))

    def test_queued_is_in_auto_transitions(self):
        from gantry.advance import AUTO_TRANSITIONS
        self.assertIn("queued", AUTO_TRANSITIONS)

    def test_advance_all_picks_up_queued_run_once_unblocked(self):
        from gantry.advance import advance_all
        prereq = self.eng.create_run("prereq", "test")
        rid = self.eng.create_run("dependent", "test", depends_on=[prereq])
        self.eng.store.update_state(prereq, status="review_approved")

        with patch.object(Engine, "run_agent_stage", _fake_run_agent_stage):
            results = advance_all(self.target, self.cfg)

        matching = [r for r in results if r.get("run_id") == rid or rid in str(r)]
        # advance_all's return shape may vary; assert on the actual state
        # change instead of over-fitting to the return payload's exact keys.
        self.assertNotEqual(self.eng.store.state(rid)["status"], "queued")


if __name__ == "__main__":
    unittest.main()
