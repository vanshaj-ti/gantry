#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json

from common import (
    PROMPTS,
    STAGES,
    get_stage_session_id,
    run_command,
    run_dir,
    save_stage_session,
    update_state,
    write_json,
)
from evaluate_result import evaluate


def render_prompt(stage: str, run_id: str) -> str:
    cfg = STAGES[stage]
    template = (PROMPTS / cfg["prompt"]).read_text()
    return template.replace("{RUN_ID}", run_id)


def answer_context(run_id: str, stage: str) -> str:
    answer_path = run_dir(run_id) / "answers" / f"{stage}.md"
    if not answer_path.exists():
        return ""
    return "\n\n# Human answer for this resumed stage\n" + answer_path.read_text()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one Claude Code harness stage")
    parser.add_argument("stage", choices=sorted(STAGES))
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--resume", action="store_true", help="Resume the stored Claude Code session for this run/stage")
    parser.add_argument("--dry-run", action="store_true", help="Print command metadata without invoking Claude")
    args = parser.parse_args()

    cfg = STAGES[args.stage]
    rdir = run_dir(args.run_id)
    if not rdir.exists():
        raise SystemExit(f"Run not found: {args.run_id}")

    prompt = render_prompt(args.stage, args.run_id) + (answer_context(args.run_id, args.stage) if args.resume else "")
    prompt_path = rdir / "logs" / f"{args.stage}-prompt{'-resume' if args.resume else ''}.md"
    prompt_path.write_text(prompt)

    cmd = [
        "claude",
        "-p",
        prompt,
        "--name",
        f"{args.run_id}-{args.stage}",
        "--agent",
        cfg["agent"],
        "--model",
        cfg["model"],
        "--output-format",
        "json",
        "--max-turns",
        cfg["max_turns"],
        "--dangerously-skip-permissions",
    ]
    if args.resume:
        session_id = get_stage_session_id(args.run_id, args.stage)
        if not session_id:
            raise SystemExit(f"No stored session_id for {args.run_id}/{args.stage}; cannot resume")
        cmd.extend(["--resume", session_id])

    if args.dry_run:
        print(json.dumps({"stage": args.stage, "run_id": args.run_id, "agent": cfg["agent"], "model": cfg["model"], "resume": args.resume, "prompt_path": str(prompt_path), "session_id": get_stage_session_id(args.run_id, args.stage)}, indent=2))
        return 0

    update_state(args.run_id, status=f"{args.stage}_running", current_stage=args.stage, resumed=args.resume)

    proc = run_command(cmd, timeout=900)
    suffix = ".resume" if args.resume else ""
    (rdir / "logs" / f"{args.stage}{suffix}.stdout").write_text(proc.stdout)
    (rdir / "logs" / f"{args.stage}{suffix}.stderr").write_text(proc.stderr)
    (rdir / "logs" / f"{args.stage}{suffix}.exit_code").write_text(str(proc.returncode))

    result_path = rdir / cfg["result_file"]
    try:
        data = json.loads(proc.stdout)
    except Exception:
        data = {"type": "result", "subtype": "invalid_json", "is_error": True, "result": proc.stdout[:4000], "stderr": proc.stderr[:4000], "exit_code": proc.returncode}
    write_json(result_path, data)
    save_stage_session(
        args.run_id,
        args.stage,
        {
            "session_id": data.get("session_id"),
            "uuid": data.get("uuid"),
            "agent": cfg["agent"],
            "model": cfg["model"],
            "session_name": f"{args.run_id}-{args.stage}",
            "last_result_file": cfg["result_file"],
        },
    )

    evaluation = evaluate(args.run_id, args.stage)
    print(json.dumps({"result_file": str(result_path), "evaluation": evaluation}, indent=2))
    return 0 if evaluation.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
