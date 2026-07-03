#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VALHALLA_ROOT = Path(__file__).resolve().parents[1]

# The target project repo (e.g. EduPaid) is provided via environment variable,
# defaulting to the current working directory.
TARGET_WORKSPACE = Path(os.environ.get("TARGET_WORKSPACE", os.getcwd())).resolve()

RUNS = TARGET_WORKSPACE / ".agent-runs"
PROMPTS = VALHALLA_ROOT / "prompts"

STAGES = {
    "plan": {
        "prompt": "plan.md",
        "agent": "plan-opus",
        "model": "claude-teams-group/claude-opus",
        "max_turns": "40",
        "artifact": "implementation-plan.md",
        "result_file": "plan-result.json",
    },
    "build": {
        "prompt": "build.md",
        "agent": "build-haiku",
        "model": "claude-teams-group/claude-haiku-4-5-20251001",
        "max_turns": "80",
        "artifact": "build-summary.md",
        "result_file": "build-result.json",
    },
    "evidence": {
        "prompt": "evidence.md",
        "agent": "evidence-sonnet",
        "model": "claude-teams-group/claude-sonnet",
        "max_turns": "120",
        "artifact": "evidence-report.md",
        "result_file": "evidence-result.json",
    },
}

QUESTION_PATTERNS = [
    r"\?$",
    r"\bI need clarification\b",
    r"\bclarify\b",
    r"\bShould I\b",
    r"\bDo you want me to\b",
    r"\bPlease confirm\b",
    r"\bcannot proceed without\b",
    r"\bneed your input\b",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_dir(run_id: str) -> Path:
    return RUNS / run_id


def load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text())


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def session_path(run_id: str) -> Path:
    return run_dir(run_id) / "sessions.json"


def load_sessions(run_id: str) -> dict[str, Any]:
    return load_json(session_path(run_id), {}) or {}


def save_stage_session(run_id: str, stage: str, data: dict[str, Any]) -> None:
    sessions = load_sessions(run_id)
    entry = sessions.get(stage, {})
    entry.update({k: v for k, v in data.items() if v is not None})
    sessions[stage] = entry
    write_json(session_path(run_id), sessions)


def get_stage_session_id(run_id: str, stage: str) -> str | None:
    entry = load_sessions(run_id).get(stage) or {}
    return entry.get("session_id")


def update_state(run_id: str, **updates: Any) -> dict[str, Any]:
    path = run_dir(run_id) / "state.json"
    state = load_json(path, {}) or {}
    state.update(updates)
    state["updated_at"] = now_iso()
    write_json(path, state)
    return state


def is_question(text: str) -> bool:
    stripped = " ".join((text or "").strip().split())
    if not stripped:
        return False
    for pattern in QUESTION_PATTERNS:
        if re.search(pattern, stripped, flags=re.IGNORECASE):
            return True
    # Conservative: short final result containing a question mark is a question.
    return "?" in stripped and len(stripped) < 800


def run_command(cmd: list[str], cwd: Path = TARGET_WORKSPACE, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout)
