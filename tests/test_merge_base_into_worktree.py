import subprocess
import tempfile
import unittest
from pathlib import Path

from gantry.git import branch_name, ensure_worktree, merge_base_into_worktree, worktree_path


def _run(cmd, cwd):
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, check=True)


def _init_scratch_repo(path: Path) -> None:
    _run(["git", "init", "-q"], path)
    _run(["git", "config", "user.email", "test@example.com"], path)
    _run(["git", "config", "user.name", "Test"], path)
    (path / "README.md").write_text("init\n")
    _run(["git", "add", "README.md"], path)
    _run(["git", "commit", "-q", "-m", "init"], path)
    _run(["git", "branch", "-M", "main"], path)


class TestMergeBaseIntoWorktree(unittest.TestCase):
    """merge_base_into_worktree covers the gap sync_local_base_branch (a
    different, earlier fix) does NOT: sync_local_base_branch only prevents a
    worktree from being CREATED off a stale base. It does nothing once a
    worktree already exists — if base_branch keeps moving after that (e.g.
    another queued run ships mid-way through this run's build), the run's own
    branch never catches up on its own, and the scope guard's merge-base diff
    against it goes stale, making already-shipped files look like new/
    unexpected ones on THIS run's diff."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.target = Path(self._tmp.name)
        _init_scratch_repo(self.target)

    def tearDown(self):
        self._tmp.cleanup()

    def _make_worktree(self, run_id="run-1"):
        return ensure_worktree(self.target, run_id, "main")

    def test_noop_when_worktree_branch_already_has_current_main(self):
        wt = self._make_worktree()
        result = merge_base_into_worktree(self.target, "run-1", "main")
        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "already_current")

    def test_merges_in_new_main_commits_that_landed_after_worktree_creation(self):
        wt = self._make_worktree()
        # Simulate another run shipping to main after this worktree was cut.
        (self.target / "shipped-by-another-run.txt").write_text("hi\n")
        _run(["git", "add", "shipped-by-another-run.txt"], self.target)
        _run(["git", "commit", "-q", "-m", "feat: another run shipped"], self.target)

        result = merge_base_into_worktree(self.target, "run-1", "main")

        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "merged")
        self.assertTrue((wt / "shipped-by-another-run.txt").exists())

    def test_scope_guard_no_longer_flags_files_shipped_by_another_run_after_merge(self):
        """The actual regression this exists to fix: without the merge, a
        scope-guard diff against the stale merge-base would flag the other
        run's files as unexpected on THIS run's branch."""
        wt = self._make_worktree()
        (self.target / "shipped-by-another-run.txt").write_text("hi\n")
        _run(["git", "add", "shipped-by-another-run.txt"], self.target)
        _run(["git", "commit", "-q", "-m", "feat: another run shipped"], self.target)

        before = subprocess.run(["git", "diff", "--name-only", "main", "--"], cwd=str(wt),
                                capture_output=True, text=True).stdout
        self.assertIn("shipped-by-another-run.txt", before)  # would falsely show as this run's diff

        merge_base_into_worktree(self.target, "run-1", "main")

        after = subprocess.run(["git", "diff", "--name-only", "main", "--"], cwd=str(wt),
                               capture_output=True, text=True).stdout
        self.assertNotIn("shipped-by-another-run.txt", after)

    def test_real_conflict_is_reported_not_silently_resolved(self):
        wt = self._make_worktree()
        # This run's branch edits README.md...
        (wt / "README.md").write_text("run's own version\n")
        _run(["git", "add", "README.md"], wt)
        _run(["git", "commit", "-q", "-m", "run edits README"], wt)
        # ...while another run ships a conflicting edit to the same line on main.
        (self.target / "README.md").write_text("main's conflicting version\n")
        _run(["git", "add", "README.md"], self.target)
        _run(["git", "commit", "-q", "-m", "main also edits README"], self.target)

        result = merge_base_into_worktree(self.target, "run-1", "main")

        self.assertFalse(result["ok"])
        self.assertEqual(result["action"], "merge_conflict")
        self.assertIn("README.md", result["output"])
        # Left in the conflicted state for build/resume or a human to resolve —
        # not force-resolved to either side.
        status = subprocess.run(["git", "status", "--porcelain"], cwd=str(wt),
                                capture_output=True, text=True).stdout
        self.assertIn("README.md", status)

    def test_returns_ok_when_worktree_does_not_exist(self):
        result = merge_base_into_worktree(self.target, "never-created", "main")
        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "no_worktree")


if __name__ == "__main__":
    unittest.main()
