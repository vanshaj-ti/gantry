"""Batch scheduling for automatic pipeline advancement."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import AGENT_STAGES, DOC_STAGES, GantryConfig
from .engine import Engine
from .retry import RetryPolicy
from .status import FailureKind, Status

# Transitions the poller drives automatically (no human gate).
AUTO_TRANSITIONS = {
    "plan_complete", "build_complete", "evidence_complete", "review_changes_requested",
    "blocked", "queued",
    *(f"awaiting_{stage}" for stage in AGENT_STAGES),
    *(f"awaiting_{stage}" for stage in DOC_STAGES),
}


def _advance_one_run(
    engine: Engine, run: dict, cfg: GantryConfig,
) -> dict[str, Any] | None:
    """Process one batch candidate while holding its per-run lock."""
    from . import advance as advance_module

    auto_transitions = (
        AUTO_TRANSITIONS
        | ({"review_approved", "ship_failed"} if cfg.git.auto_ship else set())
        | ({"checks_escalated"} if cfg.checks.auto_resolve else set())
        | (
            {f"{stage}_complete" for stage in DOC_STAGES}
            if cfg.git.auto_approve_docs
            else set()
        )
    )
    rid = run["id"]
    advance_module._sync_linear_status_if_configured(engine, rid)
    repaired = advance_module._repair_stale_running(engine, run)
    if repaired:
        return repaired
    if (
        run["status"].endswith("_failed")
        and engine.store.state(rid).get("last_failure_reason") == FailureKind.STALE_HEARTBEAT
    ):
        stage = run["status"].removesuffix("_failed")
        if stage == "resolve":
            resolve_attempts = engine.store.state(rid).get("resolve_attempt_count", 0)
            if RetryPolicy(
                max_attempts=engine.cfg.checks.resolve_attempts,
            ).exhausted(resolve_attempts):
                engine.store.update_state(
                    rid, status=Status.RESOLVE_ESCALATED, last_failure_reason=None,
                )
                return {
                    "run_id": rid,
                    "advanced": False,
                    "action": "resolve_escalated",
                    "resolve_attempts": resolve_attempts,
                }
        if not advance_module._acquire_lock(engine, rid):
            return {"run_id": rid, "advanced": False, "action": "skipped_locked"}
        try:
            engine.store.update_state(rid, last_failure_reason=None)
            if stage == "resolve":
                engine.store.update_state(
                    rid, resolve_attempt_count=resolve_attempts + 1,
                )
                engine.run_resolver_stage(rid)
            elif stage == "review":
                advance_module.run_review(
                    engine.store, rid, engine.cfg, engine.work_dir(rid),
                )
            else:
                engine.run_agent_stage(rid, stage, resume=False)
            return {
                "run_id": rid,
                "advanced": True,
                "action": f"retry_after_stale_heartbeat_{stage}",
            }
        except Exception as exc:
            return {"run_id": rid, "advanced": False, "error": str(exc)}
        finally:
            advance_module._release_lock(engine, rid)
    if run["status"] not in auto_transitions:
        return None
    if not advance_module._acquire_lock(engine, rid):
        return {"run_id": rid, "advanced": False, "action": "skipped_locked"}
    try:
        return {"run_id": rid, **advance_module.advance_run(engine, rid)}
    except Exception as exc:
        return {"run_id": rid, "advanced": False, "error": str(exc)}
    finally:
        advance_module._release_lock(engine, rid)


def advance_all(
    target: Path, cfg: GantryConfig, tag: str | None = None,
) -> list[dict[str, Any]]:
    engine = Engine(target, cfg)
    candidates = [r for r in engine.store.list_runs() if not tag or r.get("tag") == tag]

    if cfg.agent.max_concurrent and cfg.agent.max_concurrent > 1:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=cfg.agent.max_concurrent) as pool:
            futures = [pool.submit(_advance_one_run, engine, run, cfg) for run in candidates]
            results = [r for f in futures if (r := f.result()) is not None]
    else:
        results = []
        for run in candidates:
            r = _advance_one_run(engine, run, cfg)
            if r is not None:
                results.append(r)
    return results
