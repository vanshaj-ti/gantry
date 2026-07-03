#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from common import TARGET_WORKSPACE, RUNS, now_iso, update_state, write_json


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return slug[:60] or "run"


def main() -> int:
    parser = argparse.ArgumentParser(description="Create an agent harness run directory")
    parser.add_argument("--title", required=True)
    parser.add_argument("--task-class", default="unknown")
    parser.add_argument("--risk", default="medium")
    parser.add_argument("--run-id")
    parser.add_argument("--design", default="")
    args = parser.parse_args()

    timestamp = now_iso().replace(":", "").replace("-", "")[:15]
    run_id = args.run_id or f"{timestamp}-{slugify(args.title)}"
    path = RUNS / run_id
    path.mkdir(parents=True, exist_ok=True)
    (path / "logs").mkdir(exist_ok=True)
    (path / "harness").mkdir(exist_ok=True)

    routing = {
        "run_id": run_id,
        "title": args.title,
        "task_class": args.task_class,
        "risk": args.risk,
        "created_at": now_iso(),
        "controller": "hermes",
        "spec_artifact": "product-spec.md",
        "design_artifact": "architecture-design.md",
        "execution": {
            "plan": {"runtime": "claude-code", "model": "opus", "agent": "plan-opus"},
            "build": {"runtime": "claude-code", "model": "haiku", "agent": "build-haiku"},
            "evidence": {"runtime": "claude-code", "model": "sonnet", "agent": "evidence-sonnet"},
            "review": {"runtime": "hermes", "model": "openai-group/gpt-5.5"},
        },
    }
    write_json(path / "routing.json", routing)
    if args.design:
        (path / "architecture-design.md").write_text(args.design)
    else:
        (path / "architecture-design.md").write_text(f"# Architecture Design\n\n{args.title}\n")
    update_state(run_id, status="created", current_stage="created", title=args.title)
    print(json.dumps({"run_id": run_id, "path": str(path.relative_to(TARGET_WORKSPACE))}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
