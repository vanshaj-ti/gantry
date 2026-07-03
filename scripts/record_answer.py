#!/usr/bin/env python3
from __future__ import annotations

import argparse

from common import run_dir, update_state


def main() -> int:
    parser = argparse.ArgumentParser(description="Record a human answer for a blocked harness stage")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--stage", required=True, choices=["plan", "build", "evidence"])
    parser.add_argument("--answer", required=True)
    parser.add_argument("--source", default="manual")
    args = parser.parse_args()

    answers = run_dir(args.run_id) / "answers"
    answers.mkdir(parents=True, exist_ok=True)
    (answers / f"{args.stage}.md").write_text(args.answer.strip() + "\n")
    update_state(args.run_id, status="answer_received", current_stage=args.stage, answer_source=args.source, resume_stage=args.stage)
    print(f"Recorded answer for {args.run_id}/{args.stage}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
