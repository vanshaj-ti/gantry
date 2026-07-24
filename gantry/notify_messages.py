"""Notification message builders for pipeline status changes."""
from __future__ import annotations

from typing import Any

from .config import DOC_STAGES
from .failure_detail import (
    _checks_failure_detail,
    _e2e_failure_detail,
    _escape_md,
    _high_risk_detail,
    _review_findings_detail,
    _ship_checks_failure_detail,
    _spec_gate_failure_detail,
)
from .feedback import reply_prompt, route_for_state
from .labels import label

_STATUS_ICON = {
    "blocked": "\U0001f6d1",
    "review_escalated": "❗",
    "checks_escalated": "❗",
    "resolve_escalated": "❗",
    "checks_high_risk_escalated": "\U0001f6a8",
    "review_approved": "✅",
    "review_changes_requested": "\U0001f501",
    "shipped": "\U0001f680",
    "shipped_manually": "\U0001f680",
    "ship_failed": "⚠️",
    "ship_checks_failed": "\U0001f6a8",
    "held": "\U0001f91a",
    "cancelled": "\U0001f6ab",
}


def _icon(status: str) -> str:
    if status in _STATUS_ICON:
        return _STATUS_ICON[status]
    if status.endswith("_failed"):
        return "⚠️"
    if status.endswith("_running"):
        return "⏳"
    if status.endswith("_complete"):
        return "✅"
    return "ℹ️"


def notify_message(
    store: Any, run_id: str, status: str, result: dict[str, Any] | None = None,
) -> str:
    """Build a self-contained, Markdown-formatted Telegram notification."""
    icon = _icon(status)
    header = f"{icon} *{_escape_md(run_id)}*\n{_escape_md(label(status))}"
    result = result or {}
    review_result = (
        store.read_result(run_id, "review-result.json")
        if status in ("review_escalated", "review_changes_requested")
        else None
    )
    route = route_for_state(
        {**store.state(run_id), "status": status}, review_result=review_result,
    )
    replies = reply_prompt(route)

    if status in ("shipped", "shipped_manually") or status.endswith("_escalated"):
        from .cost import report_for_run
        cost = report_for_run(store, run_id).get("total_cost_usd")
        if cost:
            header += f"\nCost so far: ${cost:.2f}"

    if status in ("shipped", "shipped_manually"):
        merged = store.state(run_id).get("merged") is True
        pr_url = store.state(run_id).get("pr_url", "")
        if merged:
            return f"{header}\nMerged.{f' {pr_url}' if pr_url else ''}"
        return (f"{header}\nPR open, not yet merged.{f' {pr_url}' if pr_url else ''}\n\n"
                f"Once merged, run `gantry mark-merged --run {_escape_md(run_id)}` so any "
                f"dependent runs can start.")

    if status.endswith("_complete") and status.removesuffix("_complete") in DOC_STAGES:
        agent_text = ""
        if isinstance(result.get("raw"), dict):
            agent_text = (result["raw"].get("result") or "")[:600]
        doc_note = f"\n{_escape_md(agent_text)}\n" if agent_text else ""
        return f"{header}\n{doc_note}\n{replies}"

    if status == "blocked":
        blocked_on = store.state(run_id).get("blocked_on", "checks")
        retry_count = store.state(run_id).get("checks_retry_count", 0)
        detail = (
            _e2e_failure_detail(store, run_id)
            if blocked_on == "e2e"
            else _checks_failure_detail(store, run_id)
        )
        return (f"{header}\n\n"
                f"*Blocked on:* {blocked_on} (auto-retry attempt {retry_count})\n"
                f"{detail}\n\n"
                f"{replies}")

    if status == "ship_checks_failed":
        ship_result = store.read_result(run_id, "ship-checks-result.json") or {}
        detail = _ship_checks_failure_detail(
            store, run_id, blocking_findings=ship_result.get("blocking_findings"),
        )
        return (f"{header}\n\n"
                f"{detail}\n\n"
                f"This always requires a human decision, regardless of auto_ship — a ship-time "
                f"re-verification found a real problem, not a push/PR mechanics error.\n\n"
                f"{replies}")

    if status == "checks_high_risk_escalated":
        from .config import load_config
        cfg = load_config(store.target)
        detail = _high_risk_detail(store, run_id, cfg)
        return (f"{header}\n\n"
                f"{detail}\n\n"
                f"This always requires a human decision, regardless of "
                f"auto_approve_docs/auto_ship/auto_resolve settings.\n\n"
                f"{replies}")

    if status == "checks_escalated":
        blocked_on = store.state(run_id).get("blocked_on", "checks")
        detail = (
            _e2e_failure_detail(store, run_id)
            if blocked_on == "e2e"
            else _checks_failure_detail(store, run_id)
        )
        retry_count = store.state(run_id).get("checks_retry_count", 0)
        return (f"{header}\n\n"
                f"Auto-retry exhausted after {retry_count} attempt(s).\n"
                f"{detail}\n\n"
                f"{replies}")

    if status == "resolve_escalated":
        blocked_on = store.state(run_id).get("blocked_on", "checks")
        detail = (
            _e2e_failure_detail(store, run_id)
            if blocked_on == "e2e"
            else _checks_failure_detail(store, run_id)
        )
        resolve_attempts = store.state(run_id).get("resolve_attempt_count", 0)
        return (f"{header}\n\n"
                f"Resolver agent exhausted {resolve_attempts} attempt(s) without a passing fix.\n"
                f"{detail}\n\n"
                f"{replies}")

    if status.endswith("_failed"):
        stage = status.removesuffix("_failed")
        subtype = (
            result.get("raw", {}).get("subtype")
            if isinstance(result.get("raw"), dict)
            else None
        )
        agent_text = (
            (result.get("raw", {}).get("result") or "")[:500]
            if isinstance(result.get("raw"), dict)
            else ""
        )
        if subtype == "error_max_turns":
            return (f"{header}\n\n"
                    f"Ran out of turns before finishing the *{stage}* stage.\n\n"
                    f"{replies}")
        gate_detail = _spec_gate_failure_detail(store, run_id) if stage == "spec" else ""
        gate_note = f"\n{_escape_md(gate_detail)}\n" if gate_detail else ""
        return (f"{header}\n\n"
                f"Stage errored (`{subtype or 'unknown'}`).\n"
                f"{_escape_md(agent_text)}\n{gate_note}\n"
                f"{replies}")

    if status == "review_escalated":
        detail = _review_findings_detail(review_result)
        return (f"{header}\n\n"
                f"{detail}\n\n"
                f"{replies}")

    if status == "review_changes_requested":
        review_result = store.read_result(run_id, "review-result.json")
        detail = _review_findings_detail(review_result)
        return (f"{header}\n\n"
                f"{detail}\n\n"
                f"Sent back for a rebuild automatically with both axes' feedback in "
                f"review-comments.md — no reply needed unless you want to intervene.")

    if status == "held":
        held_from = store.state(run_id).get("held_from_status", "")
        return (f"{header}\n\n"
                f"Paused for manual work (was: `{_escape_md(held_from)}`). Gantry will not "
                f"advance or auto-retry this run while held.\n\n"
                f"Run `gantry resume --run {_escape_md(run_id)}` when you're done to hand it "
                f"back to the pipeline.")

    agent_text = ""
    if isinstance(result.get("raw"), dict):
        agent_text = result["raw"].get("result") or ""
    if agent_text and "?" in agent_text[-200:]:
        return (f"{header}\n\n"
                f"*Agent has a question before continuing:*\n{_escape_md(agent_text[:800])}\n\n"
                f"*Reply* with your decision.")

    return header
