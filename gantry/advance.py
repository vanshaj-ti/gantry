"""Auto-advance: drive the pipeline forward one tick based on run state.

It inspects a run's status and fires the next stage automatically, for the
transitions that don't require a human gate:

  plan_complete            -> run build
  build_complete           -> run checks; if pass -> run evidence
  evidence_complete        -> run review
  review_changes_requested -> resume build (with review-comments.md)

Human-gated transitions (awaiting_spec, awaiting_design, review_escalated,
checks_high_risk_escalated, blocked) are intentionally NOT auto-advanced —
they wait for `gantry approve`.

`advance_run` runs synchronously (used by `gantry advance --run ID`).
`advance_all` ticks every run once (used by the poller cron).
"""
from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from .checks import _matches_any
from .config import AGENT_STAGES, GantryConfig
from .e2e import run_e2e_tests
from .engine import HEARTBEAT_INTERVAL, Engine
from .review import run_review
from .state import _iso_to_ts

logger = logging.getLogger(__name__)

# Transitions the poller drives automatically (no human gate).
# awaiting_plan / awaiting_build / awaiting_evidence are NOT human-gated (only
# awaiting_spec / awaiting_design are — see config.DOC_STAGES) — they just mean
# "approved to start, hasn't been kicked off yet", so the poller should fire
# the stage itself rather than waiting for a manual `gantry stage <name>`.
AUTO_TRANSITIONS = {
    "plan_complete", "build_complete", "evidence_complete", "review_changes_requested",
    "blocked", "queued",
    *(f"awaiting_{stage}" for stage in AGENT_STAGES),
}

# Human-friendly status labels for notifications.
STATUS_LABELS = {
    "queued": "Queued — waiting on prerequisite run(s)",
    "awaiting_spec": "Awaiting product spec (human)",
    "spec_running": "Writing product spec",
    "spec_complete": "Spec ready for review",
    "spec_failed": "Spec stage errored",
    "awaiting_design": "Awaiting architecture design (human)",
    "design_running": "Writing architecture design",
    "design_complete": "Design ready for review",
    "design_failed": "Design stage errored",
    "awaiting_plan": "Ready to plan",
    "plan_running": "Writing implementation plan",
    "plan_complete": "Plan complete",
    "build_running": "Building & testing",
    "build_complete": "Build complete",
    "evidence_running": "Generating evidence",
    "evidence_complete": "Evidence complete",
    "review_running": "Independent review in progress",
    "review_approved": "Review APPROVED — ready to ship",
    "review_changes_requested": "Review requested changes — rebuilding",
    "review_escalated": "Review ESCALATED — human decision needed",
    "blocked": "Blocked — needs input",
    "checks_high_risk_escalated": "High-risk path touched — human decision needed",
    "checks_escalated": "Checks ESCALATED — auto-retry exhausted",
    "resolve_running": "Resolver agent fixing escalated checks",
    "resolve_escalated": "Resolver ESCALATED — auto-fix exhausted",
    "shipped": "Shipped — PR open",
    "shipped_manually": "Shipped (manual) — PR open",
    "ship_failed": "Ship FAILED — push/PR error",
    "held": "Held — human working on this run manually",
    "cancelled": "Cancelled",
}


def label(status: str) -> str:
    return STATUS_LABELS.get(status, status)


# Compact labels for space-constrained displays (the cockpit status bar is
# `gantry watch --live` running in a thin tmux pane, not a phone notification
# body with room for "Review requested changes — rebuilding"). Falls back to
# the raw status string for anything not listed here — every status not
# worth a full sentence explanation is already short as-is (e.g.
# "build_running" reads fine bare).
SHORT_STATUS_LABELS = {
    "queued": "Queued",
    "awaiting_spec": "Awaiting spec",
    "spec_running": "Writing spec",
    "spec_complete": "Spec review",
    "spec_failed": "Spec failed",
    "awaiting_design": "Awaiting design",
    "design_running": "Writing design",
    "design_complete": "Design review",
    "design_failed": "Design failed",
    "awaiting_plan": "Ready to plan",
    "plan_running": "Planning",
    "plan_complete": "Plan done",
    "build_running": "Building",
    "build_complete": "Build done",
    "evidence_running": "Evidence",
    "evidence_complete": "Evidence done",
    "review_running": "Reviewing",
    "review_approved": "Approved",
    "review_changes_requested": "Changes requested",
    "review_escalated": "Review escalated",
    "blocked": "Blocked",
    "checks_high_risk_escalated": "High-risk escalated",
    "checks_escalated": "Checks escalated",
    "resolve_running": "Resolving",
    "resolve_escalated": "Resolve escalated",
    "shipped": "Shipped",
    "shipped_manually": "Shipped (manual)",
    "ship_failed": "Ship failed",
    "held": "Held",
    "cancelled": "Cancelled",
}


def short_label(status: str) -> str:
    return SHORT_STATUS_LABELS.get(status, status)


# Emoji glyphs by outcome family — scannable at a glance in a phone notification
# list, before the message body is even opened.
_STATUS_ICON = {
    "blocked": "\U0001f6d1",           # 🛑
    "review_escalated": "❗",       # ❗
    "checks_escalated": "❗",       # ❗
    "resolve_escalated": "❗",       # ❗
    "checks_high_risk_escalated": "\U0001f6a8",  # 🚨 — distinct from the plain ❗
                                                   # escalated statuses: this one is
                                                   # never about a failure, it's a
                                                   # deliberate "stop and look" signal
                                                   # even when everything else passed.
    "review_approved": "✅",        # ✅
    "review_changes_requested": "\U0001f501",  # 🔁
    "shipped": "\U0001f680",           # 🚀
    "shipped_manually": "\U0001f680",  # 🚀
    "ship_failed": "⚠️",            # ⚠️
    "held": "\U0001f91a",              # 🤚
    "cancelled": "\U0001f6ab",         # 🚫
}


def _icon(status: str) -> str:
    if status in _STATUS_ICON:
        return _STATUS_ICON[status]
    if status.endswith("_failed"):
        return "⚠️"  # ⚠️
    if status.endswith("_running"):
        return "⏳"  # ⏳
    if status.endswith("_complete"):
        return "✅"  # ✅
    return "ℹ️"  # ℹ️


def _escape_md(text: str) -> str:
    """Escape Telegram legacy Markdown's four special chars in plain (non-code) text.
    Command snippets stay inside backticks below, which Telegram renders verbatim."""
    for ch in ("_", "*", "[", "`"):
        text = text.replace(ch, "\\" + ch)
    return text


_MAX_RETRY_ATTEMPTS_KEPT = 3


def _accumulate_retry_feedback(store: Any, run_id: str, label_word: str, this_attempt: str) -> str:
    """Build answers/build.md as a running log of the last _MAX_RETRY_ATTEMPTS_KEPT
    retry attempts, not a single overwritten attempt.

    Previously each retry replaced the file wholesale, so a build agent
    resuming after 3 failed attempts saw only the most recent failure detail
    — no way to know it (or a prior attempt) had already tried and failed at
    a particular fix. A resumed agent with the same session history but no
    visible record of its own past attempts tends to re-discover the same
    dead end blind. Keeping a short rolling history ("you already tried X and
    Y, both failed on Z") gives the agent enough context to try something
    genuinely different instead. Oldest attempts are dropped once the cap is
    hit — an unbounded history would eventually dominate the resumed prompt's
    context with stale detail from early, likely-superseded attempts."""
    existing = store.read_artifact(run_id, "answers/build.md") or ""
    prior_attempts = re.findall(r"(## Attempt \d+/\d+\n\n.*?)(?=\n## Attempt \d+/\d+\n\n|\Z)",
                                existing, re.DOTALL)
    attempts = (prior_attempts + [this_attempt])[-_MAX_RETRY_ATTEMPTS_KEPT:]
    header = f"# {label_word} failed — auto-retry history (most recent last)\n\n"
    return header + "\n".join(attempts)


def _checks_failure_detail(store: Any, run_id: str) -> str:
    """Extract a concrete, actionable description of why checks failed: the
    scope violation's file list, or the failing command name(s). Shared
    between the human-facing notify message and the feedback text fed back
    into a resumed build attempt — both need the same real detail, not a
    bare 'checks failed' label."""
    checks = store.read_result(run_id, "checks.json")
    if not checks or checks.get("pass"):
        return "Checks failed."
    merge = checks.get("base_branch_merge") or {}
    if merge.get("action") == "merge_conflict":
        return (f"Merging the base branch into this run's own branch hit a real conflict "
                f"(another queued run shipped changes that overlap with this run's files):\n\n"
                f"```\n{merge.get('output', '(no output captured)')}\n```\n\n"
                f"Resolve the conflict markers in the affected files, then continue. "
                f"This is a genuine content conflict — resolve it deliberately, don't "
                f"discard either side's changes without checking what they do.")
    scope = checks.get("scope", {})
    if scope.get("forbidden_files") or scope.get("unexpected_files"):
        bad = scope.get("forbidden_files", []) + scope.get("unexpected_files", [])
        file_list = "\n".join(f"  • `{f}`" for f in bad[:8])
        return f"Scope violation — files outside the plan:\n{file_list}"
    failing = [c["command"] for c in checks.get("checks", {}).get("results", []) if not c.get("pass")]
    if failing:
        return "Failing command(s):\n" + "\n".join(f"  • `{c}`" for c in failing)
    return "Checks failed."


def _spec_gate_failure_detail(store: Any, run_id: str) -> str:
    """Same purpose as _checks_failure_detail, for the spec stage's
    deterministic structural gate (spec-gate.json, written by
    Engine.run_agent_stage when stage == "spec") — surfaces the concrete
    reason acceptance-criteria.json failed the gate instead of a bare
    'stage errored'. Returns "" (no detail available) if the run never
    reached the gate check at all (e.g. the agent itself errored first)."""
    gate = store.read_result(run_id, "spec-gate.json")
    if not gate or gate.get("pass"):
        return ""
    return f"Structural gate failed: {gate.get('reason', 'acceptance-criteria.json invalid')}"


def _high_risk_detail(store: Any, run_id: str, cfg: GantryConfig | None = None) -> str:
    """Concrete detail for checks_high_risk_escalated: which changed file(s)
    matched which configured `high_risk_paths` glob(s), so a human reading
    the notification knows exactly what to look at and why — not just a bare
    'high risk files touched' label."""
    scope = store.read_result(run_id, "checks.json") or {}
    high_risk = ((scope.get("scope") or {}).get("high_risk_files")) or []
    if not high_risk:
        return "High-risk path(s) touched."
    patterns = cfg.scope.high_risk_paths if cfg is not None else []
    lines = []
    for f in high_risk[:8]:
        matched = [p for p in patterns if _matches_any(f, [p])] if patterns else []
        glob_note = f" (matched `{matched[0]}`)" if matched else ""
        lines.append(f"  • `{f}`{glob_note}")
    return "High-risk path(s) touched:\n" + "\n".join(lines)


def _e2e_failure_detail(store: Any, run_id: str) -> str:
    """Same purpose as _checks_failure_detail, for the deterministic e2e step
    (e2e-report.json) — surfaces which app/spec failed, not just 'e2e failed'."""
    report = store.read_result(run_id, "e2e-report.json")
    if not report or report.get("pass"):
        return "E2e tests failed."
    failing = [a["app"] for a in report.get("apps", []) if not a.get("skipped") and not a.get("pass")]
    if failing:
        return "Failing e2e app(s):\n" + "\n".join(f"  • `{a}`" for a in failing)
    return "E2e tests failed."


def _review_findings_detail(review_result: dict[str, Any] | None) -> str:
    """Summarise the review-result.json into a concise Telegram/notify body
    section that a phone-only reader can act on. Handles both shapes:
      - Legacy flat (two_axis=False): {"verdict":..., "result": "..."}
      - Two-axis (two_axis=True): {"two_axis": True, "spec": {...}, "standards": {...}}
    For two-axis results, surfaces EACH axis's blocking/ask-user findings, so
    a human reading review_escalated or review_changes_requested sees BOTH
    axes' concerns, not just the combined verdict word."""
    if not review_result:
        return "No review result found."

    if review_result.get("two_axis"):
        parts = []
        for axis_name in ("spec", "standards"):
            axis = review_result.get(axis_name) or {}
            verdict = axis.get("verdict", "?")
            findings = axis.get("findings") or []
            # Surface only blocking/ask-user findings to keep notification readable;
            # no-op findings are noise on a phone screen.
            notable = [f for f in findings if f.get("action") in ("blocking", "ask-user")]
            parts.append(f"*{axis_name.capitalize()} axis*: {verdict}")
            if notable:
                for f in notable[:5]:
                    parts.append(f"  • [{f.get('action','?')}] {_escape_md(f.get('description', '')[:120])}")
                if len(notable) > 5:
                    parts.append(f"  … and {len(notable) - 5} more (see review-result.json)")
        return "\n".join(parts)

    # Legacy single-axis shape: surface the first 400 chars of the prose result.
    note = (review_result.get("result") or "")[:400]
    return _escape_md(note) if note else "See review-result.json for details."


def notify_message(store: Any, run_id: str, status: str, result: dict[str, Any] | None = None) -> str:
    """Build a self-contained, Markdown-formatted Telegram body: what happened,
    why, and how to respond. Written for a phone-only reader replying via
    `gantry listen` (see cli.py) — no terminal access assumed, so every action
    is expressed as a plain-text reply, never a CLI command to type out.

    A bare status label ("Blocked — needs input") tells the reader nothing they
    can act on. Every branch below states the concrete failure (or the agent's
    own question) plus exactly what to reply.
    """
    icon = _icon(status)
    header = f"{icon} *{_escape_md(run_id)}*\n{_escape_md(label(status))}"
    result = result or {}

    # Cost-to-date on terminal states — the natural point a human reads this
    # and might wonder what the run cost, without opening `gantry cost --run`.
    if status in ("shipped", "shipped_manually") or status.endswith("_escalated"):
        from .cost import report_for_run
        cost = report_for_run(store, run_id).get("total_cost_usd")
        if cost:
            header += f"\nCost so far: ${cost:.2f}"

    if status in ("shipped", "shipped_manually"):
        # `merged` is otherwise invisible in a notification — a run's
        # dependents (depends_on) only start once it's ACTUALLY merged, not
        # merely shipped (see Engine._prereqs_met), so whoever reads this
        # needs to know whether anything is still blocked on a manual merge.
        merged = store.state(run_id).get("merged") is True
        pr_url = store.state(run_id).get("pr_url", "")
        if merged:
            return f"{header}\nMerged.{f' {pr_url}' if pr_url else ''}"
        return (f"{header}\nPR open, not yet merged.{f' {pr_url}' if pr_url else ''}\n\n"
                f"Once merged, run `gantry mark-merged --run {_escape_md(run_id)}` so any "
                f"dependent runs can start.")

    if status in ("spec_complete", "design_complete"):
        stage = status.removesuffix("_complete")
        agent_text = ""
        if isinstance(result.get("raw"), dict):
            agent_text = (result["raw"].get("result") or "")[:600]
        doc_note = f"\n{_escape_md(agent_text)}\n" if agent_text else ""
        return (f"{header}\n{doc_note}\n"
                f"*Reply 1* to approve and move on.\n"
                f"*Reply 2* with feedback to have it rewritten.")

    if status == "blocked":
        blocked_on = store.state(run_id).get("blocked_on", "checks")
        retry_count = store.state(run_id).get("checks_retry_count", 0)
        detail = _e2e_failure_detail(store, run_id) if blocked_on == "e2e" else _checks_failure_detail(store, run_id)
        return (f"{header}\n\n"
                f"*Blocked on:* {blocked_on} (auto-retry attempt {retry_count})\n"
                f"{detail}\n\n"
                f"*Reply 1* to re-check now (only helps if the issue already got fixed elsewhere).\n"
                f"*Reply 2* with guidance to send it back for a rebuild.")

    if status == "checks_high_risk_escalated":
        from .config import load_config
        cfg = load_config(store.target)
        detail = _high_risk_detail(store, run_id, cfg)
        return (f"{header}\n\n"
                f"{detail}\n\n"
                f"This always requires a human decision, regardless of "
                f"auto_approve_docs/auto_ship/auto_resolve settings.\n\n"
                f"*Reply 1* to approve and let the run continue.\n"
                f"*Reply 2* with guidance to send it back for a rebuild.")

    if status == "checks_escalated":
        blocked_on = store.state(run_id).get("blocked_on", "checks")
        detail = _e2e_failure_detail(store, run_id) if blocked_on == "e2e" else _checks_failure_detail(store, run_id)
        retry_count = store.state(run_id).get("checks_retry_count", 0)
        return (f"{header}\n\n"
                f"Auto-retry exhausted after {retry_count} attempt(s).\n"
                f"{detail}\n\n"
                f"*Reply 1* with guidance to send it back for a rebuild.\n"
                f"*Reply 2* to leave it — you'll investigate yourself.")

    if status == "resolve_escalated":
        blocked_on = store.state(run_id).get("blocked_on", "checks")
        detail = _e2e_failure_detail(store, run_id) if blocked_on == "e2e" else _checks_failure_detail(store, run_id)
        resolve_attempts = store.state(run_id).get("resolve_attempt_count", 0)
        return (f"{header}\n\n"
                f"Resolver agent exhausted {resolve_attempts} attempt(s) without a passing fix.\n"
                f"{detail}\n\n"
                f"*Reply 1* with guidance to send it back for a rebuild.\n"
                f"*Reply 2* to leave it — you'll investigate yourself.")

    if status.endswith("_failed"):
        stage = status.removesuffix("_failed")
        subtype = result.get("raw", {}).get("subtype") if isinstance(result.get("raw"), dict) else None
        agent_text = (result.get("raw", {}).get("result") or "")[:500] if isinstance(result.get("raw"), dict) else ""
        if subtype == "error_max_turns":
            return (f"{header}\n\n"
                    f"Ran out of turns before finishing the *{stage}* stage.\n\n"
                    f"*Reply 1* to retry the same stage.\n"
                    f"*Reply 2* to leave it — you'll check the logs yourself later.")
        gate_detail = _spec_gate_failure_detail(store, run_id) if stage == "spec" else ""
        gate_note = f"\n{_escape_md(gate_detail)}\n" if gate_detail else ""
        return (f"{header}\n\n"
                f"Stage errored (`{subtype or 'unknown'}`).\n"
                f"{_escape_md(agent_text)}\n{gate_note}\n"
                f"*Reply 1* to retry.\n"
                f"*Reply 2* to leave it — you'll check the logs yourself later.")

    if status == "review_escalated":
        # advance_all's per-tick action dict only carries {"verdict": ...},
        # not the full findings — always read the real persisted
        # review-result.json (written by run_review regardless of caller)
        # rather than trusting whatever partial `result` this call happened
        # to receive.
        review_result = store.read_result(run_id, "review-result.json")
        detail = _review_findings_detail(review_result)
        return (f"{header}\n\n"
                f"{detail}\n\n"
                f"*Reply 1* to approve as-is.\n"
                f"*Reply 2* with guidance to send back for changes.")

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

    # Agent asked a clarifying question mid-stage instead of erroring (common on
    # plan/build when the request under-specifies an architecture decision).
    agent_text = ""
    if isinstance(result.get("raw"), dict):
        agent_text = (result["raw"].get("result") or "")
    if agent_text and "?" in agent_text[-200:]:
        return (f"{header}\n\n"
                f"*Agent has a question before continuing:*\n{_escape_md(agent_text[:800])}\n\n"
                f"*Reply* with your decision.")

    return header


def advance_run(engine: Engine, run_id: str) -> dict[str, Any]:
    """Fire the appropriate next stage for a single run based on its status.
    Returns {advanced: bool, from, action, ...}. No-op for gated/terminal states."""
    status = engine.store.state(run_id).get("status", "")

    if status == "queued":
        if not engine._prereqs_met(run_id):
            deps = engine.store.state(run_id).get("depends_on") or []
            unmet = [d for d in deps
                    if engine.store.state(d).get("status") not in ("shipped", "shipped_manually")
                    or engine.store.state(d).get("merged") is not True]
            return {"advanced": False, "from": status, "action": "waiting_on_prereqs",
                    "unmet_depends_on": unmet}
        first = engine.store.state(run_id).get("current_stage") or (
            engine.cfg.stages[0] if engine.cfg.stages else "plan")
        engine.store.update_state(run_id, status=f"awaiting_{first}")
        return {"advanced": True, "from": status, "action": "prereqs_met->awaiting_" + first}

    if status.startswith("awaiting_"):
        stage = status.removeprefix("awaiting_")
        if stage in AGENT_STAGES:
            engine.run_agent_stage(run_id, stage)
            return {"advanced": True, "from": status, "action": f"start_{stage}"}
        return {"advanced": False, "from": status, "action": "no_auto_transition"}

    if status in ("spec_complete", "design_complete") and engine.cfg.git.auto_approve_docs:
        stage = status.removesuffix("_complete")
        nxt = engine.approve(run_id, stage)
        return {"advanced": True, "from": status, "action": f"auto_approved_{stage}->{nxt}"}

    if status == "plan_complete":
        engine.run_agent_stage(run_id, "build")
        return {"advanced": True, "from": status, "action": "build"}

    if status == "build_complete":
        checks = engine.run_checks(run_id)
        if not checks["pass"]:
            return {"advanced": False, "from": status, "action": "checks_failed",
                    "blocked_on": engine.store.state(run_id).get("blocked_on")}
        high_risk_files = checks.get("scope", {}).get("high_risk_files") or []
        if high_risk_files:
            # A high-risk path match forces a human-gated status regardless of
            # auto_approve_docs/auto_ship/auto_resolve — see
            # "checks_high_risk_escalated"'s exclusion from AUTO_TRANSITIONS
            # and _advance_one_run's conditionally-unioned auto-transition
            # sets (mirrors how review_escalated is always human-gated). This
            # check runs regardless of any autonomy flag being enabled —
            # that's the whole point of the feature.
            engine.store.update_state(run_id, status="checks_high_risk_escalated",
                                      blocked_on="high_risk_paths")
            return {"advanced": False, "from": status, "action": "checks_high_risk_escalated",
                    "high_risk_files": high_risk_files}
        # Deterministic e2e run (no LLM) between checks and the evidence stage —
        # keeps a slow/hanging Playwright suite from burning or killing the
        # expensive evidence agent turn. No-op (pass=True) when unconfigured.
        e2e = run_e2e_tests(engine.store, run_id, engine.cfg.e2e, engine.work_dir(run_id),
                            engine.cfg.git.base_branch)
        if not e2e["pass"]:
            engine.store.update_state(run_id, status="blocked", blocked_on="e2e", checks="pass")
            return {"advanced": False, "from": status, "action": "e2e_failed"}
        if "evidence" not in engine.cfg.stages:
            # Project's cfg.stages skips the evidence stage entirely — jump
            # straight to whatever comes after it (review, if enabled) rather
            # than unconditionally invoking an agent stage the project never
            # asked to run. Mirrors the evidence_complete branch immediately
            # below, since this run is skipping directly to that point.
            if engine.cfg.review.enabled:
                out = run_review(engine.store, run_id, engine.cfg, engine.work_dir(run_id))
                return {"advanced": True, "from": status, "action": "evidence_skipped->review",
                        "verdict": out["verdict"]}
            # Neither evidence nor review is configured — build_complete is
            # already the pipeline's actual terminus for this project; there's
            # nothing further to auto-advance (same as evidence_complete's own
            # "review_disabled" branch just below, reached the normal way).
            return {"advanced": False, "from": status, "action": "review_disabled"}
        # Resume the prior evidence session if one exists for this run (e.g. a
        # second pass after review sent build back for fixes) instead of always
        # starting fresh — a fresh session re-does all evidence-gathering work
        # from scratch (re-greps, re-reads every file, re-runs build/lint) even
        # though the evidence.md prompt template's own "append ## Pass N, don't
        # overwrite" instruction only prevents the *file* from being clobbered,
        # not the redundant verification work a brand-new session repeats.
        has_prior_evidence_session = bool(engine.store.get_session_id(run_id, "evidence"))
        engine.run_agent_stage(run_id, "evidence", resume=has_prior_evidence_session)
        return {"advanced": True, "from": status, "action": "checks_passed->evidence"}

    if status == "evidence_complete":
        if engine.cfg.review.enabled:
            out = run_review(engine.store, run_id, engine.cfg, engine.work_dir(run_id))
            return {"advanced": True, "from": status, "action": "review", "verdict": out["verdict"]}
        return {"advanced": False, "from": status, "action": "review_disabled"}

    if status == "review_changes_requested":
        engine.run_agent_stage(run_id, "build", resume=True)
        return {"advanced": True, "from": status, "action": "resume_build"}

    if status == "review_approved" and engine.cfg.git.auto_ship:
        from .ship import ship_run
        out = ship_run(engine, run_id)
        return {"advanced": True, "from": status,
                "action": "auto_shipped" if out.get("ok") else "auto_ship_failed",
                "pr_url": (out.get("pr") or {}).get("url")}

    if status == "ship_failed" and engine.cfg.git.auto_ship:
        # Previously no auto-retry existed at all for ship_failed — every
        # occurrence (git push/PR-create step interrupted mid-flight, a
        # transient network blip, gh rate-limiting) needed manual
        # intervention every time, even though the underlying commit was
        # already real and a bare re-run of ship_run is safe (commit_all is
        # a no-op if nothing changed, push/create_pr are naturally
        # idempotent-ish for a retry). Capped at the same resolve_attempts
        # limit as the resolver escalation path — not a dedicated config
        # knob, since ship failures are rarer and don't need their own.
        ship_attempts = engine.store.state(run_id).get("ship_attempt_count", 0)
        if ship_attempts >= engine.cfg.checks.resolve_attempts:
            return {"advanced": False, "from": status, "action": "no_auto_transition"}
        engine.store.update_state(run_id, ship_attempt_count=ship_attempts + 1)
        from .ship import ship_run
        out = ship_run(engine, run_id)
        return {"advanced": True, "from": status,
                "action": "auto_shipped" if out.get("ok") else "auto_ship_retry_failed",
                "ship_attempts": ship_attempts + 1,
                "pr_url": (out.get("pr") or {}).get("url")}

    if status == "blocked":
        blocked_on = engine.store.state(run_id).get("blocked_on")
        if blocked_on not in ("scope", "checks", "e2e"):
            return {"advanced": False, "from": status, "action": "no_auto_transition"}
        retry_count = engine.store.state(run_id).get("checks_retry_count", 0)
        if retry_count >= engine.cfg.checks.retry_checks:
            engine.store.update_state(run_id, status="checks_escalated")
            return {"advanced": False, "from": status, "action": "checks_escalated",
                    "retry_count": retry_count}
        detail = (_e2e_failure_detail(engine.store, run_id) if blocked_on == "e2e"
                  else _checks_failure_detail(engine.store, run_id))
        label_word = "E2e tests" if blocked_on == "e2e" else "Checks"
        engine.store.artifact_path(run_id, "answers/build.md").parent.mkdir(
            parents=True, exist_ok=True)
        this_attempt = (
            f"## Attempt {retry_count + 1}/{engine.cfg.checks.retry_checks}\n\n"
            f"{detail}\n\nFix the above and ensure {label_word.lower()} pass this time.\n")
        answers_text = _accumulate_retry_feedback(engine.store, run_id, label_word, this_attempt)
        engine.store.artifact_path(run_id, "answers/build.md").write_text(answers_text)
        engine.store.update_state(run_id, checks_retry_count=retry_count + 1)
        engine.run_agent_stage(run_id, "build", resume=True)
        return {"advanced": True, "from": status, "action": "retry_build_after_checks_failure",
                "retry_count": retry_count + 1}

    if status == "checks_escalated" and engine.cfg.checks.auto_resolve:
        # Real incident that motivated this: escalated states used to
        # dead-end at a human forever under passive polling — a run could
        # sit here indefinitely with nobody noticing unless someone happened
        # to check `gantry watch`. auto_resolve spawns a dedicated resolver
        # agent (Engine.run_resolver_stage) with the actual failure detail,
        # git diff/status, and an explicit instruction to verify its own fix
        # by re-running the repo's real check commands — gantry itself then
        # re-verifies via run_checks before accepting success, never trusting
        # the resolver's self-report (that's exactly the failure mode from
        # the build-agent incident: a resumed build claimed "build_complete"
        # while a real unresolved merge-conflict marker was still committed,
        # and auto-retry kept re-running the identical broken state because
        # nothing re-verified the actual result).
        resolve_attempts = engine.store.state(run_id).get("resolve_attempt_count", 0)
        if resolve_attempts >= engine.cfg.checks.resolve_attempts:
            engine.store.update_state(run_id, status="resolve_escalated")
            return {"advanced": False, "from": status, "action": "resolve_escalated",
                    "resolve_attempts": resolve_attempts}
        engine.store.update_state(run_id, resolve_attempt_count=resolve_attempts + 1)
        result = engine.run_resolver_stage(run_id)
        new_status = engine.store.state(run_id).get("status")
        return {"advanced": True, "from": status, "action": "resolver_attempted",
                "resolve_attempts": resolve_attempts + 1, "verified_pass": result["verified_pass"],
                "new_status": new_status}

    return {"advanced": False, "from": status, "action": "no_auto_transition"}


def _lock_path(engine: Engine, run_id: str) -> Path:
    return engine.store.run_dir(run_id) / ".advance.lock"


def _pid_alive(pid: int) -> bool:
    """True if a process with this PID currently exists. Uses signal 0 (no-op
    signal, just existence/permission check) rather than anything that could
    actually affect the process."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but owned by another user — still "alive" for our
        # purposes (can't be this reclaiming process's PID either way).
        return True
    except OSError:
        return False


def _acquire_lock(engine: Engine, run_id: str, stale_after: int = 1800) -> bool:
    """Best-effort lock so a slow-running stage (build/evidence routinely take
    minutes) doesn't get double-fired by the next 60s cron tick.

    Reclaims immediately (regardless of age) if the PID recorded in the lock
    file is no longer a running process — a crashed/killed holder leaves no
    live process, so there's no reason to wait out stale_after in that case.
    Falls back to the time-based stale_after threshold only when the PID
    check itself is inconclusive (e.g. unreadable/corrupt lock content) —
    a real incident hit this: a lock from an interrupted manual invocation
    sat for 13 minutes (under the 30-minute stale_after) blocking `gantry
    loop`'s passive advance --all from ever touching that run again, even
    though the process that wrote the lock was long gone."""
    lock = _lock_path(engine, run_id)
    if lock.exists():
        try:
            held_pid_text = lock.read_text().strip()
            held_pid = int(held_pid_text) if held_pid_text else None
        except (OSError, ValueError):
            held_pid = None
        if held_pid is not None and held_pid != os.getpid() and _pid_alive(held_pid):
            return False
        if held_pid is None:
            # Couldn't determine liveness — fall back to time-based staleness
            # rather than reclaiming an indeterminate lock immediately.
            try:
                age = time.time() - lock.stat().st_mtime
                if age < stale_after:
                    return False
            except OSError:
                logger.debug("could not stat lock file %s for staleness check", lock, exc_info=True)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(str(os.getpid()))
    return True


def _release_lock(engine: Engine, run_id: str) -> None:
    _lock_path(engine, run_id).unlink(missing_ok=True)


def _stage_timeout(cfg: GantryConfig, stage: str) -> int:
    if stage == "checks":
        return cfg.checks.timeout
    if stage == "review":
        return 900  # review.py has no configurable timeout today; matches its hardcoded default
    return cfg.model_for(stage).timeout


def _repair_stale_running(engine: Engine, run: dict[str, Any]) -> dict[str, Any] | None:
    """A run's own agent subprocess dying without going through
    Engine.run_agent_stage's normal return path (killed externally — OOM,
    terminal closed, machine slept, or a `gantry stage`/`gantry advance --run`
    invocation that itself got killed before reaching the TimeoutExpired
    handler) leaves state.json stuck at "{stage}_running" forever: nothing
    else ever writes to it again, so `gantry watch`/status lies about a dead
    run still being in flight.

    Engine.run_agent_stage now writes a `heartbeat_at` timestamp every
    HEARTBEAT_INTERVAL seconds for as long as its own process is alive — that
    heartbeat thread dies immediately with the process, unlike the agent
    subprocess it's timing, so staleness against a small multiple of the
    heartbeat interval detects a dead run in ~1 minute instead of waiting out
    the full stage timeout (which can be 15-30 minutes). Older runs with no
    `heartbeat_at` (state predates this field, or the process crashed before
    the first beat) fall back to the previous stage-timeout-based check.
    """
    status = run["status"]
    if not status.endswith("_running"):
        return None
    stage = status.removesuffix("_running")
    state = engine.store.state(run["id"])
    heartbeat_at = state.get("heartbeat_at")
    if heartbeat_at:
        age = time.time() - _iso_to_ts(heartbeat_at)
        # 3x was too tight in practice — a real resolve run observed taking
        # ~60s (== 3 * HEARTBEAT_INTERVAL) got killed right at the edge of
        # finishing. 6x gives real stages headroom against daemon-tick
        # scheduling jitter without meaningfully slowing genuine-crash
        # detection (still well under a minute at the default interval).
        grace = HEARTBEAT_INTERVAL * 6
        if age <= grace:
            return None
        detail = f"no heartbeat for {int(age)}s (interval {HEARTBEAT_INTERVAL}s)"
    else:
        timeout = _stage_timeout(engine.cfg, stage)
        age = time.time() - run["mtime"]
        if age <= timeout + 120:  # grace period beyond the subprocess's own cap
            return None
        detail = f"stale for {int(age)}s (stage timeout {timeout}s), no heartbeat recorded"
    engine.store.write_log(run["id"], f"{stage}.stderr",
                           f"Repaired stale status: {status} — {detail} — process presumed dead.")
    # Tagged so advance_run can tell "the subprocess got killed by something
    # outside gantry's control" (infra hiccup — safe to auto-retry fresh)
    # apart from a real `{stage}_failed` the agent itself reported (kept
    # human-gated, since that might be a genuine content problem).
    engine._set_status(run["id"], f"{stage}_failed", last_failure_reason="stale_heartbeat")
    return {"run_id": run["id"], "advanced": False, "action": "repaired_stale_running",
            "was": status, "age_seconds": int(age)}


def _advance_one_run(engine: Engine, run: dict, cfg: GantryConfig) -> dict[str, Any] | None:
    """Process a single run inside advance_all. Returns a result dict or None
    (when the run was filtered by status, no result to report). The per-run
    lock guarantees two concurrent advance_all ticks can never double-process
    the same run — same correctness mechanism as before, now the lock is also
    the bound that keeps concurrent advance_one_run calls from racing on a
    single run's state.json."""
    auto_transitions = (AUTO_TRANSITIONS
                       | ({"review_approved", "ship_failed"} if cfg.git.auto_ship else set())
                       | ({"checks_escalated"} if cfg.checks.auto_resolve else set())
                       | ({"spec_complete", "design_complete"} if cfg.git.auto_approve_docs else set()))
    rid = run["id"]
    repaired = _repair_stale_running(engine, run)
    if repaired:
        return repaired
    if (run["status"].endswith("_failed")
            and engine.store.state(rid).get("last_failure_reason") == "stale_heartbeat"):
        # The prior attempt didn't fail on its own merits — its subprocess got
        # killed by something outside gantry (OOM, terminal closed, machine
        # slept). Retrying fresh (no resume — there's no useful session to
        # continue from a process that never got to finish) is safe and
        # matches what a human would do by hand via `gantry retry`. A real
        # `{stage}_failed` from the agent itself has no this tag and stays
        # human-gated, same as before.
        stage = run["status"].removesuffix("_failed")
        if stage == "resolve":
            # run_resolver_stage doesn't self-track resolve_attempt_count
            # (the checks_escalated caller below does) — a heartbeat-killed
            # resolve attempt never got the chance to increment it either,
            # so without this check the stale-heartbeat path could retry
            # resolve forever, blowing straight past resolve_attempts.
            resolve_attempts = engine.store.state(rid).get("resolve_attempt_count", 0)
            if resolve_attempts >= engine.cfg.checks.resolve_attempts:
                engine.store.update_state(rid, status="resolve_escalated", last_failure_reason=None)
                return {"run_id": rid, "advanced": False, "action": "resolve_escalated",
                        "resolve_attempts": resolve_attempts}
        if not _acquire_lock(engine, rid):
            return {"run_id": rid, "advanced": False, "action": "skipped_locked"}
        try:
            engine.store.update_state(rid, last_failure_reason=None)
            if stage == "resolve":
                # Not a normal agent stage (no render_prompt template, no
                # resume semantics) — run_resolver_stage is its own method,
                # see Engine.run_resolver_stage. Calling run_agent_stage here
                # would mis-resolve cfg.model_for("resolve") and blow up.
                engine.store.update_state(rid, resolve_attempt_count=resolve_attempts + 1)
                engine.run_resolver_stage(rid)
            elif stage == "review":
                # Also not a normal agent stage — run_review has its own
                # (store, run_id, cfg, cwd) signature, see review.py.
                run_review(engine.store, rid, engine.cfg, engine.work_dir(rid))
            else:
                engine.run_agent_stage(rid, stage, resume=False)
            return {"run_id": rid, "advanced": True, "action": f"retry_after_stale_heartbeat_{stage}"}
        except Exception as exc:
            return {"run_id": rid, "advanced": False, "error": str(exc)}
        finally:
            _release_lock(engine, rid)
    if run["status"] not in auto_transitions:
        return None
    if not _acquire_lock(engine, rid):
        return {"run_id": rid, "advanced": False, "action": "skipped_locked"}
    try:
        return {"run_id": rid, **advance_run(engine, rid)}
    except Exception as exc:
        return {"run_id": rid, "advanced": False, "error": str(exc)}
    finally:
        _release_lock(engine, rid)


def advance_all(target: Path, cfg: GantryConfig, tag: str | None = None) -> list[dict[str, Any]]:
    engine = Engine(target, cfg)
    candidates = [r for r in engine.store.list_runs() if not tag or r.get("tag") == tag]

    # [agent].max_concurrent = 0 (default) means "unbounded" in the config's
    # own terms, but is deliberately treated here as "stay serial" — today's
    # actual behavior, unchanged unless a project explicitly opts in by
    # setting a concurrency number. A silent switch to unbounded threaded
    # subprocesses the moment this code shipped (with no config change on the
    # project's part) would be a surprising behavior change for every
    # existing gantry.toml, not a bug fix.
    if cfg.agent.max_concurrent and cfg.agent.max_concurrent > 1:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=cfg.agent.max_concurrent) as pool:
            futures = [pool.submit(_advance_one_run, engine, run, cfg) for run in candidates]
            results = [r for f in futures if (r := f.result()) is not None]
    else:
        results = []
        for run in candidates:
            r = _advance_one_run(engine, run, cfg)
            if r is not None:
                results.append(r)
    return results
