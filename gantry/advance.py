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
