import subprocess
import tempfile
import unittest
from pathlib import Path

from gantry.git import commit_all


def _init_scratch_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(path), check=True)
    subprocess.run(["git", "commit", "--allow-empty", "-m", "init", "-q"], cwd=str(path), check=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=str(path), check=True)


class TestCommitAll(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        _init_scratch_repo(self.repo)

    def tearDown(self):
        self._tmp.cleanup()

    def test_commits_real_changes(self):
        (self.repo / "deliverable.txt").write_text("hello\n")
        result = commit_all(self.repo, "add deliverable")
        self.assertTrue(result["ok"])
        self.assertTrue(result["committed"])

    def test_noop_when_truly_nothing_changed(self):
        result = commit_all(self.repo, "nothing to do")
        self.assertTrue(result["ok"])
        self.assertFalse(result["committed"])
        self.assertEqual(result["reason"], "no changes")

    def test_noop_when_only_agent_runs_dir_present(self):
        """Regression test: a real bug had `commit_all` gate on raw
        `git status --porcelain` output, which still reports untracked
        `.agent-runs/` (unstaged by the `git reset -- .agent-runs` line just
        above it) as `?? .agent-runs/` — non-empty, so the function proceeded
        to `git commit` with NOTHING actually staged. That commit legitimately
        fails ("nothing added to commit but untracked files present"),
        surfacing as ship_failed for runs that had no real work left to ship.
        The fix checks staged changes (`git diff --cached --name-only`)
        instead, which correctly reads as empty here."""
        agent_runs = self.repo / ".agent-runs" / "some-run"
        agent_runs.mkdir(parents=True)
        (agent_runs / "build-summary.md").write_text("# summary\n")

        result = commit_all(self.repo, "should be a no-op")
        self.assertTrue(result["ok"])
        self.assertFalse(result["committed"])
        self.assertEqual(result["reason"], "no changes")

    def test_commits_real_changes_even_alongside_agent_runs_noise(self):
        agent_runs = self.repo / ".agent-runs" / "some-run"
        agent_runs.mkdir(parents=True)
        (agent_runs / "build-summary.md").write_text("# summary\n")
        (self.repo / "deliverable.txt").write_text("real work\n")

        result = commit_all(self.repo, "add deliverable")
        self.assertTrue(result["ok"])
        self.assertTrue(result["committed"])

        proc = subprocess.run(["git", "show", "--stat", "HEAD"], cwd=str(self.repo),
                              capture_output=True, text=True, check=True)
        self.assertIn("deliverable.txt", proc.stdout)
        self.assertNotIn(".agent-runs", proc.stdout)


if __name__ == "__main__":
    unittest.main()
