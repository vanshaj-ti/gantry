"""Tests for machine-owned automatic advance dispatch and definition stage."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from gantry.config import DOC_STAGES, GantryConfig
from gantry.engine import Engine
from gantry.machine import dispatch_rule_names
from gantry.pipeline import materialize_stages
from gantry.sessions import STAGE_LINEAGE
from gantry.status import Status


class TestMachineDispatch(unittest.TestCase):
    def test_dispatch_rule_order_matches_historical_chain(self):
        self.assertEqual(
            dispatch_rule_names(),
            (
                "queued",
                "awaiting",
                "doc_auto_approve",
                "plan_complete",
                "build_complete",
                "checks_passed",
                "e2e_done",
                "evidence_complete",
                "review_changes_requested",
                "auto_ship",
                "ship_retry",
                "retry_blocked",
                "checks_escalated",
            ),
        )

    def test_advance_delegates_to_machine_dispatch(self):
        from gantry.advance import _advance_run_inner

        engine = MagicMock()
        engine.store.state.return_value = {"status": "plan_complete"}
        with patch(
            "gantry.advance.dispatch_automatic_advance",
            return_value={"advanced": True, "action": "build"},
        ) as mocked:
            out = _advance_run_inner(engine, "run-1")
        mocked.assert_called_once_with(engine, "run-1")
        self.assertEqual(out["action"], "build")


class TestMaterializeStages(unittest.TestCase):
    def test_combined_replaces_spec_and_design(self):
        self.assertEqual(
            materialize_stages(
                ["spec", "design", "plan", "build", "evidence", "review"],
                "combined",
            ),
            ["definition", "plan", "build", "evidence", "review"],
        )

    def test_skip_removes_definition_family(self):
        self.assertEqual(
            materialize_stages(["spec", "design", "plan", "build"], "skip"),
            ["plan", "build"],
        )

    def test_separate_keeps_spec_design_drops_definition(self):
        self.assertEqual(
            materialize_stages(["definition", "plan"], "separate"),
            ["plan"],
        )
        self.assertEqual(
            materialize_stages(["spec", "design", "plan"], "separate"),
            ["spec", "design", "plan"],
        )


class TestDefinitionStageWiring(unittest.TestCase):
    def test_definition_is_doc_stage_with_isolated_session(self):
        self.assertIn("definition", DOC_STAGES)
        self.assertEqual(STAGE_LINEAGE["definition"], "isolated")
        self.assertEqual(Status.AWAITING_DEFINITION, "awaiting_definition")
        self.assertEqual(Status.DEFINITION_COMPLETE, "definition_complete")

    def test_create_run_medium_feature_pins_definition_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            (target / ".git").mkdir()
            cfg = GantryConfig()
            cfg.pipeline.version = 1
            engine = Engine(target, cfg)
            rid = engine.create_run("add export endpoint", "new feature API", tag="feature")
            state = engine.store.state(rid)
            self.assertEqual(state["definition_policy"], "combined")
            self.assertEqual(state["stages"][0], "definition")
            self.assertNotIn("spec", state["stages"])
            self.assertNotIn("design", state["stages"])
            self.assertEqual(state["status"], "awaiting_definition")

    def test_create_run_large_keeps_separate_spec_design(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            (target / ".git").mkdir()
            cfg = GantryConfig()
            engine = Engine(target, cfg)
            rid = engine.create_run(
                "auth migration", "breaking authentication redesign", tag="feature",
            )
            state = engine.store.state(rid)
            self.assertEqual(state["definition_policy"], "separate")
            self.assertEqual(state["stages"][:2], ["spec", "design"])


if __name__ == "__main__":
    unittest.main()
