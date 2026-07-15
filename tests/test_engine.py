import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gantry.config import GantryConfig
from gantry.engine import Engine
from gantry.runners import RunnerResult


def _init_scratch_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(path), check=True)
    subprocess.run(["git", "commit", "--allow-empty", "-m", "init", "-q"], cwd=str(path), check=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=str(path), check=True)


class _FakeRunner:
    name = "claude-code"

    def __init__(self, result: RunnerResult = None, exc: Exception = None):
        self.result = result
        self.exc = exc

    def run(self, **kwargs):
        if self.exc:
            raise self.exc
        return self.result


class TestRunAgentStage(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.target = Path(self._tmp.name)
        _init_scratch_repo(self.target)
        self.cfg = GantryConfig()
        self.eng = Engine(self.target, self.cfg)
        self.run_id = self.eng.create_run("t", "do the thing")

    def tearDown(self):
        self._tmp.cleanup()

    def test_happy_path_sets_complete_status_and_writes_logs(self):
        fake = _FakeRunner(result=RunnerResult(
            ok=True, session_id="sess-1", exit_code=0,
            raw={"result": "done"}, stdout="did it", stderr=""))
        with patch("gantry.engine.get_runner", return_value=fake):
            res = self.eng.run_agent_stage(self.run_id, "plan")
        self.assertTrue(res["ok"])
        self.assertEqual(res["session_id"], "sess-1")
        self.assertEqual(self.eng.store.state(self.run_id)["status"], "plan_complete")
        self.assertEqual((self.eng.store.run_dir(self.run_id) / "logs" / "plan.stdout").read_text(), "did it")

    def test_failed_run_sets_failed_status(self):
        fake = _FakeRunner(result=RunnerResult(
            ok=False, session_id=None, exit_code=1,
            raw={"result": "error"}, stdout="", stderr="boom"))
        with patch("gantry.engine.get_runner", return_value=fake):
            res = self.eng.run_agent_stage(self.run_id, "plan")
        self.assertFalse(res["ok"])
        self.assertEqual(self.eng.store.state(self.run_id)["status"], "plan_failed")

    def test_timeout_sets_failed_status_and_error(self):
        fake = _FakeRunner(exc=subprocess.TimeoutExpired(cmd="agent", timeout=900))
        with patch("gantry.engine.get_runner", return_value=fake):
            res = self.eng.run_agent_stage(self.run_id, "plan")
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "timeout")
        self.assertEqual(self.eng.store.state(self.run_id)["status"], "plan_failed")

    def test_resume_without_stored_session_raises(self):
        fake = _FakeRunner(result=RunnerResult(
            ok=True, session_id="sess-1", exit_code=0, raw={}, stdout="", stderr=""))
        with patch("gantry.engine.get_runner", return_value=fake):
            with self.assertRaises(ValueError):
                self.eng.run_agent_stage(self.run_id, "plan", resume=True)

    def test_resume_with_stored_session_reuses_it(self):
        fake = _FakeRunner(result=RunnerResult(
            ok=True, session_id="sess-1", exit_code=0, raw={}, stdout="", stderr=""))
        with patch("gantry.engine.get_runner", return_value=fake):
            self.eng.run_agent_stage(self.run_id, "plan")
            res = self.eng.run_agent_stage(self.run_id, "plan", resume=True)
        self.assertTrue(res["ok"])

    def test_unknown_run_raises(self):
        with self.assertRaises(ValueError):
            self.eng.run_agent_stage("does-not-exist", "plan")

    def test_heartbeat_set_at_stage_start(self):
        fake = _FakeRunner(result=RunnerResult(
            ok=True, session_id="s1", exit_code=0, raw={}, stdout="", stderr=""))
        with patch("gantry.engine.get_runner", return_value=fake):
            self.eng.run_agent_stage(self.run_id, "plan")
        self.assertIn("heartbeat_at", self.eng.store.state(self.run_id))

    def test_heartbeat_thread_ticks_and_stops_after_stage(self):
        import time as _time

        class _SlowRunner(_FakeRunner):
            def run(self, **kwargs):
                _time.sleep(0.3)
                return self.result

        fake = _SlowRunner(result=RunnerResult(
            ok=True, session_id="s1", exit_code=0, raw={}, stdout="", stderr=""))
        with patch("gantry.engine.HEARTBEAT_INTERVAL", 0.05), \
             patch("gantry.engine.get_runner", return_value=fake):
            self.eng.run_agent_stage(self.run_id, "plan")

        beat_after = self.eng.store.state(self.run_id)["heartbeat_at"]
        _time.sleep(0.2)
        beat_later = self.eng.store.state(self.run_id)["heartbeat_at"]
        self.assertEqual(beat_after, beat_later)  # thread stopped, no further beats


class _CapturingRunner(_FakeRunner):
    """Records every prompt it's invoked with, so a test can assert on what
    the agent actually received rather than just on the final status."""

    def __init__(self, result: RunnerResult):
        super().__init__(result=result)
        self.prompts: list[str] = []

    def run(self, **kwargs):
        self.prompts.append(kwargs["prompt"])
        return self.result


class TestAnswerContextOnResume(unittest.TestCase):
    """Regression coverage for a real bug: `revise()` wrote reviewer comments
    to review-comments.md, but the resumed agent's prompt only ever pulled
    from answers/{stage}.md — a file `revise()` never wrote. A resumed build
    stage therefore saw NO new guidance and just re-confirmed its previously
    rejected output, silently looping forever. Separately, advance.py's
    checks/e2e auto-retry path writes failure detail to answers/build.md
    directly (a second, independent producer) — that path must keep working
    too. Both must reach the resumed prompt; neither may be dropped in favor
    of the other."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.target = Path(self._tmp.name)
        _init_scratch_repo(self.target)
        self.cfg = GantryConfig()
        self.eng = Engine(self.target, self.cfg)
        self.run_id = self.eng.create_run("t", "do the thing")

    def tearDown(self):
        self._tmp.cleanup()

    def _run_stage(self, runner, stage="build", resume=False):
        with patch("gantry.engine.get_runner", return_value=runner):
            return self.eng.run_agent_stage(self.run_id, stage, resume=resume)

    def test_revise_comments_reach_resumed_prompt(self):
        result = RunnerResult(ok=True, session_id="s1", exit_code=0, raw={}, stdout="", stderr="")
        runner = _CapturingRunner(result)
        self._run_stage(runner)  # initial build, establishes a session to resume

        self.eng.revise(self.run_id, "build", "Fix the git-staging bug, please.")
        self._run_stage(runner, resume=True)

        self.assertEqual(len(runner.prompts), 2)
        self.assertIn("Fix the git-staging bug, please.", runner.prompts[1])

    def test_checks_retry_answer_reaches_resumed_prompt(self):
        result = RunnerResult(ok=True, session_id="s1", exit_code=0, raw={}, stdout="", stderr="")
        runner = _CapturingRunner(result)
        self._run_stage(runner)

        answers_path = self.eng.store.artifact_path(self.run_id, "answers/build.md")
        answers_path.parent.mkdir(parents=True, exist_ok=True)
        answers_path.write_text("# Checks failed\n\nlint exited 1.\n")
        self._run_stage(runner, resume=True)

        self.assertIn("lint exited 1.", runner.prompts[1])

    def test_both_answer_sources_reach_resumed_prompt_when_both_present(self):
        result = RunnerResult(ok=True, session_id="s1", exit_code=0, raw={}, stdout="", stderr="")
        runner = _CapturingRunner(result)
        self._run_stage(runner)

        answers_path = self.eng.store.artifact_path(self.run_id, "answers/build.md")
        answers_path.parent.mkdir(parents=True, exist_ok=True)
        answers_path.write_text("# Checks failed\n\nbuild exited 1.\n")
        self.eng.revise(self.run_id, "build", "Also fix the scope issue.")
        self._run_stage(runner, resume=True)

        self.assertIn("build exited 1.", runner.prompts[1])
        self.assertIn("Also fix the scope issue.", runner.prompts[1])

    def test_no_answer_context_when_neither_file_exists(self):
        result = RunnerResult(ok=True, session_id="s1", exit_code=0, raw={}, stdout="", stderr="")
        runner = _CapturingRunner(result)
        self._run_stage(runner)
        self._run_stage(runner, resume=True)

        self.assertNotIn("Revision comments", runner.prompts[1])
        self.assertNotIn("Checks/e2e failure detail", runner.prompts[1])


if __name__ == "__main__":
    unittest.main()
