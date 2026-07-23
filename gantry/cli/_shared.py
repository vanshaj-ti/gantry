"""Shared helpers used across gantry.cli submodules.

Kept out of __init__.py to avoid an import cycle: __init__.py imports the
cmd_* functions from the submodules below, and those submodules need these
helpers — so the helpers can't live in __init__.py itself.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from ..config import DOC_STAGES, load_config
from ..engine import Engine

# Statuses where a run is actually waiting on a human decision — the set
# `gantry listen` matches replies against.
NEEDS_INPUT_STATUSES = {
    "blocked", "review_escalated", "checks_high_risk_escalated",
    *(f"{stage}_complete" for stage in DOC_STAGES),  # always human-gated — never auto-advanced
    *(f"{stage}_failed" for stage in DOC_STAGES),
    "plan_failed", "build_failed", "evidence_failed",
}

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"


def _notify(store, notifier, run_id: str, text: str, meta: dict | None = None) -> dict:
    """Send a notification and record which run it belongs to, so a Telegram
    *reply* to this exact message resolves unambiguously to this run — even
    with several runs stuck at once. See RunStore.record_telegram_message."""
    res = notifier.send(text, meta=meta)
    if res.get("sent") and res.get("message_id") is not None:
        store.record_telegram_message(res["message_id"], run_id)
    return res


def _target() -> Path:
    return Path(os.environ.get("GANTRY_TARGET", os.getcwd())).resolve()


def _engine() -> Engine:
    tgt = _target()
    return Engine(tgt, load_config(tgt))


def _out(obj) -> int:
    print(json.dumps(obj, indent=2, default=str))
    return 0
