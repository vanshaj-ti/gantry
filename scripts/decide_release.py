#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json

from common import load_json, run_dir, update_state, write_json


def main() -> int:
    parser = argparse.ArgumentParser(description="Decide final harness release state")
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    rdir = run_dir(args.run_id)
    inputs = {
        "plan_eval": load_json(rdir / "harness" / "plan-evaluation.json", {}),
        "build_eval": load_json(rdir / "harness" / "build-evaluation.json", {}),
        "evidence_eval": load_json(rdir / "harness" / "evidence-evaluation.json", {}),
        "scope": load_json(rdir / "harness" / "scope.json", {}),
        "checks": load_json(rdir / "harness" / "checks.json", {}),
        "domain_rules": load_json(rdir / "harness" / "domain-rules.json", {}),
        "review": load_json(rdir / "review-result.json", {}),
    }
    failures = []
    for key in ["plan_eval", "build_eval", "evidence_eval", "scope", "checks", "domain_rules"]:
        val = inputs[key]
        if val and val.get("ok") is False:
            failures.append(key)
        if val and val.get("pass") is False:
            failures.append(key)
    review_text = str(inputs["review"].get("result", inputs["review"])) if inputs["review"] else ""
    review_verdict = str(inputs["review"].get("verdict", "")) if inputs["review"] else ""
    if review_verdict == "REQUEST_CHANGES" or (review_text and "REQUEST_CHANGES" in review_text):
        failures.append("review")
    if review_verdict == "ESCALATE" or (review_text and "ESCALATE" in review_text):
        failures.append("review_escalate")

    decision = "ship" if not failures else ("escalate" if "review_escalate" in failures else "hold")
    out = {"decision": decision, "failures": failures, "inputs_present": {k: bool(v) for k, v in inputs.items()}}
    write_json(rdir / "decision.json", out)
    update_state(args.run_id, status=decision, decision=decision)
    print(json.dumps(out, indent=2))
    return 0 if decision == "ship" else 2


if __name__ == "__main__":
    raise SystemExit(main())
