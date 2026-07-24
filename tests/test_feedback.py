import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gantry.config import GantryConfig
from gantry.engine import Engine
from gantry.feedback import (
    FINDING_TARGETS,
    NEEDS_INPUT_STATUSES,
    FeedbackRoute,
    reply_prompt,
    route_feedback,
)
from gantry.notify_messages import notify_message
from gantry.state import RunStore


class TestFeedbackRouteMatrix(unittest.TestCase):
    def test_finding_categories_route_to_responsible_stage(self):
        expected = {
            "requirement": ("spec", "answers/spec.md"),
            "architecture": ("design", "answers/design.md"),
            "diagnosis": ("investigation", "answers/investigation.md"),
            "approach": ("plan", "answers/plan.md"),
            "scope": ("plan", "answers/plan.md"),
            "implementation": ("build", "answers/build.md"),
            "proof": ("evidence", "answers/evidence.md"),
        }
        self.assertEqual(set(FINDING_TARGETS), set(expected))
        for category, (stage, artifact) in expected.items():
            with self.subTest(category=category):
                route = route_feedback(
                    "review_escalated", finding_category=category, task_profile="feature",
                )
                self.assertEqual((route.target_stage, route.artifact), (stage, artifact))
                self.assertEqual(route.resume_policy, "resume")
                self.assertEqual(route.next_state, f"{stage}_running")

    def test_status_and_blocked_reason_matrix_covers_escalations(self):
        cases = {
            ("blocked", "checks"): ("build", ("retry", "revise")),
            ("blocked", "e2e"): ("build", ("retry", "revise")),
            ("checks_escalated", "checks"): ("build", ("revise", "hold")),
            ("resolve_escalated", "checks"): ("build", ("revise", "hold")),
            ("checks_high_risk_escalated", "high_risk_paths"): (
                "build", ("approve", "revise"),
            ),
            ("ship_checks_failed", "checks"): ("build", ("retry_ship", "revise")),
            ("ship_failed", None): ("ship", ("retry_ship", "hold")),
        }
        for (status, reason), (stage, options) in cases.items():
            with self.subTest(status=status, reason=reason):
                route = route_feedback(status, blocked_reason=reason)
                self.assertIsInstance(route, FeedbackRoute)
                self.assertEqual(route.target_stage, stage)
                self.assertEqual(route.reply_options, options)
                self.assertIn(status, NEEDS_INPUT_STATUSES)

    def test_task_profile_falls_back_to_a_stage_the_profile_owns(self):
        self.assertEqual(
            route_feedback(
                "review_escalated", finding_category="requirement", task_profile="bug",
            ).target_stage,
            "plan",
        )
        self.assertEqual(
            route_feedback(
                "review_escalated", finding_category="diagnosis", task_profile="bug",
            ).target_stage,
            "investigation",
        )
        self.assertEqual(
            route_feedback(
                "review_escalated", finding_category="architecture", task_profile="hotfix",
            ).target_stage,
            "build",
        )

    def test_reply_prompts_have_consistent_notification_watch_and_linear_shapes(self):
        route = route_feedback("checks_escalated", blocked_reason="checks")
        notification = reply_prompt(route, channel="notification")
        watch = reply_prompt(route, channel="watch")
        linear = reply_prompt(route, channel="linear")
        self.assertIn("*Reply 1*", notification)
        self.assertIn("1.", watch)
        self.assertIn("Reply `1`", linear)
        for text in (notification, watch, linear):
            self.assertIn("guidance", text)
            self.assertIn("leave", text)

    def test_cli_needs_input_statuses_are_the_routed_statuses(self):
        from gantry.cli._shared import NEEDS_INPUT_STATUSES as cli_statuses

        self.assertIs(cli_statuses, NEEDS_INPUT_STATUSES)

    def test_notification_uses_route_reply_options_for_omitted_escalations(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp))
            store.create("r", "route")
            for status in (
                "checks_escalated", "resolve_escalated",
                "checks_high_risk_escalated", "ship_checks_failed",
            ):
                with self.subTest(status=status):
                    store.update_state("r", status=status, blocked_on="checks")
                    message = notify_message(store, "r", status)
                    route = route_feedback(status, blocked_reason="checks")
                    self.assertIn(reply_prompt(route), message)

    def test_linear_failure_prompt_uses_same_route_options(self):
        from gantry.linear import feedback_reply_prompt

        route = route_feedback("ship_checks_failed", blocked_reason="checks")
        self.assertEqual(feedback_reply_prompt(route), reply_prompt(route, channel="linear"))

    def test_review_parser_preserves_responsibility_category(self):
        from gantry.review import _parse_findings

        parsed = _parse_findings(
            '```json\n{"findings":[{"severity":"Major","action":"blocking",'
            '"category":"architecture","location":"x.py","description":"coupling",'
            '"recommendation":"move boundary"}]}\n```',
        )
        self.assertEqual(parsed[0]["category"], "architecture")


class TestRoutedWatchReplies(unittest.TestCase):
    def test_high_risk_approval_returns_to_build_complete_and_advances(self):
        from gantry.cli.watch import _handle_reply

        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp))
            store.create("r", "route")
            store.update_state(
                "r", status="checks_high_risk_escalated",
                blocked_on="high_risk_paths", tag="feature",
            )
            notifier = type("Notifier", (), {"send": lambda self, text, meta=None: {"sent": True}})()
            with patch("gantry.advance.advance_run") as advance:
                _handle_reply(store, GantryConfig(), notifier, "r", "1")
            self.assertEqual(store.state("r")["status"], "build_complete")
            advance.assert_called_once()

    def test_checks_escalation_guidance_is_written_to_routed_artifact(self):
        from gantry.cli.watch import _handle_reply

        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp))
            store.create("r", "route")
            store.update_state("r", status="checks_escalated", blocked_on="checks")
            notifier = type("Notifier", (), {"send": lambda self, text, meta=None: {"sent": True}})()
            with patch("gantry.engine.Engine.run_agent_stage") as resume:
                _handle_reply(store, GantryConfig(), notifier, "r", "1 fix the lint config")
            self.assertIn(
                "fix the lint config",
                store.artifact_path("r", "answers/build.md").read_text(),
            )
            resume.assert_called_once_with("r", "build", resume=True)


class TestRoutedAnswerContext(unittest.TestCase):
    def test_answer_context_reads_only_the_target_stage_routes_in_stable_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            (target / ".git").mkdir()
            eng = Engine(target, GantryConfig())
            run_id = eng.create_run("route", "feedback")
            answers = eng.store.artifact_path(run_id, "answers/build.md")
            answers.parent.mkdir(parents=True, exist_ok=True)
            answers.write_text("checks detail")
            eng.store.artifact_path(run_id, "review-comments.md").write_text("review detail")

            context = eng._answer_context(run_id, "build")

            self.assertLess(context.index("checks detail"), context.index("review detail"))
            self.assertNotIn("answers/spec.md", context)


if __name__ == "__main__":
    unittest.main()
