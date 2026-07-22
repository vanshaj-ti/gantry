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

    def test_rollback_note_appears_in_shipped_pr_body(self):
        """Task 3: draft_ship_meta's rollback_note gets appended as a
        '## Rollback' section in the PR body that actually gets passed to
        create_pr."""
        meta = {"title": "t", "body": "## Summary\n\nt", "branch_slug": "t",
                "rollback_note": "Revert this PR; no migration was added."}
        commit_res = {"ok": True, "committed": True, "output": ""}
        push_res = {"ok": True, "output": "", "remote_branch": "feat/x"}
        pr_res = {"ok": True, "url": "https://example.com/pr/1", "output": ""}

        with patch("gantry.ship.draft_ship_meta", return_value=meta), \
             patch("gantry.ship.commit_all", return_value=commit_res), \
             patch("gantry.ship.push", return_value=push_res), \
             patch("gantry.ship.create_pr", return_value=pr_res) as mock_create_pr:
            result = ship_run(self.eng, self.run_id)

        self.assertTrue(result["ok"])
        body_arg = mock_create_pr.call_args.args[4]
        self.assertIn("## Rollback", body_arg)
        self.assertIn("Revert this PR; no migration was added.", body_arg)


class TestShipFinalGate(unittest.TestCase):
    """Task 1 + Task 2: the re-verification gate at the start of ship_run."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.target = Path(self._tmp.name)
        _init_scratch_repo(self.target)
        self.cfg = GantryConfig()
        self.eng = Engine(self.target, self.cfg)
        self.run_id = self.eng.create_run("t", "test")

    def tearDown(self):
        self._tmp.cleanup()

    def test_checks_failure_at_ship_time_blocks_and_sets_ship_checks_failed(self):
        """(a) A checks failure at the start of ship_run must block push/commit
        entirely and set ship_checks_failed — not silently retried as generic
        ship_failed, not auto-resumed (verified separately in test_advance.py's
        AUTO_TRANSITIONS coverage)."""
        with patch.object(Engine, "run_checks", return_value={"pass": False, "scope": {"pass": True},
                                                               "checks": {"results": []}}), \
             patch("gantry.ship.draft_ship_meta") as mock_draft, \
             patch("gantry.ship.commit_all") as mock_commit, \
             patch("gantry.ship.push") as mock_push, \
             patch("gantry.ship.create_pr") as mock_pr:
            result = ship_run(self.eng, self.run_id)

        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "ship_gate")
        self.assertEqual(result["reason"], "checks_failed")
        mock_draft.assert_not_called()
        mock_commit.assert_not_called()
        mock_push.assert_not_called()
        mock_pr.assert_not_called()
        self.assertEqual(self.eng.store.state(self.run_id)["status"], "ship_checks_failed")

    def test_surviving_blocking_finding_blocks_ship_even_from_review_approved(self):
        """(b) A surviving blocking finding in review-result.json (two_axis=True)
        blocks ship even if status somehow reached review_approved — constructed
        directly rather than relying on it happening naturally."""
        self.eng.store.update_state(self.run_id, status="review_approved")
        self.eng.store.write_result(self.run_id, "review-result.json", {
            "two_axis": True,
            "verdict": "APPROVE",
            "combined_verdict": "APPROVE",
            "spec": {"verdict": "APPROVE", "findings": [
                {"severity": "Critical", "action": "blocking", "location": "a.py",
                 "description": "unresolved conflict marker", "recommendation": "fix it"},
            ]},
            "standards": {"verdict": "APPROVE", "findings": []},
        })

        with patch.object(Engine, "run_checks") as mock_run_checks, \
             patch("gantry.ship.draft_ship_meta") as mock_draft, \
             patch("gantry.ship.commit_all") as mock_commit:
            result = ship_run(self.eng, self.run_id)

        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "ship_gate")
        self.assertEqual(result["reason"], "blocking_findings_survived")
        self.assertEqual(len(result["blocking_findings"]), 1)
        # The surviving-finding gate short-circuits before even reaching the
        # fresh run_checks call — no reason to spend a real checks run when
        # ship is already blocked on something worse.
        mock_run_checks.assert_not_called()
        mock_draft.assert_not_called()
        mock_commit.assert_not_called()
        self.assertEqual(self.eng.store.state(self.run_id)["status"], "ship_checks_failed")

    def test_legacy_two_axis_false_review_result_skips_finding_check_gracefully(self):
        """(c) two_axis=False legacy shape doesn't crash the new finding-check
        logic — it should just skip that sub-check gracefully (no structured
        findings to inspect), relying only on the fresh checks re-run."""
        self.eng.store.write_result(self.run_id, "review-result.json", {
            "verdict": "APPROVE", "ok": True, "model": "m", "session_id": "s",
            "result": "looks good",
        })
        meta = {"title": "t", "body": "## Summary\n\nt", "branch_slug": "t", "rollback_note": "Revert this PR."}
        commit_res = {"ok": True, "committed": True, "output": ""}
        push_res = {"ok": True, "output": "", "remote_branch": "feat/x"}
        pr_res = {"ok": True, "url": "https://example.com/pr/1", "output": ""}

        with patch.object(Engine, "run_checks", return_value={"pass": True, "scope": {"pass": True},
                                                               "checks": {"results": []}}), \
             patch("gantry.ship.draft_ship_meta", return_value=meta), \
             patch("gantry.ship.commit_all", return_value=commit_res), \
             patch("gantry.ship.push", return_value=push_res), \
             patch("gantry.ship.create_pr", return_value=pr_res):
            result = ship_run(self.eng, self.run_id)

        self.assertTrue(result["ok"])
        self.assertEqual(self.eng.store.state(self.run_id)["status"], "shipped")


class TestShipConflictResolution(unittest.TestCase):
    """Task 4: conflict-shaped push/create_pr failures route to the
    conflict-resolver stage (resolve by intent), non-conflict failures keep
    using the existing generic ship_failed retry path unchanged."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.target = Path(self._tmp.name)
        _init_scratch_repo(self.target)
        self.cfg = GantryConfig()
        self.eng = Engine(self.target, self.cfg)
        self.run_id = self.eng.create_run("t", "test")
        self.meta = {"title": "t", "body": "## Summary\n\nt", "branch_slug": "t",
                    "rollback_note": "Revert this PR."}
        self.passing_checks = {"pass": True, "scope": {"pass": True}, "checks": {"results": []}}

    def tearDown(self):
        self._tmp.cleanup()

    def test_conflict_shaped_push_failure_routes_to_resolver_then_reattempts(self):
        """(e) A conflict-shaped push failure routes to run_resolver_stage with
        the new intent-framing, then re-attempts push/create_pr for real
        (verified via a fake runner/fake git output) rather than the generic
        retry path."""
        commit_res = {"ok": True, "committed": True, "output": ""}
        conflict_push_res = {"ok": False, "output": "! [rejected] failed to push some refs",
                             "remote_branch": "feat/x"}
        good_push_res = {"ok": True, "output": "", "remote_branch": "feat/x"}
        pr_res = {"ok": True, "url": "https://example.com/pr/1", "output": ""}

        push_calls = [conflict_push_res, good_push_res]

        def _fake_push(*a, **kw):
            return push_calls.pop(0)

        with patch.object(Engine, "run_checks", return_value=self.passing_checks), \
             patch("gantry.ship.draft_ship_meta", return_value=self.meta), \
             patch("gantry.ship.commit_all", return_value=commit_res), \
             patch("gantry.ship.push", side_effect=_fake_push), \
             patch("gantry.ship.create_pr", return_value=pr_res), \
             patch.object(Engine, "run_resolver_stage") as mock_resolver:
            result = ship_run(self.eng, self.run_id)

        mock_resolver.assert_called_once_with(self.run_id, failure_kind="conflict",
                                              conflict_output=conflict_push_res["output"])
        self.assertTrue(result["ok"])
        self.assertEqual(self.eng.store.state(self.run_id)["status"], "shipped")

    def test_conflict_shaped_push_failure_still_fails_if_resolver_does_not_fix_it(self):
        """The resolver's own claim is never trusted — if the re-attempted push
        still fails after the resolver ran, ship still reports failure (and
        does not fall into an infinite loop, capped at
        _MAX_CONFLICT_RESOLVE_ATTEMPTS)."""
        commit_res = {"ok": True, "committed": True, "output": ""}
        conflict_push_res = {"ok": False, "output": "CONFLICT (content): merge conflict in a.py",
                             "remote_branch": "feat/x"}

        with patch.object(Engine, "run_checks", return_value=self.passing_checks), \
             patch("gantry.ship.draft_ship_meta", return_value=self.meta), \
             patch("gantry.ship.commit_all", return_value=commit_res), \
             patch("gantry.ship.push", return_value=conflict_push_res), \
             patch("gantry.ship.create_pr") as mock_pr, \
             patch.object(Engine, "run_resolver_stage") as mock_resolver:
            result = ship_run(self.eng, self.run_id)

        mock_resolver.assert_called_once()
        # push kept failing on retry -> ship_run returns at the push stage,
        # never reaching create_pr.
        mock_pr.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "push")
        self.assertEqual(self.eng.store.state(self.run_id)["status"], "ship_failed")

    def test_non_conflict_push_failure_uses_generic_ship_failed_path_unchanged(self):
        """(f) A non-conflict failure (simulated network error text) still
        uses the existing generic ship_failed retry path unchanged — explicit
        regression test that Task 4 doesn't change this path's behavior."""
        commit_res = {"ok": True, "committed": True, "output": ""}
        network_push_res = {"ok": False, "output": "curl: (7) Failed to connect to github.com port 443",
                            "remote_branch": "feat/x"}

        with patch.object(Engine, "run_checks", return_value=self.passing_checks), \
             patch("gantry.ship.draft_ship_meta", return_value=self.meta), \
             patch("gantry.ship.commit_all", return_value=commit_res), \
             patch("gantry.ship.push", return_value=network_push_res), \
             patch("gantry.ship.create_pr") as mock_pr, \
             patch.object(Engine, "run_resolver_stage") as mock_resolver:
            result = ship_run(self.eng, self.run_id)

        mock_resolver.assert_not_called()
        mock_pr.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "push")
        self.assertEqual(self.eng.store.state(self.run_id)["status"], "ship_failed")


if __name__ == "__main__":
    unittest.main()
