"""Ordered automatic-advance dispatch table.

``machine.dispatch_automatic_advance`` is the sole entry point; this module
owns the ordered rules and handlers that preserve the historical if-chain
semantics from ``advance._advance_run_inner``.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable

from .config import AGENT_STAGES, DOC_STAGES
from .checks import evaluate_checks
from .e2e import evaluate_e2e, run_e2e_tests
from .engine import Engine
from .failure_detail import _checks_failure_detail, _e2e_failure_detail
from .retry import RetryPolicy
from .review import run_review
from .state import now_iso
from .status import BlockedReason, Status

_MAX_RETRY_ATTEMPTS_KEPT = 3

AdvanceHandler = Callable[[Engine, str, str], dict[str, Any] | None]
AdvanceGuard = Callable[[Engine, str, str], bool]


@dataclass(frozen=True)
class DispatchRule:
    """One ordered automatic-advance rule.

    Rules are evaluated in declaration order. The first matching rule whose
    handler returns a non-None result wins. A handler may return None to
    decline (e.g. when a nested guard fails), allowing later rules to run.
    """

    name: str
    matches: Callable[[str], bool]
    guard: AdvanceGuard | None
    handler: AdvanceHandler


def _pipeline_version(engine: Engine, run_id: str) -> int:
    return int(engine.store.state(run_id).get("pipeline_version") or 1)


def _uses_explicit_verification(engine: Engine, run_id: str) -> bool:
    stages = engine.stages_for_run(run_id)
    return (
        _pipeline_version(engine, run_id) >= 2
        and "checks" in stages
        and "e2e" in stages
    )


def _run_v2_checks(engine: Engine, run_id: str) -> dict[str, Any]:
    from .git import merge_base_into_worktree

    engine.store.update_state(
        run_id, status=Status.CHECKS_RUNNING, current_stage="checks",
        checks_started_at=now_iso(),
    )
    merge_result = merge_base_into_worktree(
        engine.target, run_id, engine.cfg.git.base_branch,
    )
    outcome = evaluate_checks(
        engine.store, run_id, engine.cfg.scope, engine.cfg.checks,
        engine.work_dir(run_id), engine.cfg.git.base_branch,
    )
    payload = outcome.to_dict()
    payload["base_branch_merge"] = merge_result
    engine.store.write_result(run_id, "checks.json", payload)
    engine.store.write_log(run_id, "checks.log", json.dumps(payload, indent=2))
    timing = {
        "checks_started_at": outcome.started_at,
        "checks_completed_at": outcome.completed_at,
        "checks_duration_seconds": outcome.duration_seconds,
    }
    high_risk_files = outcome.scope.get("high_risk_files") or []
    if high_risk_files:
        engine.store.update_state(
            run_id, status=Status.CHECKS_HIGH_RISK_ESCALATED,
            blocked_on=BlockedReason.HIGH_RISK_PATHS, checks="pass", **timing,
        )
        return {
            "advanced": False, "from": Status.BUILD_COMPLETE,
            "action": "checks_high_risk_escalated",
            "high_risk_files": high_risk_files,
        }
    if outcome.passed:
        engine.store.update_state(
            run_id, status=Status.CHECKS_PASSED, blocked_on=None,
            checks="pass", **timing,
        )
        return {"advanced": True, "from": Status.BUILD_COMPLETE, "action": "checks_passed"}
    blocked = BlockedReason.SCOPE if not outcome.scope["pass"] else BlockedReason.CHECKS
    engine.store.update_state(
        run_id, status=Status.CHECKS_FAILED, blocked_on=blocked,
        checks="fail", **timing,
    )
    return {
        "advanced": False, "from": Status.BUILD_COMPLETE,
        "action": "checks_failed", "blocked_on": blocked,
    }


def _run_v2_e2e(engine: Engine, run_id: str) -> dict[str, Any]:
    engine.store.update_state(
        run_id, status=Status.E2E_RUNNING, current_stage="e2e",
        e2e_started_at=now_iso(),
    )
    outcome = evaluate_e2e(
        engine.store, run_id, engine.cfg.e2e, engine.work_dir(run_id),
        engine.cfg.git.base_branch,
    )
    payload = outcome.to_dict()
    engine.store.write_result(run_id, "e2e-report.json", payload)
    engine.store.write_log(run_id, "e2e.log", json.dumps(payload, indent=2))
    terminal = {
        "passed": Status.E2E_PASSED,
        "failed": Status.E2E_FAILED,
        "skipped": Status.E2E_SKIPPED,
    }[outcome.status]
    engine.store.update_state(
        run_id, status=terminal,
        blocked_on=BlockedReason.E2E if terminal == Status.E2E_FAILED else None,
        e2e=outcome.status,
        e2e_started_at=outcome.started_at,
        e2e_completed_at=outcome.completed_at,
        e2e_duration_seconds=outcome.duration_seconds,
    )
    return {
        "advanced": terminal != Status.E2E_FAILED,
        "from": Status.CHECKS_PASSED,
        "action": f"e2e_{outcome.status}",
    }


def _continue_after_verification(engine: Engine, run_id: str, status: str) -> dict[str, Any]:
    if "evidence" not in engine.stages_for_run(run_id):
        if engine.cfg.review.enabled:
            out = run_review(engine.store, run_id, engine.cfg, engine.work_dir(run_id))
            return {
                "advanced": True, "from": status,
                "action": "evidence_skipped->review", "verdict": out["verdict"],
            }
        return {"advanced": False, "from": status, "action": "review_disabled"}
    engine.run_agent_stage(run_id, "evidence", resume=False)
    return {
        "advanced": True, "from": status,
        "action": "verification_passed->evidence",
    }


def _accumulate_retry_feedback(
    store: Any, run_id: str, label_word: str, this_attempt: str,
) -> str:
    existing = store.read_artifact(run_id, "answers/build.md") or ""
    prior_attempts = re.findall(
        r"(## Attempt \d+/\d+\n\n.*?)(?=\n## Attempt \d+/\d+\n\n|\Z)",
        existing,
        re.DOTALL,
    )
    attempts = (prior_attempts + [this_attempt])[-_MAX_RETRY_ATTEMPTS_KEPT:]
    header = f"# {label_word} failed — auto-retry history (most recent last)\n\n"
    return header + "\n".join(attempts)


def _handle_queued(engine: Engine, run_id: str, status: str) -> dict[str, Any]:
    if not engine._prereqs_met(run_id):
        deps = engine.store.state(run_id).get("depends_on") or []
        unmet = [
            d for d in deps
            if engine.store.state(d).get("status") not in ("shipped", "shipped_manually")
            or engine.store.state(d).get("merged") is not True
        ]
        return {
            "advanced": False,
            "from": status,
            "action": "waiting_on_prereqs",
            "unmet_depends_on": unmet,
        }
    run_stages = engine.stages_for_run(run_id)
    first = engine.store.state(run_id).get("current_stage") or (
        run_stages[0] if run_stages else "plan"
    )
    engine.store.update_state(run_id, status=f"awaiting_{first}")
    return {
        "advanced": True,
        "from": status,
        "action": "prereqs_met->awaiting_" + first,
    }


def _handle_awaiting(engine: Engine, run_id: str, status: str) -> dict[str, Any]:
    stage = status.removeprefix("awaiting_")
    if stage in AGENT_STAGES or stage in DOC_STAGES:
        engine.run_agent_stage(run_id, stage)
        return {"advanced": True, "from": status, "action": f"start_{stage}"}
    return {"advanced": False, "from": status, "action": "no_auto_transition"}


def _handle_doc_auto_approve(engine: Engine, run_id: str, status: str) -> dict[str, Any]:
    stage = status.removesuffix("_complete")
    nxt = engine.approve(run_id, stage)
    return {
        "advanced": True,
        "from": status,
        "action": f"auto_approved_{stage}->{nxt}",
    }


def _handle_plan_complete(engine: Engine, run_id: str, status: str) -> dict[str, Any]:
    engine.run_agent_stage(run_id, "build", resume=True)
    return {"advanced": True, "from": status, "action": "build"}


def _handle_build_complete(engine: Engine, run_id: str, status: str) -> dict[str, Any]:
    if _uses_explicit_verification(engine, run_id):
        return _run_v2_checks(engine, run_id)
    checks = engine.run_checks(run_id)
    if not checks["pass"]:
        return {
            "advanced": False,
            "from": status,
            "action": "checks_failed",
            "blocked_on": engine.store.state(run_id).get("blocked_on"),
        }
    high_risk_files = checks.get("scope", {}).get("high_risk_files") or []
    if high_risk_files:
        engine.store.update_state(
            run_id,
            status=Status.CHECKS_HIGH_RISK_ESCALATED,
            blocked_on=BlockedReason.HIGH_RISK_PATHS,
        )
        return {
            "advanced": False,
            "from": status,
            "action": "checks_high_risk_escalated",
            "high_risk_files": high_risk_files,
        }
    e2e = run_e2e_tests(
        engine.store,
        run_id,
        engine.cfg.e2e,
        engine.work_dir(run_id),
        engine.cfg.git.base_branch,
    )
    if not e2e["pass"]:
        engine.store.update_state(
            run_id,
            status=Status.BLOCKED,
            blocked_on=BlockedReason.E2E,
            checks="pass",
        )
        return {"advanced": False, "from": status, "action": "e2e_failed"}
    if "evidence" not in engine.stages_for_run(run_id):
        if engine.cfg.review.enabled:
            out = run_review(
                engine.store, run_id, engine.cfg, engine.work_dir(run_id),
            )
            return {
                "advanced": True,
                "from": status,
                "action": "evidence_skipped->review",
                "verdict": out["verdict"],
            }
        return {"advanced": False, "from": status, "action": "review_disabled"}
    engine.run_agent_stage(run_id, "evidence", resume=False)
    return {
        "advanced": True,
        "from": status,
        "action": "checks_passed->evidence",
    }


def _handle_checks_passed(engine: Engine, run_id: str, status: str) -> dict[str, Any]:
    return _run_v2_e2e(engine, run_id)


def _handle_e2e_done(engine: Engine, run_id: str, status: str) -> dict[str, Any]:
    return _continue_after_verification(engine, run_id, status)


def _handle_evidence_complete(engine: Engine, run_id: str, status: str) -> dict[str, Any]:
    if engine.cfg.review.enabled:
        out = run_review(
            engine.store, run_id, engine.cfg, engine.work_dir(run_id),
        )
        return {
            "advanced": True,
            "from": status,
            "action": "review",
            "verdict": out["verdict"],
        }
    return {"advanced": False, "from": status, "action": "review_disabled"}


def _handle_review_changes(engine: Engine, run_id: str, status: str) -> dict[str, Any]:
    engine.run_agent_stage(run_id, "build", resume=True)
    return {"advanced": True, "from": status, "action": "resume_build"}


def _handle_auto_ship(engine: Engine, run_id: str, status: str) -> dict[str, Any]:
    from .ship import ship_run
    out = ship_run(engine, run_id)
    return {
        "advanced": True,
        "from": status,
        "action": "auto_shipped" if out.get("ok") else "auto_ship_failed",
        "pr_url": (out.get("pr") or {}).get("url"),
    }


def _handle_ship_retry(engine: Engine, run_id: str, status: str) -> dict[str, Any]:
    ship_attempts = engine.store.state(run_id).get("ship_attempt_count", 0)
    if RetryPolicy(
        max_attempts=engine.cfg.git.ship_retry_attempts,
    ).exhausted(ship_attempts):
        return {"advanced": False, "from": status, "action": "no_auto_transition"}
    engine.store.update_state(run_id, ship_attempt_count=ship_attempts + 1)
    from .ship import ship_run
    out = ship_run(engine, run_id)
    return {
        "advanced": True,
        "from": status,
        "action": "auto_shipped" if out.get("ok") else "auto_ship_retry_failed",
        "ship_attempts": ship_attempts + 1,
        "pr_url": (out.get("pr") or {}).get("url"),
    }


def _handle_retry_blocked(engine: Engine, run_id: str, status: str) -> dict[str, Any]:
    blocked_on = engine.store.state(run_id).get("blocked_on")
    if status == "checks_failed" and not blocked_on:
        blocked_on = BlockedReason.CHECKS
    elif status == "e2e_failed":
        blocked_on = BlockedReason.E2E
    if blocked_on not in (
        BlockedReason.SCOPE,
        BlockedReason.CHECKS,
        BlockedReason.E2E,
    ):
        return {"advanced": False, "from": status, "action": "no_auto_transition"}
    retry_count = engine.store.state(run_id).get("checks_retry_count", 0)
    if RetryPolicy(
        max_attempts=engine.cfg.checks.retry_checks,
    ).exhausted(retry_count):
        escalated = (
            Status.E2E_ESCALATED
            if status == "e2e_failed"
            else Status.CHECKS_ESCALATED
        )
        engine.store.update_state(run_id, status=escalated)
        return {
            "advanced": False,
            "from": status,
            "action": str(escalated),
            "retry_count": retry_count,
        }
    detail = (
        _e2e_failure_detail(engine.store, run_id)
        if blocked_on == "e2e"
        else _checks_failure_detail(engine.store, run_id)
    )
    label_word = "E2e tests" if blocked_on == "e2e" else "Checks"
    engine.store.artifact_path(run_id, "answers/build.md").parent.mkdir(
        parents=True, exist_ok=True,
    )
    this_attempt = (
        f"## Attempt {retry_count + 1}/{engine.cfg.checks.retry_checks}\n\n"
        f"{detail}\n\nFix the above and ensure {label_word.lower()} pass this time.\n"
    )
    answers_text = _accumulate_retry_feedback(
        engine.store, run_id, label_word, this_attempt,
    )
    engine.store.artifact_path(run_id, "answers/build.md").write_text(
        answers_text,
    )
    engine.store.update_state(
        run_id, checks_retry_count=retry_count + 1,
    )
    engine.run_agent_stage(run_id, "build", resume=True)
    return {
        "advanced": True,
        "from": status,
        "action": (
            "retry_build_after_e2e_failure"
            if status == "e2e_failed"
            else "retry_build_after_checks_failure"
        ),
        "retry_count": retry_count + 1,
    }


def _handle_checks_escalated(engine: Engine, run_id: str, status: str) -> dict[str, Any]:
    resolve_attempts = engine.store.state(run_id).get(
        "resolve_attempt_count", 0,
    )
    if RetryPolicy(
        max_attempts=engine.cfg.checks.resolve_attempts,
    ).exhausted(resolve_attempts):
        engine.store.update_state(run_id, status=Status.RESOLVE_ESCALATED)
        return {
            "advanced": False,
            "from": status,
            "action": "resolve_escalated",
            "resolve_attempts": resolve_attempts,
        }
    engine.store.update_state(
        run_id, resolve_attempt_count=resolve_attempts + 1,
    )
    result = engine.run_resolver_stage(run_id)
    new_status = engine.store.state(run_id).get("status")
    return {
        "advanced": True,
        "from": status,
        "action": "resolver_attempted",
        "resolve_attempts": resolve_attempts + 1,
        "verified_pass": result["verified_pass"],
        "new_status": new_status,
    }


def _always(_engine: Engine, _run_id: str, _status: str) -> bool:
    return True


def _guard_doc_auto_approve(engine: Engine, _run_id: str, status: str) -> bool:
    return (
        status.endswith("_complete")
        and status.removesuffix("_complete") in DOC_STAGES
        and engine.cfg.git.auto_approve_docs
    )


def _guard_explicit_v2(engine: Engine, run_id: str, _status: str) -> bool:
    return _uses_explicit_verification(engine, run_id)


def _guard_auto_ship(engine: Engine, _run_id: str, _status: str) -> bool:
    return bool(engine.cfg.git.auto_ship)


def _guard_auto_resolve(engine: Engine, _run_id: str, _status: str) -> bool:
    return bool(engine.cfg.checks.auto_resolve)


# Exact historical if-chain order — do not reorder without parity tests.
DISPATCH_RULES: tuple[DispatchRule, ...] = (
    DispatchRule("queued", lambda s: s == "queued", _always, _handle_queued),
    DispatchRule("awaiting", lambda s: s.startswith("awaiting_"), _always, _handle_awaiting),
    DispatchRule(
        "doc_auto_approve",
        lambda s: s.endswith("_complete"),
        _guard_doc_auto_approve,
        _handle_doc_auto_approve,
    ),
    DispatchRule("plan_complete", lambda s: s == "plan_complete", _always, _handle_plan_complete),
    DispatchRule("build_complete", lambda s: s == "build_complete", _always, _handle_build_complete),
    DispatchRule(
        "checks_passed",
        lambda s: s == "checks_passed",
        _guard_explicit_v2,
        _handle_checks_passed,
    ),
    DispatchRule(
        "e2e_done",
        lambda s: s in ("e2e_passed", "e2e_skipped"),
        _guard_explicit_v2,
        _handle_e2e_done,
    ),
    DispatchRule(
        "evidence_complete",
        lambda s: s == "evidence_complete",
        _always,
        _handle_evidence_complete,
    ),
    DispatchRule(
        "review_changes_requested",
        lambda s: s == "review_changes_requested",
        _always,
        _handle_review_changes,
    ),
    DispatchRule(
        "auto_ship",
        lambda s: s == "review_approved",
        _guard_auto_ship,
        _handle_auto_ship,
    ),
    DispatchRule(
        "ship_retry",
        lambda s: s == "ship_failed",
        _guard_auto_ship,
        _handle_ship_retry,
    ),
    DispatchRule(
        "retry_blocked",
        lambda s: s in ("blocked", "checks_failed", "e2e_failed"),
        _always,
        _handle_retry_blocked,
    ),
    DispatchRule(
        "checks_escalated",
        lambda s: s == "checks_escalated",
        _guard_auto_resolve,
        _handle_checks_escalated,
    ),
)


def execute_dispatch(engine: Engine, run_id: str) -> dict[str, Any]:
    """Run the ordered dispatch table for one automatic advance tick."""
    status = engine.store.state(run_id).get("status", "")
    for rule in DISPATCH_RULES:
        if not rule.matches(status):
            continue
        if rule.guard is not None and not rule.guard(engine, run_id, status):
            continue
        result = rule.handler(engine, run_id, status)
        if result is not None:
            return result
    return {"advanced": False, "from": status, "action": "no_auto_transition"}
