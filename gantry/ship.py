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
from .git import branch_name, commit_all, create_pr, is_conflict_shaped_failure, merge_pr, push
from .redact import proxy_secrets, redact_secrets
from .shipmeta import draft_ship_meta
from .status import Status

# Cap on conflict-resolver attempts per ship_run call — mirrors the bounded
# nature of every other auto-retry loop in this codebase (checks.retry_checks,
# checks.resolve_attempts). One retry after one resolver pass; if the
# re-attempted push/create_pr still looks conflict-shaped after that, this
# falls through to the generic ship_failed path rather than looping forever
# against something the resolver couldn't actually fix.
_MAX_CONFLICT_RESOLVE_ATTEMPTS = 1


def _ship_checks_failure_detail(checks: dict[str, Any] | None) -> str:
    """Mirrors advance.py's `_checks_failure_detail` shape/purpose, scoped to
    ship_run's own fresh re-verification call (not the run's earlier
    build/checks history) — kept local to ship.py rather than imported so
    ship.py doesn't need advance.py's RunStore-shaped helper (this one just
    takes the checks dict ship_run already has in hand)."""
    if not checks:
        return "Checks failed."
    scope = checks.get("scope") or {}
    if scope.get("forbidden_files") or scope.get("unexpected_files"):
        bad = scope.get("forbidden_files", []) + scope.get("unexpected_files", [])
        file_list = "\n".join(f"  • `{f}`" for f in bad[:8])
        return f"Scope violation — files outside the plan:\n{file_list}"
    failing = [c["command"] for c in (checks.get("checks") or {}).get("results", []) if not c.get("pass")]
    if failing:
        return "Failing command(s):\n" + "\n".join(f"  • `{c}`" for c in failing)
    return "Checks failed."


def _surviving_blocking_findings(review_result: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Task 2's defense-in-depth check: scan review-result.json for any
    finding with action == "blocking" that somehow still exists at ship time.
    This should essentially never trigger — REQUEST_CHANGES already gates on
    blocking findings earlier in the pipeline (see review._findings_verdict)
    — but a bug in the review->ship handoff (e.g. a run's status hand-edited
    back to review_approved, or a future refactor that drops that gate)
    should still be caught here rather than silently shipping a diff with a
    known-blocking finding.

    Only meaningful for the two_axis=True shape, which carries structured
    findings per axis. The legacy flat shape (two_axis=False, or absent
    entirely) has no structured findings to inspect at all — this returns []
    for that case, deliberately skipping the sub-check rather than guessing,
    exactly as the task spec requires."""
    if not review_result or not review_result.get("two_axis"):
        return []
    out: list[dict[str, Any]] = []
    for axis_name in ("spec", "standards"):
        axis = review_result.get(axis_name) or {}
        findings = axis.get("findings") or []
        out.extend(f for f in findings if f.get("action") == "blocking")
    return out


def _run_final_gate(engine: Engine, run_id: str) -> dict[str, Any] | None:
    """Task 1 + Task 2, run at the very start of ship_run before
    draft_ship_meta/commit_all: a fresh `engine.run_checks(run_id)` call (the
    same method advance.py's build_complete branch already calls — never
    duplicate check-running logic), plus a scan of the run's persisted
    review-result.json for any surviving `blocking` finding (two_axis=True
    only; see `_surviving_blocking_findings`).

    Returns None if both gates pass (ship_run should proceed as normal).
    Returns a failure dict (already written to state/log) if either gate
    fails — ship_run must return immediately with that dict and must NOT
    proceed to draft/commit/push. The `ship_checks_failed` status this sets
    is deliberately distinct from `ship_failed` (a push/PR-mechanics problem)
    — a code-correctness regression resurfacing at ship time, or a review
    handoff bug, is never auto-resumed and always requires explicit human
    action (see advance.py: `ship_checks_failed` is never added to
    AUTO_TRANSITIONS, same treatment as checks_high_risk_escalated/
    review_escalated)."""
    review_result = engine.store.read_result(run_id, "review-result.json")
    blocking = _surviving_blocking_findings(review_result)
    if blocking:
        engine.store.write_result(run_id, "ship-checks-result.json",
                                  {"pass": False, "reason": "blocking_findings_survived",
                                   "blocking_findings": blocking})
        engine.store.write_log(
            run_id, "ship.stderr",
            f"ship blocked: {len(blocking)} blocking review finding(s) survived to ship time "
            f"(defense-in-depth — this should never happen, see review._findings_verdict):\n"
            + "\n".join(f"  • [{f.get('severity', '?')}] {f.get('location', '')}: "
                        f"{f.get('description', '')}" for f in blocking))
        engine.store.update_state(run_id, status=Status.SHIP_CHECKS_FAILED)
        return {"ok": False, "stage": "ship_gate", "reason": "blocking_findings_survived",
                "blocking_findings": blocking}

    checks = engine.run_checks(run_id)
    if not checks["pass"]:
        engine.store.write_result(run_id, "ship-checks-result.json", checks)
        engine.store.write_log(
            run_id, "ship.stderr",
            f"ship blocked: final re-verification checks failed just before commit/push:\n"
            f"{_ship_checks_failure_detail(checks)}")
        engine.store.update_state(run_id, status=Status.SHIP_CHECKS_FAILED)
        return {"ok": False, "stage": "ship_gate", "reason": "checks_failed", "checks": checks}

    return None


def _conflict_signal_from(*results: dict[str, Any]) -> str | None:
    """Given commit_all/push/create_pr result dicts (any with an "output"
    key), return the captured output text of the first one whose text looks
    conflict-shaped (see git.is_conflict_shaped_failure), or None if none do."""
    for res in results:
        out = res.get("output") or ""
        if out and is_conflict_shaped_failure(out):
            return out
    return None


def _reattempt_push_and_pr(engine: Engine, run_id: str, wt, branch: str, remote_branch: str,
                           title: str, body: str, secrets: list[str]) -> dict[str, Any]:
    """Re-attempt push + create_pr after the conflict-resolver agent claims
    to have finished — never trusts the resolver's own claim, actually
    re-runs the real push/create_pr calls and reports whatever THEY say,
    same "verify for real" discipline as Engine.run_resolver_stage's
    checks-path re-run of run_checks."""
    push_res = push(wt, branch, remote_branch=remote_branch)
    if not push_res["ok"]:
        engine.store.write_log(run_id, "ship.stderr",
                               redact_secrets(f"push retry after conflict-resolve failed: {push_res}",
                                              extra_secrets=secrets))
        return {"ok": False, "stage": "push", "push": push_res, "pr": None}

    pr_res = create_pr(wt, remote_branch, engine.cfg.git.base_branch, title, body)
    if not pr_res["ok"]:
        engine.store.write_log(run_id, "ship.stderr",
                               redact_secrets(f"create_pr retry after conflict-resolve failed: {pr_res}",
                                              extra_secrets=secrets))
        return {"ok": False, "stage": "create_pr", "push": push_res, "pr": pr_res}

    return {"ok": True, "push": push_res, "pr": pr_res}


def ship_run(engine: Engine, run_id: str) -> dict[str, Any]:
    wt = engine.work_dir(run_id)
    branch = branch_name(run_id)

    # Task 1 + Task 2: final re-verification gate before draft/commit/push —
    # see _run_final_gate's docstring. Must run BEFORE draft_ship_meta (no
    # point drafting a PR for a diff that just failed re-verification) and
    # must short-circuit ship_run entirely on failure.
    gate_failure = _run_final_gate(engine, run_id)
    if gate_failure is not None:
        return gate_failure

    meta = draft_ship_meta(engine.store, run_id, engine.cfg, wt)
    title, body, remote_branch = meta["title"], meta["body"], meta["branch_slug"]
    rollback_note = meta.get("rollback_note", "")
    if rollback_note:
        body = f"{body}\n\n## Rollback\n\n{rollback_note}\n"

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
        engine.store.update_state(run_id, status=Status.SHIP_FAILED)
        return {"ok": False, "stage": "commit", **commit_res}

    push_res = push(wt, branch, remote_branch=remote_branch)
    pr_res = None
    if push_res["ok"]:
        pr_res = create_pr(wt, remote_branch, engine.cfg.git.base_branch, title, body)

    # Task 4: a conflict-shaped push/create_pr failure gets a genuinely
    # different response from every other failure here — route to the
    # conflict-resolver stage (resolve by intent) and re-attempt, instead of
    # the generic ship_failed path a non-conflict failure still uses
    # completely unchanged (see below).
    conflict_output = None
    if not push_res["ok"]:
        conflict_output = _conflict_signal_from(push_res)
    elif pr_res is not None and not pr_res["ok"]:
        conflict_output = _conflict_signal_from(pr_res)

    if conflict_output is not None:
        resolve_attempts = engine.store.state(run_id).get("ship_conflict_resolve_count", 0)
        if resolve_attempts < _MAX_CONFLICT_RESOLVE_ATTEMPTS:
            engine.store.update_state(run_id, ship_conflict_resolve_count=resolve_attempts + 1)
            engine.run_resolver_stage(run_id, failure_kind="conflict", conflict_output=conflict_output)
            retry = _reattempt_push_and_pr(engine, run_id, wt, branch, remote_branch, title, body, secrets)
            if retry["ok"]:
                push_res, pr_res = retry["push"], retry["pr"]
            else:
                # Resolver ran, but the re-verified push/create_pr STILL
                # fails — never trust the resolver's own claim; fall through
                # to the same failure handling below (which will now see a
                # real, current push_res/pr_res reflecting the retry).
                push_res = retry.get("push", push_res)
                pr_res = retry.get("pr", pr_res)

    if not push_res["ok"]:
        engine.store.write_log(run_id, "ship.stderr",
                               redact_secrets(f"push failed: {push_res}", extra_secrets=secrets))
        engine.store.update_state(run_id, status=Status.SHIP_FAILED)
        return {"ok": False, "stage": "push", **push_res}

    if pr_res is None or not pr_res["ok"]:
        engine.store.write_log(run_id, "ship.stderr",
                               redact_secrets(f"create_pr failed: {pr_res}", extra_secrets=secrets))
        engine.store.update_state(run_id, status=Status.SHIP_FAILED, pr_url=None)
        return {"ok": False, "commit": commit_res, "push": push_res, "pr": pr_res,
                "branch": remote_branch, "title": title}

    merge_res = None
    if engine.cfg.git.auto_merge:
        merge_res = merge_pr(wt, remote_branch)
        # A failed auto-merge still leaves a real, open PR — that's a normal,
        # recoverable state (status stays "shipped", not "ship_failed"; a
        # human or a later retry can merge it manually), not the same failure
        # class as a broken commit/push/PR-creation step above.
        engine.store.update_state(run_id, status=Status.SHIPPED, pr_url=pr_res.get("url"),
                                  merged=merge_res["ok"])
    else:
        engine.store.update_state(run_id, status=Status.SHIPPED, pr_url=pr_res.get("url"))

    return {"ok": True, "commit": commit_res, "push": push_res, "pr": pr_res, "merge": merge_res,
            "branch": remote_branch, "title": title}
