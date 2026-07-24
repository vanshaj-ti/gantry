"""Parity tests for the declarative transition machine."""
from __future__ import annotations

import unittest

from gantry.machine import (
    AUTOMATIC_MACHINE,
    MACHINE,
    machine_parity_errors,
    transitions_from,
    validate_transition,
)
from gantry.status import Status, validate_transition as status_validate


class TestMachineParity(unittest.TestCase):
    def test_no_parity_errors_against_automatic_transitions(self):
        self.assertEqual(machine_parity_errors(), [])

    def test_machine_non_empty(self):
        self.assertGreater(len(AUTOMATIC_MACHINE), 20)
        self.assertGreater(len(MACHINE), len(AUTOMATIC_MACHINE))

    def test_validate_transition_matches_status_module(self):
        pairs = [
            (Status.QUEUED, Status.AWAITING_PLAN, None),
            (Status.PLAN_COMPLETE, Status.BUILD_RUNNING, None),
            (Status.BUILD_COMPLETE, Status.EVIDENCE_RUNNING, None),
            (Status.BLOCKED, Status.BUILD_RUNNING, "checks"),
        ]
        for frm, to, side in pairs:
            validate_transition(frm, to, side)
            status_validate(frm, to, side)

    def test_transitions_from_plan_complete(self):
        dests = {str(e.destination) for e in transitions_from(Status.PLAN_COMPLETE, automatic_only=True)}
        self.assertIn("build_running", dests)


if __name__ == "__main__":
    unittest.main()
