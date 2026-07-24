import tempfile
import unittest
from pathlib import Path

from gantry.status import (
    BlockedReason,
    FailureKind,
    InvalidTransitionError,
    Status,
    can_hold_or_cancel,
    validate_transition,
)
from gantry.state import RunStore


class TestStatusEnumValues(unittest.TestCase):
    """Status members must equal today's exact on-disk strings — this is an
    additive typed wrapper, not a breaking rewrite."""

    def test_values_match_on_disk_strings(self):
        self.assertEqual(Status.BLOCKED.value, "blocked")
        self.assertEqual(Status.CHECKS_HIGH_RISK_ESCALATED.value, "checks_high_risk_escalated")
        self.assertEqual(Status.SHIP_CHECKS_FAILED.value, "ship_checks_failed")
        self.assertEqual(Status.SHIP_FAILED.value, "ship_failed")
        self.assertEqual(Status.RESOLVE_ESCALATED.value, "resolve_escalated")
        self.assertEqual(Status.CHECKS_RUNNING.value, "checks_running")
        self.assertEqual(Status.E2E_SKIPPED.value, "e2e_skipped")
        self.assertEqual(Status.HELD.value, "held")
        self.assertEqual(Status.CANCELLED.value, "cancelled")

    def test_string_equality(self):
        # StrEnum must compare equal to its bare string value so dict-keying
        # by plain strings (STATUS_LABELS etc.) keeps working transparently.
        self.assertEqual(Status.BLOCKED, "blocked")
        d = {"blocked": "x"}
        self.assertEqual(d.get(Status.BLOCKED), "x")
        d2 = {Status.BLOCKED: "y"}
        self.assertEqual(d2.get("blocked"), "y")

    def test_json_serializable_as_plain_string(self):
        import json
        payload = json.dumps({"status": Status.BLOCKED, "blocked_on": BlockedReason.E2E})
        self.assertEqual(json.loads(payload), {"status": "blocked", "blocked_on": "e2e"})


class TestValidateTransition(unittest.TestCase):
    def test_brand_new_run_first_write_never_rejected(self):
        # from_status is None (no prior status at all)
        validate_transition(None, Status.AWAITING_PLAN)
        validate_transition(None, "queued")
        validate_transition("", Status.QUEUED)

    def test_held_and_cancelled_always_reachable(self):
        for frm in (Status.BUILD_RUNNING, Status.BLOCKED, Status.SHIPPED, Status.PLAN_COMPLETE):
            validate_transition(frm, Status.HELD)
            validate_transition(frm, Status.CANCELLED)

    def test_resuming_a_held_run_restores_any_prior_status(self):
        for restored in (Status.BLOCKED, Status.BUILD_FAILED, Status.CHECKS_ESCALATED):
            validate_transition(Status.HELD, restored)

    def test_unrecognized_status_strings_do_not_raise(self):
        validate_transition("some_future_status", "another_future_status")
        validate_transition(Status.BLOCKED, "some_future_status")

    def test_legal_automatic_transitions_pass(self):
        validate_transition(Status.QUEUED, Status.AWAITING_PLAN)
        validate_transition(Status.AWAITING_PLAN, Status.PLAN_RUNNING)
        validate_transition(Status.PLAN_RUNNING, Status.PLAN_COMPLETE)
        validate_transition(Status.PLAN_RUNNING, Status.PLAN_FAILED)
        validate_transition(Status.PLAN_COMPLETE, Status.BUILD_RUNNING)
        validate_transition(Status.BUILD_COMPLETE, Status.CHECKS_HIGH_RISK_ESCALATED)
        validate_transition(Status.BUILD_COMPLETE, Status.CHECKS_RUNNING)
        validate_transition(Status.CHECKS_RUNNING, Status.CHECKS_PASSED)
        validate_transition(Status.CHECKS_PASSED, Status.E2E_RUNNING)
        validate_transition(Status.E2E_RUNNING, Status.E2E_SKIPPED)
        validate_transition(Status.E2E_SKIPPED, Status.EVIDENCE_RUNNING)
        validate_transition(Status.BUILD_COMPLETE, Status.BLOCKED)
        validate_transition(Status.BUILD_COMPLETE, Status.EVIDENCE_RUNNING)
        validate_transition(Status.EVIDENCE_COMPLETE, Status.REVIEW_RUNNING)
        validate_transition(Status.REVIEW_CHANGES_REQUESTED, Status.BUILD_RUNNING)
        validate_transition(Status.REVIEW_APPROVED, Status.SHIPPED)
        validate_transition(Status.REVIEW_APPROVED, Status.SHIP_FAILED)
        validate_transition(Status.REVIEW_APPROVED, Status.SHIP_CHECKS_FAILED)
        validate_transition(Status.SHIP_FAILED, Status.SHIPPED)
        validate_transition(Status.SHIP_FAILED, Status.SHIP_FAILED)
        validate_transition(Status.CHECKS_ESCALATED, Status.RESOLVE_RUNNING)
        validate_transition(Status.CHECKS_ESCALATED, Status.RESOLVE_ESCALATED)
        validate_transition(Status.RESOLVE_RUNNING, Status.BUILD_COMPLETE)
        validate_transition(Status.RESOLVE_RUNNING, Status.CHECKS_ESCALATED)
        validate_transition(Status.RESOLVE_RUNNING, Status.RESOLVE_FAILED)

    def test_blocked_on_side_field_branches(self):
        for reason in (BlockedReason.SCOPE, BlockedReason.CHECKS, BlockedReason.E2E):
            validate_transition(Status.BLOCKED, Status.CHECKS_ESCALATED, side_field=reason.value)
            validate_transition(Status.BLOCKED, Status.BUILD_RUNNING, side_field=reason.value)

    def test_stale_heartbeat_side_field_branches(self):
        validate_transition(Status.BUILD_FAILED, Status.BUILD_RUNNING,
                            side_field=FailureKind.STALE_HEARTBEAT.value)
        validate_transition(Status.RESOLVE_FAILED, Status.RESOLVE_RUNNING,
                            side_field=FailureKind.STALE_HEARTBEAT.value)
        validate_transition(Status.RESOLVE_FAILED, Status.RESOLVE_ESCALATED,
                            side_field=FailureKind.STALE_HEARTBEAT.value)

    def test_manually_reachable_statuses_from_any_status(self):
        # gantry stage/retry/checks/review/approve/revise have no status
        # precondition today — must not be newly forbidden.
        for frm in (Status.BLOCKED, Status.SHIPPED, Status.CHECKS_ESCALATED, Status.QUEUED):
            validate_transition(frm, Status.BUILD_RUNNING)
            validate_transition(frm, Status.REVIEW_RUNNING)

    def test_invalid_jump_rejected(self):
        # QUEUED is not reachable from anywhere except its own automatic
        # entry (S.QUEUED: {..., S.QUEUED}) and is NOT in MANUALLY_REACHABLE
        # (no manual CLI command writes "queued" directly) — a jump straight
        # to it from an unrelated in-flight status is genuinely illegal.
        with self.assertRaises(InvalidTransitionError):
            validate_transition(Status.BUILD_RUNNING, Status.QUEUED)

    def test_invalid_jump_rejected_review_approved_to_queued(self):
        with self.assertRaises(InvalidTransitionError):
            validate_transition(Status.REVIEW_APPROVED, Status.QUEUED)

    def test_update_state_with_no_status_kwarg_never_validated(self):
        # A bare heartbeat_at update carries no `status` kwarg at all.
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp))
            run_id = "r1"
            store.create(run_id, "t")
            store.update_state(run_id, status=Status.QUEUED)
            # Would be an invalid jump if validated against "status", but no
            # status kwarg is present here — must not raise.
            store.update_state(run_id, heartbeat_at="2024-01-01T00:00:00+00:00")
            self.assertEqual(store.state(run_id)["status"], "queued")


class TestRunStoreValidatesTransitions(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = RunStore(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_first_status_write_on_new_run_is_never_rejected(self):
        run_id = "r1"
        self.store.create(run_id, "t")  # writes status="created" — must not raise
        self.assertEqual(self.store.state(run_id)["status"], "created")

    def test_invalid_transition_raises_through_update_state(self):
        run_id = "r1"
        self.store.create(run_id, "t")
        self.store.update_state(run_id, status="build_running")
        with self.assertRaises(InvalidTransitionError):
            self.store.update_state(run_id, status="queued")

    def test_blocked_on_resolved_from_current_stored_value_when_not_in_call(self):
        run_id = "r1"
        self.store.create(run_id, "t")
        self.store.update_state(run_id, status="blocked", blocked_on="e2e")
        # blocked_on not passed in this call — must fall back to the
        # currently-stored value ("e2e") for side-field resolution.
        self.store.update_state(run_id, status="build_running")
        self.assertEqual(self.store.state(run_id)["status"], "build_running")


class TestCanHoldOrCancel(unittest.TestCase):
    def test_running_statuses_refused(self):
        for s in (Status.BUILD_RUNNING, Status.PLAN_RUNNING, Status.RESOLVE_RUNNING,
                  Status.REVIEW_RUNNING):
            self.assertFalse(can_hold_or_cancel(s))

    def test_non_running_statuses_allowed(self):
        for s in (Status.BLOCKED, Status.SHIPPED, Status.CHECKS_ESCALATED, Status.HELD):
            self.assertTrue(can_hold_or_cancel(s))

    def test_unknown_status_string_falls_back_to_suffix_check(self):
        self.assertFalse(can_hold_or_cancel("some_future_running"))
        self.assertTrue(can_hold_or_cancel("some_future_status"))


if __name__ == "__main__":
    unittest.main()
