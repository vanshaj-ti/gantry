import subprocess
import tempfile
import unittest
from pathlib import Path

from gantry.git import sync_local_base_branch


def _run(cmd, cwd):
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, check=True)


def _init_bare_and_clone(tmp: Path) -> tuple[Path, Path]:
    """Real origin + real local clone — sync_local_base_branch's whole job is
    reconciling local main against origin/main, so a fake single-repo setup
    (no actual remote) wouldn't exercise the real code path."""
    bare = tmp / "origin.git"
    bare.mkdir()
    _run(["git", "init", "--bare", "-q"], bare)

    local = tmp / "local"
    _run(["git", "clone", "-q", str(bare), str(local)], tmp)
    _run(["git", "config", "user.email", "test@example.com"], local)
    _run(["git", "config", "user.name", "Test"], local)
    (local / "README.md").write_text("init\n")
    _run(["git", "add", "README.md"], local)
    _run(["git", "commit", "-q", "-m", "init"], local)
    _run(["git", "branch", "-M", "main"], local)
    _run(["git", "push", "-q", "-u", "origin", "main"], local)
    return bare, local


class TestSyncLocalBaseBranch(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.bare, self.local = _init_bare_and_clone(self.tmp)

    def tearDown(self):
        self._tmp.cleanup()

    def _push_extra_commit_via_second_clone(self):
        """Simulate another actor (e.g. auto_merge squash-merging a PR on
        GitHub) advancing origin/main without touching our local clone."""
        second = self.tmp / "second"
        _run(["git", "clone", "-q", str(self.bare), str(second)], self.tmp)
        _run(["git", "config", "user.email", "t@e.com"], second)
        _run(["git", "config", "user.name", "T"], second)
        (second / "new-feature.txt").write_text("shipped\n")
        _run(["git", "add", "new-feature.txt"], second)
        _run(["git", "commit", "-q", "-m", "feat: ship something"], second)
        _run(["git", "push", "-q", "origin", "main"], second)

    def test_already_current_is_a_noop(self):
        result = sync_local_base_branch(self.local, "main")
        self.assertEqual(result["action"], "already_current")
        self.assertTrue(result["ok"])

    def test_fast_forwards_when_local_is_behind_origin(self):
        self._push_extra_commit_via_second_clone()
        result = sync_local_base_branch(self.local, "main")

        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "fast_forwarded")
        self.assertTrue((self.local / "new-feature.txt").exists())

    def test_fast_forward_works_even_when_a_different_branch_is_checked_out(self):
        """The real bug scenario: ensure_worktree calls this from the TARGET
        repo, which may have any branch checked out (or none in particular
        relevant), not necessarily main itself. Must still update local main
        without requiring a checkout."""
        self._push_extra_commit_via_second_clone()
        _run(["git", "checkout", "-q", "-b", "some-other-branch"], self.local)

        result = sync_local_base_branch(self.local, "main")

        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "fast_forwarded")
        # main itself advanced even though we're sitting on another branch.
        show = subprocess.run(["git", "log", "--oneline", "main"], cwd=str(self.local),
                              capture_output=True, text=True, check=True)
        self.assertIn("feat: ship something", show.stdout)

    def test_diverged_local_branch_is_left_alone(self):
        """Local main has a commit origin doesn't (not just behind) — must
        not force-update and silently discard it."""
        self._push_extra_commit_via_second_clone()
        (self.local / "local-only.txt").write_text("mine\n")
        _run(["git", "add", "local-only.txt"], self.local)
        _run(["git", "commit", "-q", "-m", "local work not yet pushed"], self.local)

        result = sync_local_base_branch(self.local, "main")

        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "skipped_diverged")
        self.assertTrue((self.local / "local-only.txt").exists())

    def test_remote_ref_base_branch_is_a_noop(self):
        result = sync_local_base_branch(self.local, "origin/main")
        self.assertEqual(result["action"], "skipped_remote_ref")

    def test_no_upstream_branch_is_a_noop_not_an_error(self):
        _run(["git", "checkout", "-q", "-b", "orphan-branch"], self.local)
        result = sync_local_base_branch(self.local, "orphan-branch")
        self.assertEqual(result["action"], "skipped_no_upstream")
        self.assertTrue(result["ok"])


if __name__ == "__main__":
    unittest.main()
