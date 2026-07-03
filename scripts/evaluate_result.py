#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys

from common import STAGES, is_question, load_json, run_dir, update_state, write_json


def evaluate(run_id: str, stage: str) -> dict:
    cfg = STAGES[stage]
    rdir = run_dir(run_id)
    result_path = rdir / cfg["result_file"]
    data = load_json(result_path)
    if data is None:
        return {"classification": "missing_result", "ok": False, "reason": f"Missing {result_path}"}

    result_text = data.get("result", "") if isinstance(data, dict) else ""
    subtype = data.get("subtype") if isinstance(data, dict) else None
    terminal_reason = data.get("terminal_reason") if isinstance(data, dict) else None
    artifact_path = rdir / cfg["artifact"]

    if subtype != "success":
        return {"classification": "claude_error", "ok": False, "subtype": subtype, "terminal_reason": terminal_reason, "result": result_text[:1000]}
    if is_question(result_text):
        question = {
            "run_id": run_id,
            "stage": stage,
            "question": result_text.strip(),
            "source": "claude_inline_result",
        }
        qpath = rdir / "questions" / f"{stage}-inline-question.json"
        write_json(qpath, question)
        update_state(run_id, status="blocked", current_stage=stage, blocked_on=str(qpath))
        return {"classification": "inline_question", "ok": False, "question_file": str(qpath), "question": result_text.strip()}
    if not artifact_path.exists() or not artifact_path.read_text().strip():
        update_state(run_id, status="blocked", current_stage=stage, blocked_on="missing_artifact")
        return {"classification": "missing_artifact", "ok": False, "artifact": str(artifact_path)}

    update_state(run_id, status=f"{stage}_complete", current_stage=stage)
    return {"classification": "artifact_complete", "ok": True, "artifact": str(artifact_path), "result": result_text[:1000]}


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate Claude Code stage JSON output")
    parser.add_argument("stage", choices=sorted(STAGES))
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    out = evaluate(args.run_id, args.stage)
    out_path = run_dir(args.run_id) / "harness" / f"{args.stage}-evaluation.json"
    write_json(out_path, out)
    print(json.dumps(out, indent=2))
    return 0 if out.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
