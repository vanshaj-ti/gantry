import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gantry.config import GantryConfig
from gantry.engine import Engine
from gantry.ship import ship_run


def _init_scratch_repo(path: Path) -> None:
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(path), check=True)
    subprocess.run(["git", "commit", "--allow-empty", "-m", "init", "-q"], cwd=str(path), check=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=str(path), check=True)


class TestShipRun(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.target = Path(self._tmp.name)
        _init_scratch_repo(self.target)
        self.cfg = GantryConfig()
        self.eng = Engine(self.target, self.cfg)
        self.run_id = self.eng.create_run("t", "test")

    def tearDown(self):
        self._tmp.cleanup()

    def _patch_all(self, commit_ok=True, push_ok=True, pr_ok=True, merge_ok=True):
        meta = {"title": "t", "body": "## Summary\n\nt", "branch_slug": "t"}
        commit_res = {"ok": commit_ok, "committed": True, "output": ""}
        push_res = {"ok": push_ok, "output": "", "remote_branch": "feat/x"}
        pr_res = {"ok": pr_ok, "url": "https://example.com/pr/1" if pr_ok else None, "output": ""}
        merge_res = {"ok": merge_ok, "output": ""}
        return (
            patch("gantry.ship.draft_ship_meta", return_value=meta),
            patch("gantry.ship.commit_all", return_value=commit_res),
            patch("gantry.ship.push", return_value=push_res),
            patch("gantry.ship.create_pr", return_value=pr_res),
            patch("gantry.ship.merge_pr", return_value=merge_res),
        )

    def test_auto_merge_disabled_by_default_does_not_call_merge_pr(self):
        p0, p1, p2, p3, p4 = self._patch_all()
        with p0, p1, p2, p3, p4 as mock_merge:
            result = ship_run(self.eng, self.run_id)

        self.assertTrue(result["ok"])
        self.assertIsNone(result["merge"])
        mock_merge.assert_not_called()
        self.assertEqual(self.eng.store.state(self.run_id)["status"], "shipped")

    def test_auto_merge_enabled_calls_merge_pr_and_records_merged_true(self):
        self.cfg.git.auto_merge = True
        p0, p1, p2, p3, p4 = self._patch_all(merge_ok=True)
        with p0, p1, p2, p3, p4 as mock_merge:
            result = ship_run(self.eng, self.run_id)

        self.assertTrue(result["ok"])
        mock_merge.assert_called_once()
        self.assertEqual(result["merge"]["ok"], True)
        st = self.eng.store.state(self.run_id)
        self.assertEqual(st["status"], "shipped")
        self.assertTrue(st["merged"])

    def test_auto_merge_failure_still_leaves_run_shipped_not_ship_failed(self):
        """A failed auto-merge leaves a real, open PR — a normal recoverable
        state, not the same failure class as a broken commit/push/PR-create
        step. status must stay 'shipped' (with merged=False) so the run isn't
        mistaken for one that needs re-shipping from scratch."""
        self.cfg.git.auto_merge = True
        p0, p1, p2, p3, p4 = self._patch_all(merge_ok=False)
        with p0, p1, p2, p3, p4:
            result = ship_run(self.eng, self.run_id)

        self.assertTrue(result["ok"])
        st = self.eng.store.state(self.run_id)
        self.assertEqual(st["status"], "shipped")
        self.assertFalse(st["merged"])

    def test_pr_creation_failure_sets_ship_failed_and_skips_merge(self):
        p0, p1, p2, p3, p4 = self._patch_all(pr_ok=False)
        with p0, p1, p2, p3, p4 as mock_merge:
            result = ship_run(self.eng, self.run_id)

        self.assertFalse(result["ok"])
        mock_merge.assert_not_called()
        self.assertEqual(self.eng.store.state(self.run_id)["status"], "ship_failed")

    def test_push_failure_sets_ship_failed_and_never_reaches_merge(self):
        p0, p1, p2, p3, p4 = self._patch_all(push_ok=False)
        with p0, p1, p2, p3, p4 as mock_merge:
            result = ship_run(self.eng, self.run_id)

        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "push")
        mock_merge.assert_not_called()
        self.assertEqual(self.eng.store.state(self.run_id)["status"], "ship_failed")


if __name__ == "__main__":
    unittest.main()
