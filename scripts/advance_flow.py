#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
from typing import Any

from common import REPO, load_json, run_dir, update_state, write_json
from start_flow import ARCHITECTURE_DESIGN, ODIN_BOARD, PRODUCT_SPEC, THOR_BOARD, assert_board_idle, create_task, task_id


def run_command(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=str(REPO), capture_output=True, text=True, timeout=120)


def load_routing(run_id: str) -> dict[str, Any]:
    return load_json(run_dir(run_id) / "routing.json", {}) or {}


def save_routing(run_id: str, routing: dict[str, Any]) -> None:
    write_json(run_dir(run_id) / "routing.json", routing)


def comment_and_unblock(board: str, tid: str, comment: str) -> None:
    run_command(["hermes", "kanban", "--board", board, "comment", tid, comment, "--author", "default-hermes"])
    run_command(["hermes", "kanban", "--board", board, "unblock", tid, "--reason", comment])


def complete_task(board: str, tid: str, result: str) -> None:
    proc = run_command(["hermes", "kanban", "--board", board, "complete", tid, "--result", result])
    if proc.returncode != 0:
        raise SystemExit(f"Failed to complete {tid} on {board}:\n{proc.stdout}\n{proc.stderr}")


def create_architecture_task(run_id: str, routing: dict[str, Any]) -> dict[str, Any]:
    assert_board_idle(THOR_BOARD)
    title = routing.get("title") or run_id
    body = f"""Default Hermes approved the product specification for run `{run_id}`.

Read `.agent-runs/{run_id}/intake.md` and `.agent-runs/{run_id}/{PRODUCT_SPEC}`. Write the architecture design to `.agent-runs/{run_id}/{ARCHITECTURE_DESIGN}`.

Do not create app code. Use Thor architecture judgment: affected modules, constraints, data flow, test strategy, risks, and open questions.

When the artifact is ready, do NOT mark the task done. Instead block it for human review:
`hermes kanban --board {THOR_BOARD} block --kind needs_input <task_id> "Ready for human review: .agent-runs/{run_id}/{ARCHITECTURE_DESIGN}"`
"""
    task = create_task(THOR_BOARD, f"Architecture design: {title}", body, "thor-architect", f"{run_id}:architecture-design")
    routing.setdefault("tasks", {})["architecture_design"] = task
    routing.setdefault("stages", {}).setdefault("architecture_design", {})["task_id"] = task_id(task)
    return task


def main() -> int:
    parser = argparse.ArgumentParser(description="Advance a complete EduPaid agent-harness workflow through human gates")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--stage", required=True, choices=["product-spec", "architecture-design"])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--approve", action="store_true")
    group.add_argument("--revise", metavar="COMMENT")
    args = parser.parse_args()

    rdir = run_dir(args.run_id)
    routing = load_routing(args.run_id)
    tasks = routing.get("tasks") or {}

    if args.stage == "product-spec":
        tid = routing.get("stages", {}).get("product_spec", {}).get("task_id") or task_id(tasks.get("product_spec", {}))
        if not tid:
            raise SystemExit("No product spec task id found in routing.json")
        if args.revise:
            comment_and_unblock(ODIN_BOARD, tid, f"Revision requested: {args.revise}")
            update_state(args.run_id, status="product_spec_revision_requested", current_stage="product_spec")
        else:
            if not (rdir / PRODUCT_SPEC).exists():
                raise SystemExit(f"Cannot approve: missing .agent-runs/{args.run_id}/{PRODUCT_SPEC}")
            complete_task(ODIN_BOARD, tid, f"Approved product specification: .agent-runs/{args.run_id}/{PRODUCT_SPEC}")
            create_architecture_task(args.run_id, routing)
            update_state(args.run_id, status="awaiting_architecture_design", current_stage="architecture_design")

    if args.stage == "architecture-design":
        tid = routing.get("stages", {}).get("architecture_design", {}).get("task_id") or task_id(tasks.get("architecture_design", {}))
        if not tid:
            raise SystemExit("No architecture design task id found in routing.json")
        if args.revise:
            comment_and_unblock(THOR_BOARD, tid, f"Revision requested: {args.revise}")
            update_state(args.run_id, status="architecture_design_revision_requested", current_stage="architecture_design")
        else:
            if not (rdir / ARCHITECTURE_DESIGN).exists():
                raise SystemExit(f"Cannot approve: missing .agent-runs/{args.run_id}/{ARCHITECTURE_DESIGN}")
            complete_task(THOR_BOARD, tid, f"Approved architecture design: .agent-runs/{args.run_id}/{ARCHITECTURE_DESIGN}")
            update_state(args.run_id, status="ready_for_claude_plan", current_stage="claude_plan")

    save_routing(args.run_id, routing)
    print(json.dumps({"run_id": args.run_id, "stage": args.stage, "status": load_json(rdir / "state.json", {})}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
