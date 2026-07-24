"""Auto-advance a Gantry run based on its current pipeline state.

Support helpers are re-exported here for backward compatibility.
"""
from __future__ import annotations

import logging
import os
import re
import json
from typing import Any

from .advance_batch import (
    AUTO_TRANSITIONS as AUTO_TRANSITIONS,
    _advance_one_run as _advance_one_run,
    advance_all as advance_all,
)
from .advance_lock import (
    _acquire_lock as _acquire_lock,
    _lock_path as _lock_path,
    _pid_alive as _pid_alive,
    _release_lock as _release_lock,
)
from .config import AGENT_STAGES, DOC_STAGES
from .checks import evaluate_checks
from .e2e import evaluate_e2e, run_e2e_tests
from .engine import Engine
from .failure_detail import (
    _checks_failure_detail,
    _e2e_failure_detail,
    _escape_md as _escape_md,
    _high_risk_detail as _high_risk_detail,
    _review_findings_detail as _review_findings_detail,
    _ship_checks_failure_detail as _ship_checks_failure_detail,
    _spec_gate_failure_detail as _spec_gate_failure_detail,
)
from .labels import (
    SHORT_STATUS_LABELS as SHORT_STATUS_LABELS,
    STATUS_LABELS as STATUS_LABELS,
    label as label,
    short_label as short_label,
)
from .notify_messages import (
    _STATUS_ICON as _STATUS_ICON,
    _icon as _icon,
    notify_message as notify_message,
)
from .retry import RetryPolicy
from .review import run_review
from .state import now_iso
from .stale_repair import (
    _repair_stale_running as _repair_stale_running,
    _stage_timeout as _stage_timeout,
)
from .status import BlockedReason, Status

logger = logging.getLogger(__name__)

_MAX_RETRY_ATTEMPTS_KEPT = 3


def _pipeline_version(engine: Engine, run_id: str) -> int:
    """Missing version means legacy embedded verification."""
    return int(engine.store.state(run_id).get("pipeline_version") or 1)


def _uses_explicit_verification(engine: Engine, run_id: str) -> bool:
    """Require both the v2 marker and its pinned stage snapshot.

    Phase 8 pipeline-policy evolution also increments ``pipeline_version`` on
    older runs. Requiring the pinned verification stages prevents such an
    in-flight legacy run from silently changing execution semantics.
    """
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
    """Keep a rolling history of recent automatic retry feedback."""
    existing = store.read_artifact(run_id, "answers/build.md") or ""
    prior_attempts = re.findall(
        r"(## Attempt \d+/\d+\n\n.*?)(?=\n## Attempt \d+/\d+\n\n|\Z)",
        existing,
        re.DOTALL,
    )
    attempts = (prior_attempts + [this_attempt])[-_MAX_RETRY_ATTEMPTS_KEPT:]
    header = f"# {label_word} failed — auto-retry history (most recent last)\n\n"
    return header + "\n".join(attempts)


def advance_run(engine: Engine, run_id: str) -> dict[str, Any]:
    """Fire the appropriate next stage for one run."""
    result = _advance_run_inner(engine, run_id)
    _sync_linear_status_if_configured(engine, run_id)
    return result


def _sync_linear_status_if_configured(engine: Engine, run_id: str) -> None:
    api_key = os.environ.get("GANTRY_LINEAR_API_KEY")
    team_id = os.environ.get("GANTRY_LINEAR_TEAM_ID")
    if not api_key or not team_id:
        return
    from .linear import sync_issue_status
    try:
        sync_issue_status(run_id, engine.store, team_id, api_key)
    except Exception:
        logger.warning("Linear status sync failed for run %s", run_id, exc_info=True)


def _advance_run_inner(engine: Engine, run_id: str) -> dict[str, Any]:
    status = engine.store.state(run_id).get("status", "")

    if status == "queued":
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

    if status.startswith("awaiting_"):
        stage = status.removeprefix("awaiting_")
        if stage in AGENT_STAGES or stage in DOC_STAGES:
            engine.run_agent_stage(run_id, stage)
            return {"advanced": True, "from": status, "action": f"start_{stage}"}
        return {"advanced": False, "from": status, "action": "no_auto_transition"}

    if (
        status.endswith("_complete")
        and status.removesuffix("_complete") in DOC_STAGES
        and engine.cfg.git.auto_approve_docs
    ):
        stage = status.removesuffix("_complete")
        nxt = engine.approve(run_id, stage)
        return {
            "advanced": True,
            "from": status,
            "action": f"auto_approved_{stage}->{nxt}",
        }

    if status == "plan_complete":
        # Shared implementation lineage: resume the plan session into build when
        # present; otherwise start fresh (artifact continuation).
        engine.run_agent_stage(run_id, "build", resume=True)
        return {"advanced": True, "from": status, "action": "build"}

    if status == "build_complete":
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
        # Evidence is a fresh session axis (approved topology) — never native-
        # resume a prior evidence agent id. Artifact context still flows via
        # the evidence prompt / prior report on disk.
        engine.run_agent_stage(run_id, "evidence", resume=False)
        return {
            "advanced": True,
            "from": status,
            "action": "checks_passed->evidence",
        }

    if status == "checks_passed" and _uses_explicit_verification(engine, run_id):
        return _run_v2_e2e(engine, run_id)

    if (
        status in ("e2e_passed", "e2e_skipped")
        and _uses_explicit_verification(engine, run_id)
    ):
        return _continue_after_verification(engine, run_id, status)

    if status == "evidence_complete":
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

    if status == "review_changes_requested":
        engine.run_agent_stage(run_id, "build", resume=True)
        return {"advanced": True, "from": status, "action": "resume_build"}

    if status == "review_approved" and engine.cfg.git.auto_ship:
        from .ship import ship_run
        out = ship_run(engine, run_id)
        return {
            "advanced": True,
            "from": status,
            "action": "auto_shipped" if out.get("ok") else "auto_ship_failed",
            "pr_url": (out.get("pr") or {}).get("url"),
        }

    if status == "ship_failed" and engine.cfg.git.auto_ship:
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

    if status in ("blocked", "checks_failed", "e2e_failed"):
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

    if status == "checks_escalated" and engine.cfg.checks.auto_resolve:
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

    return {"advanced": False, "from": status, "action": "no_auto_transition"}
