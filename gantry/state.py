"""Run state and artifact storage.

State lives in the *target repo* under .agent-runs/<run_id>/. Gantry itself is
stateless — everything about a run is on disk in the target workspace, so runs
survive across invocations and machines.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

RUNS_DIRNAME = ".agent-runs"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", (text or "").lower()).strip("-")
    return slug[:60] or "run"


def _iso_to_ts(iso: str | None) -> float:
    if not iso:
        return 0.0
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return 0.0


class RunStore:
    def __init__(self, target_workspace: Path):
        self.target = target_workspace.resolve()
        self.runs = self.target / RUNS_DIRNAME

    def run_dir(self, run_id: str) -> Path:
        return self.runs / run_id

    def new_run_id(self, title: str, explicit: str | None = None) -> str:
        if explicit:
            return explicit
        ts = now_iso().replace(":", "").replace("-", "")[:15]
        return f"{ts}-{slugify(title)}"

    def create(self, run_id: str, title: str) -> Path:
        d = self.run_dir(run_id)
        (d / "logs").mkdir(parents=True, exist_ok=True)
        self.update_state(run_id, status="created", current_stage="created", title=title)
        return d

    def exists(self, run_id: str) -> bool:
        return (self.run_dir(run_id) / "state.json").exists()

    # --- generic json helpers ---
    def _load(self, path: Path, default: Any = None) -> Any:
        if not path.exists():
            return default
        return json.loads(path.read_text())

    def _write(self, path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")

    # --- state ---
    def state(self, run_id: str) -> dict[str, Any]:
        return self._load(self.run_dir(run_id) / "state.json", {}) or {}

    def update_state(self, run_id: str, **updates: Any) -> dict[str, Any]:
        path = self.run_dir(run_id) / "state.json"
        st = self._load(path, {}) or {}
        st.update(updates)
        st["updated_at"] = now_iso()
        self._write(path, st)
        return st

    # --- sessions (per-stage agent session ids for resume) ---
    def get_session_id(self, run_id: str, stage: str) -> str | None:
        sessions = self._load(self.run_dir(run_id) / "sessions.json", {}) or {}
        return (sessions.get(stage) or {}).get("session_id")

    def get_session(self, run_id: str, stage: str) -> dict[str, Any]:
        """Full per-stage session record — runner, model, session_id — for
        display (e.g. `gantry watch`'s detail column), not just the id."""
        sessions = self._load(self.run_dir(run_id) / "sessions.json", {}) or {}
        return sessions.get(stage) or {}

    def save_session(self, run_id: str, stage: str, **data: Any) -> None:
        path = self.run_dir(run_id) / "sessions.json"
        sessions = self._load(path, {}) or {}
        entry = sessions.get(stage, {})
        entry.update({k: v for k, v in data.items() if v is not None})
        sessions[stage] = entry
        self._write(path, sessions)

    # --- artifacts ---
    def artifact_path(self, run_id: str, name: str) -> Path:
        return self.run_dir(run_id) / name

    def read_artifact(self, run_id: str, name: str) -> str | None:
        p = self.artifact_path(run_id, name)
        return p.read_text(errors="ignore") if p.exists() else None

    def write_result(self, run_id: str, name: str, data: Any) -> Path:
        p = self.run_dir(run_id) / name
        self._write(p, data)
        return p

    def read_result(self, run_id: str, name: str) -> Any:
        return self._load(self.run_dir(run_id) / name, {})

    def write_log(self, run_id: str, name: str, content: str) -> None:
        (self.run_dir(run_id) / "logs" / name).write_text(content)

    # --- Telegram message -> run mapping (chat-scoped, not per-run) ---
    # Lets `gantry listen` resolve a Telegram *reply* to the exact run whose
    # notification is being replied to, instead of guessing "the most recent
    # run that needs input" — which breaks the moment two runs are stuck at
    # once. One flat file: bounded, prunes entries older than 30 days on write
    # so it never grows unbounded across a long-lived chat history.
    def _message_map_path(self) -> Path:
        return self.runs / "telegram-message-map.json"

    def record_telegram_message(self, message_id: int, run_id: str) -> None:
        path = self._message_map_path()
        data = self._load(path, {}) or {}
        data[str(message_id)] = {"run_id": run_id, "recorded_at": now_iso()}
        cutoff = datetime.now(timezone.utc).timestamp() - 30 * 86400
        data = {
            k: v for k, v in data.items()
            if _iso_to_ts(v.get("recorded_at")) >= cutoff
        }
        self._write(path, data)

    def run_for_telegram_message(self, message_id: int) -> str | None:
        data = self._load(self._message_map_path(), {}) or {}
        entry = data.get(str(message_id))
        return entry.get("run_id") if entry else None

    def list_runs(self) -> list[dict[str, Any]]:
        if not self.runs.exists():
            return []
        out = []
        for sf in self.runs.glob("*/state.json"):
            try:
                st = json.loads(sf.read_text())
                out.append({"id": sf.parent.name, "status": st.get("status", "unknown"),
                            "title": st.get("title", ""), "mtime": sf.stat().st_mtime})
            except Exception:
                pass
        return sorted(out, key=lambda x: x["mtime"], reverse=True)
