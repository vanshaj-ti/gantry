"""Per-run locking for automatic pipeline advancement."""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from .engine import Engine

logger = logging.getLogger(__name__)


def _lock_path(engine: Engine, run_id: str) -> Path:
    return engine.store.run_dir(run_id) / ".advance.lock"


def _pid_alive(pid: int) -> bool:
    """Return whether a process with this PID currently exists."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _acquire_lock(engine: Engine, run_id: str, stale_after: int = 1800) -> bool:
    """Acquire a best-effort lock, reclaiming dead or stale holders."""
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
            try:
                age = time.time() - lock.stat().st_mtime
                if age < stale_after:
                    return False
            except OSError:
                logger.debug(
                    "could not stat lock file %s for staleness check", lock, exc_info=True,
                )
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(str(os.getpid()))
    return True


def _release_lock(engine: Engine, run_id: str) -> None:
    _lock_path(engine, run_id).unlink(missing_ok=True)
