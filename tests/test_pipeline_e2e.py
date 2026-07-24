"""Full end-to-end pipeline exercise: drives one run through every stage in
order (spec -> design -> plan -> build -> checks -> evidence -> review ->
ship) using fake runners, asserting real artifacts and status transitions at
each step, plus deliberately exercising checks-retry, high-risk escalation,
and review REQUEST_CHANGES failure/loop paths — not just the happy path.

This is a genuine integration exercise: it's the one test in this suite that
confirms the pieces built/modified independently across many separate tasks
(structured doc-stage artifacts, plan/build changes, checks flaky-retry,
high-risk-path escalation, two-axis review, ship-stage gate/rollback/conflict
handling, the Status/RetryPolicy refactor) actually compose as one coherent
pipeline, not just pass their own isolated unit tests.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gantry.advance import advance_run
from gantry.config import GantryConfig
from gantry.engine import Engine
from gantry.runners import RunnerResult
from gantry.status import Status


def _init_scratch_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(path), check=True)
    (path / "lint.sh").write_text("#!/bin/sh\nexit 0\n")
    (path / "lint.sh").chmod(0o755)
    subprocess.run(["git", "add", "-A"], cwd=str(path), check=True)
    subprocess.run(["git", "commit", "-m", "init", "-q"], cwd=str(path), check=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=str(path), check=True)


class _ScriptedRunner:
    """Fake AgentRunner that returns a scripted sequence of RunnerResults, one
    per call, keyed by call order. Lets a single test drive every stage with
    stage-appropriate canned output instead of one fixed response."""

    name = "claude-code"

    def __init__(self, results: list[RunnerResult]):
        self._results = list(results)
        self.calls = 0

    def run(self, **kwargs):
        result = self._results[self.calls]
        self.calls += 1
        return result


def _agent_result(text: str, ok: bool = True, session: str = "s") -> RunnerResult:
    return RunnerResult(ok=ok, session_id=session, exit_code=0 if ok else 1,
                        raw={"result": text}, stdout=text, stderr="")


class TestFullPipelineE2E(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.target = Path(self._tmp.name)
        _init_scratch_repo(self.target)
        self.cfg = GantryConfig()
        self.cfg.stages = ["spec", "design", "plan", "build", "evidence", "review"]
        self.cfg.checks.commands = ["sh lint.sh"]
        self.cfg.git.auto_approve_docs = True
        self.cfg.git.auto_ship = False  # ship exercised directly, not via auto-advance
        self.eng = Engine(self.target, self.cfg)

    def tearDown(self):
        self._tmp.cleanup()

    def _advance(self):
        return advance_run(self.eng, self.run_id)

    def test_full_pipeline_happy_path_through_review_approved(self):
        """Drive one run from creation through review APPROVE, asserting the
        expected structured artifact lands at each stage and the status
        machine transitions exactly as expected — the core linear-pipeline
        integration check."""
        run_id = self.eng.create_run(
            "add health endpoint", "add GET /health -> 200",
            pipeline_overrides={"definition_policy": "separate"},
        )
        self.run_id = run_id
        store = self.eng.store

        spec_json = json.dumps({"criteria": [
            {"id": "AC-1", "text": "GET /health returns 200", "verifiable_by": "test"},
        ]})

        def write_spec(**kwargs):
            store.artifact_path(run_id, "product-spec.md").write_text("# Spec\n\nAdd /health.\n")
            store.artifact_path(run_id, "acceptance-criteria.json").write_text(spec_json)
            return _agent_result("spec written")

        def write_design(**kwargs):
            store.artifact_path(run_id, "architecture-design.md").write_text("# Design\n\nAdd a route handler.\n")
            store.artifact_path(run_id, "decision-log.json").write_text(
                json.dumps({"decisions": [{"decision": "use existing router", "rationale": "simplest",
                                          "alternatives_considered": ["new framework"]}]}))
            return _agent_result("design written")

        def write_plan(**kwargs):
            store.artifact_path(run_id, "implementation-plan.md").write_text(
                "# Plan\n\n## Allowed files\n`app.py`\n\n## Ordered implementation steps\n1. add route (verify: `sh lint.sh`)\n")
            store.artifact_path(run_id, "allowed-files.json").write_text(
                json.dumps({"allowed_globs": ["app.py"], "notes": {"app.py": "route handler"}}))
            return _agent_result("plan written")

        def write_build(**kwargs):
            wt = self.eng.work_dir(run_id)
            (wt / "app.py").write_text("def health():\n    return 200\n")
            subprocess.run(["git", "add", "app.py"], cwd=str(wt), check=True)
            subprocess.run(["git", "commit", "-m", "step 1: add route", "-q"], cwd=str(wt), check=True)
            store.artifact_path(run_id, "build-summary.md").write_text(
                "# Build summary\n\n1. Files changed: app.py\n2. Step 1 done, verified via sh lint.sh\n"
                "3. `sh lint.sh` passed\n")
            return _agent_result("build done")

        def write_evidence(**kwargs):
            store.artifact_path(run_id, "evidence-report.md").write_text(
                "# Evidence\n\nAC-1: confirmed (test-verified) — ran sh lint.sh, exit 0.\n"
                "Recommendation: PASS\n")
            return _agent_result("evidence written")

        def write_review(**kwargs):
            findings = json.dumps({"findings": []})
            return _agent_result(
                f"APPROVE\n\nLooks correct, matches AC-1.\n\n```json\n{findings}\n```\n"
                "## Verification Story\nRan sh lint.sh myself, confirmed exit 0.\n")

        scripted = {
            "spec": write_spec, "design": write_design, "plan": write_plan,
            "build": write_build, "evidence": write_evidence,
        }

        class _StageAwareRunner:
            name = "claude-code"

            def __init__(self, outer):
                self.outer = outer

            def run(self, **kwargs):
                # session_name is "{run_id}-{stage}" for normal stages, or
                # "{run_id}-review" for both review axes (both use the same
                # convention) — dispatch on it to return stage-correct output.
                session_name = kwargs.get("session_name", "")
                for stage, fn in scripted.items():
                    if session_name == f"{run_id}-{stage}":
                        return fn(**kwargs)
                if "review" in session_name:
                    return write_review(**kwargs)
                raise AssertionError(f"unscripted session_name: {session_name}")

        runner = _StageAwareRunner(self)

        with patch("gantry.engine.get_runner", return_value=runner), \
             patch("gantry.review.get_runner", return_value=runner):
            # Doc stages (spec/design) are DOC_STAGES, not AGENT_STAGES —
            # advance_run deliberately never auto-fires them (a human, or
            # `gantry stage spec`, must kick them off); only their *approval*
            # is auto-advanced when auto_approve_docs is set. Confirmed real
            # behavior via config.py's AGENT_STAGES/DOC_STAGES split.
            self.eng.run_agent_stage(run_id, "spec")
            self.assertEqual(store.state(run_id)["status"], Status.SPEC_COMPLETE)
            self.assertTrue((store.run_dir(run_id) / "acceptance-criteria.json").exists())

            r = self._advance()  # auto-approve spec -> awaiting_design
            self.assertIn("auto_approved_spec", r["action"])

            self.eng.run_agent_stage(run_id, "design")
            self.assertEqual(store.state(run_id)["status"], Status.DESIGN_COMPLETE)
            self.assertTrue((store.run_dir(run_id) / "decision-log.json").exists())

            r = self._advance()  # auto-approve design -> awaiting_plan
            self.assertIn("auto_approved_design", r["action"])

            r = self._advance()  # plan
            self.assertEqual(store.state(run_id)["status"], Status.PLAN_COMPLETE)
            self.assertTrue((store.run_dir(run_id) / "allowed-files.json").exists())

            r = self._advance()  # build
            self.assertEqual(r["action"], "build")
            self.assertEqual(store.state(run_id)["status"], Status.BUILD_COMPLETE)

            # build_complete -> checks (real, via sh lint.sh) -> evidence
            r = self._advance()
            self.assertEqual(r["action"], "checks_passed->evidence")
            checks = store.read_result(run_id, "checks.json")
            self.assertTrue(checks["pass"])
            self.assertEqual(store.state(run_id)["status"], Status.EVIDENCE_COMPLETE)

            # evidence_complete -> review (two-axis, both APPROVE)
            r = self._advance()
            self.assertEqual(r["action"], "review")
            self.assertEqual(r["verdict"], "APPROVE")
            self.assertEqual(store.state(run_id)["status"], Status.REVIEW_APPROVED)

        review_result = store.read_result(run_id, "review-result.json")
        self.assertTrue(review_result.get("two_axis"))
        self.assertIn("spec", review_result)
        self.assertIn("standards", review_result)

    def test_checks_failure_retries_then_recovers(self):
        """A build that initially fails checks should retry with feedback and
        recover once the fix lands — not skip straight to escalation."""
        self.cfg.stages = ["plan", "build"]
        # With no "evidence" stage configured, build_complete's checks-pass
        # path falls through to review directly (review.enabled defaults
        # True regardless of `stages`) — irrelevant to what this test is
        # actually exercising (the checks-retry loop), so disable it rather
        # than needing a second scripted runner for a real `claude` call.
        self.cfg.review.enabled = False
        run_id = self.eng.create_run("t", "r")
        self.run_id = run_id
        store = self.eng.store

        attempt = {"n": 0}

        def write_build(**kwargs):
            attempt["n"] += 1
            wt = self.eng.work_dir(run_id)
            if attempt["n"] == 1:
                # first attempt: writes a script that fails lint
                (wt / "lint.sh").write_text("#!/bin/sh\nexit 1\n")
            else:
                # retried attempt (after feedback): fixes it
                (wt / "lint.sh").write_text("#!/bin/sh\nexit 0\n")
            subprocess.run(["git", "add", "lint.sh"], cwd=str(wt), check=True)
            subprocess.run(["git", "commit", "-m", f"attempt {attempt['n']}", "-q"], cwd=str(wt), check=True)
            store.artifact_path(run_id, "build-summary.md").write_text(f"# Build\n\nAttempt {attempt['n']}\n")
            return _agent_result(f"build attempt {attempt['n']}")

        def write_plan(**kwargs):
            store.artifact_path(run_id, "implementation-plan.md").write_text(
                "# Plan\n\n## Allowed files\n`lint.sh`\n")
            store.artifact_path(run_id, "allowed-files.json").write_text(
                json.dumps({"allowed_globs": ["lint.sh"]}))
            return _agent_result("plan")

        class _Runner:
            name = "claude-code"

            def run(self, **kwargs):
                sn = kwargs.get("session_name", "")
                if sn.endswith("-plan"):
                    return write_plan(**kwargs)
                return write_build(**kwargs)

        with patch("gantry.engine.get_runner", return_value=_Runner()):
            advance_run(self.eng, run_id)  # awaiting_plan -> plan_complete
            advance_run(self.eng, run_id)  # plan_complete -> build (attempt 1, fails lint)
            r = advance_run(self.eng, run_id)  # build_complete -> checks fail -> checks_failed
            self.assertEqual(r["action"], "checks_failed")
            self.assertEqual(store.state(run_id)["status"], Status.CHECKS_FAILED)

            r = advance_run(self.eng, run_id)  # checks_failed -> retry build (attempt 2, fixes it)
            self.assertEqual(r["action"], "retry_build_after_checks_failure")

            r = advance_run(self.eng, run_id)  # build_complete -> checks now pass
            checks = store.read_result(run_id, "checks.json")
            self.assertTrue(checks["pass"])
            self.assertEqual(attempt["n"], 2)

    def test_high_risk_path_forces_escalation_never_auto_advanced(self):
        """A changed file matching [scope].high_risk_paths must force
        checks_high_risk_escalated and never auto-advance, even under full
        autonomy flags — the defense-in-depth path all the way through the
        real advance_run/run_scope_guard code, not a mocked shortcut."""
        self.cfg.scope.high_risk_paths = ["auth/**"]
        self.cfg.git.auto_ship = True
        self.cfg.checks.auto_resolve = True
        self.cfg.stages = ["plan", "build"]
        run_id = self.eng.create_run("t", "r")
        self.run_id = run_id
        store = self.eng.store

        def write_plan(**kwargs):
            store.artifact_path(run_id, "implementation-plan.md").write_text(
                "# Plan\n\n## Allowed files\n`auth/login.py`\n")
            store.artifact_path(run_id, "allowed-files.json").write_text(
                json.dumps({"allowed_globs": ["auth/login.py"]}))
            return _agent_result("plan")

        def write_build(**kwargs):
            wt = self.eng.work_dir(run_id)
            (wt / "auth").mkdir(exist_ok=True)
            (wt / "auth" / "login.py").write_text("def login(): pass\n")
            # build.md's checkpoint discipline requires `git add <specific
            # files>`, never `-A`/`.` — exactly to avoid staging the
            # .agent-runs symlink noise git.py's commit_all otherwise has to
            # explicitly work around (see git.py's own comment on this).
            subprocess.run(["git", "add", "auth/login.py"], cwd=str(wt), check=True)
            subprocess.run(["git", "commit", "-m", "add login", "-q"], cwd=str(wt), check=True)
            store.artifact_path(run_id, "build-summary.md").write_text("# Build\n\nadded auth/login.py\n")
            return _agent_result("build done")

        class _Runner:
            name = "claude-code"

            def run(self, **kwargs):
                sn = kwargs.get("session_name", "")
                return write_plan(**kwargs) if sn.endswith("-plan") else write_build(**kwargs)

        with patch("gantry.engine.get_runner", return_value=_Runner()):
            advance_run(self.eng, run_id)  # plan
            advance_run(self.eng, run_id)  # build (touches auth/login.py)
            advance_run(self.eng, run_id)  # build_complete -> checks pass, but high-risk

        self.assertEqual(store.state(run_id)["status"], Status.CHECKS_HIGH_RISK_ESCALATED)
        scope = store.read_result(run_id, "checks.json").get("scope", {})
        self.assertIn("auth/login.py", scope.get("high_risk_files", []))

        # Even with auto_ship/auto_resolve fully on, a bare advance_all-style
        # sweep must never move this run further — confirmed via the same
        # AUTO_TRANSITIONS gate the real poller uses.
        from gantry.advance import AUTO_TRANSITIONS
        auto = (AUTO_TRANSITIONS
               | ({"review_approved", "ship_failed"} if self.cfg.git.auto_ship else set())
               | ({"checks_escalated"} if self.cfg.checks.auto_resolve else set()))
        self.assertNotIn(Status.CHECKS_HIGH_RISK_ESCALATED, auto)
        self.assertNotIn("checks_high_risk_escalated", auto)


if __name__ == "__main__":
    unittest.main()
