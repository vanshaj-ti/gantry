#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from common import TARGET_WORKSPACE, load_json, run_dir, update_state, write_json
from run_review import parse_verdict


def run_command(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=str(TARGET_WORKSPACE), capture_output=True, text=True, timeout=1200)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run/interpret the independent GPT-5.5 reviewer gate")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--skip-run", action="store_true", help="Use existing review-result.json instead of running GPT review")
    parser.add_argument("--dry-run", action="store_true", help="Build review prompt only; do not call GPT reviewer")
    args = parser.parse_args()

    rdir = run_dir(args.run_id)
    if not args.skip_run:
        proc = run_command(["python3", "agent-harness/scripts/run_review.py", "--run-id", args.run_id] + (["--dry-run"] if args.dry_run else []))
        print(proc.stdout or proc.stderr)
        if args.dry_run:
            return proc.returncode
        if proc.returncode != 0:
            update_state(args.run_id, status="review_failed", current_stage="review")
            return proc.returncode

    review = load_json(rdir / "review-result.json", {}) or {}
    review_text = str(review.get("result", review))
    verdict = str(review.get("verdict") or parse_verdict(review_text))
    write_json(rdir / "review-gate.json", {"verdict": verdict, "review_result_present": bool(review)})

    if verdict == "APPROVE":
        update_state(args.run_id, status="review_approved", current_stage="review", review_verdict=verdict)
        print(json.dumps({"verdict": verdict, "next_action": "run decide_release.py or summarize for ship decision"}, indent=2))
        return 0

    if verdict == "REQUEST_CHANGES":
        (rdir / "review-comments.md").write_text(review_text.strip() + "\n")
        update_state(args.run_id, status="review_changes_requested", current_stage="build", review_verdict=verdict)
        print(json.dumps({"verdict": verdict, "next_action": "resume Claude Code build with review-comments.md", "comments": str(rdir / "review-comments.md")}, indent=2))
        return 2

    update_state(args.run_id, status="review_escalated", current_stage="review", review_verdict=verdict)
    print(json.dumps({"verdict": verdict, "next_action": "human decision required"}, indent=2))
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
