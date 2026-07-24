"""Optional herdr integration.

herdr (https://herdr.dev) is a terminal-native agent multiplexer. When Gantry
runs inside a herdr-managed pane (HERDR_ENV=1), it can:

  1. Report its SEMANTIC pipeline stage to the herdr sidebar — both the fixed
     idle/working/blocked state herdr's own agent-state machine needs, and a
     human-readable title/status ("MFA admin — Build complete" instead of a
     raw run_id + status string) via report-metadata for the visible display.
  2. Wait event-driven on an agent pane reaching a state, instead of polling.

This is entirely opt-in and degrades to no-ops: if herdr isn't present, isn't
running, or [herdr].enabled is false, every call here silently does nothing.
Gantry is a *client* of herdr's socket/CLI — it never runs a socket server.

herdr pane ids (w1:p1) compact when panes close, so we never cache them across
runs: `herdr pane current` resolves the live focused-pane id on every call, per
herdr's own "discover yourself" guidance (there is no self-identifying env var).

herdr's `pane report-agent --state` only accepts idle|working|blocked|unknown.
"done" is NOT settable directly — herdr derives it itself from idle-after-working
screen detection ("done means the agent finished, but you have not looked at
that finished pane yet"). So Gantry's *_complete/*_approved map to idle, and
`herdr wait agent-status --status done` is used to observe the derived state.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any

_STATE_MAP = {
    "spec_running": "working", "design_running": "working", "definition_running": "working",
    "plan_running": "working", "build_running": "working", "evidence_running": "working",
    "review_running": "working", "checks_running": "working", "e2e_running": "working",
    "spec_complete": "idle", "design_complete": "idle", "definition_complete": "idle",
    "plan_complete": "idle", "build_complete": "idle", "evidence_complete": "idle",
    "review_approved": "idle", "checks_passed": "idle", "e2e_passed": "idle",
    "e2e_skipped": "idle",
    "spec_failed": "blocked", "design_failed": "blocked", "definition_failed": "blocked",
    "plan_failed": "blocked", "build_failed": "blocked", "evidence_failed": "blocked",
    "review_changes_requested": "blocked", "review_escalated": "blocked", "blocked": "blocked",
    "checks_failed": "blocked", "e2e_failed": "blocked", "e2e_escalated": "blocked",
    "awaiting_spec": "blocked", "awaiting_design": "blocked",
    "awaiting_definition": "blocked", "awaiting_plan": "idle",
}

SOURCE = "gantry"


def inside_herdr() -> bool:
    return os.environ.get("HERDR_ENV") == "1"


def _herdr_available() -> bool:
    return inside_herdr() and shutil.which("herdr") is not None


def _current_pane() -> str | None:
    """Resolve the live pane id for the pane Gantry is running in.
    Never cached: herdr ids compact when other panes close."""
    try:
        proc = subprocess.run(["herdr", "pane", "current"],
                              capture_output=True, text=True, timeout=10)
        if proc.returncode != 0:
            return None
        return json.loads(proc.stdout)["result"]["pane"]["pane_id"]
    except Exception:
        return None


def report_state(run_id: str, status: str, *, title: str = "", enabled: bool = True,
                 pane: str | None = None) -> dict[str, Any]:
    """Report Gantry's pipeline stage to herdr's sidebar for the current pane.
    No-op unless enabled AND running inside herdr with the binary present.

    Two calls, not one: `report-agent` drives herdr's own idle/working/blocked
    state machine (what `wait_for_done` observes below) — it needs a value from
    that fixed enum, so it can't carry a human-readable label. `report-metadata`
    is display-only and takes free-text title/custom-status, which is what
    actually makes the sidebar entry readable at a glance instead of showing a
    raw run_id and status string like `20260707T091143-soc2-admin-mfa: blocked`.
    """
    if not enabled or not _herdr_available():
        return {"reported": False, "reason": "herdr-not-active"}
    pane_id = pane or _current_pane()
    if not pane_id:
        return {"reported": False, "reason": "could-not-resolve-pane"}
    state = _STATE_MAP.get(status, "unknown")
    try:
        from .labels import label as _label
        human_status = _label(status)
    except Exception:
        human_status = status
    display_title = f"{title} — {human_status}" if title else human_status

    agent_cmd = ["herdr", "pane", "report-agent", pane_id,
                 "--source", SOURCE, "--agent", "gantry",
                 "--state", state, "--message", f"{run_id}: {status}"]
    meta_cmd = ["herdr", "pane", "report-metadata", pane_id,
                "--source", SOURCE, "--agent", "gantry",
                "--title", display_title[:80], "--custom-status", human_status[:32]]
    try:
        agent_proc = subprocess.run(agent_cmd, capture_output=True, text=True, timeout=15)
        meta_proc = subprocess.run(meta_cmd, capture_output=True, text=True, timeout=15)
        return {"reported": agent_proc.returncode == 0 and meta_proc.returncode == 0,
                "state": state, "pane": pane_id, "title": display_title,
                "output": (agent_proc.stdout + agent_proc.stderr + meta_proc.stdout + meta_proc.stderr)[-300:]}
    except Exception as exc:
        return {"reported": False, "error": str(exc)}


def wait_for_done(pane: str | None = None, *, enabled: bool = True,
                  timeout: int = 3600) -> dict[str, Any]:
    """Block until the given herdr pane's agent reaches `done` (event-driven,
    herdr-derived state). Returns immediately (no-op) if herdr isn't active.
    Callers should fall back to their own polling when 'waited' is False.
    `timeout` is in seconds; herdr's --timeout flag wants milliseconds."""
    if not enabled or not _herdr_available():
        return {"waited": False, "reason": "herdr-not-active"}
    pane_id = pane or _current_pane()
    if not pane_id:
        return {"waited": False, "reason": "could-not-resolve-pane"}
    try:
        proc = subprocess.run(
            ["herdr", "wait", "agent-status", pane_id, "--status", "done",
             "--timeout", str(timeout * 1000)],
            capture_output=True, text=True, timeout=timeout + 10)
        return {"waited": True, "ok": proc.returncode == 0, "pane": pane_id}
    except subprocess.TimeoutExpired:
        return {"waited": True, "ok": False, "reason": "timeout"}
    except Exception as exc:
        return {"waited": False, "error": str(exc)}
