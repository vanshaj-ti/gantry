import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gantry.cli import build_parser
from gantry.cli.run_commands import (
    cmd_cancel, cmd_cleanup, cmd_hold, cmd_init, cmd_mark_merged, cmd_mark_shipped,
    cmd_resume_hold, cmd_retry, cmd_run, cmd_status,
)
from gantry.git import ensure_worktree


def _init_scratch_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(path), check=True)
    subprocess.run(["git", "commit", "--allow-empty", "-m", "init", "-q"], cwd=str(path), check=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=str(path), check=True)


class TestBuildParser(unittest.TestCase):
    """Smoke test: every documented subcommand parses without raising, and
    dispatches to the expected handler."""

    def setUp(self):
        self.parser = build_parser()

    def _parse(self, argv):
        return self.parser.parse_args(argv)

    def test_init(self):
        args = self._parse(["init"])
        self.assertEqual(args.func, cmd_init)

    def test_run(self):
        args = self._parse(["run", "--title", "t"])
        self.assertEqual(args.title, "t")

    def test_stage(self):
        args = self._parse(["stage", "plan", "--run", "r1"])
        self.assertEqual(args.stage, "plan")

    def test_retry(self):
        args = self._parse(["retry", "build", "--run", "r1"])
        self.assertEqual(args.func, cmd_retry)
        self.assertEqual(args.stage, "build")

    def test_checks(self):
        self._parse(["checks", "--run", "r1"])

    def test_review(self):
        self._parse(["review", "--run", "r1"])

    def test_approve(self):
        self._parse(["approve", "--run", "r1", "--stage", "plan"])

    def test_revise(self):
        self._parse(["revise", "--run", "r1", "--stage", "plan", "comment text"])

    def test_ship(self):
        self._parse(["ship", "--run", "r1"])

    def test_status(self):
        args = self._parse(["status"])
        self.assertEqual(args.func, cmd_status)

    def test_advance(self):
        self._parse(["advance", "--all"])

    def test_loop(self):
        self._parse(["loop", "--max-ticks", "1"])

    def test_doctor(self):
        self._parse(["doctor"])

    def test_listen(self):
        self._parse(["listen"])

    def test_docs(self):
        self._parse(["docs"])

    def test_watch(self):
        self._parse(["watch"])

    def test_mcp(self):
        self._parse(["mcp", "--list"])

    def test_daemon(self):
        self._parse(["daemon", "status"])

    def test_cockpit(self):
        self._parse(["cockpit"])

    def test_update(self):
        self._parse(["update"])

    def test_cancel(self):
        args = self._parse(["cancel", "--run", "r1"])
        self.assertEqual(args.func, cmd_cancel)

    def test_cleanup(self):
        args = self._parse(["cleanup"])
        self.assertEqual(args.func, cmd_cleanup)

    def test_missing_command_errors(self):
        with self.assertRaises(SystemExit):
            self._parse([])


class TestCmdInitAndRun(unittest.TestCase):
    """Integration-style: exercise the highest-risk handlers against a real
    scratch repo, mirroring the pattern in test_advance.py."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.target = Path(self._tmp.name)
        _init_scratch_repo(self.target)
        self._env_patch = patch.dict("os.environ", {"GANTRY_TARGET": str(self.target)})
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()
        self._tmp.cleanup()

    def _run_and_capture(self, func, args):
        with patch("builtins.print") as mock_print:
            rc = func(args)
        out = "".join(c.args[0] for c in mock_print.call_args_list if c.args)
        return rc, out

    def test_cmd_init_scaffolds_config(self):
        args = build_parser().parse_args(["init"])
        rc, out = self._run_and_capture(cmd_init, args)
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertTrue(payload["ok"])
        self.assertTrue((self.target / "gantry.toml").exists())

    def test_cmd_init_refuses_overwrite_without_force(self):
        args = build_parser().parse_args(["init"])
        cmd_init(args)
        rc, out = self._run_and_capture(cmd_init, args)
        payload = json.loads(out)
        self.assertFalse(payload["ok"])

    def test_cmd_run_creates_run(self):
        args = build_parser().parse_args(["run", "--title", "my feature"])
        rc, out = self._run_and_capture(cmd_run, args)
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertTrue(payload["ok"])
        self.assertIn("run_id", payload)

    def test_cmd_run_with_tag_stores_and_reports_it(self):
        from gantry.state import RunStore
        args = build_parser().parse_args(["run", "--title", "my feature", "--tag", "release-1"])
        rc, out = self._run_and_capture(cmd_run, args)
        payload = json.loads(out)
        self.assertEqual(payload["tag"], "release-1")
        self.assertEqual(RunStore(self.target).state(payload["run_id"])["tag"], "release-1")

    def test_cmd_run_without_tag_has_no_tag_in_output(self):
        args = build_parser().parse_args(["run", "--title", "my feature"])
        rc, out = self._run_and_capture(cmd_run, args)
        payload = json.loads(out)
        self.assertNotIn("tag", payload)

    def test_cmd_status_lists_runs(self):
        run_args = build_parser().parse_args(["run", "--title", "my feature"])
        cmd_run(run_args)
        status_args = build_parser().parse_args(["status"])
        rc, out = self._run_and_capture(cmd_status, status_args)
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertEqual(len(payload), 1)


class TestCmdCancelAndCleanup(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.target = Path(self._tmp.name)
        _init_scratch_repo(self.target)
        self._env_patch = patch.dict("os.environ", {"GANTRY_TARGET": str(self.target)})
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()
        self._tmp.cleanup()

    def _run_and_capture(self, func, args):
        with patch("builtins.print") as mock_print:
            rc = func(args)
        out = "".join(c.args[0] for c in mock_print.call_args_list if c.args)
        return rc, out

    def test_cmd_cancel_marks_status(self):
        run_args = build_parser().parse_args(["run", "--title", "my feature"])
        _, run_out = self._run_and_capture(cmd_run, run_args)
        run_id = json.loads(run_out)["run_id"]

        cancel_args = build_parser().parse_args(["cancel", "--run", run_id])
        rc, out = self._run_and_capture(cmd_cancel, cancel_args)
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "cancelled")

    def test_cmd_cancel_refuses_shipped_without_force(self):
        run_args = build_parser().parse_args(["run", "--title", "my feature"])
        _, run_out = self._run_and_capture(cmd_run, run_args)
        run_id = json.loads(run_out)["run_id"]
        from gantry.state import RunStore
        RunStore(self.target).update_state(run_id, status="shipped")

        cancel_args = build_parser().parse_args(["cancel", "--run", run_id])
        rc, out = self._run_and_capture(cmd_cancel, cancel_args)
        payload = json.loads(out)
        self.assertFalse(payload["ok"])

    def test_cmd_cleanup_dry_run_lists_without_deleting(self):
        run_args = build_parser().parse_args(["run", "--title", "my feature"])
        _, run_out = self._run_and_capture(cmd_run, run_args)
        run_id = json.loads(run_out)["run_id"]
        from gantry.state import RunStore
        store = RunStore(self.target)
        store.update_state(run_id, status="shipped")
        wt = ensure_worktree(self.target, run_id, "main")
        self.assertTrue(wt.exists())

        cleanup_args = build_parser().parse_args(["cleanup"])
        rc, out = self._run_and_capture(cmd_cleanup, cleanup_args)
        payload = json.loads(out)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["count"], 1)
        self.assertTrue(wt.exists())  # dry-run: nothing actually removed

    def test_cmd_cleanup_yes_removes_worktree(self):
        run_args = build_parser().parse_args(["run", "--title", "my feature"])
        _, run_out = self._run_and_capture(cmd_run, run_args)
        run_id = json.loads(run_out)["run_id"]
        from gantry.state import RunStore
        store = RunStore(self.target)
        store.update_state(run_id, status="shipped")
        wt = ensure_worktree(self.target, run_id, "main")

        cleanup_args = build_parser().parse_args(["cleanup", "--yes"])
        rc, out = self._run_and_capture(cmd_cleanup, cleanup_args)
        payload = json.loads(out)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["dry_run"])
        self.assertFalse(wt.exists())

        result = subprocess.run(["git", "worktree", "list"], cwd=str(self.target),
                                capture_output=True, text=True, check=True)
        self.assertNotIn(run_id, result.stdout)


class TestCmdHoldAndResume(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.target = Path(self._tmp.name)
        _init_scratch_repo(self.target)
        self._env_patch = patch.dict("os.environ", {"GANTRY_TARGET": str(self.target)})
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()
        self._tmp.cleanup()

    def _run_and_capture(self, func, args):
        with patch("builtins.print") as mock_print:
            rc = func(args)
        out = "".join(c.args[0] for c in mock_print.call_args_list if c.args)
        return rc, out

    def test_hold_then_resume_round_trips_prior_status(self):
        from gantry.state import RunStore
        run_args = build_parser().parse_args(["run", "--title", "my feature"])
        _, run_out = self._run_and_capture(cmd_run, run_args)
        run_id = json.loads(run_out)["run_id"]
        store = RunStore(self.target)
        store.update_state(run_id, status="blocked", blocked_on="checks")

        hold_args = build_parser().parse_args(["hold", "--run", run_id])
        rc, out = self._run_and_capture(cmd_hold, hold_args)
        payload = json.loads(out)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "held")
        self.assertEqual(payload["held_from_status"], "blocked")
        self.assertEqual(store.state(run_id)["status"], "held")

        resume_args = build_parser().parse_args(["resume", "--run", run_id])
        rc, out = self._run_and_capture(cmd_resume_hold, resume_args)
        payload = json.loads(out)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(store.state(run_id)["status"], "blocked")

    def test_hold_refuses_while_stage_running(self):
        from gantry.state import RunStore
        run_args = build_parser().parse_args(["run", "--title", "my feature"])
        _, run_out = self._run_and_capture(cmd_run, run_args)
        run_id = json.loads(run_out)["run_id"]
        RunStore(self.target).update_state(run_id, status="build_running")

        hold_args = build_parser().parse_args(["hold", "--run", run_id])
        rc, out = self._run_and_capture(cmd_hold, hold_args)
        payload = json.loads(out)
        self.assertFalse(payload["ok"])

    def test_hold_refuses_when_already_held(self):
        from gantry.state import RunStore
        run_args = build_parser().parse_args(["run", "--title", "my feature"])
        _, run_out = self._run_and_capture(cmd_run, run_args)
        run_id = json.loads(run_out)["run_id"]
        RunStore(self.target).update_state(run_id, status="held", held_from_status="blocked")

        hold_args = build_parser().parse_args(["hold", "--run", run_id])
        rc, out = self._run_and_capture(cmd_hold, hold_args)
        payload = json.loads(out)
        self.assertFalse(payload["ok"])

    def test_resume_refuses_when_not_held(self):
        run_args = build_parser().parse_args(["run", "--title", "my feature"])
        _, run_out = self._run_and_capture(cmd_run, run_args)
        run_id = json.loads(run_out)["run_id"]

        resume_args = build_parser().parse_args(["resume", "--run", run_id])
        rc, out = self._run_and_capture(cmd_resume_hold, resume_args)
        payload = json.loads(out)
        self.assertFalse(payload["ok"])

    def test_held_run_excluded_from_advance_all(self):
        from gantry.advance import advance_all
        from gantry.state import RunStore
        run_args = build_parser().parse_args(["run", "--title", "my feature"])
        _, run_out = self._run_and_capture(cmd_run, run_args)
        run_id = json.loads(run_out)["run_id"]
        store = RunStore(self.target)
        store.update_state(run_id, status="held", held_from_status="blocked")

        from gantry.config import load_config
        results = advance_all(self.target, load_config(self.target))
        touched_ids = [r.get("run_id") for r in results]
        self.assertNotIn(run_id, touched_ids)
        self.assertEqual(store.state(run_id)["status"], "held")


class TestCmdMarkShipped(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.target = Path(self._tmp.name)
        _init_scratch_repo(self.target)
        self._env_patch = patch.dict("os.environ", {"GANTRY_TARGET": str(self.target)})
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()
        self._tmp.cleanup()

    def _run_and_capture(self, func, args):
        with patch("builtins.print") as mock_print:
            rc = func(args)
        out = "".join(c.args[0] for c in mock_print.call_args_list if c.args)
        return rc, out

    def test_marks_shipped_manually(self):
        from gantry.state import RunStore
        run_args = build_parser().parse_args(["run", "--title", "my feature"])
        _, run_out = self._run_and_capture(cmd_run, run_args)
        run_id = json.loads(run_out)["run_id"]

        args = build_parser().parse_args(["mark-shipped", "--run", run_id])
        rc, out = self._run_and_capture(cmd_mark_shipped, args)
        payload = json.loads(out)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "shipped_manually")
        self.assertEqual(RunStore(self.target).state(run_id)["status"], "shipped_manually")

    def test_refuses_when_already_shipped_without_force(self):
        from gantry.state import RunStore
        run_args = build_parser().parse_args(["run", "--title", "my feature"])
        _, run_out = self._run_and_capture(cmd_run, run_args)
        run_id = json.loads(run_out)["run_id"]
        RunStore(self.target).update_state(run_id, status="shipped")

        args = build_parser().parse_args(["mark-shipped", "--run", run_id])
        rc, out = self._run_and_capture(cmd_mark_shipped, args)
        payload = json.loads(out)
        self.assertFalse(payload["ok"])

    def test_force_overrides_already_shipped(self):
        from gantry.state import RunStore
        run_args = build_parser().parse_args(["run", "--title", "my feature"])
        _, run_out = self._run_and_capture(cmd_run, run_args)
        run_id = json.loads(run_out)["run_id"]
        RunStore(self.target).update_state(run_id, status="shipped")

        args = build_parser().parse_args(["mark-shipped", "--run", run_id, "--force"])
        rc, out = self._run_and_capture(cmd_mark_shipped, args)
        payload = json.loads(out)
        self.assertTrue(payload["ok"])


class TestCmdMarkMerged(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.target = Path(self._tmp.name)
        _init_scratch_repo(self.target)
        self._env_patch = patch.dict("os.environ", {"GANTRY_TARGET": str(self.target)})
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()
        self._tmp.cleanup()

    def _run_and_capture(self, func, args):
        with patch("builtins.print") as mock_print:
            rc = func(args)
        out = "".join(c.args[0] for c in mock_print.call_args_list if c.args)
        return rc, out

    def test_marks_merged_when_shipped(self):
        from gantry.state import RunStore
        run_args = build_parser().parse_args(["run", "--title", "my feature"])
        _, run_out = self._run_and_capture(cmd_run, run_args)
        run_id = json.loads(run_out)["run_id"]
        RunStore(self.target).update_state(run_id, status="shipped")

        args = build_parser().parse_args(["mark-merged", "--run", run_id])
        rc, out = self._run_and_capture(cmd_mark_merged, args)
        payload = json.loads(out)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["merged"])
        self.assertTrue(RunStore(self.target).state(run_id)["merged"])

    def test_marks_merged_when_shipped_manually(self):
        from gantry.state import RunStore
        run_args = build_parser().parse_args(["run", "--title", "my feature"])
        _, run_out = self._run_and_capture(cmd_run, run_args)
        run_id = json.loads(run_out)["run_id"]
        RunStore(self.target).update_state(run_id, status="shipped_manually")

        args = build_parser().parse_args(["mark-merged", "--run", run_id])
        rc, out = self._run_and_capture(cmd_mark_merged, args)
        payload = json.loads(out)
        self.assertTrue(payload["ok"])

    def test_refuses_when_not_shipped(self):
        run_args = build_parser().parse_args(["run", "--title", "my feature"])
        _, run_out = self._run_and_capture(cmd_run, run_args)
        run_id = json.loads(run_out)["run_id"]

        args = build_parser().parse_args(["mark-merged", "--run", run_id])
        rc, out = self._run_and_capture(cmd_mark_merged, args)
        payload = json.loads(out)
        self.assertFalse(payload["ok"])


class TestCmdRetry(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.target = Path(self._tmp.name)
        _init_scratch_repo(self.target)
        self._env_patch = patch.dict("os.environ", {"GANTRY_TARGET": str(self.target)})
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()
        self._tmp.cleanup()

    def _run_and_capture(self, func, args):
        with patch("builtins.print") as mock_print:
            rc = func(args)
        out = "".join(c.args[0] for c in mock_print.call_args_list if c.args)
        return rc, out

    def test_cmd_retry_runs_stage_without_resuming(self):
        from gantry.runners import RunnerResult
        run_args = build_parser().parse_args(["run", "--title", "my feature"])
        _, run_out = self._run_and_capture(cmd_run, run_args)
        run_id = json.loads(run_out)["run_id"]

        fake_result = RunnerResult(ok=True, session_id="sess-new", exit_code=0,
                                    raw={"result": "done"}, stdout="ok", stderr="")
        with patch("gantry.engine.get_runner") as mock_get_runner:
            mock_get_runner.return_value.name = "claude-code"
            mock_get_runner.return_value.run.return_value = fake_result
            retry_args = build_parser().parse_args(["retry", "plan", "--run", run_id])
            rc, out = self._run_and_capture(cmd_retry, retry_args)

        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertTrue(payload["ok"])
        # resume=False means no session_id kwarg is threaded through to the runner
        call_kwargs = mock_get_runner.return_value.run.call_args.kwargs
        self.assertIsNone(call_kwargs["session_id"])
        from gantry.state import RunStore
        self.assertEqual(RunStore(self.target).state(run_id)["status"], "plan_complete")


class TestCmdWatchMergeDetail(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.target = Path(self._tmp.name)
        _init_scratch_repo(self.target)
        self._env_patch = patch.dict("os.environ", {"GANTRY_TARGET": str(self.target)})
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()
        self._tmp.cleanup()

    def test_shipped_not_merged_shows_in_detail_column(self):
        from gantry.cli.watch import cmd_watch
        from gantry.state import RunStore
        run_args = build_parser().parse_args(["run", "--title", "my feature"])
        with patch("builtins.print") as mock_print:
            cmd_run(run_args)
        run_id = json.loads("".join(c.args[0] for c in mock_print.call_args_list if c.args))["run_id"]
        RunStore(self.target).update_state(run_id, status="shipped")

        watch_args = build_parser().parse_args(["watch"])
        with patch("sys.stdout.write") as mock_write:
            cmd_watch(watch_args)
        output = "".join(c.args[0] for c in mock_write.call_args_list if c.args)
        self.assertIn("not yet merged", output)

    def test_shipped_and_merged_shows_merged_in_detail_column(self):
        from gantry.cli.watch import cmd_watch
        from gantry.state import RunStore
        run_args = build_parser().parse_args(["run", "--title", "my feature"])
        with patch("builtins.print") as mock_print:
            cmd_run(run_args)
        run_id = json.loads("".join(c.args[0] for c in mock_print.call_args_list if c.args))["run_id"]
        RunStore(self.target).update_state(run_id, status="shipped", merged=True)

        watch_args = build_parser().parse_args(["watch"])
        with patch("sys.stdout.write") as mock_write:
            cmd_watch(watch_args)
        output = "".join(c.args[0] for c in mock_write.call_args_list if c.args)
        self.assertIn("merged", output)
        self.assertNotIn("not yet merged", output)


class TestIsLoopTerminal(unittest.TestCase):
    def test_review_approved_terminal_without_auto_ship(self):
        from gantry.cli.run_commands import _is_loop_terminal
        from gantry.config import GantryConfig
        cfg = GantryConfig()
        self.assertFalse(cfg.git.auto_ship)
        self.assertTrue(_is_loop_terminal("review_approved", cfg))

    def test_review_approved_not_terminal_with_auto_ship(self):
        from gantry.cli.run_commands import _is_loop_terminal
        from gantry.config import GantryConfig
        cfg = GantryConfig()
        cfg.git.auto_ship = True
        self.assertFalse(_is_loop_terminal("review_approved", cfg))

    def test_review_approved_terminal_when_cfg_omitted(self):
        from gantry.cli.run_commands import _is_loop_terminal
        self.assertTrue(_is_loop_terminal("review_approved"))

    def test_checks_escalated_terminal_without_auto_resolve(self):
        from gantry.cli.run_commands import _is_loop_terminal
        from gantry.config import GantryConfig
        cfg = GantryConfig()
        self.assertTrue(_is_loop_terminal("checks_escalated", cfg))

    def test_checks_escalated_not_terminal_with_auto_resolve(self):
        from gantry.cli.run_commands import _is_loop_terminal
        from gantry.config import GantryConfig
        cfg = GantryConfig()
        cfg.checks.auto_resolve = True
        self.assertFalse(_is_loop_terminal("checks_escalated", cfg))

    def test_other_terminal_statuses_unaffected_by_cfg(self):
        from gantry.cli.run_commands import _is_loop_terminal
        from gantry.config import GantryConfig
        cfg = GantryConfig()
        cfg.git.auto_ship = True
        cfg.checks.auto_resolve = True
        self.assertTrue(_is_loop_terminal("blocked", cfg))
        self.assertTrue(_is_loop_terminal("build_failed", cfg))
        self.assertTrue(_is_loop_terminal("review_escalated", cfg))
        self.assertTrue(_is_loop_terminal("shipped", cfg))

    def test_non_terminal_status_unaffected(self):
        from gantry.cli.run_commands import _is_loop_terminal
        from gantry.config import GantryConfig
        cfg = GantryConfig()
        self.assertFalse(_is_loop_terminal("build_running", cfg))
        self.assertFalse(_is_loop_terminal("plan_complete", cfg))

    def test_awaiting_agent_stage_not_terminal(self):
        """Real regression: a freshly-created run starts at awaiting_plan (the
        first stage in gantry.toml's default `stages` list), not awaiting_build.
        `gantry loop --run ID` must fire the plan stage instead of treating
        the fresh run as already done — same non-human-gated semantics
        advance.py's AUTO_TRANSITIONS already gives awaiting_{plan,build,evidence}."""
        from gantry.cli.run_commands import _is_loop_terminal
        self.assertFalse(_is_loop_terminal("awaiting_plan"))
        self.assertFalse(_is_loop_terminal("awaiting_build"))
        self.assertFalse(_is_loop_terminal("awaiting_evidence"))

    def test_awaiting_doc_stage_still_terminal(self):
        """awaiting_spec/awaiting_design are real human gates (DOC_STAGES) —
        loop must still stop there for `gantry approve` to be meaningful."""
        from gantry.cli.run_commands import _is_loop_terminal
        self.assertTrue(_is_loop_terminal("awaiting_spec"))
        self.assertTrue(_is_loop_terminal("awaiting_design"))


class TestCmdLoopAutoShip(unittest.TestCase):
    """Real regression: `gantry loop --run ID` on an auto_ship project must
    not stop at review_approved — it should keep ticking until ship_run
    actually fires, same as advance_all already does."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.target = Path(self._tmp.name)
        _init_scratch_repo(self.target)
        self._env_patch = patch.dict("os.environ", {"GANTRY_TARGET": str(self.target)})
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()
        self._tmp.cleanup()

    def _run_and_capture(self, func, args):
        with patch("builtins.print") as mock_print:
            rc = func(args)
        out = "".join(c.args[0] for c in mock_print.call_args_list if c.args)
        return rc, out

    def test_loop_keeps_ticking_past_review_approved_when_auto_ship(self):
        from gantry.cli.run_commands import cmd_loop
        from gantry.state import RunStore

        cfg_path = self.target / "gantry.toml"
        cfg_path.write_text("[git]\nauto_ship = true\n")

        run_args = build_parser().parse_args(["run", "--title", "t"])
        _, run_out = self._run_and_capture(cmd_run, run_args)
        run_id = json.loads(run_out)["run_id"]
        RunStore(self.target).update_state(run_id, status="review_approved")

        def fake_advance_run(eng, rid):
            eng.store.update_state(rid, status="shipped", merged=True)
            return {"advanced": True, "action": "shipped"}

        loop_args = build_parser().parse_args(
            ["loop", "--run", run_id, "--interval", "0", "--max-ticks", "3"])
        with patch("gantry.advance.advance_run", fake_advance_run), \
             patch("time.sleep"):
            rc, out = self._run_and_capture(cmd_loop, loop_args)

        # Must have actually advanced past review_approved to shipped, not
        # stopped immediately at tick 1 reporting review_approved as terminal.
        self.assertIn("shipped", out)
        self.assertEqual(RunStore(self.target).state(run_id)["status"], "shipped")


if __name__ == "__main__":
    unittest.main()
