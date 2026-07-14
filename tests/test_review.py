import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gantry.config import GantryConfig
from gantry.runners import RunnerResult
from gantry.review import _parse_verdict, run_review
from gantry.state import RunStore


def _init_scratch_repo(path: Path) -> None:
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(path), check=True)
    subprocess.run(["git", "commit", "--allow-empty", "-m", "init", "-q"], cwd=str(path), check=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=str(path), check=True)


class TestParseVerdict(unittest.TestCase):
    def setUp(self):
        self.cfg = GantryConfig()

    def test_approve_keyword(self):
        self.assertEqual(_parse_verdict("Looks great. APPROVE", self.cfg), "APPROVE")

    def test_request_changes_keyword(self):
        self.assertEqual(_parse_verdict("Missing tests. REQUEST_CHANGES", self.cfg), "REQUEST_CHANGES")

    def test_escalate_keyword(self):
        self.assertEqual(_parse_verdict("Unclear scope. ESCALATE", self.cfg), "ESCALATE")

    def test_case_insensitive(self):
        self.assertEqual(_parse_verdict("approve", self.cfg), "APPROVE")

    def test_no_keyword_defaults_to_escalate(self):
        self.assertEqual(_parse_verdict("I have no idea what to say here.", self.cfg), "ESCALATE")

    def test_empty_text_defaults_to_escalate(self):
        self.assertEqual(_parse_verdict("", self.cfg), "ESCALATE")
        self.assertEqual(_parse_verdict(None, self.cfg), "ESCALATE")

    def test_request_changes_takes_priority_over_approve(self):
        # A REQUEST_CHANGES verdict may still reason about what *would* be
        # approvable — priority order (request_changes > escalate > approve)
        # must not let a mentioned "APPROVE" elsewhere override it.
        text = "This would normally APPROVE but there's a bug: REQUEST_CHANGES"
        self.assertEqual(_parse_verdict(text, self.cfg), "REQUEST_CHANGES")

    def test_escalate_takes_priority_over_approve(self):
        text = "Would APPROVE but I'm not confident; ESCALATE to a human"
        self.assertEqual(_parse_verdict(text, self.cfg), "ESCALATE")


class TestRunReview(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.target = Path(self._tmp.name)
        _init_scratch_repo(self.target)
        self.store = RunStore(self.target)
        self.cfg = GantryConfig()
        self.run_id = self.store.new_run_id("t")
        self.store.create(self.run_id, "t")

    def tearDown(self):
        self._tmp.cleanup()

    def _fake_runner(self, verdict_text: str, ok: bool = True):
        class _FakeRunner:
            name = "claude-code"

            def run(self, **kwargs):
                return RunnerResult(ok=ok, session_id="sess-123", exit_code=0 if ok else 1,
                                    raw={"result": verdict_text}, stdout=verdict_text, stderr="")
        return _FakeRunner()

    def test_approve_writes_result_and_updates_state(self):
        with patch("gantry.review.get_runner", return_value=self._fake_runner("Looks good. APPROVE")):
            out = run_review(self.store, self.run_id, self.cfg, self.target)
        self.assertEqual(out["verdict"], "APPROVE")
        result = self.store.read_result(self.run_id, "review-result.json")
        self.assertEqual(result["verdict"], "APPROVE")
        self.assertEqual(self.store.state(self.run_id)["status"], "review_approved")
        self.assertEqual(self.store.state(self.run_id)["review_verdict"], "APPROVE")

    def test_request_changes_writes_comments_file(self):
        with patch("gantry.review.get_runner", return_value=self._fake_runner("Needs work. REQUEST_CHANGES")):
            run_review(self.store, self.run_id, self.cfg, self.target)
        self.assertEqual(self.store.state(self.run_id)["status"], "review_changes_requested")
        comments = self.store.read_artifact(self.run_id, "review-comments.md")
        self.assertIsNotNone(comments)
        self.assertIn("Needs work", comments)

    def test_approve_does_not_write_comments_file(self):
        with patch("gantry.review.get_runner", return_value=self._fake_runner("APPROVE")):
            run_review(self.store, self.run_id, self.cfg, self.target)
        self.assertIsNone(self.store.read_artifact(self.run_id, "review-comments.md"))

    def test_runner_failure_defaults_to_escalate(self):
        with patch("gantry.review.get_runner", return_value=self._fake_runner("APPROVE", ok=False)):
            out = run_review(self.store, self.run_id, self.cfg, self.target)
        self.assertEqual(out["verdict"], "ESCALATE")
        self.assertEqual(self.store.state(self.run_id)["status"], "review_escalated")


if __name__ == "__main__":
    unittest.main()
