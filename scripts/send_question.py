#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json

from common import load_json, run_command, run_dir, update_state
from telegram_bot import TelegramBotError, is_configured, send_message


def main() -> int:
    parser = argparse.ArgumentParser(description="Send a blocked harness question to Telegram")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--question-file", required=True)
    parser.add_argument("--to", default="telegram", help="Fallback hermes send target when dedicated harness bot env is not configured")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    q = load_json(run_dir(args.run_id) / args.question_file, None)
    qpath = run_dir(args.run_id) / args.question_file
    if q is None and qpath.exists():
        q = load_json(qpath)
    if q is None:
        # also allow absolute path
        import pathlib
        q = load_json(pathlib.Path(args.question_file))
    if q is None:
        raise SystemExit(f"Question file not found: {args.question_file}")

    message = (
        "🚧 Agent question — blocking\n\n"
        f"Run: {q.get('run_id', args.run_id)}\n"
        f"Stage: {q.get('stage')}\n\n"
        f"{q.get('question')}\n\n"
        "Reply directly to this message with your answer. The harness will resume the work."
    )
    if args.dry_run:
        print(message)
        return 0
    if is_configured():
        try:
            result = send_message(message)
        except TelegramBotError as exc:
            (run_dir(args.run_id) / "logs" / "telegram-question.stderr").write_text(str(exc))
            update_state(args.run_id, telegram_question_sent=False, telegram_question_transport="harness_bot")
            print(str(exc))
            return 1
        (run_dir(args.run_id) / "logs" / "telegram-question.stdout").write_text(json.dumps(result, indent=2) + "\n")
        sent_message = (result.get("result") or {}) if isinstance(result, dict) else {}
        update_state(
            args.run_id,
            telegram_question_sent=True,
            telegram_question_transport="harness_bot",
            telegram_question_message_id=sent_message.get("message_id"),
        )
        print(json.dumps({"sent": True, "transport": "harness_bot", "message_id": sent_message.get("message_id")}, indent=2))
        return 0

    proc = run_command(["hermes", "send", "--to", args.to, message], timeout=60)
    (run_dir(args.run_id) / "logs" / "telegram-question.stdout").write_text(proc.stdout)
    (run_dir(args.run_id) / "logs" / "telegram-question.stderr").write_text(proc.stderr)
    update_state(args.run_id, telegram_question_sent=(proc.returncode == 0), telegram_question_transport="hermes_gateway")
    print(proc.stdout or proc.stderr)
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
