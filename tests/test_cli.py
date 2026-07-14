import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gantry.cli import build_parser
from gantry.cli.run_commands import cmd_init, cmd_run, cmd_status


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

    def test_cmd_status_lists_runs(self):
        run_args = build_parser().parse_args(["run", "--title", "my feature"])
        cmd_run(run_args)
        status_args = build_parser().parse_args(["status"])
        rc, out = self._run_and_capture(cmd_status, status_args)
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertEqual(len(payload), 1)


if __name__ == "__main__":
    unittest.main()
