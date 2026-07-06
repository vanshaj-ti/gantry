"""Optional herdr integration.

herdr (https://herdr.dev) is a terminal-native agent multiplexer. When Gantry
runs inside a herdr-managed pane (HERDR_ENV=1), it can:

  1. Report its SEMANTIC pipeline stage to the herdr sidebar, so herdr shows
     "evidence_running" / "review_approved" instead of a generic working/done.
  2. Wait event-driven on an agent pane reaching `done`, instead of polling.

This is entirely opt-in and degrades to no-ops: if herdr isn't present, isn't
running, or [herdr].enabled is false, every call here silently does nothing.
Gantry is a *client* of herdr's socket — it never runs a socket server itself.

herdr pane ids (w1:p1) compact when panes close, so we never cache them; the
active focused pane is used when no explicit pane is given (herdr's default).
"""
from __future__ import annotations

import os
import shutil
import subprocess
from typing import Any

# Map Gantry status -> herdr semantic state (idle|working|blocked|done|unknown).
_STATE_MAP = {
    "plan_running": "working", "build_running": "working", "evidence_running": "working",
    "review_running": "working",
    "plan_complete": "done", "build_complete": "done", "evidence_complete": "done",
    "review_approved": "done",
    "review_changes_requested": "blocked", "review_escalated": "blocked", "blocked": "blocked",
    "awaiting_spec": "blocked", "awaiting_design": "blocked", "awaiting_plan": "idle",
}

SOURCE = "gantry"


def inside_herdr() -> bool:
    return os.environ.get("HERDR_ENV") == "1"


def _herdr_available() -> bool:
    return inside_herdr() and shutil.which("herdr") is not None


def report_state(run_id: str, status: str, *, enabled: bool = True,
                 pane: str | None = None) -> dict[str, Any]:
    """Report Gantry's pipeline stage to herdr's sidebar for the current pane.
    No-op unless enabled AND running inside herdr with the binary present."""
    if not enabled or not _herdr_available():
        return {"reported": False, "reason": "herdr-not-active"}
    state = _STATE_MAP.get(status, "unknown")
    # Use the raw socket method via `herdr api` if available; else the metadata CLI.
    # We report both semantic state and a visible custom_status (the run + status).
    cmd = ["herdr", "pane", "report-agent",
           "--source", SOURCE, "--agent", "gantry",
           "--state", state, "--custom-status", status[:32],
           "--message", f"{run_id}: {status}"]
    if pane:
        cmd += ["--pane", pane]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return {"reported": proc.returncode == 0, "state": state,
                "output": (proc.stdout + proc.stderr)[-300:]}
    except Exception as exc:
        return {"reported": False, "error": str(exc)}


def wait_for_done(pane: str, *, enabled: bool = True, timeout: int = 3600) -> dict[str, Any]:
    """Block until the given herdr pane's agent reaches `done` (event-driven).
    Returns immediately (no-op) if herdr isn't active. Callers should fall back
    to their own polling when this returns {'waited': False}."""
    if not enabled or not _herdr_available():
        return {"waited": False, "reason": "herdr-not-active"}
    try:
        proc = subprocess.run(["herdr", "wait", "agent-status", pane, "--status", "done"],
                              capture_output=True, text=True, timeout=timeout)
        return {"waited": True, "ok": proc.returncode == 0}
    except subprocess.TimeoutExpired:
        return {"waited": True, "ok": False, "reason": "timeout"}
    except Exception as exc:
        return {"waited": False, "error": str(exc)}
