"""Repair runs left in stale in-progress states."""
from __future__ import annotations

import time
from typing import Any

from .config import GantryConfig
from .engine import HEARTBEAT_INTERVAL, Engine
from .state import _iso_to_ts
from .status import FailureKind


def _stage_timeout(cfg: GantryConfig, stage: str) -> int:
    if stage == "checks":
        return cfg.checks.timeout
    if stage == "review":
        return 900
    return cfg.model_for(stage).timeout


def _repair_stale_running(engine: Engine, run: dict[str, Any]) -> dict[str, Any] | None:
    """Mark a running stage failed when its owning process is presumed dead."""
    status = run["status"]
    if not status.endswith("_running"):
        return None
    stage = status.removesuffix("_running")
    state = engine.store.state(run["id"])
    heartbeat_at = state.get("heartbeat_at")
    if heartbeat_at:
        age = time.time() - _iso_to_ts(heartbeat_at)
        grace = HEARTBEAT_INTERVAL * 6
        if age <= grace:
            return None
        detail = f"no heartbeat for {int(age)}s (interval {HEARTBEAT_INTERVAL}s)"
    else:
        timeout = _stage_timeout(engine.cfg, stage)
        age = time.time() - run["mtime"]
        if age <= timeout + 120:
            return None
        detail = f"stale for {int(age)}s (stage timeout {timeout}s), no heartbeat recorded"
    engine.store.write_log(
        run["id"], f"{stage}.stderr",
        f"Repaired stale status: {status} — {detail} — process presumed dead.",
    )
    engine._set_status(
        run["id"], f"{stage}_failed", last_failure_reason=FailureKind.STALE_HEARTBEAT,
    )
    return {
        "run_id": run["id"],
        "advanced": False,
        "action": "repaired_stale_running",
        "was": status,
        "age_seconds": int(age),
    }
