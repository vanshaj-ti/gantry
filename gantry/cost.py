"""Cost/token tracking, independent of any one runner or storage detail.

Kept as its own module (not folded into runners.py/state.py) so the
normalization logic — what "usage" means, how per-run totals accumulate,
how a repo-wide total is computed — has one home as more runners and more
callers (CLI, watch, herdr, notify) are added. Callers only ever go through
the functions here; nothing else parses a runner's raw JSON for cost fields
or reaches directly into cost.json's shape.
"""
from __future__ import annotations

from typing import Any

from .state import RunStore

COST_RESULT_NAME = "cost.json"

_EMPTY_USAGE = {"cost_usd": None, "input_tokens": None, "output_tokens": None, "duration_ms": None}


def extract_usage(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize a runner's raw JSON result into {cost_usd, input_tokens,
    output_tokens, duration_ms}. Never raises on missing/differently-shaped
    fields — an unsupported runner (or a codex transcript with no usage
    event) just yields all-None, which every caller here already treats as
    "no data" rather than "zero cost"."""
    if not isinstance(raw, dict):
        return dict(_EMPTY_USAGE)
    usage_block = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
    return {
        "cost_usd": raw.get("total_cost_usd"),
        "input_tokens": usage_block.get("input_tokens"),
        "output_tokens": usage_block.get("output_tokens"),
        "duration_ms": raw.get("duration_ms"),
    }


def _empty_report() -> dict[str, Any]:
    return {"total_cost_usd": 0.0, "total_input_tokens": 0, "total_output_tokens": 0, "by_stage": {}}


def accumulate(store: RunStore, run_id: str, stage: str, usage: dict[str, Any]) -> dict[str, Any]:
    """Fold one stage invocation's usage into the run's cost.json, and mirror
    the running total onto state.json (cheap: `gantry watch` and the herdr
    status pane both want the total without opening cost.json for every run
    on every render)."""
    report = store.read_result(run_id, COST_RESULT_NAME) or _empty_report()
    if not report.get("by_stage"):
        report = _empty_report()

    cost = usage.get("cost_usd")
    in_tok = usage.get("input_tokens")
    out_tok = usage.get("output_tokens")

    if cost is not None:
        report["total_cost_usd"] = round(report["total_cost_usd"] + cost, 6)
    if in_tok is not None:
        report["total_input_tokens"] += in_tok
    if out_tok is not None:
        report["total_output_tokens"] += out_tok

    entry = report["by_stage"].get(stage, {"cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0, "calls": 0})
    if cost is not None:
        entry["cost_usd"] = round(entry["cost_usd"] + cost, 6)
    if in_tok is not None:
        entry["input_tokens"] += in_tok
    if out_tok is not None:
        entry["output_tokens"] += out_tok
    entry["calls"] += 1
    report["by_stage"][stage] = entry

    store.write_result(run_id, COST_RESULT_NAME, report)
    store.update_state(run_id, total_cost_usd=report["total_cost_usd"])
    return report


def report_for_run(store: RunStore, run_id: str) -> dict[str, Any]:
    return store.read_result(run_id, COST_RESULT_NAME) or _empty_report()


def total_all_runs(store: RunStore) -> dict[str, Any]:
    """Repo-wide total across every run this store knows about. Cheap at the
    scale gantry operates at (one small JSON read per run, only called on
    status transitions or explicit `gantry cost`/watch renders — never
    polled in a tight loop)."""
    total_cost = 0.0
    total_in = 0
    total_out = 0
    per_run: list[dict[str, Any]] = []
    for run in store.list_runs():
        report = report_for_run(store, run["id"])
        total_cost += report.get("total_cost_usd", 0.0)
        total_in += report.get("total_input_tokens", 0)
        total_out += report.get("total_output_tokens", 0)
        if report.get("total_cost_usd"):
            per_run.append({"run_id": run["id"], "title": run.get("title", ""),
                            "cost_usd": report["total_cost_usd"]})
    per_run.sort(key=lambda r: r["cost_usd"], reverse=True)
    return {
        "total_cost_usd": round(total_cost, 6),
        "total_input_tokens": total_in,
        "total_output_tokens": total_out,
        "top_runs": per_run,
    }
