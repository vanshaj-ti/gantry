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

    def test_retry_feedback_accumulates_across_attempts_not_overwrites(self):
        # Each advance_run tick from build_complete re-runs checks (still
        # failing, per _fake_run_agent_stage always landing back at
        # build_complete) then writes exactly one new retry attempt to
        # answers/build.md — two ticks from blocked should leave two
        # attempts recorded, not one overwritten.
        eng = self._make_engine(retry_checks=3)
        run_id = eng.create_run("t", "test")
        eng.store.update_state(run_id, status="build_complete")

        with patch.object(Engine, "run_agent_stage", _fake_run_agent_stage):
            advance_run(eng, run_id)  # build_complete -> blocked (checks fail)
            advance_run(eng, run_id)  # blocked -> retry (attempt 1) -> build_complete
            advance_run(eng, run_id)  # build_complete -> blocked (checks fail again)
            advance_run(eng, run_id)  # blocked -> retry (attempt 2) -> build_complete

        answer = eng.store.read_artifact(run_id, "answers/build.md")
        self.assertIn("Attempt 1/3", answer)
        self.assertIn("Attempt 2/3", answer)

    def test_retry_feedback_caps_at_max_attempts_kept(self):
        from gantry.advance import _MAX_RETRY_ATTEMPTS_KEPT
        eng = self._make_engine(retry_checks=10)
        run_id = eng.create_run("t", "test")
        eng.store.update_state(run_id, status="build_complete")

        with patch.object(Engine, "run_agent_stage", _fake_run_agent_stage):
            for _ in range(12):
                advance_run(eng, run_id)

        answer = eng.store.read_artifact(run_id, "answers/build.md")
        attempt_headers = [line for line in answer.splitlines() if line.startswith("## Attempt")]
        self.assertEqual(len(attempt_headers), _MAX_RETRY_ATTEMPTS_KEPT)
        # oldest attempts dropped, most recent kept
        self.assertNotIn("Attempt 1/10", answer)


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

    def test_base_branch_merge_conflict_detail(self):
        self.eng.store.write_result(self.run_id, "checks.json", {
            "pass": False,
            "base_branch_merge": {"ok": False, "action": "merge_conflict",
                                  "output": "CONFLICT (content): Merge conflict in src/foo.ts"},
        })
        detail = _checks_failure_detail(self.eng.store, self.run_id)
        self.assertIn("src/foo.ts", detail)
        self.assertIn("conflict", detail.lower())
        self.assertIn("Resolve the conflict markers", detail)

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

    def test_advance_all_does_not_pick_up_review_approved_when_auto_ship_off(self):
        """review_approved is a human-gated terminal state by default —
        gantry loop/advance --all must never touch it unless auto_ship is on."""
        from gantry.advance import advance_all
        cfg = GantryConfig()
        self.assertFalse(cfg.git.auto_ship)
        eng = Engine(self.target, cfg)
        run_id = eng.create_run("t", "test")
        eng.store.update_state(run_id, status="review_approved")

        with patch("gantry.ship.ship_run") as mock_ship:
            results = advance_all(self.target, cfg)

        self.assertFalse(mock_ship.called)
        touched = [r for r in results if r.get("run_id") == run_id]
        self.assertEqual(touched, [])

    def test_advance_all_picks_up_review_approved_and_ships_when_auto_ship_on(self):
        """Regression test for a real bug: AUTO_TRANSITIONS (the static gate
        advance_all checks BEFORE calling advance_run) never included
        review_approved, even conditionally — so a project with auto_ship=true
        in gantry.toml never actually got its approved runs shipped by the
        passive poller (`gantry loop` / cron calling advance --all). Only a
        direct `gantry advance --run <id>` call (which invokes advance_run
        directly, bypassing advance_all's gate) would trigger the ship. This
        silently defeated the entire point of auto_ship existing — a run
        would sit at review_approved indefinitely under passive polling."""
        from gantry.advance import advance_all
        cfg = GantryConfig()
        cfg.git.auto_ship = True
        eng = Engine(self.target, cfg)
        run_id = eng.create_run("t", "test")
        eng.store.update_state(run_id, status="review_approved")

        with patch("gantry.ship.ship_run") as mock_ship:
            mock_ship.return_value = {"ok": True, "pr": {"url": "https://example.com/pr/1"}}
            results = advance_all(self.target, cfg)

        self.assertTrue(mock_ship.called)
        touched = [r for r in results if r.get("run_id") == run_id]
        self.assertEqual(len(touched), 1)
        self.assertEqual(touched[0]["action"], "auto_shipped")


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
    status="queued" until every listed run is actually merged (not merely
    review_approved — a dependent starting the moment its prereq is approved
    would build against code that isn't even on base_branch yet); the
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

    def test_advance_run_leaves_queued_run_parked_while_prereq_only_approved(self):
        # review_approved alone must NOT satisfy prereqs — the PR hasn't even
        # been opened yet at that point, let alone merged.
        prereq = self.eng.create_run("prereq", "test")
        rid = self.eng.create_run("dependent", "test", depends_on=[prereq])
        self.eng.store.update_state(prereq, status="review_approved")

        result = advance_run(self.eng, rid)
        self.assertFalse(result["advanced"])
        self.assertEqual(self.eng.store.state(rid)["status"], "queued")

    def test_advance_run_leaves_queued_run_parked_while_shipped_but_unmerged(self):
        # shipped (PR opened) without merged=True still must not satisfy
        # prereqs — the PR could still be sitting open, unreviewed, or closed
        # without merging.
        prereq = self.eng.create_run("prereq", "test")
        rid = self.eng.create_run("dependent", "test", depends_on=[prereq])
        self.eng.store.update_state(prereq, status="shipped")

        result = advance_run(self.eng, rid)
        self.assertFalse(result["advanced"])
        self.assertEqual(self.eng.store.state(rid)["status"], "queued")

    def test_advance_run_starts_queued_run_once_prereq_shipped_and_merged(self):
        prereq = self.eng.create_run("prereq", "test")
        rid = self.eng.create_run("dependent", "test", depends_on=[prereq])
        self.eng.store.update_state(prereq, status="shipped", merged=True)

        result = advance_run(self.eng, rid)
        self.assertTrue(result["advanced"])
        self.assertEqual(self.eng.store.state(rid)["status"], f"awaiting_{self.cfg.stages[0]}")

    def test_advance_run_starts_queued_run_once_prereq_shipped_manually_and_merged(self):
        prereq = self.eng.create_run("prereq", "test")
        rid = self.eng.create_run("dependent", "test", depends_on=[prereq])
        self.eng.store.update_state(prereq, status="shipped_manually", merged=True)

        result = advance_run(self.eng, rid)
        self.assertTrue(result["advanced"])

    def test_prereqs_met_false_if_any_dependency_not_merged(self):
        p1 = self.eng.create_run("p1", "test")
        p2 = self.eng.create_run("p2", "test")
        rid = self.eng.create_run("dependent", "test", depends_on=[p1, p2])
        self.eng.store.update_state(p1, status="shipped", merged=True)
        # p2 still awaiting_plan — not shipped or merged
        self.assertFalse(self.eng._prereqs_met(rid))

        self.eng.store.update_state(p2, status="shipped", merged=True)
        self.assertTrue(self.eng._prereqs_met(rid))

    def test_queued_is_in_auto_transitions(self):
        from gantry.advance import AUTO_TRANSITIONS
        self.assertIn("queued", AUTO_TRANSITIONS)

    def test_advance_all_picks_up_queued_run_once_unblocked(self):
        from gantry.advance import advance_all
        prereq = self.eng.create_run("prereq", "test")
        rid = self.eng.create_run("dependent", "test", depends_on=[prereq])
        self.eng.store.update_state(prereq, status="shipped", merged=True)

        with patch.object(Engine, "run_agent_stage", _fake_run_agent_stage):
            advance_all(self.target, self.cfg)

        self.assertNotEqual(self.eng.store.state(rid)["status"], "queued")


class TestResolverStage(unittest.TestCase):
    """auto_resolve: when normal build/checks auto-retry exhausts and a run
    hits checks_escalated, spawn a dedicated resolver agent instead of
    dead-ending at a human forever. Critically, gantry must re-verify the
    resolver's work itself (real run_checks) rather than trust the agent's
    own claim — that's the exact failure mode from the incident that
    motivated this feature (a resumed build agent reported build_complete
    while a real unresolved merge-conflict marker was still committed)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.target = Path(self._tmp.name)
        _init_scratch_repo(self.target)

    def tearDown(self):
        self._tmp.cleanup()

    def _make_engine(self, auto_resolve=True, resolve_attempts=2):
        cfg = GantryConfig()
        cfg.checks.auto_resolve = auto_resolve
        cfg.checks.resolve_attempts = resolve_attempts
        return Engine(self.target, cfg)

    def test_checks_escalated_not_auto_advanced_when_auto_resolve_off(self):
        from gantry.advance import advance_run
        eng = self._make_engine(auto_resolve=False)
        run_id = eng.create_run("t", "test")
        eng.store.update_state(run_id, status="checks_escalated")

        with patch.object(Engine, "run_resolver_stage") as mock_resolve:
            result = advance_run(eng, run_id)

        self.assertFalse(mock_resolve.called)
        self.assertEqual(result["action"], "no_auto_transition")

    def test_checks_escalated_spawns_resolver_when_auto_resolve_on(self):
        from gantry.advance import advance_run
        eng = self._make_engine(auto_resolve=True)
        run_id = eng.create_run("t", "test")
        eng.store.update_state(run_id, status="checks_escalated")

        def _fake_resolver(self, rid):
            # Simulate the resolver stage's own real behavior: it transitions
            # status itself (to build_complete on verified success) before
            # returning — advance_run must read that post-call, not assume it.
            self.store.update_state(rid, status="build_complete")
            return {"agent_ok": True, "verified_pass": True}

        with patch.object(Engine, "run_resolver_stage", _fake_resolver):
            result = advance_run(eng, run_id)

        self.assertEqual(result["action"], "resolver_attempted")
        self.assertTrue(result["verified_pass"])
        self.assertEqual(eng.store.state(run_id)["status"], "build_complete")

    def test_resolver_attempts_capped_then_resolve_escalated(self):
        from gantry.advance import advance_run
        eng = self._make_engine(auto_resolve=True, resolve_attempts=2)
        run_id = eng.create_run("t", "test")
        eng.store.update_state(run_id, status="checks_escalated", resolve_attempt_count=2)

        with patch.object(Engine, "run_resolver_stage") as mock_resolve:
            result = advance_run(eng, run_id)

        self.assertFalse(mock_resolve.called)
        self.assertEqual(result["action"], "resolve_escalated")
        self.assertEqual(eng.store.state(run_id)["status"], "resolve_escalated")

    def test_checks_escalated_in_auto_transitions_only_when_auto_resolve_on(self):
        from gantry.advance import advance_all
        eng_off = self._make_engine(auto_resolve=False)
        run_id = eng_off.create_run("t", "test")
        eng_off.store.update_state(run_id, status="checks_escalated")

        with patch.object(Engine, "run_resolver_stage") as mock_resolve:
            advance_all(self.target, eng_off.cfg)
        self.assertFalse(mock_resolve.called)

        # Flip auto_resolve on for the SAME run — advance_all's gate must
        # reflect the config passed to it, not a stale property of the run.
        cfg_on = self._make_engine(auto_resolve=True).cfg
        with patch.object(Engine, "run_resolver_stage") as mock_resolve:
            mock_resolve.return_value = {"agent_ok": True, "verified_pass": True}
            advance_all(self.target, cfg_on)
        mock_resolve.assert_called_once_with(run_id)

    def test_resolver_stage_verifies_via_real_checks_not_agent_self_report(self):
        """The core guarantee: even if the resolver agent's own subprocess
        result claims success, run_resolver_stage's returned verified_pass
        must reflect an ACTUAL re-run of checks, not the agent's stdout."""
        eng = self._make_engine(auto_resolve=True)
        run_id = eng.create_run("t", "test")
        eng.store.write_result(run_id, "checks.json", {"pass": False, "scope": {"pass": False}})

        fake_runner_result = type("R", (), {
            "ok": True, "stdout": "I fixed it and everything passes!",
            "stderr": "", "raw": {"result": "done"}, "session_id": "s1",
            "usage": {"cost_usd": None, "input_tokens": None, "output_tokens": None, "duration_ms": None},
        })()

        with patch("gantry.engine.get_runner") as mock_get_runner, \
             patch.object(Engine, "run_checks") as mock_run_checks:
            mock_get_runner.return_value.run.return_value = fake_runner_result
            # Real checks say it's STILL failing, despite the agent's claim.
            mock_run_checks.return_value = {"pass": False, "scope": {"pass": True}}
            result = eng.run_resolver_stage(run_id)

        self.assertFalse(result["verified_pass"])
        self.assertEqual(eng.store.state(run_id)["status"], "checks_escalated")

    def test_resolver_stage_accepts_success_only_on_real_verified_pass(self):
        eng = self._make_engine(auto_resolve=True)
        run_id = eng.create_run("t", "test")
        eng.store.write_result(run_id, "checks.json", {"pass": False, "scope": {"pass": False}})

        fake_runner_result = type("R", (), {
            "ok": True, "stdout": "fixed", "stderr": "", "raw": {"result": "done"},
            "session_id": "s1",
            "usage": {"cost_usd": None, "input_tokens": None, "output_tokens": None, "duration_ms": None},
        })()

        with patch("gantry.engine.get_runner") as mock_get_runner, \
             patch.object(Engine, "run_checks") as mock_run_checks:
            mock_get_runner.return_value.run.return_value = fake_runner_result
            mock_run_checks.return_value = {"pass": True}
            result = eng.run_resolver_stage(run_id)

        self.assertTrue(result["verified_pass"])
        self.assertEqual(eng.store.state(run_id)["status"], "build_complete")


class TestShortLabel(unittest.TestCase):
    def test_known_status_is_shorter_than_full_label(self):
        from gantry.advance import label, short_label
        status = "review_changes_requested"
        self.assertLess(len(short_label(status)), len(label(status)))

    def test_unknown_status_falls_back_to_raw_string(self):
        from gantry.advance import short_label
        self.assertEqual(short_label("some_new_status"), "some_new_status")

    def test_held_and_shipped_manually_have_short_labels(self):
        from gantry.advance import short_label
        self.assertEqual(short_label("held"), "Held")
        self.assertEqual(short_label("shipped_manually"), "Shipped (manual)")


class TestRunTags(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.target = Path(self._tmp.name)
        _init_scratch_repo(self.target)
        self.cfg = GantryConfig()
        self.eng = Engine(self.target, self.cfg)

    def tearDown(self):
        self._tmp.cleanup()

    def test_create_run_with_tag_stores_it(self):
        rid = self.eng.create_run("t", "test", tag="auth-feature")
        self.assertEqual(self.eng.store.state(rid)["tag"], "auth-feature")

    def test_create_run_without_tag_has_no_tag_key_forced(self):
        rid = self.eng.create_run("t", "test")
        self.assertNotIn("tag", self.eng.store.state(rid))

    def test_list_runs_surfaces_tag(self):
        rid = self.eng.create_run("t", "test", tag="release-42")
        runs = self.eng.store.list_runs()
        matching = [r for r in runs if r["id"] == rid]
        self.assertEqual(matching[0]["tag"], "release-42")

    def test_advance_all_tag_filter_only_touches_matching_runs(self):
        from gantry.advance import advance_all
        r1 = self.eng.create_run("t1", "test", tag="alpha")
        r2 = self.eng.create_run("t2", "test", tag="beta")
        self.eng.store.update_state(r1, status="plan_complete")
        self.eng.store.update_state(r2, status="plan_complete")

        with patch.object(Engine, "run_agent_stage", _fake_run_agent_stage):
            results = advance_all(self.target, self.cfg, tag="alpha")

        touched_ids = [r["run_id"] for r in results]
        self.assertIn(r1, touched_ids)
        self.assertNotIn(r2, touched_ids)

    def test_advance_all_no_tag_touches_all_runs(self):
        from gantry.advance import advance_all
        r1 = self.eng.create_run("t1", "test", tag="alpha")
        r2 = self.eng.create_run("t2", "test", tag="beta")
        self.eng.store.update_state(r1, status="plan_complete")
        self.eng.store.update_state(r2, status="plan_complete")

        with patch.object(Engine, "run_agent_stage", _fake_run_agent_stage):
            results = advance_all(self.target, self.cfg)

        touched_ids = [r["run_id"] for r in results]
        self.assertIn(r1, touched_ids)
        self.assertIn(r2, touched_ids)


class TestConcurrencyCap(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.target = Path(self._tmp.name)
        _init_scratch_repo(self.target)
        self.cfg = GantryConfig()
        self.eng = Engine(self.target, self.cfg)

    def tearDown(self):
        self._tmp.cleanup()

    def test_serial_path_by_default_processes_all_runs(self):
        from gantry.advance import advance_all
        ids = [self.eng.create_run(f"t{i}", "test") for i in range(4)]
        for rid in ids:
            self.eng.store.update_state(rid, status="plan_complete")

        with patch.object(Engine, "run_agent_stage", _fake_run_agent_stage):
            results = advance_all(self.target, self.cfg)

        self.assertEqual(len(results), 4)

    def test_max_concurrent_above_one_still_processes_all_runs(self):
        from gantry.advance import advance_all
        self.cfg.agent.max_concurrent = 3
        ids = [self.eng.create_run(f"t{i}", "test") for i in range(6)]
        for rid in ids:
            self.eng.store.update_state(rid, status="plan_complete")

        with patch.object(Engine, "run_agent_stage", _fake_run_agent_stage):
            results = advance_all(self.target, self.cfg)

        self.assertEqual(len(results), 6)
        for rid in ids:
            self.assertEqual(self.eng.store.state(rid)["status"], "build_complete")


class TestStageSkip(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.target = Path(self._tmp.name)
        _init_scratch_repo(self.target)

    def tearDown(self):
        self._tmp.cleanup()

    def test_build_complete_skips_directly_to_review_when_evidence_not_in_stages(self):
        cfg = GantryConfig()
        cfg.stages = ["plan", "build", "review"]
        eng = Engine(self.target, cfg)
        run_id = eng.create_run("t", "test")
        eng.store.update_state(run_id, status="build_complete")

        with patch("gantry.advance.run_e2e_tests", return_value={"pass": True}), \
             patch.object(Engine, "run_checks", return_value={"pass": True}), \
             patch("gantry.advance.run_review", return_value={"verdict": "APPROVE"}) as mock_review:
            result = advance_run(eng, run_id)

        self.assertTrue(mock_review.called)
        self.assertEqual(result["action"], "evidence_skipped->review")
        # evidence stage's own agent invocation must never have been reached
        self.assertIsNone(eng.store.get_session_id(run_id, "evidence"))

    def test_build_complete_reports_no_further_stages_when_evidence_and_review_both_skipped(self):
        cfg = GantryConfig()
        cfg.stages = ["plan", "build"]
        cfg.review.enabled = False
        eng = Engine(self.target, cfg)
        run_id = eng.create_run("t", "test")
        eng.store.update_state(run_id, status="build_complete")

        with patch("gantry.advance.run_e2e_tests", return_value={"pass": True}), \
             patch.object(Engine, "run_checks", return_value={"pass": True}):
            result = advance_run(eng, run_id)

        self.assertFalse(result["advanced"])
        self.assertEqual(result["action"], "review_disabled")

    def test_evidence_in_stages_still_runs_evidence_normally(self):
        cfg = GantryConfig()  # default stages include evidence
        eng = Engine(self.target, cfg)
        run_id = eng.create_run("t", "test")
        eng.store.update_state(run_id, status="build_complete")

        with patch("gantry.advance.run_e2e_tests", return_value={"pass": True}), \
             patch.object(Engine, "run_checks", return_value={"pass": True}), \
             patch.object(Engine, "run_agent_stage") as mock_run_agent_stage:
            result = advance_run(eng, run_id)

        mock_run_agent_stage.assert_called_once_with(run_id, "evidence", resume=False)
        self.assertEqual(result["action"], "checks_passed->evidence")


if __name__ == "__main__":
    unittest.main()
