#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from common import TARGET_WORKSPACE, run_command, run_dir, update_state, write_json

RULES = [
    {"id": "no-console-log-core", "pattern": r"console\.(log|error|warn)", "paths": ["apps/core/src/"], "severity": "fail"},
    {"id": "no-edge-functions", "path_prefix": "supabase/functions/", "severity": "fail"},
    {"id": "no-env-files", "path_prefix": ".env", "severity": "fail"},
]


def changed_files(base: str) -> list[str]:
    proc = run_command(["git", "diff", "--name-only", base, "--"])
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description="Run EduPaid domain hard-rule checks")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--base", default="origin/staging")
    args = parser.parse_args()

    files = changed_files(args.base)
    findings = []
    for f in files:
        for rule in RULES:
            if rule.get("path_prefix") and f.startswith(rule["path_prefix"]):
                findings.append({"rule": rule["id"], "file": f, "severity": rule["severity"]})
            if rule.get("pattern") and any(f.startswith(p) for p in rule.get("paths", [""])):
                path = TARGET_WORKSPACE / f
                if path.exists() and re.search(rule["pattern"], path.read_text(errors="ignore")):
                    findings.append({"rule": rule["id"], "file": f, "severity": rule["severity"]})
    out = {"pass": not any(x["severity"] == "fail" for x in findings), "findings": findings}
    write_json(run_dir(args.run_id) / "harness" / "domain-rules.json", out)
    update_state(args.run_id, domain_rules="pass" if out["pass"] else "fail", **({} if out["pass"] else {"status": "blocked", "blocked_on": "domain_rules"}))
    print(json.dumps(out, indent=2))
    return 0 if out["pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
