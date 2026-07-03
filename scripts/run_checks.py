#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json

from common import run_command, run_dir, update_state, write_json


def main() -> int:
    parser = argparse.ArgumentParser(description="Run selected verification commands for a harness run")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--command", action="append", default=[])
    args = parser.parse_args()

    commands = args.command or ["npm run build -- --help"]
    results = []
    ok = True
    logs = run_dir(args.run_id) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    for i, command in enumerate(commands, start=1):
        proc = run_command(["bash", "-lc", command], timeout=900)
        (logs / f"check-{i}.stdout").write_text(proc.stdout)
        (logs / f"check-{i}.stderr").write_text(proc.stderr)
        result = {"command": command, "exit_code": proc.returncode, "stdout_log": f"logs/check-{i}.stdout", "stderr_log": f"logs/check-{i}.stderr"}
        results.append(result)
        if proc.returncode != 0:
            ok = False
    out = {"pass": ok, "results": results}
    write_json(run_dir(args.run_id) / "harness" / "checks.json", out)
    update_state(args.run_id, checks="pass" if ok else "fail", **({} if ok else {"status": "blocked", "blocked_on": "checks"}))
    print(json.dumps(out, indent=2))
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
