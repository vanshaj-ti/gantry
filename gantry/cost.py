"""Cost/token tracking, independent of any one runner or storage detail.

Kept as its own module (not folded into runners.py/state.py) so the
normalization logic — what "usage" means, how per-run totals accumulate,
how a repo-wide total is computed — has one home as more runners and more
callers (CLI, watch, herdr, notify) are added. Callers only ever go through
the functions here; nothing else parses a runner's raw JSON for cost fields
or reaches directly into cost.json's shape.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from typing import Any

from .state import RunStore

logger = logging.getLogger(__name__)

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


def _codex_cost_from_ccusage(session_id: str | None) -> float | None:
    """codex-cli (ChatGPT/gateway-auth) never reports cost_usd in its own
    `--json` event stream — no billing field exists there at all, only token
    counts (see runners.py CodexRunner._parse_jsonl). ccusage
    (https://ccusage.com, npm package) computes real $ cost from the local
    ~/.codex/sessions rollout files against LiteLLM/gateway pricing, keyed by
    the same thread_id gantry already stores as session_id. Returns None
    (never raises) if ccusage isn't installed, the session hasn't been
    written to disk yet, or nothing matches — callers already treat None as
    "no data", same as an unsupported runner."""
    if not session_id or not shutil.which("npx"):
        return None
    try:
        proc = subprocess.run(
            ["npx", "--yes", "ccusage@20", "codex", "session", "--json"],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            return None
        data = json.loads(proc.stdout)
        for session in data.get("sessions", []):
            if session_id in session.get("sessionId", ""):
                return session.get("costUSD")
    except Exception:
        logger.debug("ccusage cost lookup failed for session_id=%s", session_id, exc_info=True)
    return None


def accumulate(store: RunStore, run_id: str, stage: str, usage: dict[str, Any],
               runner: str = "", session_id: str | None = None) -> dict[str, Any]:
    """Fold one stage invocation's usage into the run's cost.json, and mirror
    the running total onto state.json (cheap: `gantry watch` and the herdr
    status pane both want the total without opening cost.json for every run
    on every render)."""
    report = store.read_result(run_id, COST_RESULT_NAME) or _empty_report()
    if not report.get("by_stage"):
        report = _empty_report()

    cost = usage.get("cost_usd")
    if cost is None and runner == "codex-cli":
        cost = _codex_cost_from_ccusage(session_id)
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
    # Mirror token totals alongside cost_usd — codex-cli (ChatGPT-auth) never
    # reports cost_usd at all (ChatGPT auth isn't billed per-token via this
    # CLI, see CodexRunner._parse_jsonl), so a codex-only run's total_cost_usd
    # stays 0.0 forever even though real token usage IS being tracked in
    # cost.json. Without this, `gantry watch`'s status pane looked like codex
    # stages weren't being cost-tracked at all — they were, just not in the
    # $-denominated field the pane displayed.
    store.update_state(run_id, total_cost_usd=report["total_cost_usd"],
                       total_input_tokens=report["total_input_tokens"],
                       total_output_tokens=report["total_output_tokens"])
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
