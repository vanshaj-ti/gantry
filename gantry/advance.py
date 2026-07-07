"""Auto-advance: drive the pipeline forward one tick based on run state.

This is the engine-side of what used to be edupaid-auto-advancer.py. It inspects
a run's status and fires the next stage automatically, for the transitions that
don't require a human gate:

  plan_complete            -> run build
  build_complete           -> run checks; if pass -> run evidence
  evidence_complete        -> run review
  review_changes_requested -> resume build (with review-comments.md)

Human-gated transitions (awaiting_spec, awaiting_design, review_escalated,
blocked) are intentionally NOT auto-advanced — they wait for `gantry approve`.

`advance_run` runs synchronously (used by `gantry advance --run ID`).
`advance_all` ticks every run once (used by the poller cron).
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from .config import GantryConfig
from .engine import Engine
from .review import run_review

# Transitions the poller drives automatically (no human gate).
AUTO_TRANSITIONS = {
    "plan_complete", "build_complete", "evidence_complete", "review_changes_requested",
}

# Human-friendly status labels for notifications.
STATUS_LABELS = {
    "awaiting_spec": "Awaiting product spec (human)",
    "awaiting_design": "Awaiting architecture design (human)",
    "awaiting_plan": "Ready to plan",
    "plan_running": "Writing implementation plan",
    "plan_complete": "Plan complete",
    "build_running": "Building & testing",
    "build_complete": "Build complete",
    "evidence_running": "Generating evidence",
    "evidence_complete": "Evidence complete",
    "review_running": "Independent review in progress",
    "review_approved": "Review APPROVED — ready to ship",
    "review_changes_requested": "Review requested changes — rebuilding",
    "review_escalated": "Review ESCALATED — human decision needed",
    "blocked": "Blocked — needs input",
}


def label(status: str) -> str:
    return STATUS_LABELS.get(status, status)


def notify_message(store: Any, run_id: str, status: str, result: dict[str, Any] | None = None) -> str:
    """Build a self-contained Telegram body: what happened, why, and what to do next.

    A bare status label ("Blocked — needs input") tells you nothing you can act
    on away from a terminal. Every message below states the concrete failure
    (or agent question) and the exact command to run next.
    """
    header = f"[{run_id}] {label(status)}"
    result = result or {}

    if status == "blocked":
        blocked_on = store.state(run_id).get("blocked_on", "checks")
        checks = store.read_result(run_id, "checks.json")
        detail = ""
        if checks and not checks.get("pass"):
            scope = checks.get("scope", {})
            if scope.get("forbidden_files") or scope.get("unexpected_files"):
                bad = scope.get("forbidden_files", []) + scope.get("unexpected_files", [])
                detail = "Scope violation — files outside the plan: " + ", ".join(bad[:8])
            else:
                failing = [c["command"] for c in checks.get("checks", {}).get("results", []) if not c.get("pass")]
                detail = "Failing command(s): " + ", ".join(failing) if failing else "Checks failed (see checks.json for detail)."
        return (f"{header}\n"
                f"Blocked on: {blocked_on}\n"
                f"{detail}\n"
                f"Options: (1) fix the issue and run `gantry checks --run {run_id}` to re-check and unblock, "
                f"or (2) `gantry revise --run {run_id} --stage build \"<comments>\"` to send it back with guidance.")

    if status.endswith("_failed"):
        subtype = result.get("raw", {}).get("subtype") if isinstance(result.get("raw"), dict) else None
        agent_text = (result.get("raw", {}).get("result") or "")[:500] if isinstance(result.get("raw"), dict) else ""
        if subtype == "error_max_turns":
            return (f"{header}\n"
                    f"Ran out of turns before finishing the stage.\n"
                    f"Options: (1) raise [models.{status.removesuffix('_failed')}].max_turns in gantry.toml and "
                    f"`gantry stage {status.removesuffix('_failed')} --run {run_id} --resume` to continue the same "
                    f"session, or (2) inspect .agent-runs/{run_id}/logs/ and decide manually.")
        return (f"{header}\n"
                f"Stage errored (subtype={subtype or 'unknown'}).\n"
                f"{agent_text}\n"
                f"Options: (1) `gantry stage {status.removesuffix('_failed')} --run {run_id} --resume` to retry, "
                f"or (2) inspect .agent-runs/{run_id}/logs/ for the full transcript.")

    if status == "review_escalated":
        verdict = result.get("verdict") or store.read_result(run_id, "review.json")
        note = verdict.get("note", "") if isinstance(verdict, dict) else ""
        return (f"{header}\n{note[:800]}\n"
                f"Options: (1) `gantry approve --run {run_id} --stage review` to accept as-is, or "
                f"(2) `gantry revise --run {run_id} --stage build \"<comments>\"` to send back for changes.")

    # Agent asked a clarifying question mid-stage instead of erroring (common on
    # plan/build when the request under-specifies an architecture decision).
    agent_text = ""
    if isinstance(result.get("raw"), dict):
        agent_text = (result["raw"].get("result") or "")
    if agent_text and "?" in agent_text[-200:]:
        return (f"{header}\n"
                f"Agent has a question before continuing:\n{agent_text[:800]}\n"
                f"Reply by writing .agent-runs/{run_id}/answers/{{stage}}.md with your decision, then "
                f"`gantry stage {{stage}} --run {run_id} --resume`.")

    return header


def advance_run(engine: Engine, run_id: str) -> dict[str, Any]:
    """Fire the appropriate next stage for a single run based on its status.
    Returns {advanced: bool, from, action, ...}. No-op for gated/terminal states."""
    status = engine.store.state(run_id).get("status", "")

    if status == "plan_complete":
        engine.run_agent_stage(run_id, "build")
        return {"advanced": True, "from": status, "action": "build"}

    if status == "build_complete":
        checks = engine.run_checks(run_id)
        if not checks["pass"]:
            return {"advanced": False, "from": status, "action": "checks_failed",
                    "blocked_on": engine.store.state(run_id).get("blocked_on")}
        engine.run_agent_stage(run_id, "evidence")
        return {"advanced": True, "from": status, "action": "checks_passed->evidence"}

    if status == "evidence_complete":
        if engine.cfg.review.enabled:
            out = run_review(engine.store, run_id, engine.cfg, engine.work_dir(run_id))
            return {"advanced": True, "from": status, "action": "review", "verdict": out["verdict"]}
        return {"advanced": False, "from": status, "action": "review_disabled"}

    if status == "review_changes_requested":
        engine.run_agent_stage(run_id, "build", resume=True)
        return {"advanced": True, "from": status, "action": "resume_build"}

    return {"advanced": False, "from": status, "action": "no_auto_transition"}


def _lock_path(engine: Engine, run_id: str) -> Path:
    return engine.store.run_dir(run_id) / ".advance.lock"


def _acquire_lock(engine: Engine, run_id: str, stale_after: int = 1800) -> bool:
    """Best-effort lock so a slow-running stage (build/evidence routinely take
    minutes) doesn't get double-fired by the next 60s cron tick. Stale locks
    (crashed process, stale_after seconds old) are reclaimed automatically."""
    lock = _lock_path(engine, run_id)
    if lock.exists():
        try:
            age = time.time() - lock.stat().st_mtime
            if age < stale_after:
                return False
        except OSError:
            pass
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(str(os.getpid()))
    return True


def _release_lock(engine: Engine, run_id: str) -> None:
    _lock_path(engine, run_id).unlink(missing_ok=True)


def advance_all(target: Path, cfg: GantryConfig) -> list[dict[str, Any]]:
    engine = Engine(target, cfg)
    results = []
    for run in engine.store.list_runs():
        rid = run["id"]
        if run["status"] not in AUTO_TRANSITIONS:
            continue
        if not _acquire_lock(engine, rid):
            results.append({"run_id": rid, "advanced": False, "action": "skipped_locked"})
            continue
        try:
            results.append({"run_id": rid, **advance_run(engine, rid)})
        except Exception as exc:
            results.append({"run_id": rid, "advanced": False, "error": str(exc)})
        finally:
            _release_lock(engine, rid)
    return results
