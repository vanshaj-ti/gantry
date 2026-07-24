"""Declarative transition machine — sole automatic-advance dispatcher.

Automatic advance ticks enter through ``dispatch_automatic_advance``. Typed
``Transition`` records remain the parity source for allowed status edges.
Legacy status *values* remain byte-identical; this module does not invent
new on-disk strings.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Iterable

from gantry.status import (
    AUTOMATIC_TRANSITIONS,
    MANUALLY_REACHABLE,
    Status,
    TRANSITIONS,
    validate_transition as _legacy_validate_transition,
)

if TYPE_CHECKING:
    from gantry.engine import Engine

Guard = Callable[[dict[str, Any]], bool] | None
Action = str  # symbolic action name consumed by advance / CLI


@dataclass(frozen=True)
class Transition:
    """One typed edge in the pipeline graph."""

    source: Status | str
    destination: Status | str
    side_condition: str | None = None  # blocked_on / last_failure_reason
    guard: str | None = None           # named guard id (resolved by advance)
    action: str = ""
    retry_policy: str | None = None
    human_gate: bool = False
    notification_event: str | None = None
    automatic: bool = True
    extra: dict[str, Any] = field(default_factory=dict)


def _edge(
    source: Status | str,
    destinations: Iterable[Status | str],
    *,
    side: str | None = None,
    action: str = "",
    human_gate: bool = False,
    automatic: bool = True,
) -> list[Transition]:
    return [
        Transition(
            source=source,
            destination=dest,
            side_condition=side,
            action=action or str(dest),
            human_gate=human_gate,
            automatic=automatic,
        )
        for dest in destinations
    ]


def _build_automatic_transitions() -> tuple[Transition, ...]:
    """Compile AUTOMATIC_TRANSITIONS into typed Transition records."""
    out: list[Transition] = []
    for key, targets in AUTOMATIC_TRANSITIONS.items():
        if isinstance(key, tuple):
            source, side = key
            out.extend(_edge(source, targets, side=side, automatic=True))
        else:
            out.extend(_edge(key, targets, automatic=True))
    return tuple(out)


def _build_manual_transitions() -> tuple[Transition, ...]:
    """Every MANUALLY_REACHABLE target is reachable from any Status member."""
    out: list[Transition] = []
    for source in Status:
        for dest in MANUALLY_REACHABLE:
            out.append(Transition(
                source=source,
                destination=dest,
                action=f"manual->{dest}",
                human_gate=False,
                automatic=False,
            ))
    return tuple(out)


AUTOMATIC_MACHINE: tuple[Transition, ...] = _build_automatic_transitions()
MANUAL_MACHINE: tuple[Transition, ...] = _build_manual_transitions()
MACHINE: tuple[Transition, ...] = AUTOMATIC_MACHINE + MANUAL_MACHINE


def transitions_from(
    source: Status | str,
    *,
    side_field: str | None = None,
    automatic_only: bool = False,
) -> list[Transition]:
    """List allowed transitions from ``source`` (+ optional side field)."""
    results: list[Transition] = []
    for edge in (AUTOMATIC_MACHINE if automatic_only else MACHINE):
        if str(edge.source) != str(source):
            continue
        if edge.side_condition is not None:
            if side_field != edge.side_condition:
                continue
        elif side_field is not None and (str(source), side_field) in TRANSITIONS:
            # Prefer side-keyed edges when a side field is active.
            continue
        if automatic_only and not edge.automatic:
            continue
        results.append(edge)
    return results


def validate_transition(from_status, to_status, side_field: str | None = None) -> None:
    """Delegate to legacy validate_transition — same contract, single entry."""
    _legacy_validate_transition(from_status, to_status, side_field)


def machine_parity_errors() -> list[str]:
    """Return mismatches between MACHINE and status.TRANSITIONS (empty = ok)."""
    errors: list[str] = []
    # Every automatic TRANSITIONS target must appear as an automatic edge.
    for key, targets in AUTOMATIC_TRANSITIONS.items():
        if isinstance(key, tuple):
            source, side = key
            edges = {
                str(e.destination)
                for e in AUTOMATIC_MACHINE
                if str(e.source) == str(source) and e.side_condition == side
            }
        else:
            source = key
            edges = {
                str(e.destination)
                for e in AUTOMATIC_MACHINE
                if str(e.source) == str(source) and e.side_condition is None
            }
        missing = {str(t) for t in targets} - edges
        if missing:
            errors.append(f"missing automatic edges from {key}: {sorted(missing)}")
    return errors


def dispatch_automatic_advance(engine: "Engine", run_id: str) -> dict[str, Any]:
    """Sole automatic-advance entry point used by ``advance.advance_run``.

    Execution order is owned by ``gantry.advance_dispatch.DISPATCH_RULES``
    (historical if-chain order). Allowed destinations remain validated by
    ``AUTOMATIC_MACHINE`` / ``validate_transition``.
    """
    from gantry.advance_dispatch import execute_dispatch
    return execute_dispatch(engine, run_id)


def dispatch_rule_names() -> tuple[str, ...]:
    """Ordered rule names — useful for parity / docs tests."""
    from gantry.advance_dispatch import DISPATCH_RULES
    return tuple(rule.name for rule in DISPATCH_RULES)
