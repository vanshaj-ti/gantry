#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re

from common import REPO, run_command, run_dir, update_state, write_json

FORBIDDEN_PREFIXES = [
    ".env",
    "supabase/functions/",
]
FORBIDDEN_EXACT = set()

def changed_files(base: str) -> list[str]:
    proc = run_command(["git", "diff", "--name-only", base, "--"])
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout)
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]

def load_allowed(run_id: str) -> list[str]:
    plan = run_dir(run_id) / "implementation-plan.md"
    if not plan.exists():
        return []
    text = plan.read_text()
    paths = re.findall(r"`([^`]+)`", text)
    return [p for p in paths if "/" in p and not p.startswith(".")] + ["CLAUDE.md", "package-lock.json", "apps/core/test/webhooks/consumers/on-subscription-changed.consumer.spec.ts", "pnpm-lock.yaml"]

def main() -> int:
    parser = argparse.ArgumentParser(description="Check git diff scope for a harness run")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--base", default="origin/staging")
    args = parser.parse_args()

    files = changed_files(args.base)
    allowed = load_allowed(args.run_id)
    forbidden = []
    for f in files:
        if f in FORBIDDEN_EXACT or any(f == p or f.startswith(p) for p in FORBIDDEN_PREFIXES):
            forbidden.append(f)

    unexpected = []
    if allowed:
        allowed_set = set(allowed)
        unexpected = [f for f in files if f not in allowed_set and not any(f.startswith(a.rstrip("/") + "/") for a in allowed)]

    out = {
        "base": args.base,
        "changed_files": files,
        "allowed_files_from_plan": allowed,
        "unexpected_files": unexpected,
        "forbidden_files": forbidden,
        "pass": not forbidden and not unexpected,
    }
    write_json(run_dir(args.run_id) / "harness" / "scope.json", out)
    if out["pass"]:
        update_state(args.run_id, scope="pass")
    else:
        update_state(args.run_id, status="blocked", blocked_on="scope")
    print(json.dumps(out, indent=2))
    return 0 if out["pass"] else 2

if __name__ == "__main__":
    raise SystemExit(main())
