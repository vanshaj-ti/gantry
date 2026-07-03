#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from common import REPO, RUNS, load_json, run_dir, update_state, write_json
from telegram_bot import TelegramBotError, chat_id, get_updates, is_configured, send_message

STATE_DB = Path(__import__("os").environ.get("HERMES_STATE_DB", "/Users/vanshaj/.hermes/state.db"))
ANSWER_RE = re.compile(r"^\s*ANSWER\s+(?P<run_id>[A-Za-z0-9_.:-]+)\s+(?P<stage>plan|build|evidence)\s*:?\s*(?P<answer>.+)", re.I | re.S)
SLASH_ANSWER_RE = re.compile(r"^\s*/(?:answer|a)(?:@[A-Za-z0-9_]+)?(?:\s+(?P<body>.+))?\s*$", re.I | re.S)


def max_message_id() -> int:
    conn = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True, timeout=5)
    try:
        row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()
        return int(row[0] or 0)
    finally:
        conn.close()


def offset_key() -> str:
    return "last_seen_update_id" if is_configured() else "last_seen_message_id"


def current_offset() -> int:
    if is_configured():
        updates = get_updates()
        return max((int(u.get("update_id", 0)) for u in updates), default=0)
    return max_message_id()


def pending_runs() -> list[tuple[str, dict]]:
    out = []
    if not RUNS.exists():
        return out
    for state_path in RUNS.glob("*/state.json"):
        state = load_json(state_path, {}) or {}
        blocked_on = str(state.get("blocked_on") or "")
        is_question_block = "question" in blocked_on or state.get("telegram_question_sent") is True
        if state.get("status") == "blocked" and state.get("current_stage") in {"plan", "build", "evidence"} and is_question_block:
            out.append((state_path.parent.name, state))
    return out


def scan_harness_bot_updates(after_id: int) -> list[dict]:
    messages = []
    allowed_chat_id = chat_id()
    updates = get_updates(after_id + 1 if after_id else None)
    for update in updates:
        msg = update.get("message") or {}
        text = msg.get("text")
        if not text:
            continue
        message_chat_id = str((msg.get("chat") or {}).get("id") or "")
        if allowed_chat_id and message_chat_id != allowed_chat_id:
            continue
        messages.append(
            {
                "id": int(update.get("update_id") or 0),
                "telegram_message_id": msg.get("message_id"),
                "reply_to_message_id": (msg.get("reply_to_message") or {}).get("message_id"),
                "content": text,
                "timestamp": msg.get("date"),
                "source": "harness_telegram_bot",
                "chat_id": message_chat_id,
                "thread_id": None,
            }
        )
    return messages


def scan_hermes_messages(after_id: int) -> list[dict]:
    conn = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT m.id, m.content, m.timestamp, s.source, s.chat_id, s.thread_id
            FROM messages m
            JOIN sessions s ON s.id = m.session_id
            WHERE m.id > ? AND m.role = 'user' AND m.content IS NOT NULL
            ORDER BY m.id ASC
            """,
            (after_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def scan_messages(after_id: int) -> list[dict]:
    if is_configured():
        return scan_harness_bot_updates(after_id)
    return scan_hermes_messages(after_id)


def record_answer(run_id: str, stage: str, answer: str, message: dict) -> None:
    answers = run_dir(run_id) / "answers"
    answers.mkdir(parents=True, exist_ok=True)
    (answers / f"{stage}.md").write_text(answer.strip() + "\n")
    write_json(run_dir(run_id) / "answers" / f"{stage}.source.json", message)
    update_state(
        run_id,
        status="answer_received",
        current_stage=stage,
        answer_source=message.get("source", "telegram"),
        resume_stage=stage,
        telegram_answer_message_id=message.get("telegram_message_id") or message.get("id"),
    )


def resume_stage(run_id: str, stage: str) -> int:
    cmd = [sys.executable, str(REPO / "agent-harness" / "scripts" / "run_stage.py"), stage, "--run-id", run_id, "--resume"]
    proc = subprocess.run(cmd, cwd=str(REPO), capture_output=True, text=True, timeout=1200)
    logs = run_dir(run_id) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / f"{stage}.auto-resume.stdout").write_text(proc.stdout)
    (logs / f"{stage}.auto-resume.stderr").write_text(proc.stderr)
    return proc.returncode


def parse_answer(text: str, blocked: dict[str, dict]) -> tuple[str, str, str] | None:
    text = text.strip()
    m = ANSWER_RE.match(text)
    if m:
        return m.group("run_id"), m.group("stage").lower(), m.group("answer").strip()

    slash = SLASH_ANSWER_RE.match(text)
    if slash:
        body = (slash.group("body") or "").strip()
        if not body:
            return None
        parts = body.split(maxsplit=2)
        if len(parts) >= 3 and parts[1].lower() in {"plan", "build", "evidence"}:
            return parts[0], parts[1].lower(), parts[2].strip()
        if len(blocked) == 1:
            run_id, pending_state = next(iter(blocked.items()))
            stage = str(pending_state.get("current_stage") or "").lower()
            return run_id, stage, body
        return None

    if len(blocked) == 1 and text and not text.startswith("/"):
        run_id, pending_state = next(iter(blocked.items()))
        stage = str(pending_state.get("current_stage") or "").lower()
        return run_id, stage, text

    return None


def parse_reply_to_answer(message: dict, blocked: dict[str, dict]) -> tuple[str, str, str] | None:
    reply_to_message_id = message.get("reply_to_message_id")
    if reply_to_message_id is None:
        return None
    text = str(message.get("content") or "").strip()
    if not text or text.startswith("/"):
        return None
    for run_id, state in blocked.items():
        if state.get("telegram_question_transport") != "harness_bot":
            continue
        if state.get("telegram_question_message_id") == reply_to_message_id:
            stage = str(state.get("current_stage") or "").lower()
            return run_id, stage, text
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Watch Telegram answers to blocked harness questions")
    parser.add_argument("--since-id", type=int)
    parser.add_argument("--auto-resume", action="store_true")
    parser.add_argument("--state-file", default=str(RUNS / ".telegram-watcher-state.json"))
    args = parser.parse_args()

    state_file = Path(args.state_file)
    watcher_state = load_json(state_file, {}) or {}
    key = offset_key()
    after_id = args.since_id if args.since_id is not None else int(watcher_state.get(key, 0))
    blocked = {rid: st for rid, st in pending_runs()}
    if after_id == 0 and not blocked:
        after_id = current_offset()
        watcher_state[key] = after_id
        watcher_state["initialized_at"] = time.time()
        watcher_state["transport"] = "harness_bot" if is_configured() else "hermes_state_db"
        write_json(state_file, watcher_state)
        return 0

    if not blocked:
        watcher_state[key] = max(after_id, current_offset())
        write_json(state_file, watcher_state)
        return 0

    handled = []
    max_seen = after_id
    for msg in scan_messages(after_id):
        max_seen = max(max_seen, int(msg["id"]))
        text = str(msg.get("content") or "").strip()
        parsed = parse_reply_to_answer(msg, blocked) or parse_answer(text, blocked)
        if not parsed:
            continue
        run_id, stage, answer = parsed
        if not is_configured() and not text.upper().startswith("ANSWER ") and msg.get("source") != "telegram":
            continue
        if run_id not in blocked:
            continue
        if (blocked[run_id].get("current_stage") or "") != stage:
            continue
        if is_configured() and blocked[run_id].get("telegram_question_transport") != "harness_bot":
            continue
        record_answer(run_id, stage, answer, msg)
        rc = resume_stage(run_id, stage) if args.auto_resume else None
        if is_configured():
            try:
                status = "resumed" if rc == 0 else f"resume failed with exit code {rc}"
                send_message(f"✅ Answer received for {run_id}/{stage}; {status}.", reply_to_message_id=msg.get("telegram_message_id"))
            except TelegramBotError:
                pass
        handled.append({"run_id": run_id, "stage": stage, "message_id": msg["id"], "resume_exit_code": rc})

    watcher_state[key] = max_seen
    watcher_state["last_checked_at"] = time.time()
    watcher_state["transport"] = "harness_bot" if is_configured() else "hermes_state_db"
    if handled:
        watcher_state["last_handled"] = handled
    write_json(state_file, watcher_state)
    if handled:
        print(json.dumps({"handled": handled}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
