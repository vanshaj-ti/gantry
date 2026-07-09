"""Commit, push, and open a PR for a run's worktree.

Extracted from `cmd_ship` so `advance_run` (advance.py) can ship a run
automatically on `review_approved` when `[git].auto_ship` is enabled, without
importing cli.py (which would create a cli -> engine -> cli import cycle).
`cmd_ship` is a thin wrapper over `ship_run` that also handles the
`review_approved`-or-`--force` gate and CLI-specific output formatting.
"""
from __future__ import annotations

from typing import Any

from .engine import Engine
from .git import branch_name, commit_all, create_pr, push
from .shipmeta import draft_ship_meta


def ship_run(engine: Engine, run_id: str) -> dict[str, Any]:
    wt = engine.work_dir(run_id)
    branch = branch_name(run_id)

    meta = draft_ship_meta(engine.store, run_id, engine.cfg, wt)
    title, body, remote_branch = meta["title"], meta["body"], meta["branch_slug"]

    commit_res = commit_all(wt, title)
    if not commit_res["ok"]:
        engine.store.update_state(run_id, status="ship_failed")
        return {"ok": False, "stage": "commit", **commit_res}

    push_res = push(wt, branch, remote_branch=remote_branch)
    if not push_res["ok"]:
        engine.store.update_state(run_id, status="ship_failed")
        return {"ok": False, "stage": "push", **push_res}

    pr_res = create_pr(wt, remote_branch, engine.cfg.git.base_branch, title, body)
    engine.store.update_state(run_id, status="shipped" if pr_res["ok"] else "ship_failed",
                              pr_url=pr_res.get("url"))
    return {"ok": pr_res["ok"], "commit": commit_res, "push": push_res, "pr": pr_res,
            "branch": remote_branch, "title": title}
