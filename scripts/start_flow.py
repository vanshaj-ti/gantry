#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any

from common import TARGET_WORKSPACE, RUNS, now_iso, run_dir, update_state, write_json

ODIN_BOARD = "edupaid-odin"
THOR_BOARD = "edupaid-thor"
PRODUCT_SPEC = "product-spec.md"
ARCHITECTURE_DESIGN = "architecture-design.md"


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return slug[:60] or "run"


def run_command(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=str(TARGET_WORKSPACE), capture_output=True, text=True, timeout=120)


def ensure_board(slug: str, name: str) -> None:
    proc = run_command([
        "hermes",
        "kanban",
        "boards",
        "create",
        slug,
        "--name",
        name,
        "--default-workdir",
        str(TARGET_WORKSPACE),
    ])
    if proc.returncode != 0 and "already" not in (proc.stderr + proc.stdout).lower():
        raise SystemExit(f"Failed to create board {slug}:\n{proc.stdout}\n{proc.stderr}")


def create_task(board: str, title: str, body: str, assignee: str, key: str) -> dict[str, Any]:
    proc = run_command([
        "hermes",
        "kanban",
        "--board",
        board,
        "create",
        title,
        "--body",
        body,
        "--assignee",
        assignee,
        "--workspace",
        f"dir:{TARGET_WORKSPACE}",
        "--idempotency-key",
        key,
        "--json",
    ])
    if proc.returncode != 0:
        raise SystemExit(f"Failed to create task on {board}:\n{proc.stdout}\n{proc.stderr}")
    try:
        return json.loads(proc.stdout)
    except Exception:
        return {"raw": proc.stdout}


def active_tasks(board: str) -> list[dict[str, Any]]:
    proc = run_command(["hermes", "kanban", "--board", board, "list", "--json"])
    if proc.returncode != 0:
        raise SystemExit(f"Failed to list board {board}:\n{proc.stdout}\n{proc.stderr}")
    try:
        tasks = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Failed to parse board {board} JSON: {exc}\n{proc.stdout}") from exc
    if not isinstance(tasks, list):
        return []
    return [t for t in tasks if t.get("status") not in {"done", "archived"}]


def assert_board_idle(board: str) -> None:
    open_tasks = active_tasks(board)
    if open_tasks:
        preview = ", ".join(f"{t.get('id')}:{t.get('status')}" for t in open_tasks[:5])
        raise SystemExit(f"Board {board} already has active task(s): {preview}. Finish/review those before creating another.")


def task_id(task: dict[str, Any]) -> str | None:
    for key in ("id", "task_id"):
        if task.get(key):
            return str(task[key])
    data = task.get("data")
    if isinstance(data, dict):
        for key in ("id", "task_id"):
            if data.get(key):
                return str(data[key])
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Start a complete EduPaid agent-harness workflow run")
    parser.add_argument("--title", required=True)
    parser.add_argument("--task-class", default="feature")
    parser.add_argument("--risk", default="medium")
    parser.add_argument("--run-id")
    parser.add_argument("--request", default="")
    parser.add_argument("--no-odin", action="store_true")
    parser.add_argument("--no-thor", action="store_true")
    args = parser.parse_args()

    run_id = args.run_id or f"{now_iso().replace(':', '').replace('-', '')[:15]}-{slugify(args.title)}"
    rdir = run_dir(run_id)
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / "logs").mkdir(exist_ok=True)
    (rdir / "harness").mkdir(exist_ok=True)

    request = args.request or args.title
    (rdir / "intake.md").write_text(f"# Intake\n\n{request.strip()}\n")

    needs_odin = not args.no_odin
    needs_thor = not args.no_thor
    routing: dict[str, Any] = {
        "run_id": run_id,
        "title": args.title,
        "task_class": args.task_class,
        "risk": args.risk,
        "created_at": now_iso(),
        "controller": "default-hermes",
        "removed_roles": ["ragnar-techlead"],
        "boards": {
            "odin": ODIN_BOARD,
            "thor": THOR_BOARD,
        },
        "stages": {
            "intake": {"runtime": "default-hermes", "artifact": "intake.md", "status": "complete"},
            "product_spec": {"runtime": "hermes", "profile": "odin-pm", "artifact": PRODUCT_SPEC, "required": needs_odin, "gate": "human_review"},
            "architecture_design": {"runtime": "hermes", "profile": "thor-architect", "artifact": ARCHITECTURE_DESIGN, "required": needs_thor, "gate": "human_review"},
            "claude_plan": {"runtime": "claude-code", "mode": "plan", "model": "opus", "agent": "plan-opus", "artifact": "implementation-plan.md"},
            "claude_build": {"runtime": "claude-code", "model": "haiku", "agent": "build-haiku", "artifact": "build-summary.md"},
            "claude_evidence": {"runtime": "claude-code", "model": "sonnet", "agent": "evidence-sonnet", "artifact": "evidence-report.md"},
            "review": {"runtime": "hermes", "model": "openai-group/gpt-5.5", "artifact": "review-result.json"},
        },
    }

    ensure_board(ODIN_BOARD, "EduPaid Odin")
    ensure_board(THOR_BOARD, "EduPaid Thor")
    if needs_odin:
        assert_board_idle(ODIN_BOARD)
    elif needs_thor:
        assert_board_idle(THOR_BOARD)

    tasks: dict[str, Any] = {}
    if needs_odin:
        odin_body = f"""Default Hermes created run `{run_id}`.

Read `.agent-runs/{run_id}/intake.md`. Write the product specification to `.agent-runs/{run_id}/{PRODUCT_SPEC}`.

Do not create app code. Use Odin PM judgment: expected behavior, acceptance criteria, edge cases, non-goals, and open questions.

When the artifact is ready, do NOT mark the task done. Instead block it for human review:
`hermes kanban --board {ODIN_BOARD} block --kind needs_input <task_id> "Ready for human review: .agent-runs/{run_id}/{PRODUCT_SPEC}"`
"""
        tasks["product_spec"] = create_task(ODIN_BOARD, f"Product spec: {args.title}", odin_body, "odin-pm", f"{run_id}:product-spec")
        routing["stages"]["product_spec"]["task_id"] = task_id(tasks["product_spec"])
    elif needs_thor:
        # If product spec is skipped, create the architecture task immediately.
        # Otherwise Thor is created only after product-spec human approval.
        thor_body = f"""Default Hermes created run `{run_id}`.

Read `.agent-runs/{run_id}/intake.md`. Write the architecture design to `.agent-runs/{run_id}/{ARCHITECTURE_DESIGN}`.

Do not create app code. Use Thor architecture judgment: affected modules, constraints, data flow, test strategy, risks, and open questions.

When the artifact is ready, do NOT mark the task done. Instead block it for human review:
`hermes kanban --board {THOR_BOARD} block --kind needs_input <task_id> "Ready for human review: .agent-runs/{run_id}/{ARCHITECTURE_DESIGN}"`
"""
        tasks["architecture_design"] = create_task(THOR_BOARD, f"Architecture design: {args.title}", thor_body, "thor-architect", f"{run_id}:architecture-design")
        routing["stages"]["architecture_design"]["task_id"] = task_id(tasks["architecture_design"])
    routing["tasks"] = tasks

    write_json(rdir / "routing.json", routing)
    current_stage = "product_spec" if needs_odin else "architecture_design"
    update_state(run_id, status=f"awaiting_{current_stage}", current_stage=current_stage, title=args.title)
    print(json.dumps({"run_id": run_id, "path": str(rdir.relative_to(TARGET_WORKSPACE)), "routing": routing}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
