import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gantry.config import GantryConfig
from gantry.runners import RunnerResult
from gantry.review import (
    FAIL_CLOSED_ACTION, _build_prompt, _checklist_section, _combine_axis_verdicts,
    _findings_verdict, _high_risk_files_for, _parse_findings, _parse_verdict,
    _rebuild_diff_context, _structured_evidence_summary, run_review,
)
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

    def test_line_start_mode_ignores_keyword_in_prose(self):
        self.cfg.review.keyword_mode = "line_start"
        # "escalate" appears mid-sentence, not as a verdict declaration —
        # line_start mode must not false-positive on this.
        text = "I considered whether to escalate this but decided it's fine.\nAPPROVE"
        self.assertEqual(_parse_verdict(text, self.cfg), "APPROVE")

    def test_line_start_mode_matches_real_declaration(self):
        self.cfg.review.keyword_mode = "line_start"
        text = "Some reasoning here.\n\nREQUEST_CHANGES\n\nMore detail."
        self.assertEqual(_parse_verdict(text, self.cfg), "REQUEST_CHANGES")

    def test_line_start_mode_matches_with_markdown_emphasis_prefix(self):
        self.cfg.review.keyword_mode = "line_start"
        text = "Reasoning.\n\n**APPROVE**\n"
        self.assertEqual(_parse_verdict(text, self.cfg), "APPROVE")

    def test_line_start_mode_defaults_to_escalate_with_no_real_declaration(self):
        self.cfg.review.keyword_mode = "line_start"
        text = "This diff would probably approve if I were less careful."
        self.assertEqual(_parse_verdict(text, self.cfg), "ESCALATE")

    def test_anywhere_mode_is_default(self):
        self.assertEqual(self.cfg.review.keyword_mode, "anywhere")


class TestRunReview(unittest.TestCase):
    """Exercises the LEGACY single-axis review path (cfg.review.two_axis =
    False) — this is the critical regression guard for the opt-out path: it
    must remain byte-identical to review.py's behavior before two-axis review
    existed. See TestRunReviewTwoAxis below for the new default (two_axis=True)
    behavior."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.target = Path(self._tmp.name)
        _init_scratch_repo(self.target)
        self.store = RunStore(self.target)
        self.cfg = GantryConfig()
        self.cfg.review.two_axis = False
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
        # Transport failures retry then escalate — still a terminal escalate,
        # not a silent approve.
        self.cfg.agent.stage_retry_attempts = 0
        with patch("gantry.review.get_runner", return_value=self._fake_runner("APPROVE", ok=False)):
            out = run_review(self.store, self.run_id, self.cfg, self.target)
        self.assertEqual(out["verdict"], "ESCALATE")
        self.assertTrue(out.get("runner_failed"))
        self.assertEqual(self.store.state(self.run_id)["status"], "review_escalated")

    def test_max_turns_from_config_passed_to_runner(self):
        captured = {}

        class _CapturingRunner:
            name = "claude-code"

            def run(self, **kwargs):
                captured.update(kwargs)
                return RunnerResult(ok=True, session_id="s1", exit_code=0,
                                    raw={"result": "APPROVE"}, stdout="APPROVE", stderr="")

        self.cfg.review.max_turns = 25
        with patch("gantry.review.get_runner", return_value=_CapturingRunner()):
            run_review(self.store, self.run_id, self.cfg, self.target)
        self.assertEqual(captured["max_turns"], 25)

    def test_checklist_appears_in_logged_prompt(self):
        self.cfg.review.checklist = ["confirm no secrets committed"]
        with patch("gantry.review.get_runner", return_value=self._fake_runner("APPROVE")):
            run_review(self.store, self.run_id, self.cfg, self.target)
        prompt_log = self.store.read_artifact(self.run_id, "logs/review-prompt.md")
        self.assertIn("confirm no secrets committed", prompt_log)


class TestStructuredEvidenceSummary(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.target = Path(self._tmp.name)
        self.store = RunStore(self.target)
        self.run_id = self.store.new_run_id("t")
        self.store.create(self.run_id, "t")

    def tearDown(self):
        self._tmp.cleanup()

    def test_none_when_no_evidence_report(self):
        self.assertIsNone(_structured_evidence_summary(self.store, self.run_id))

    def test_none_when_prose_only_no_json_block(self):
        self.store.artifact_path(self.run_id, "evidence-report.md").write_text(
            "# Evidence\n\nAll good, no structured block here.\n")
        self.assertIsNone(_structured_evidence_summary(self.store, self.run_id))

    def test_parses_valid_json_block(self):
        self.store.artifact_path(self.run_id, "evidence-report.md").write_text(
            "# Evidence\n\nProse here.\n\n```json\n"
            '{"pass_count": 5, "fail_count": 0, "coverage_pct": 92.5, "scope_summary": "ok"}\n'
            "```\n")
        summary = _structured_evidence_summary(self.store, self.run_id)
        self.assertEqual(summary["pass_count"], 5)
        self.assertEqual(summary["coverage_pct"], 92.5)

    def test_uses_last_block_when_multiple_passes(self):
        self.store.artifact_path(self.run_id, "evidence-report.md").write_text(
            "## Pass 1\n\n```json\n"
            '{"pass_count": 1, "fail_count": 3, "coverage_pct": 10, "scope_summary": "old"}\n'
            "```\n\n## Pass 2\n\n```json\n"
            '{"pass_count": 5, "fail_count": 0, "coverage_pct": 92.5, "scope_summary": "new"}\n'
            "```\n")
        summary = _structured_evidence_summary(self.store, self.run_id)
        self.assertEqual(summary["scope_summary"], "new")

    def test_malformed_json_returns_none_not_raises(self):
        self.store.artifact_path(self.run_id, "evidence-report.md").write_text(
            "# Evidence\n\n```json\n{not valid json\n```\n")
        self.assertIsNone(_structured_evidence_summary(self.store, self.run_id))

    def test_missing_pass_count_key_returns_none(self):
        self.store.artifact_path(self.run_id, "evidence-report.md").write_text(
            "# Evidence\n\n```json\n{\"unrelated\": true}\n```\n")
        self.assertIsNone(_structured_evidence_summary(self.store, self.run_id))


class TestChecklistSection(unittest.TestCase):
    def test_empty_when_no_checklist_configured(self):
        cfg = GantryConfig()
        self.assertEqual(_checklist_section(cfg), "")

    def test_includes_each_item(self):
        cfg = GantryConfig()
        cfg.review.checklist = ["confirm no secrets committed", "confirm migration is reversible"]
        section = _checklist_section(cfg)
        self.assertIn("confirm no secrets committed", section)
        self.assertIn("confirm migration is reversible", section)


class TestRebuildDiffContext(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.target = Path(self._tmp.name)
        self.store = RunStore(self.target)
        self.run_id = self.store.new_run_id("t")
        self.store.create(self.run_id, "t")

    def tearDown(self):
        self._tmp.cleanup()

    def test_empty_when_no_prior_review(self):
        self.assertEqual(_rebuild_diff_context(self.store, self.run_id), "")

    def test_empty_when_prior_verdict_was_approve(self):
        self.store.write_result(self.run_id, "review-result.json", {"verdict": "APPROVE"})
        self.assertEqual(_rebuild_diff_context(self.store, self.run_id), "")

    def test_includes_prior_comments_when_prior_verdict_was_request_changes(self):
        self.store.write_result(self.run_id, "review-result.json", {"verdict": "REQUEST_CHANGES"})
        self.store.artifact_path(self.run_id, "review-comments.md").write_text(
            "# Review: changes requested\n\nMissing error handling on X.\n")
        context = _rebuild_diff_context(self.store, self.run_id)
        self.assertIn("Missing error handling on X.", context)
        self.assertIn("RE-review", context)


class TestBuildPromptIncludesStructuredSummary(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.target = Path(self._tmp.name)
        self.store = RunStore(self.target)
        self.run_id = self.store.new_run_id("t")
        self.store.create(self.run_id, "t")

    def tearDown(self):
        self._tmp.cleanup()

    def test_prompt_includes_structured_summary_when_present(self):
        self.store.artifact_path(self.run_id, "evidence-report.md").write_text(
            "```json\n"
            '{"pass_count": 3, "fail_count": 0, "coverage_pct": 80, "scope_summary": "x"}\n'
            "```\n")
        prompt = _build_prompt(self.store, self.run_id, self.target, "main", "template text")
        self.assertIn("pass_count", prompt)

    def test_prompt_omits_summary_section_when_no_json_block(self):
        prompt = _build_prompt(self.store, self.run_id, self.target, "main", "template text")
        self.assertNotIn("structured summary", prompt)


class TestParseFindings(unittest.TestCase):
    def test_none_when_no_json_block(self):
        self.assertIsNone(_parse_findings("Just prose, APPROVE."))

    def test_empty_findings_list_is_valid(self):
        text = 'APPROVE\n\n```json\n{"findings": []}\n```\n'
        self.assertEqual(_parse_findings(text), [])

    def test_parses_valid_finding(self):
        text = (
            'REQUEST_CHANGES\n\n```json\n'
            '{"findings": [{"severity": "Critical", "action": "blocking", '
            '"location": "a.py:10", "description": "bug", "recommendation": "fix it"}]}\n'
            '```\n'
        )
        findings = _parse_findings(text)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["action"], "blocking")
        self.assertEqual(findings[0]["severity"], "Critical")

    def test_missing_action_fails_closed_to_ask_user(self):
        text = (
            '```json\n{"findings": [{"severity": "Important", '
            '"location": "a.py", "description": "x", "recommendation": "y"}]}\n```\n'
        )
        findings = _parse_findings(text)
        self.assertEqual(findings[0]["action"], FAIL_CLOSED_ACTION)

    def test_garbage_action_fails_closed_to_ask_user(self):
        text = (
            '```json\n{"findings": [{"severity": "Important", "action": "delete-everything", '
            '"location": "a.py", "description": "x", "recommendation": "y"}]}\n```\n'
        )
        findings = _parse_findings(text)
        self.assertEqual(findings[0]["action"], FAIL_CLOSED_ACTION)

    def test_empty_string_action_fails_closed_to_ask_user(self):
        text = (
            '```json\n{"findings": [{"severity": "Important", "action": "", '
            '"location": "a.py", "description": "x", "recommendation": "y"}]}\n```\n'
        )
        findings = _parse_findings(text)
        self.assertEqual(findings[0]["action"], FAIL_CLOSED_ACTION)

    def test_malformed_json_returns_none(self):
        self.assertIsNone(_parse_findings("```json\n{not valid\n```\n"))

    def test_missing_findings_key_returns_none(self):
        self.assertIsNone(_parse_findings('```json\n{"other": true}\n```\n'))

    def test_uses_last_json_block(self):
        text = (
            '```json\n{"findings": [{"action": "no-op", "severity": "s", "location": "l", '
            '"description": "old", "recommendation": "r"}]}\n```\n\n'
            'More reasoning.\n\n'
            '```json\n{"findings": [{"action": "blocking", "severity": "s", "location": "l", '
            '"description": "new", "recommendation": "r"}]}\n```\n'
        )
        findings = _parse_findings(text)
        self.assertEqual(findings[0]["description"], "new")


class TestFindingsVerdict(unittest.TestCase):
    def test_no_findings_approves(self):
        self.assertEqual(_findings_verdict([]), "APPROVE")

    def test_only_no_op_approves(self):
        findings = [{"action": "no-op"}]
        self.assertEqual(_findings_verdict(findings), "APPROVE")

    def test_only_ask_user_still_approves(self):
        findings = [{"action": "ask-user"}]
        self.assertEqual(_findings_verdict(findings), "APPROVE")

    def test_any_blocking_requests_changes(self):
        findings = [{"action": "ask-user"}, {"action": "blocking"}]
        self.assertEqual(_findings_verdict(findings), "REQUEST_CHANGES")


class TestCombineAxisVerdicts(unittest.TestCase):
    def test_both_approve_combines_to_approve(self):
        self.assertEqual(_combine_axis_verdicts("APPROVE", "APPROVE"), "APPROVE")

    def test_either_request_changes_combines_to_request_changes(self):
        self.assertEqual(_combine_axis_verdicts("REQUEST_CHANGES", "APPROVE"), "REQUEST_CHANGES")
        self.assertEqual(_combine_axis_verdicts("APPROVE", "REQUEST_CHANGES"), "REQUEST_CHANGES")

    def test_either_escalate_wins_over_everything(self):
        self.assertEqual(_combine_axis_verdicts("ESCALATE", "APPROVE"), "ESCALATE")
        self.assertEqual(_combine_axis_verdicts("REQUEST_CHANGES", "ESCALATE"), "ESCALATE")
        self.assertEqual(_combine_axis_verdicts("ESCALATE", "ESCALATE"), "ESCALATE")


class TestHighRiskFilesFor(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.target = Path(self._tmp.name)
        self.store = RunStore(self.target)
        self.run_id = self.store.new_run_id("t")
        self.store.create(self.run_id, "t")

    def tearDown(self):
        self._tmp.cleanup()

    def test_empty_when_no_checks_json(self):
        self.assertEqual(_high_risk_files_for(self.store, self.run_id), [])

    def test_empty_when_no_high_risk_files_key(self):
        self.store.write_result(self.run_id, "checks.json", {"scope": {}})
        self.assertEqual(_high_risk_files_for(self.store, self.run_id), [])

    def test_returns_declared_high_risk_files(self):
        self.store.write_result(self.run_id, "checks.json",
                                {"scope": {"high_risk_files": ["auth/login.py"]}})
        self.assertEqual(_high_risk_files_for(self.store, self.run_id), ["auth/login.py"])


def _findings_json_block(action: str, description: str = "finding") -> str:
    return (
        f'```json\n{{"findings": [{{"severity": "Important", "action": "{action}", '
        f'"location": "a.py:1", "description": "{description}", '
        f'"recommendation": "fix"}}]}}\n```\n'
    )


class TestRunReviewTwoAxis(unittest.TestCase):
    """Exercises the DEFAULT (two_axis=True) parallel-axis review path."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.target = Path(self._tmp.name)
        _init_scratch_repo(self.target)
        self.store = RunStore(self.target)
        self.cfg = GantryConfig()
        self.assertTrue(self.cfg.review.two_axis, "two_axis must default to True")
        self.run_id = self.store.new_run_id("t")
        self.store.create(self.run_id, "t")

    def tearDown(self):
        self._tmp.cleanup()

    def _runner_for(self, axis_verdicts: dict[str, str], axis_findings: dict[str, str] | None = None):
        """Returns a fake runner whose response depends on which axis is
        calling (identified via session_name, e.g. "<run_id>-review-spec")."""
        axis_findings = axis_findings or {}

        class _AxisAwareRunner:
            name = "claude-code"

            def run(self, **kwargs):
                session_name = kwargs.get("session_name", "")
                axis = "spec" if session_name.endswith("-review-spec") else "standards"
                verdict = axis_verdicts[axis]
                findings_block = axis_findings.get(axis, "")
                text = f"{verdict}\n\nVerification Story: I ran the tests.\n\n{findings_block}"
                return RunnerResult(ok=True, session_id=f"sess-{axis}", exit_code=0,
                                    raw={"result": text}, stdout=text, stderr="")
        return _AxisAwareRunner()

    def test_both_axes_approve_combines_to_review_approved(self):
        runner = self._runner_for({"spec": "APPROVE", "standards": "APPROVE"})
        with patch("gantry.review.get_runner", return_value=runner):
            out = run_review(self.store, self.run_id, self.cfg, self.target)
        self.assertEqual(out["verdict"], "APPROVE")
        self.assertEqual(out["combined_verdict"], "APPROVE")
        self.assertTrue(out["two_axis"])
        self.assertEqual(self.store.state(self.run_id)["status"], "review_approved")

    def test_one_axis_request_changes_combines_and_surfaces_both_findings(self):
        findings = {
            "spec": _findings_json_block("blocking", "missing AC-2 coverage"),
            "standards": _findings_json_block("no-op", "minor style nit"),
        }
        runner = self._runner_for({"spec": "REQUEST_CHANGES", "standards": "APPROVE"}, findings)
        with patch("gantry.review.get_runner", return_value=runner):
            out = run_review(self.store, self.run_id, self.cfg, self.target)
        self.assertEqual(out["combined_verdict"], "REQUEST_CHANGES")
        self.assertEqual(self.store.state(self.run_id)["status"], "review_changes_requested")
        comments = self.store.read_artifact(self.run_id, "review-comments.md")
        self.assertIn("Spec axis", comments)
        self.assertIn("Standards axis", comments)
        self.assertIn("missing AC-2 coverage", comments)

    def test_one_axis_escalate_wins(self):
        runner = self._runner_for({"spec": "ESCALATE", "standards": "APPROVE"})
        with patch("gantry.review.get_runner", return_value=runner):
            out = run_review(self.store, self.run_id, self.cfg, self.target)
        self.assertEqual(out["combined_verdict"], "ESCALATE")
        self.assertEqual(self.store.state(self.run_id)["status"], "review_escalated")

    def test_finding_with_missing_action_fails_closed_not_dropped(self):
        bad_block = (
            '```json\n{"findings": [{"severity": "Important", '
            '"location": "a.py", "description": "weird case", "recommendation": "check"}]}\n```\n'
        )
        findings = {"spec": bad_block, "standards": _findings_json_block("no-op")}
        runner = self._runner_for({"spec": "APPROVE", "standards": "APPROVE"}, findings)
        with patch("gantry.review.get_runner", return_value=runner):
            out = run_review(self.store, self.run_id, self.cfg, self.target)
        spec_findings = out["spec"]["findings"]
        self.assertEqual(len(spec_findings), 1)
        self.assertEqual(spec_findings[0]["action"], FAIL_CLOSED_ACTION)
        self.assertIn("weird case", spec_findings[0]["description"])

    def test_each_axis_resumes_its_own_session_on_re_review(self):
        runner = self._runner_for({"spec": "APPROVE", "standards": "APPROVE"})
        with patch("gantry.review.get_runner", return_value=runner):
            run_review(self.store, self.run_id, self.cfg, self.target)
        spec_session = self.store.get_session_id(self.run_id, "review_spec")
        standards_session = self.store.get_session_id(self.run_id, "review_standards")
        self.assertEqual(spec_session, "sess-spec")
        self.assertEqual(standards_session, "sess-standards")
        self.assertNotEqual(spec_session, standards_session)

        # Simulated re-review (e.g. after REQUEST_CHANGES -> rebuild -> evidence
        # -> review again): each axis must resume ITS OWN prior session id, not
        # the other axis's.
        captured_session_ids = {}

        class _CapturingRunner:
            name = "claude-code"

            def run(self, **kwargs):
                session_name = kwargs.get("session_name", "")
                axis = "spec" if session_name.endswith("-review-spec") else "standards"
                captured_session_ids[axis] = kwargs.get("session_id")
                text = "APPROVE\n\nVerification Story: re-checked.\n\n" + _findings_json_block("no-op")
                return RunnerResult(ok=True, session_id=f"sess-{axis}-2", exit_code=0,
                                    raw={"result": text}, stdout=text, stderr="")

        with patch("gantry.review.get_runner", return_value=_CapturingRunner()):
            run_review(self.store, self.run_id, self.cfg, self.target)
        self.assertEqual(captured_session_ids["spec"], "sess-spec")
        self.assertEqual(captured_session_ids["standards"], "sess-standards")

    def test_high_risk_files_trigger_extra_scrutiny_instruction_in_both_prompts(self):
        self.store.write_result(self.run_id, "checks.json",
                                {"scope": {"high_risk_files": ["auth/login.py"]}})
        runner = self._runner_for({"spec": "APPROVE", "standards": "APPROVE"})
        with patch("gantry.review.get_runner", return_value=runner):
            run_review(self.store, self.run_id, self.cfg, self.target)
        spec_prompt = self.store.read_artifact(self.run_id, "logs/review-spec-prompt.md")
        standards_prompt = self.store.read_artifact(self.run_id, "logs/review-standards-prompt.md")
        self.assertIn("auth/login.py", spec_prompt)
        self.assertIn("extra scrutiny", spec_prompt.lower())
        self.assertIn("auth/login.py", standards_prompt)
        self.assertIn("extra scrutiny", standards_prompt.lower())

    def test_spec_checklist_only_appears_in_spec_prompt(self):
        self.cfg.review.checklist = ["confirm AC coverage"]
        self.cfg.review.standards_checklist = ["confirm docstrings present"]
        runner = self._runner_for({"spec": "APPROVE", "standards": "APPROVE"})
        with patch("gantry.review.get_runner", return_value=runner):
            run_review(self.store, self.run_id, self.cfg, self.target)
        spec_prompt = self.store.read_artifact(self.run_id, "logs/review-spec-prompt.md")
        standards_prompt = self.store.read_artifact(self.run_id, "logs/review-standards-prompt.md")
        self.assertIn("confirm AC coverage", spec_prompt)
        self.assertNotIn("confirm AC coverage", standards_prompt)
        self.assertIn("confirm docstrings present", standards_prompt)
        self.assertNotIn("confirm docstrings present", spec_prompt)

    def test_runner_failure_on_one_axis_escalates_that_axis(self):
        self.cfg.agent.stage_retry_attempts = 0

        class _MixedRunner:
            name = "claude-code"

            def run(self, **kwargs):
                session_name = kwargs.get("session_name", "")
                if session_name.endswith("-review-spec"):
                    return RunnerResult(ok=False, session_id=None, exit_code=1,
                                        raw={"result": ""}, stdout="", stderr="boom")
                text = "APPROVE\n\nVerification Story: checked.\n\n" + _findings_json_block("no-op")
                return RunnerResult(ok=True, session_id="sess-standards", exit_code=0,
                                    raw={"result": text}, stdout=text, stderr="")

        with patch("gantry.review.get_runner", return_value=_MixedRunner()):
            out = run_review(self.store, self.run_id, self.cfg, self.target)
        self.assertEqual(out["spec"]["verdict"], "ESCALATE")
        self.assertEqual(out["combined_verdict"], "ESCALATE")
        self.assertTrue(out.get("runner_failed"))
        self.assertEqual(self.store.state(self.run_id)["status"], "review_escalated")

    def test_runner_failure_retries_then_recovers(self):
        self.cfg.agent.stage_retry_attempts = 2
        calls = {"n": 0}

        class _FlakyThenOk:
            name = "claude-code"

            def run(self, **kwargs):
                calls["n"] += 1
                # Fail both axes on first review pass (2 calls), succeed after.
                if calls["n"] <= 2:
                    return RunnerResult(ok=False, session_id=None, exit_code=1,
                                        raw={"result": ""}, stdout="", stderr="boom")
                text = (
                    "APPROVE\n\nVerification Story: ok.\n\n"
                    + _findings_json_block("no-op")
                )
                return RunnerResult(ok=True, session_id=f"s{calls['n']}", exit_code=0,
                                    raw={"result": text}, stdout=text, stderr="")

        with patch("gantry.review.get_runner", return_value=_FlakyThenOk()):
            out = run_review(self.store, self.run_id, self.cfg, self.target)
        self.assertEqual(out["combined_verdict"], "APPROVE")
        self.assertFalse(out.get("runner_failed"))
        self.assertEqual(self.store.state(self.run_id)["status"], "review_approved")
        self.assertGreaterEqual(calls["n"], 4)  # failed pass (2) + success pass (2)

    def test_two_axis_false_matches_legacy_single_verdict_shape(self):
        """The critical regression guard: two_axis=False must produce the
        EXACT same review-result.json shape and behavior as the original
        (pre-two-axis) review.py — a flat {verdict, ok, model, session_id,
        result} dict, single "review" session key, single review-prompt.md."""
        self.cfg.review.two_axis = False

        class _SingleRunner:
            name = "claude-code"

            def run(self, **kwargs):
                return RunnerResult(ok=True, session_id="sess-legacy", exit_code=0,
                                    raw={"result": "Looks good. APPROVE"},
                                    stdout="Looks good. APPROVE", stderr="")

        with patch("gantry.review.get_runner", return_value=_SingleRunner()):
            out = run_review(self.store, self.run_id, self.cfg, self.target)

        self.assertEqual(set(out.keys()), {"verdict", "ok", "model", "session_id", "result"})
        self.assertEqual(out["verdict"], "APPROVE")
        self.assertEqual(out["session_id"], "sess-legacy")
        self.assertEqual(self.store.get_session_id(self.run_id, "review"), "sess-legacy")
        self.assertIsNone(self.store.get_session_id(self.run_id, "review_spec"))
        self.assertIsNone(self.store.get_session_id(self.run_id, "review_standards"))
        prompt_log = self.store.read_artifact(self.run_id, "logs/review-prompt.md")
        self.assertIsNotNone(prompt_log)
        self.assertIsNone(self.store.read_artifact(self.run_id, "logs/review-spec-prompt.md"))


if __name__ == "__main__":
    unittest.main()
