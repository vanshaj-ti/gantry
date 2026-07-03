#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
from typing import Any

from common import REPO, load_json, run_dir
from start_flow import ARCHITECTURE_DESIGN, PRODUCT_SPEC


def run_command(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=str(REPO), capture_output=True, text=True, timeout=120)


def main() -> int:
    parser = argparse.ArgumentParser(description="Trigger Claude Code plan stage after product/spec and architecture/design approval")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    rdir = run_dir(args.run_id)
    missing = [name for name in ["routing.json", "intake.md", PRODUCT_SPEC, ARCHITECTURE_DESIGN] if not (rdir / name).exists()]
    if missing:
        raise SystemExit(f"Cannot trigger Claude plan; missing artifact(s): {', '.join(missing)}")
    state = load_json(rdir / "state.json", {}) or {}
    if state.get("status") != "ready_for_claude_plan" and not args.dry_run:
        raise SystemExit(f"Run is not ready_for_claude_plan (status={state.get('status')!r})")

    cmd = ["python3", "agent-harness/scripts/run_stage.py", "plan", "--run-id", args.run_id]
    if args.dry_run:
        cmd.append("--dry-run")
    proc = run_command(cmd)
    print(proc.stdout or proc.stderr)
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
