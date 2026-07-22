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
from .git import branch_name, commit_all, create_pr, merge_pr, push
from .redact import proxy_secrets, redact_secrets
from .shipmeta import draft_ship_meta


def ship_run(engine: Engine, run_id: str) -> dict[str, Any]:
    wt = engine.work_dir(run_id)
    branch = branch_name(run_id)

    meta = draft_ship_meta(engine.store, run_id, engine.cfg, wt)
    title, body, remote_branch = meta["title"], meta["body"], meta["branch_slug"]

    # Every step below previously discarded its own `output` on failure —
    # ship_failed left no trail beyond a bare status flip, same gap fixed in
    # review.py. Without this, diagnosing WHY a ship failed (real git error?
    # network blip? gh not authenticated?) required re-running the exact
    # same commit/push/PR sequence by hand and hoping to reproduce it.
    # gh/git subprocess output (commit_res/push_res/pr_res's "output" fields)
    # can in principle echo GH_TOKEN or a proxy secret back (e.g. in an error
    # message) — redact before persisting to ship.stderr, never after.
    secrets = proxy_secrets(engine.cfg)

    commit_res = commit_all(wt, title)
    if not commit_res["ok"]:
        engine.store.write_log(run_id, "ship.stderr",
                               redact_secrets(f"commit failed: {commit_res}", extra_secrets=secrets))
        engine.store.update_state(run_id, status="ship_failed")
        return {"ok": False, "stage": "commit", **commit_res}

    push_res = push(wt, branch, remote_branch=remote_branch)
    if not push_res["ok"]:
        engine.store.write_log(run_id, "ship.stderr",
                               redact_secrets(f"push failed: {push_res}", extra_secrets=secrets))
        engine.store.update_state(run_id, status="ship_failed")
        return {"ok": False, "stage": "push", **push_res}

    pr_res = create_pr(wt, remote_branch, engine.cfg.git.base_branch, title, body)
    if not pr_res["ok"]:
        engine.store.write_log(run_id, "ship.stderr",
                               redact_secrets(f"create_pr failed: {pr_res}", extra_secrets=secrets))
        engine.store.update_state(run_id, status="ship_failed", pr_url=None)
        return {"ok": False, "commit": commit_res, "push": push_res, "pr": pr_res,
                "branch": remote_branch, "title": title}

    merge_res = None
    if engine.cfg.git.auto_merge:
        merge_res = merge_pr(wt, remote_branch)
        # A failed auto-merge still leaves a real, open PR — that's a normal,
        # recoverable state (status stays "shipped", not "ship_failed"; a
        # human or a later retry can merge it manually), not the same failure
        # class as a broken commit/push/PR-creation step above.
        engine.store.update_state(run_id, status="shipped", pr_url=pr_res.get("url"),
                                  merged=merge_res["ok"])
    else:
        engine.store.update_state(run_id, status="shipped", pr_url=pr_res.get("url"))

    return {"ok": True, "commit": commit_res, "push": push_res, "pr": pr_res, "merge": merge_res,
            "branch": remote_branch, "title": title}
