#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from common import PROMPTS, run_command, run_dir, update_state, write_json, load_json


REVIEW_MODEL = "openai-group/gpt-5.5"
REVIEW_PROVIDER = "truefoundry"


def parse_verdict(text: str) -> str:
    upper = (text or "").upper()
    if "REQUEST_CHANGES" in upper:
        return "REQUEST_CHANGES"
    if "ESCALATE" in upper:
        return "ESCALATE"
    if "APPROVE" in upper or "APPROVED" in upper:
        return "APPROVE"
    return "ESCALATE"


def parse_session_id(text: str) -> str | None:
    for line in (text or "").splitlines():
        if line.startswith("session_id:"):
            value = line.split(":", 1)[1].strip()
            return value or None
    return None


def reviewer_session_path(run_id: str) -> Path:
    return run_dir(run_id) / "reviewer-session.json"


def build_review_prompt(run_id: str) -> str:
    rdir = run_dir(run_id)
    template = (PROMPTS / "review.md").read_text().replace("{RUN_ID}", run_id)
    diff = run_command(["git", "diff", "origin/staging", "--"], timeout=120).stdout[:50000]
    artifacts = []
    for name in [
        "routing.json",
        "intake.md",
        "product-spec.md",
        "architecture-design.md",
        "implementation-plan.md",
        "build-summary.md",
        "evidence-report.md",
        "harness/scope.json",
        "harness/checks.json",
        "harness/domain-rules.json",
    ]:
        p = rdir / name
        if p.exists():
            artifacts.append(f"\n--- {name} ---\n{p.read_text(errors='ignore')[:12000]}")
        else:
            artifacts.append(f"\n--- {name} ---\n<MISSING>")
    return template + "\n\n# Artifacts\n" + "\n".join(artifacts) + "\n\n# Git diff vs origin/staging\n" + diff


def main() -> int:
    parser = argparse.ArgumentParser(description="Run independent GPT review via Hermes/TrueFoundry")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    rdir = run_dir(args.run_id)
    prompt = build_review_prompt(args.run_id)
    (rdir / "logs" / "review-prompt.md").write_text(prompt)
    if args.dry_run:
        print(json.dumps({"prompt_path": str(rdir / "logs" / "review-prompt.md"), "model": REVIEW_MODEL}, indent=2))
        return 0

    session_state = load_json(reviewer_session_path(args.run_id), {}) or {}
    cmd = ["hermes", "chat", "-q", prompt, "--provider", REVIEW_PROVIDER, "--model", REVIEW_MODEL, "-Q"]
    reused_existing_session = bool(session_state.get("session_id"))
    if reused_existing_session:
        cmd.extend(["--resume", str(session_state["session_id"])])

    proc = run_command(cmd, timeout=900)
    session_id = parse_session_id(proc.stdout) or parse_session_id(proc.stderr) or session_state.get("session_id")
    if session_id:
        write_json(
            reviewer_session_path(args.run_id),
            {
                "session_id": session_id,
                "model": REVIEW_MODEL,
                "provider": REVIEW_PROVIDER,
                "reused_existing_session": reused_existing_session,
            },
        )

    verdict = parse_verdict(proc.stdout) if proc.returncode == 0 else "ESCALATE"
    result = {
        "exit_code": proc.returncode,
        "result": proc.stdout,
        "stderr": proc.stderr,
        "model": REVIEW_MODEL,
        "provider": REVIEW_PROVIDER,
        "verdict": verdict,
        "session_id": session_id,
        "reused_existing_session": reused_existing_session,
    }
    write_json(rdir / "review-result.json", result)
    update_state(args.run_id, review="complete" if proc.returncode == 0 else "failed", review_verdict=verdict)
    print(json.dumps(result, indent=2)[:4000])
    return 0 if proc.returncode == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
