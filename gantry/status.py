"""Typed status/transition machinery for run state.json.

This is an ADDITIVE typed wrapper around the exact status strings gantry has
always written to state.json — not a breaking rewrite. `Status` values equal
today's on-disk strings byte-for-byte (StrEnum), so an in-flight run's
state.json (written by a previous gantry version, or by a different process
mid-run) stays fully readable/writable across this change.

`TRANSITIONS` is a faithful transcription of `advance.py::advance_run`'s
CURRENT if-chain — the automatic (poller-driven) state graph — plus the
handful of transitions actually reachable through manual commands (`gantry
stage`/`retry`/`checks`/`review`/`approve`/`revise`/`ship`/`mark-shipped`) that
have NO status precondition of their own today (a deliberate design property
of those commands, not something this change may narrow). Where a manual
command already has its own precondition (e.g. `gantry ship` normally requires
review_approved), that entry point still gates itself before ever calling
`update_state` — `validate_transition` here does not re-implement or tighten
that gate, it just needs to not be MORE restrictive than the real code paths
that exist today.
"""
from __future__ import annotations

import sys

if sys.version_info >= (3, 11):
    from enum import StrEnum
else:  # pragma: no cover - fallback for <3.11
    from enum import Enum

    class StrEnum(str, Enum):  # type: ignore[no-redef]
        def __str__(self) -> str:
            return str(self.value)


class Status(StrEnum):
    """Every status string ever written to state.json, exhaustively grepped
    from the current codebase (advance.py, engine.py, review.py, ship.py,
    checks.py, cli/run_commands.py, state.py)."""
    CREATED = "created"
    QUEUED = "queued"

    AWAITING_SPEC = "awaiting_spec"
    SPEC_RUNNING = "spec_running"
    SPEC_COMPLETE = "spec_complete"
    SPEC_FAILED = "spec_failed"

    AWAITING_DESIGN = "awaiting_design"
    DESIGN_RUNNING = "design_running"
    DESIGN_COMPLETE = "design_complete"
    DESIGN_FAILED = "design_failed"

    AWAITING_DEFINITION = "awaiting_definition"
    DEFINITION_RUNNING = "definition_running"
    DEFINITION_COMPLETE = "definition_complete"
    DEFINITION_FAILED = "definition_failed"

    AWAITING_PLAN = "awaiting_plan"
    PLAN_RUNNING = "plan_running"
    PLAN_COMPLETE = "plan_complete"
    PLAN_FAILED = "plan_failed"

    AWAITING_BUILD = "awaiting_build"
    BUILD_RUNNING = "build_running"
    BUILD_COMPLETE = "build_complete"
    BUILD_FAILED = "build_failed"
    BUILD_CHANGES_REQUESTED = "build_changes_requested"  # engine.revise()'s
    # f"{stage}_changes_requested" is generic, but "build" is the only stage
    # ever passed to revise() by any current caller (cmd_revise's --stage arg
    # is free-text so other stages are technically reachable too — see
    # PLAN_CHANGES_REQUESTED/etc. below for the other AGENT_STAGES/DOC_STAGES
    # values that same f-string can legitimately produce).
    PLAN_CHANGES_REQUESTED = "plan_changes_requested"
    EVIDENCE_CHANGES_REQUESTED = "evidence_changes_requested"
    SPEC_CHANGES_REQUESTED = "spec_changes_requested"
    DESIGN_CHANGES_REQUESTED = "design_changes_requested"
    DEFINITION_CHANGES_REQUESTED = "definition_changes_requested"

    CHECKS_RUNNING = "checks_running"
    CHECKS_PASSED = "checks_passed"
    CHECKS_FAILED = "checks_failed"

    E2E_RUNNING = "e2e_running"
    E2E_PASSED = "e2e_passed"
    E2E_FAILED = "e2e_failed"
    E2E_SKIPPED = "e2e_skipped"
    E2E_ESCALATED = "e2e_escalated"

    AWAITING_EVIDENCE = "awaiting_evidence"
    EVIDENCE_RUNNING = "evidence_running"
    EVIDENCE_COMPLETE = "evidence_complete"
    EVIDENCE_FAILED = "evidence_failed"

    REVIEW_RUNNING = "review_running"
    REVIEW_APPROVED = "review_approved"
    REVIEW_CHANGES_REQUESTED = "review_changes_requested"
    REVIEW_ESCALATED = "review_escalated"

    BLOCKED = "blocked"
    CHECKS_HIGH_RISK_ESCALATED = "checks_high_risk_escalated"
    CHECKS_ESCALATED = "checks_escalated"
    RESOLVE_RUNNING = "resolve_running"
    RESOLVE_FAILED = "resolve_failed"  # written by _repair_stale_running when a
                                        # resolver's own subprocess dies without
                                        # returning (status.removesuffix("_running")
                                        # applied to "resolve_running") — see
                                        # advance.py::_advance_one_run's stage ==
                                        # "resolve" special-case handling of this.
    RESOLVE_ESCALATED = "resolve_escalated"

    SHIPPED = "shipped"
    SHIPPED_MANUALLY = "shipped_manually"
    SHIP_FAILED = "ship_failed"
    SHIP_CHECKS_FAILED = "ship_checks_failed"

    HELD = "held"
    CANCELLED = "cancelled"


class BlockedReason(StrEnum):
    """`blocked_on` values — the second, independent dimension the "blocked"
    status encodes via a side field."""
    SCOPE = "scope"
    CHECKS = "checks"
    E2E = "e2e"
    HIGH_RISK_PATHS = "high_risk_paths"  # set alongside CHECKS_HIGH_RISK_ESCALATED,
                                          # not BLOCKED — included here since it's
                                          # the same conceptual field, not a new one.


class FailureKind(StrEnum):
    """`last_failure_reason` values — the second, independent dimension a
    `_failed` status encodes via a side field (stale-heartbeat repair vs. a
    real agent-reported failure)."""
    AGENT_REPORTED = "agent_reported"  # not actually written as a literal
                                        # string anywhere today (an ordinary
                                        # {stage}_failed simply leaves
                                        # last_failure_reason unset/None) —
                                        # named here as the explicit
                                        # complement of STALE_HEARTBEAT so
                                        # side-field-keyed TRANSITIONS entries
                                        # have a real value to key on; see
                                        # validate_transition's handling of
                                        # side_field=None for the actual
                                        # "agent reported it" case.
    STALE_HEARTBEAT = "stale_heartbeat"


class InvalidTransitionError(Exception):
    """Raised by validate_transition when to_status is not reachable from
    from_status (+ side-field, where applicable) per TRANSITIONS."""


S = Status

# Every AGENT_STAGE/DOC_STAGE-shaped status family, expanded once here so the
# table below doesn't need to hand-enumerate spec/design/plan/build/evidence
# five times over for the shape they all share.
_AWAITING = {
    "spec": S.AWAITING_SPEC, "design": S.AWAITING_DESIGN,
    "definition": S.AWAITING_DEFINITION, "plan": S.AWAITING_PLAN,
    "build": S.AWAITING_BUILD, "evidence": S.AWAITING_EVIDENCE,
}
_RUNNING = {
    "spec": S.SPEC_RUNNING, "design": S.DESIGN_RUNNING,
    "definition": S.DEFINITION_RUNNING, "plan": S.PLAN_RUNNING,
    "build": S.BUILD_RUNNING, "evidence": S.EVIDENCE_RUNNING,
}
_COMPLETE = {
    "spec": S.SPEC_COMPLETE, "design": S.DESIGN_COMPLETE,
    "definition": S.DEFINITION_COMPLETE, "plan": S.PLAN_COMPLETE,
    "build": S.BUILD_COMPLETE, "evidence": S.EVIDENCE_COMPLETE,
}
_FAILED = {
    "spec": S.SPEC_FAILED, "design": S.DESIGN_FAILED,
    "definition": S.DEFINITION_FAILED, "plan": S.PLAN_FAILED,
    "build": S.BUILD_FAILED, "evidence": S.EVIDENCE_FAILED,
}
_CHANGES_REQUESTED = {
    "spec": S.SPEC_CHANGES_REQUESTED, "design": S.DESIGN_CHANGES_REQUESTED,
    "definition": S.DEFINITION_CHANGES_REQUESTED,
    "plan": S.PLAN_CHANGES_REQUESTED, "build": S.BUILD_CHANGES_REQUESTED,
    "evidence": S.EVIDENCE_CHANGES_REQUESTED,
}

# Statuses reachable via a manual CLI command that has NO precondition on the
# run's current status today (`gantry stage`/`retry`/`checks`/`review`/
# `approve`/`revise` all call straight into Engine.run_agent_stage/run_checks/
# run_review/approve/revise with no status gate — see cli/run_commands.py).
# This is a set of TARGET statuses reachable from ANY current status (not a
# from->to mapping) — merged into every from-status's allowed-targets set
# below, since a human can legitimately invoke any of these commands against
# a run in any status today and validate_transition must not newly forbid
# that.
MANUALLY_REACHABLE: set[Status] = (
    set(_RUNNING.values())              # gantry stage / retry
    | set(_COMPLETE.values())           # a manually-run stage finishing ok
    | set(_FAILED.values())             # a manually-run stage finishing failed
    | set(_CHANGES_REQUESTED.values())  # gantry revise
    | {
        S.BLOCKED,                    # gantry checks (run_all_checks writes blocked on fail)
        S.REVIEW_RUNNING,             # gantry review
        S.REVIEW_APPROVED,            # gantry review / gantry approve --stage review
        S.REVIEW_CHANGES_REQUESTED,   # gantry review
        S.REVIEW_ESCALATED,           # gantry review
        S.SHIPPED,                    # gantry ship --force
        S.SHIP_FAILED,                # gantry ship --force
        S.SHIP_CHECKS_FAILED,         # gantry ship --force
        S.SHIPPED_MANUALLY,           # gantry mark-shipped
        S.RESOLVE_RUNNING,            # Engine.run_resolver_stage — callable directly
                                       # (not only via the checks_escalated auto-path)
                                       # with no status precondition of its own.
        S.RESOLVE_FAILED,             # _repair_stale_running's own stale-heartbeat
                                       # write, likewise reachable regardless of the
                                       # run's prior status.
        S.CHECKS_ESCALATED,           # gantry checks (run_all_checks can leave a run
                                       # here after an escalation gets re-checked and
                                       # still fails) — also directly settable via
                                       # RunStore.update_state with no CLI-level
                                       # precondition today (there is no
                                       # "gantry escalate" gate to speak of).
        S.RESOLVE_ESCALATED,          # same reasoning as CHECKS_ESCALATED above —
                                       # nothing in today's codebase gates writing
                                       # this status directly.
        S.CHECKS_HIGH_RISK_ESCALATED, # same reasoning — checks.py/advance.py write
                                       # this unconditionally on a high-risk match,
                                       # not gated behind any other current status.
    }
    | set(_AWAITING.values())           # gantry approve
)

# The automatic (poller-driven) graph — a faithful, line-by-line transcription
# of advance.py::advance_run's current if-chain. Kept separate from
# MANUALLY_REACHABLE above (which is unioned in below) purely for readability/
# auditability against advance_run's source; the two are merged into one
# lookup table before being exposed as TRANSITIONS.
AUTOMATIC_TRANSITIONS: dict[Status | tuple[Status, str], set[Status]] = {
    # queued -> awaiting_{first} once _prereqs_met
    S.QUEUED: {_AWAITING[stage] for stage in _AWAITING} | {S.QUEUED},
    # awaiting_{stage} -> {stage}_running (run_agent_stage sets this itself)
    **{_AWAITING[stage]: {_RUNNING[stage]} for stage in _AWAITING},
    # {stage}_running -> {stage}_complete / {stage}_failed (run_agent_stage's
    # own terminal write, also the resolver's stale-heartbeat repair path
    # writing {stage}_failed with last_failure_reason=stale_heartbeat)
    **{_RUNNING[stage]: {_COMPLETE[stage], _FAILED[stage]} for stage in _RUNNING},
    # spec_complete/design_complete/definition_complete -> awaiting_{next}
    S.SPEC_COMPLETE: {S.AWAITING_DESIGN, S.AWAITING_PLAN},  # next stage depends on
                                                              # cfg.stages; both are
                                                              # legitimate depending on
                                                              # whether design is enabled
    S.DESIGN_COMPLETE: {S.AWAITING_PLAN},
    S.DEFINITION_COMPLETE: {S.AWAITING_PLAN},
    # plan_complete -> build_running (via run_agent_stage)
    S.PLAN_COMPLETE: {S.BUILD_RUNNING},
    # build_complete -> checks_high_risk_escalated / blocked(e2e) /
    # review_running (evidence skipped) / evidence_running
    S.BUILD_COMPLETE: {S.CHECKS_RUNNING, S.CHECKS_HIGH_RISK_ESCALATED, S.BLOCKED,
                        S.REVIEW_RUNNING, S.EVIDENCE_RUNNING},
    S.CHECKS_RUNNING: {S.CHECKS_PASSED, S.CHECKS_FAILED, S.CHECKS_HIGH_RISK_ESCALATED},
    S.CHECKS_PASSED: {S.E2E_RUNNING},
    S.CHECKS_FAILED: {S.CHECKS_ESCALATED, S.BUILD_RUNNING, S.BUILD_COMPLETE,
                      S.BUILD_FAILED},
    S.E2E_RUNNING: {S.E2E_PASSED, S.E2E_FAILED, S.E2E_SKIPPED},
    S.E2E_PASSED: {S.REVIEW_RUNNING, S.EVIDENCE_RUNNING},
    S.E2E_SKIPPED: {S.REVIEW_RUNNING, S.EVIDENCE_RUNNING},
    S.E2E_FAILED: {S.E2E_ESCALATED, S.BUILD_RUNNING, S.BUILD_COMPLETE,
                   S.BUILD_FAILED},
    # evidence_complete -> review_running
    S.EVIDENCE_COMPLETE: {S.REVIEW_RUNNING},
    # review_changes_requested -> build_running (resume)
    S.REVIEW_CHANGES_REQUESTED: {S.BUILD_RUNNING},
    # review_approved (+auto_ship) -> shipped / ship_failed / ship_checks_failed
    S.REVIEW_APPROVED: {S.SHIPPED, S.SHIP_FAILED, S.SHIP_CHECKS_FAILED},
    # ship_failed (+auto_ship, retry within cap) -> shipped / ship_failed again
    S.SHIP_FAILED: {S.SHIPPED, S.SHIP_FAILED},
    # blocked -> checks_escalated (retry exhausted) OR blocked_on-branch-keyed
    # retry-build (side-field: blocked_on is scope/checks/e2e — all three take
    # the same retry-or-escalate branch in advance_run, so one shared entry
    # covers all three side-field values). The retry path calls
    # engine.run_agent_stage(run_id, "build", resume=True), which is a single
    # logical step from advance_run's perspective even though it internally
    # writes build_running before build_complete/build_failed — model all
    # three as directly reachable so a caller/test that treats
    # run_agent_stage as an atomic black box (e.g. a mock that only performs
    # the terminal write) isn't rejected by a boundary that's an
    # implementation detail of run_agent_stage, not a caller-visible contract.
    (S.BLOCKED, BlockedReason.SCOPE.value): {S.CHECKS_ESCALATED, S.BUILD_RUNNING,
                                              S.BUILD_COMPLETE, S.BUILD_FAILED},
    (S.BLOCKED, BlockedReason.CHECKS.value): {S.CHECKS_ESCALATED, S.BUILD_RUNNING,
                                               S.BUILD_COMPLETE, S.BUILD_FAILED},
    (S.BLOCKED, BlockedReason.E2E.value): {S.CHECKS_ESCALATED, S.BUILD_RUNNING,
                                            S.BUILD_COMPLETE, S.BUILD_FAILED},
    # checks_escalated (+auto_resolve) -> resolve_escalated (attempts
    # exhausted) OR resolve_running -> (same atomic-black-box reasoning as
    # above) also directly to build_complete/checks_escalated, since
    # run_resolver_stage's own re-verification terminal write is likewise a
    # single logical step from advance_run's perspective.
    S.CHECKS_ESCALATED: {S.RESOLVE_ESCALATED, S.RESOLVE_RUNNING, S.BUILD_COMPLETE,
                          S.CHECKS_ESCALATED},
    # resolve_running -> build_complete (verified pass) / checks_escalated
    # (verified fail) — run_resolver_stage's own terminal write; also ->
    # resolve_failed if the resolver subprocess itself dies without
    # returning (_repair_stale_running's stale-heartbeat repair, same
    # mechanism as the AGENT_STAGES *_running -> *_failed entries above).
    S.RESOLVE_RUNNING: {S.BUILD_COMPLETE, S.CHECKS_ESCALATED, S.RESOLVE_FAILED},
    # resolve_failed (stale-heartbeat only — this is the ONLY way
    # resolve_failed is ever written) -> re-run resolve from scratch
    # (resolve_running) OR resolve_escalated immediately if
    # resolve_attempt_count was already at cap (_advance_one_run's
    # stage == "resolve" special case checks the cap BEFORE retrying).
    (S.RESOLVE_FAILED, FailureKind.STALE_HEARTBEAT.value): {S.RESOLVE_RUNNING, S.RESOLVE_ESCALATED},
    # the stale-heartbeat repair path's retry-after-repair branch
    # ({stage}_failed w/ last_failure_reason=stale_heartbeat -> re-run that
    # exact stage from scratch) — modeled via the _RUNNING entries above
    # (already covers {stage}_failed -> nothing; the retry itself calls
    # run_agent_stage/run_resolver_stage/run_review which set their own
    # *_running/*_escalated statuses, already covered by other entries here).
    (S.SPEC_FAILED, FailureKind.STALE_HEARTBEAT.value): {S.SPEC_RUNNING},
    (S.DESIGN_FAILED, FailureKind.STALE_HEARTBEAT.value): {S.DESIGN_RUNNING},
    (S.PLAN_FAILED, FailureKind.STALE_HEARTBEAT.value): {S.PLAN_RUNNING},
    (S.BUILD_FAILED, FailureKind.STALE_HEARTBEAT.value): {S.BUILD_RUNNING},
    (S.EVIDENCE_FAILED, FailureKind.STALE_HEARTBEAT.value): {S.EVIDENCE_RUNNING},
    (S.RESOLVE_ESCALATED, FailureKind.STALE_HEARTBEAT.value): {S.RESOLVE_ESCALATED},
}


def _merge_transitions() -> dict[Status | tuple[Status, str], set[Status]]:
    """Union MANUALLY_REACHABLE (targets reachable from ANY current status,
    via a manual CLI command with no status precondition) into every
    from-status's allowed-targets set — including every plain Status member
    (not just the ones AUTOMATIC_TRANSITIONS happens to have an entry for)
    and every (Status, side_field) tuple key, so a manual command is never
    newly forbidden regardless of which specific status or side-field
    combination a run happens to be sitting in."""
    merged: dict[Status | tuple[Status, str], set[Status]] = {}
    for key, targets in AUTOMATIC_TRANSITIONS.items():
        merged[key] = set(targets) | MANUALLY_REACHABLE
    for status in Status:
        merged.setdefault(status, set())
        merged[status] = merged[status] | MANUALLY_REACHABLE
    return merged


TRANSITIONS: dict[Status | tuple[Status, str], set[Status]] = _merge_transitions()

# held/cancelled apply orthogonally to almost any in-flight status — NOT
# baked into TRANSITIONS as literal edges from every state (that would bloat
# the table with a duplicate entry per status for no semantic gain). Instead
# a small separate check, referencing Status enum values rather than raw
# strings: holding/cancelling is disallowed only while a stage is actively
# `_running` (matches cmd_hold's own docstring/guard) — held itself is
# handled as its own special case in validate_transition (resuming a held run
# can restore ANY prior status, so from_status == HELD always validates).
_RUNNING_STATUSES = set(_RUNNING.values()) | {
    S.RESOLVE_RUNNING, S.REVIEW_RUNNING, S.CHECKS_RUNNING, S.E2E_RUNNING,
}


def can_hold_or_cancel(current: Status | str) -> bool:
    """True unless `current` is an actively-`_running` status — mirrors
    cmd_hold's existing guard (cmd_cancel has no such guard today; this is
    only consulted for the hold path, kept here as one shared helper rather
    than two copies since the underlying rule — "not mid-agent-invocation" —
    is the same concern either way).

    Falls back to a bare `.endswith("_running")` string check for a status
    string this enum doesn't recognize (a future/unknown status) — matches
    cmd_hold's original raw-string guard exactly for that case, rather than
    permissively allowing a hold on a status this version has never heard of."""
    try:
        status = Status(current)
    except ValueError:
        return not str(current).endswith("_running")
    return status not in _RUNNING_STATUSES


def validate_transition(from_status: Status | str | None,
                        to_status: Status | str,
                        side_field: str | None = None) -> None:
    """Raise InvalidTransitionError if `to_status` isn't reachable from
    `from_status` (+ `side_field`, for the handful of statuses where a side
    field genuinely branches behavior) per TRANSITIONS.

    Never rejects:
      - a brand-new run's very first status write (from_status is None/absent
        — nothing to validate against yet). "created" is treated the same
        way: RunStore.create always writes status="created" as a transient
        bootstrap value that create_run immediately overwrites with the
        run's real first status (queued or awaiting_{first}) in the very
        next call — no pipeline code, human, or notification ever observes
        "created" as a meaningful state, so a transition FROM "created" is
        exactly equivalent to "no prior status to validate against yet".
      - to_status == held/cancelled (see can_hold_or_cancel; those are
        governed by their own small check, not TRANSITIONS, and by the time
        update_state is reached the CLI layer has already gated on it).
      - from_status == held (resuming a held run restores whatever status was
        active before the hold, which was itself already validated when it
        was first written).
      - an unrecognized to_status string (defensive: a project's state.json
        predating this enum, or a future status this version doesn't know
        about yet, should not hard-crash write access to that run)."""
    if from_status is None or from_status == "" or from_status == Status.CREATED:
        return
    try:
        to = Status(to_status)
    except ValueError:
        return
    try:
        frm = Status(from_status)
    except ValueError:
        return
    if to in (Status.HELD, Status.CANCELLED):
        return
    if frm == Status.HELD:
        return

    key: Status | tuple[Status, str] = frm
    if side_field is not None and (frm, side_field) in TRANSITIONS:
        key = (frm, side_field)

    allowed = TRANSITIONS.get(key)
    if allowed is None:
        # No entry at all for this from_status (+ side_field) combination —
        # nothing in the current codebase ever transitions FROM this status,
        # so there is nothing to validate against; permissive by default
        # rather than guessing at a restriction nobody asked for.
        return
    if to not in allowed:
        raise InvalidTransitionError(
            f"invalid status transition: {frm.value!r} -> {to.value!r}"
            + (f" (side_field={side_field!r})" if side_field is not None else ""))
