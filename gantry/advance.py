"""Auto-advance a Gantry run based on its current pipeline state.

Automatic ticks are dispatched solely through ``machine.dispatch_automatic_advance``.
Support helpers are re-exported here for backward compatibility.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from .advance_batch import (
    AUTO_TRANSITIONS as AUTO_TRANSITIONS,
    _advance_one_run as _advance_one_run,
    advance_all as advance_all,
)
from .advance_dispatch import (
    _MAX_RETRY_ATTEMPTS_KEPT as _MAX_RETRY_ATTEMPTS_KEPT,
    _accumulate_retry_feedback as _accumulate_retry_feedback,
    _continue_after_verification as _continue_after_verification,
    _pipeline_version as _pipeline_version,
    _run_v2_checks as _run_v2_checks,
    _run_v2_e2e as _run_v2_e2e,
    _uses_explicit_verification as _uses_explicit_verification,
)
from .advance_lock import (
    _acquire_lock as _acquire_lock,
    _lock_path as _lock_path,
    _pid_alive as _pid_alive,
    _release_lock as _release_lock,
)
from .engine import Engine
from .failure_detail import (
    _checks_failure_detail,
    _e2e_failure_detail as _e2e_failure_detail,
    _escape_md as _escape_md,
    _high_risk_detail as _high_risk_detail,
    _review_findings_detail as _review_findings_detail,
    _ship_checks_failure_detail as _ship_checks_failure_detail,
    _spec_gate_failure_detail as _spec_gate_failure_detail,
)
from .labels import (
    SHORT_STATUS_LABELS as SHORT_STATUS_LABELS,
    STATUS_LABELS as STATUS_LABELS,
    label as label,
    short_label as short_label,
)
from .machine import dispatch_automatic_advance
from .notify_messages import (
    _STATUS_ICON as _STATUS_ICON,
    _icon as _icon,
    notify_message as notify_message,
)
from .stale_repair import (
    _repair_stale_running as _repair_stale_running,
    _stage_timeout as _stage_timeout,
)

logger = logging.getLogger(__name__)

# Re-export for callers/tests that imported these from advance.
__all__ = [
    "AUTO_TRANSITIONS",
    "advance_all",
    "advance_run",
    "label",
    "notify_message",
    "short_label",
    "_MAX_RETRY_ATTEMPTS_KEPT",
    "_acquire_lock",
    "_advance_one_run",
    "_checks_failure_detail",
    "_lock_path",
    "_pid_alive",
    "_release_lock",
    "_repair_stale_running",
    "_run_v2_checks",
    "_run_v2_e2e",
    "_uses_explicit_verification",
]


def advance_run(engine: Engine, run_id: str) -> dict[str, Any]:
    """Fire the appropriate next stage for one run."""
    result = _advance_run_inner(engine, run_id)
    _sync_linear_status_if_configured(engine, run_id)
    return result


def _sync_linear_status_if_configured(engine: Engine, run_id: str) -> None:
    from .linear import sync_issue_status_if_configured
    try:
        sync_issue_status_if_configured(engine.store, run_id)
    except Exception:
        logger.warning("Linear status sync failed for run %s", run_id, exc_info=True)


def _advance_run_inner(engine: Engine, run_id: str) -> dict[str, Any]:
    """Delegate to the machine's ordered dispatch table (sole dispatcher)."""
    return dispatch_automatic_advance(engine, run_id)
