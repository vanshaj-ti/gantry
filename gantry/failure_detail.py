"""Human-facing rendering for pipeline failures."""
from __future__ import annotations

from typing import Any

from .checks import _matches_any
from .config import GantryConfig


def _escape_md(text: str) -> str:
    """Escape Telegram legacy Markdown special chars in plain text."""
    for ch in ("_", "*", "[", "`"):
        text = text.replace(ch, "\\" + ch)
    return text


def format_checks_failure_detail(
    checks: dict[str, Any] | None,
    *,
    include_merge_conflict: bool = True,
    normalize_optional_sections: bool = False,
) -> str:
    """Render actionable detail from a checks result dictionary."""
    if not checks or checks.get("pass"):
        return "Checks failed."
    merge = checks.get("base_branch_merge") or {}
    if include_merge_conflict and merge.get("action") == "merge_conflict":
        return (f"Merging the base branch into this run's own branch hit a real conflict "
                f"(another queued run shipped changes that overlap with this run's files):\n\n"
                f"```\n{merge.get('output', '(no output captured)')}\n```\n\n"
                f"Resolve the conflict markers in the affected files, then continue. "
                f"This is a genuine content conflict — resolve it deliberately, don't "
                f"discard either side's changes without checking what they do.")
    scope = (
        (checks.get("scope") or {})
        if normalize_optional_sections
        else checks.get("scope", {})
    )
    if scope.get("forbidden_files") or scope.get("unexpected_files"):
        bad = scope.get("forbidden_files", []) + scope.get("unexpected_files", [])
        file_list = "\n".join(f"  • `{f}`" for f in bad[:8])
        return f"Scope violation — files outside the plan:\n{file_list}"
    checks_section = (
        (checks.get("checks") or {})
        if normalize_optional_sections
        else checks.get("checks", {})
    )
    failing = [
        c["command"]
        for c in checks_section.get("results", [])
        if not c.get("pass")
    ]
    if failing:
        return "Failing command(s):\n" + "\n".join(f"  • `{c}`" for c in failing)
    return "Checks failed."


def _checks_failure_detail(store: Any, run_id: str) -> str:
    """Render the persisted checks failure for a run."""
    return format_checks_failure_detail(store.read_result(run_id, "checks.json"))


def _ship_checks_failure_detail(
    store: Any, run_id: str, blocking_findings: list[dict[str, Any]] | None = None,
) -> str:
    """Render ship-time checks or surviving blocking review findings."""
    if blocking_findings:
        lines = "\n".join(
            f"  • [{f.get('severity', '?')}] {f.get('location', '')}: {f.get('description', '')}"
            for f in blocking_findings[:8]
        )
        return (f"A `blocking` review finding survived to ship time (this should never happen — "
                f"REQUEST_CHANGES already gates on blocking findings earlier in the pipeline; "
                f"this is defense-in-depth catching a review→ship handoff bug):\n{lines}")
    return _checks_failure_detail(store, run_id)


def _spec_gate_failure_detail(store: Any, run_id: str) -> str:
    """Render the spec stage's deterministic structural gate failure."""
    gate = store.read_result(run_id, "spec-gate.json")
    if not gate or gate.get("pass"):
        return ""
    return f"Structural gate failed: {gate.get('reason', 'acceptance-criteria.json invalid')}"


def _high_risk_detail(store: Any, run_id: str, cfg: GantryConfig | None = None) -> str:
    """Render changed files matched by configured high-risk paths."""
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
    """Render failed applications from the deterministic e2e report."""
    report = store.read_result(run_id, "e2e-report.json")
    if not report or report.get("pass"):
        return "E2e tests failed."
    failing = [
        a["app"] for a in report.get("apps", [])
        if not a.get("skipped") and not a.get("pass")
    ]
    if failing:
        return "Failing e2e app(s):\n" + "\n".join(f"  • `{a}`" for a in failing)
    return "E2e tests failed."


def _review_findings_detail(review_result: dict[str, Any] | None) -> str:
    """Summarize actionable review findings for notifications."""
    if not review_result:
        return "No review result found."

    if review_result.get("two_axis"):
        parts = []
        for axis_name in ("spec", "standards"):
            axis = review_result.get(axis_name) or {}
            verdict = axis.get("verdict", "?")
            findings = axis.get("findings") or []
            notable = [f for f in findings if f.get("action") in ("blocking", "ask-user")]
            parts.append(f"*{axis_name.capitalize()} axis*: {verdict}")
            if notable:
                for f in notable[:5]:
                    parts.append(
                        f"  • [{f.get('action','?')}] "
                        f"{_escape_md(f.get('description', '')[:120])}"
                    )
                if len(notable) > 5:
                    parts.append(f"  … and {len(notable) - 5} more (see review-result.json)")
        return "\n".join(parts)

    note = (review_result.get("result") or "")[:400]
    return _escape_md(note) if note else "See review-result.json for details."
