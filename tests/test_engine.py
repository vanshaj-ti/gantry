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


class TestBuildPreHook(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.target = Path(self._tmp.name)
        _init_scratch_repo(self.target)
        self.cfg = GantryConfig()
        self.eng = Engine(self.target, self.cfg)
        self.run_id = self.eng.create_run("t", "do the thing")

    def tearDown(self):
        self._tmp.cleanup()

    def _run_build(self, resume=False):
        fake = _FakeRunner(result=RunnerResult(
            ok=True, session_id="s1", exit_code=0, raw={"result": "done"}, stdout="", stderr=""))
        with patch("gantry.engine.get_runner", return_value=fake):
            if resume:
                self.eng.store.save_session(self.run_id, "build", session_id="prior-session")
            self.eng.run_agent_stage(self.run_id, "build", resume=resume)

    def test_empty_pre_hook_is_a_noop(self):
        self.cfg.build.pre_hook = ""
        self._run_build()
        log_path = self.eng.store.run_dir(self.run_id) / "logs" / "build-pre-hook.log"
        self.assertFalse(log_path.exists())

    def test_pre_hook_runs_and_logs_output(self):
        self.cfg.build.pre_hook = "echo hello-from-hook"
        self._run_build()
        log_path = self.eng.store.run_dir(self.run_id) / "logs" / "build-pre-hook.log"
        self.assertTrue(log_path.exists())
        self.assertIn("hello-from-hook", log_path.read_text())

    def test_pre_hook_does_not_run_on_resume(self):
        self.cfg.build.pre_hook = "echo should-not-run"
        self._run_build(resume=True)
        log_path = self.eng.store.run_dir(self.run_id) / "logs" / "build-pre-hook.log"
        self.assertFalse(log_path.exists())

    def test_failing_pre_hook_is_logged_not_fatal_by_default(self):
        self.cfg.build.pre_hook = "exit 1"
        self.cfg.build.pre_hook_required = False
        self._run_build()  # must not raise
        log_path = self.eng.store.run_dir(self.run_id) / "logs" / "build-pre-hook.log"
        self.assertIn("exit 1", log_path.read_text())
        self.assertIn("(exit 1)", log_path.read_text())

    def test_failing_pre_hook_raises_when_required(self):
        self.cfg.build.pre_hook = "exit 1"
        self.cfg.build.pre_hook_required = True
        with self.assertRaises(RuntimeError):
            self._run_build()

    def test_pre_hook_only_applies_to_build_stage(self):
        self.cfg.build.pre_hook = "echo should-not-run-for-plan"
        fake = _FakeRunner(result=RunnerResult(
            ok=True, session_id="s1", exit_code=0, raw={"result": "done"}, stdout="", stderr=""))
        with patch("gantry.engine.get_runner", return_value=fake):
            self.eng.run_agent_stage(self.run_id, "plan")
        log_path = self.eng.store.run_dir(self.run_id) / "logs" / "build-pre-hook.log"
        self.assertFalse(log_path.exists())


class TestSkillsDirective(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.target = Path(self._tmp.name)
        _init_scratch_repo(self.target)
        self.cfg = GantryConfig()
        self.cfg.skills.enabled = ["superpowers"]
        self.eng = Engine(self.target, self.cfg)

    def tearDown(self):
        self._tmp.cleanup()

    def test_empty_when_no_skills_enabled(self):
        self.cfg.skills.enabled = []
        self.assertEqual(self.eng._skills_directive("build"), "")
        self.assertEqual(self.eng._skills_directive("evidence"), "")

    def test_empty_for_non_execution_stages(self):
        self.assertEqual(self.eng._skills_directive("plan"), "")
        self.assertEqual(self.eng._skills_directive("spec"), "")

    def test_build_gets_execution_framing(self):
        directive = self.eng._skills_directive("build")
        self.assertIn("EXECUTION discipline", directive)
        self.assertNotIn("VERIFY", directive)

    def test_evidence_gets_verification_framing_by_default(self):
        directive = self.eng._skills_directive("evidence")
        self.assertIn("VERIFY", directive)
        self.assertNotIn("EXECUTION discipline", directive)

    def test_evidence_directive_override_used_when_set(self):
        self.cfg.skills.evidence_directive = "Custom evidence framing text."
        directive = self.eng._skills_directive("evidence")
        self.assertIn("Custom evidence framing text.", directive)
        self.assertNotIn("VERIFY", directive)

    def test_build_and_evidence_directives_differ(self):
        self.assertNotEqual(self.eng._skills_directive("build"), self.eng._skills_directive("evidence"))


class TestEvidenceOutputDirective(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.target = Path(self._tmp.name)
        _init_scratch_repo(self.target)
        self.cfg = GantryConfig()
        self.eng = Engine(self.target, self.cfg)

    def tearDown(self):
        self._tmp.cleanup()

    def test_empty_by_default_prose_format(self):
        self.assertEqual(self.eng._evidence_output_directive("evidence"), "")

    def test_empty_for_non_evidence_stages_even_when_structured(self):
        self.cfg.evidence.output_format = "structured"
        self.assertEqual(self.eng._evidence_output_directive("build"), "")

    def test_present_for_evidence_when_structured(self):
        self.cfg.evidence.output_format = "structured"
        directive = self.eng._evidence_output_directive("evidence")
        self.assertIn("pass_count", directive)
        self.assertIn("```json", directive)

    def test_render_prompt_appends_structured_directive_for_evidence(self):
        self.cfg.evidence.output_format = "structured"
        run_id = self.eng.create_run("t", "test")
        prompt = self.eng.render_prompt("evidence", run_id)
        self.assertIn("pass_count", prompt)


class TestPlanContextDirective(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.target = Path(self._tmp.name)
        _init_scratch_repo(self.target)
        self.cfg = GantryConfig()
        self.eng = Engine(self.target, self.cfg)

    def tearDown(self):
        self._tmp.cleanup()

    def test_empty_by_default(self):
        self.assertEqual(self.eng._plan_context_directive("plan"), "")

    def test_empty_for_non_plan_stages_even_when_configured(self):
        self.cfg.plan.include_git_log = True
        self.assertEqual(self.eng._plan_context_directive("build"), "")

    def test_includes_git_log_when_enabled(self):
        self.cfg.plan.include_git_log = True
        directive = self.eng._plan_context_directive("plan")
        self.assertIn("Recent history", directive)
        self.assertIn("init", directive)  # the scratch repo's init commit message

    def test_includes_context_file_contents(self):
        (self.target / "NOTES.md").write_text("Important context for planning.")
        self.cfg.plan.context_files = ["NOTES.md"]
        directive = self.eng._plan_context_directive("plan")
        self.assertIn("Important context for planning.", directive)
        self.assertIn("NOTES.md", directive)

    def test_missing_context_file_does_not_raise(self):
        self.cfg.plan.context_files = ["does-not-exist.md"]
        directive = self.eng._plan_context_directive("plan")
        self.assertEqual(directive, "")

    def test_render_prompt_prepends_context_for_plan(self):
        self.cfg.plan.include_git_log = True
        run_id = self.eng.create_run("t", "test")
        prompt = self.eng.render_prompt("plan", run_id)
        self.assertIn("Recent history", prompt)
        # Context comes before the stage's own base instructions.
        self.assertLess(prompt.index("Recent history"), prompt.index("Stage: plan"))


class TestPlanDepthTemplateSelection(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.target = Path(self._tmp.name)
        _init_scratch_repo(self.target)
        self.cfg = GantryConfig()
        self.eng = Engine(self.target, self.cfg)
        self.prompts_dir = self.target / ".gantry" / "prompts"
        self.prompts_dir.mkdir(parents=True)
        (self.prompts_dir / "plan.md").write_text("# Detailed plan template\n")

    def tearDown(self):
        self._tmp.cleanup()

    def test_default_depth_uses_plan_md(self):
        path = self.eng._prompt_template_path("plan")
        self.assertEqual(path.name, "plan.md")

    def test_brief_depth_falls_back_to_plan_md_when_no_brief_variant_exists(self):
        self.cfg.plan.depth = "brief"
        path = self.eng._prompt_template_path("plan")
        self.assertEqual(path.name, "plan.md")

    def test_brief_depth_uses_plan_brief_md_when_it_exists(self):
        (self.prompts_dir / "plan-brief.md").write_text("# Brief plan template\n")
        self.cfg.plan.depth = "brief"
        path = self.eng._prompt_template_path("plan")
        self.assertEqual(path.name, "plan-brief.md")

    def test_depth_only_affects_plan_stage(self):
        (self.prompts_dir / "build.md").write_text("# Build template\n")
        self.cfg.plan.depth = "brief"
        path = self.eng._prompt_template_path("build")
        self.assertEqual(path.name, "build.md")


if __name__ == "__main__":
    unittest.main()
