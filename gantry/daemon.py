"""24/7 auto-advance daemon: install/uninstall a per-OS background job that
runs the daemon tick (advancing every registered target repo) on a fixed
interval.

Without this, hands-off pipeline progression only exists as long as someone
manually re-runs `gantry advance --all` (or the broken-until-recently
`gantry loop`) in a foreground shell. This generates the OS-native background
job (launchd on macOS, a systemd user timer on Linux) so it survives reboots
and terminal closes, without hardcoding any machine's paths.

A single job (fixed label/unit name) serves every target repo — the targets
themselves are a small persisted list (see `add_target`/`remove_target`),
not one job per repo. `run_tick` (in this module) is what the job actually
execs each tick; it loops the persisted target list and calls
`advance.advance_all` on each.
"""
from __future__ import annotations

import concurrent.futures
import json
import logging
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .state import _iso_to_ts, now_iso

logger = logging.getLogger(__name__)

LABEL = "ai.gantry.advance"
SYSTEMD_UNIT_NAME = "gantry-advance"

_CONFIG_DIR = Path.home() / ".config" / "gantry"
_TARGETS_FILE = _CONFIG_DIR / "daemon-targets.json"
_LOG_DIR = _CONFIG_DIR / "daemon-logs"
_LAST_TICK_FILE = _CONFIG_DIR / "daemon-last-tick.json"

# Multiplier against the daemon's own tick interval to decide "the daemon
# job itself has gone quiet" — mirrors the grace-multiplier pattern in
# advance.py::_repair_stale_running (that one detects a dead per-run agent
# subprocess via a heartbeat file; this one detects a dead *daemon job*, at
# a much coarser granularity, via the last-completed-tick timestamp). A
# single missed tick (scheduling jitter, one long-running target eating the
# interval) shouldn't page anyone — only a run of several.
_HEARTBEAT_STALE_MULTIPLIER = 4


def _launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _systemd_unit_dir() -> Path:
    return Path.home() / ".config" / "systemd" / "user"


def _gantry_bin() -> str:
    """Resolve the `gantry` console-script actually on PATH for this venv,
    so the daemon invokes the same install the user is running `gantry
    daemon install` from — not a hardcoded location."""
    found = shutil.which("gantry")
    return found or (str(Path(sys.executable).parent / "gantry"))


def _load_targets() -> list[Path]:
    if not _TARGETS_FILE.exists():
        return []
    try:
        raw = json.loads(_TARGETS_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    return [Path(p) for p in raw]


def _save_targets(targets: list[Path]) -> None:
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _TARGETS_FILE.write_text(json.dumps([str(t) for t in targets], indent=2))


def add_target(target: Path) -> list[Path]:
    """Register a target repo for the shared daemon tick. Idempotent —
    re-adding an already-registered target is a no-op. Returns the full
    target list after the add."""
    target = target.resolve()
    targets = _load_targets()
    if target not in targets:
        targets.append(target)
        _save_targets(targets)
    return targets


def remove_target(target: Path) -> list[Path]:
    """Unregister a target repo. Returns the remaining target list — an
    empty result means the caller should tear the whole job down."""
    target = target.resolve()
    targets = [t for t in _load_targets() if t != target]
    _save_targets(targets)
    return targets


_TICK_LOCK = _CONFIG_DIR / "daemon-tick.lock"


def _tick_lock_acquire() -> bool:
    """Single-flight guard around the whole tick. launchd's StartInterval
    fires every interval_seconds regardless of whether the previous
    invocation finished — a resolve/build/evidence stage routinely runs
    longer than the 60s default interval, so without this a second tick can
    start advancing the same runs concurrently with the first, racing on
    each run's state.json (the per-run .advance.lock only protects a single
    run — an in-flight resolve/evidence call on run A doesn't stop a second
    tick from processing run B, or worse, catching run A mid-write)."""
    import os
    if _TICK_LOCK.exists():
        try:
            held_pid = int(_TICK_LOCK.read_text().strip())
        except (OSError, ValueError):
            held_pid = None
        if held_pid is not None:
            try:
                os.kill(held_pid, 0)
                return False  # still alive — previous tick not done yet
            except ProcessLookupError:
                pass  # holder is dead — safe to reclaim
            except PermissionError:
                return False
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _TICK_LOCK.write_text(str(os.getpid()))
    return True


def _tick_lock_release() -> None:
    _TICK_LOCK.unlink(missing_ok=True)


def _record_tick_completed() -> None:
    """Persist "the daemon job itself completed a tick just now" — separate
    from any individual target's success/failure (those are already
    reported per-target below). This file existing and being fresh is what
    `daemon_heartbeat_status` checks: it answers "is the background job
    still firing at all", not "are the targets healthy" (a target can fail
    every tick forever and this file still updates fine, which is correct —
    that's a target problem, not a daemon-liveness problem)."""
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _LAST_TICK_FILE.write_text(json.dumps({"completed_at": now_iso()}))


def daemon_heartbeat_status(interval_seconds: int) -> dict:
    """Is the background job still actually firing? Compares the
    last-completed-tick timestamp (written by `_record_tick_completed` at
    the end of every successful `run_tick`) against a small multiple of the
    configured interval — see `_HEARTBEAT_STALE_MULTIPLIER` for why a
    multiple instead of the raw interval (avoid false-alarming on a single
    slow tick or scheduling jitter)."""
    if not _LAST_TICK_FILE.exists():
        # Never ticked yet (freshly installed) — not "stale", just unknown.
        return {"stale": False, "last_tick_at": None, "age_seconds": None}
    try:
        data = json.loads(_LAST_TICK_FILE.read_text())
        last_tick_at = data.get("completed_at")
    except (OSError, json.JSONDecodeError):
        last_tick_at = None
    if not last_tick_at:
        return {"stale": False, "last_tick_at": None, "age_seconds": None}
    age = int(time.time() - _iso_to_ts(last_tick_at))
    grace = interval_seconds * _HEARTBEAT_STALE_MULTIPLIER
    return {"stale": age > grace, "last_tick_at": last_tick_at, "age_seconds": age}


def _notify_daemon_stale(heartbeat: dict) -> None:
    """A tick starting after a long silence usually means something OTHER
    than this tick was broken (job unloaded, machine asleep for days, a
    prior tick wedged the lock forever) — by definition the daemon status
    field nobody polls won't help here, so this fires a real notification.

    Multiple targets can each carry their own [notify] config in their own
    gantry.toml — rather than pick one target's backend arbitrarily (which
    would silently depend on target list ordering) or fan the same message
    out through every registered target's backend (noisy, and most
    installs only configure notify on one "primary" target anyway), this
    uses the FIRST registered target that has notify configured (backend !=
    "none"). That's a deliberate simplification: daemon liveness is a
    machine-wide concern, not a per-target one, so one real channel getting
    the alert is enough — it is not trying to fan out per target."""
    from .config import load_config
    from .notify import get_notifier

    for target in _load_targets():
        try:
            cfg = load_config(target)
        except Exception:
            continue
        if cfg.notify.backend == "none":
            continue
        notifier = get_notifier(cfg.notify)
        age = heartbeat.get("age_seconds")
        last = heartbeat.get("last_tick_at") or "never"
        text = (f"gantry daemon-tick has not completed successfully in "
                f"{age}s (last completed: {last}). The background job may "
                f"be stuck, unloaded, or the machine was asleep — check "
                f"`gantry daemon status`.")
        try:
            notifier.send(text, meta={"kind": "daemon_stale", "age_seconds": age})
        except Exception:
            logger.warning("failed to send daemon-stale notification", exc_info=True)
        return  # one notified target is enough — see docstring


def _advance_target(target: Path) -> dict:
    from .advance import advance_all
    from .config import load_config
    cfg = load_config(target)
    advanced = advance_all(target, cfg)
    return {"target": str(target), "ok": True, "advanced": len(advanced)}


def run_tick(interval_seconds: int = 60) -> list[dict]:
    """What the background job actually execs each interval: advance every
    registered target once. A broken target (deleted repo, bad gantry.toml)
    is caught and reported per-target so it can't stop the rest from
    advancing — this runs unattended, so one bad repo silently blocking
    every other project's pipeline would be far worse than a single logged
    error line.

    Each target additionally gets a wall-clock timeout (see
    `_advance_target_with_timeout`) so one pathologically slow target can't
    consume the whole tick's time at every other target's expense — they're
    still processed strictly one at a time, in order; this is not about
    running targets concurrently, only about not letting one hang forever.
    """
    from .config import load_config

    # Staleness is checked at the START of the tick (before this tick's own
    # success can mask it) — see `_notify_daemon_stale` for why this is a
    # real notification, not just a status field.
    heartbeat = daemon_heartbeat_status(interval_seconds)
    if heartbeat["stale"]:
        _notify_daemon_stale(heartbeat)

    if not _tick_lock_acquire():
        print("skipped — previous tick still running")
        return []
    try:
        results = []
        for target in _load_targets():
            try:
                timeout = load_config(target).daemon.per_target_timeout_seconds
                results.append(_advance_target_with_timeout(target, timeout))
            except Exception as exc:
                results.append({"target": str(target), "ok": False, "error": str(exc)})
        for r in results:
            if r["ok"]:
                print(f"{r['target']}: advanced {r['advanced']} run(s)")
            else:
                print(f"{r['target']}: ERROR {r['error']}")
        _record_tick_completed()
        return results
    finally:
        _tick_lock_release()


def _advance_target_with_timeout(target: Path, timeout_seconds: int) -> dict:
    """Run one target's `advance_all` with a wall-clock cap so a hung
    `load_config`/subprocess check can't eat every other target's turn in
    this tick.

    `advance_all` is plain synchronous Python, not already isolated in a
    subprocess, so there is no clean way to actually kill it mid-flight —
    Python threads cannot be forcibly terminated. `ThreadPoolExecutor.
    result(timeout=...)` only stops *waiting* for the thread; the thread
    itself keeps running in the background (holding whatever locks/files it
    had open) until it finishes or the process exits. That's an accepted
    limitation here: the tick loop reports the timeout and moves on to the
    next target rather than blocking on a wedged one, which is the actual
    goal (protect the OTHER targets' turn this tick) — it does not attempt
    to reclaim the hung target's resources immediately. A one-worker pool
    is used deliberately (not shared across targets) so this stays strictly
    sequential processing with a per-target timeout, not concurrency across
    targets — a genuinely parallel tick is a different, out-of-scope
    change."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_advance_target, target)
        try:
            return future.result(timeout=timeout_seconds)
        except concurrent.futures.TimeoutError:
            return {"target": str(target), "ok": False,
                    "error": f"exceeded per-target timeout ({timeout_seconds}s)"}
        except Exception as exc:
            return {"target": str(target), "ok": False, "error": str(exc)}


def _macos_plist_xml(interval_seconds: int) -> str:
    gantry_bin = _gantry_bin()
    venv_bin_dir = str(Path(gantry_bin).parent)
    path_env = f"{venv_bin_dir}:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    out_log = _LOG_DIR / "advance.log"
    err_log = _LOG_DIR / "advance.error.log"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LABEL}</string>

    <key>ProgramArguments</key>
    <array>
        <string>{gantry_bin}</string>
        <string>daemon-tick</string>
    </array>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>{path_env}</string>
        <key>HOME</key>
        <string>{Path.home()}</string>
    </dict>

    <key>StartInterval</key>
    <integer>{interval_seconds}</integer>

    <key>RunAtLoad</key>
    <true/>

    <!-- `gantry daemon-tick` (see cmd_daemon_tick) always exits 0 on a
    normal tick — even a per-target failure is caught and reported inside
    run_tick, not raised — so a non-zero exit here can only mean the
    invocation itself crashed hard before reaching that return (uncaught
    exception, OOM kill, bad interpreter, etc). KeepAlive+SuccessfulExit=false
    tells launchd to relaunch specifically on that non-zero-exit case,
    which is exactly the "hard crash" gap StartInterval alone doesn't cover
    (StartInterval only re-fires on its own fixed schedule regardless of
    how the previous run exited). This does not fight with StartInterval:
    launchd documents KeepAlive and StartInterval as independent triggers
    for the same on-demand job — StartInterval keeps its normal periodic
    re-fire on success, KeepAlive only adds an extra relaunch after a crash.
    ThrottleInterval caps how soon that crash-triggered relaunch can happen
    so a persistently-broken invocation can't tight-loop launchd. -->
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>

    <key>ThrottleInterval</key>
    <integer>30</integer>

    <key>StandardOutPath</key>
    <string>{out_log}</string>

    <key>StandardErrorPath</key>
    <string>{err_log}</string>
</dict>
</plist>
"""


def _systemd_service_ini() -> str:
    gantry_bin = _gantry_bin()
    # Restart=on-failure: same rationale as the launchd KeepAlive/
    # SuccessfulExit block above — `gantry daemon-tick` only exits non-zero
    # when the invocation itself crashed hard (a normal tick, including one
    # with per-target failures, always exits 0), so this specifically
    # covers a hard crash getting retried sooner than the next timer fire.
    # RestartSec gives a small cool-down so a persistently-broken
    # invocation can't tight-loop systemd restarting it every few ms.
    return f"""[Unit]
Description=Gantry auto-advance (all registered targets)

[Service]
Type=oneshot
ExecStart={gantry_bin} daemon-tick
Restart=on-failure
RestartSec=10
StandardOutput=append:{_LOG_DIR / "advance.log"}
StandardError=append:{_LOG_DIR / "advance.error.log"}
"""


def _systemd_timer_ini(interval_seconds: int) -> str:
    return f"""[Unit]
Description=Run gantry auto-advance every {interval_seconds}s

[Timer]
OnBootSec={interval_seconds}s
OnUnitActiveSec={interval_seconds}s
AccuracySec=5s

[Install]
WantedBy=timers.target
"""


def install_daemon(target: Path, interval_seconds: int = 60) -> dict:
    add_target(target)
    system = platform.system()
    _LOG_DIR.mkdir(parents=True, exist_ok=True)

    if system == "Darwin":
        plist_path = _launchd_plist_path()
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        plist_path.write_text(_macos_plist_xml(interval_seconds))
        subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
        proc = subprocess.run(["launchctl", "load", str(plist_path)],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            return {"ok": False, "platform": system, "error": proc.stderr.strip(),
                    "path": str(plist_path)}
        return {"ok": True, "platform": system, "path": str(plist_path),
                "interval_seconds": interval_seconds, "targets": [str(t) for t in _load_targets()]}

    if system == "Linux":
        unit_dir = _systemd_unit_dir()
        unit_dir.mkdir(parents=True, exist_ok=True)
        service_path = unit_dir / f"{SYSTEMD_UNIT_NAME}.service"
        timer_path = unit_dir / f"{SYSTEMD_UNIT_NAME}.timer"
        service_path.write_text(_systemd_service_ini())
        timer_path.write_text(_systemd_timer_ini(interval_seconds))
        proc = subprocess.run(
            ["systemctl", "--user", "enable", "--now", f"{SYSTEMD_UNIT_NAME}.timer"],
            capture_output=True, text=True)
        if proc.returncode != 0:
            return {"ok": False, "platform": system, "error": proc.stderr.strip(),
                    "path": str(timer_path)}
        return {"ok": True, "platform": system, "path": str(timer_path),
                "interval_seconds": interval_seconds, "targets": [str(t) for t in _load_targets()]}

    return {"ok": False, "platform": system,
            "error": f"no daemon support for {system!r} yet — run "
                     f"`gantry daemon-tick` on a cron/scheduled task manually."}


def uninstall_daemon(target: Path) -> dict:
    remaining = remove_target(target)
    if remaining:
        return {"ok": True, "note": "target removed; job keeps running for remaining targets",
                "targets": [str(t) for t in remaining]}

    system = platform.system()

    if system == "Darwin":
        plist_path = _launchd_plist_path()
        if not plist_path.exists():
            return {"ok": True, "platform": system, "note": "not installed"}
        subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
        plist_path.unlink()
        return {"ok": True, "platform": system, "removed": str(plist_path)}

    if system == "Linux":
        unit_dir = _systemd_unit_dir()
        service_path = unit_dir / f"{SYSTEMD_UNIT_NAME}.service"
        timer_path = unit_dir / f"{SYSTEMD_UNIT_NAME}.timer"
        if not timer_path.exists() and not service_path.exists():
            return {"ok": True, "platform": system, "note": "not installed"}
        subprocess.run(["systemctl", "--user", "disable", "--now", f"{SYSTEMD_UNIT_NAME}.timer"],
                       capture_output=True)
        for p in (service_path, timer_path):
            if p.exists():
                p.unlink()
        return {"ok": True, "platform": system, "removed": [str(service_path), str(timer_path)]}

    return {"ok": False, "platform": system, "error": f"no daemon support for {system!r}"}


def daemon_status(interval_seconds: int = 60) -> dict:
    """`interval_seconds` defaults to `install_daemon`'s own default (60) —
    the actual configured interval isn't persisted anywhere separately
    queryable (it's baked into the installed plist/timer XML/ini), so this
    is a best-effort staleness threshold rather than reading the live
    installed interval back out. Good enough for "is this job clearly
    dead" — a wrong-by-a-few-seconds threshold doesn't change that
    conclusion given the multi-tick grace multiplier in
    `daemon_heartbeat_status`."""
    system = platform.system()
    targets = [str(t) for t in _load_targets()]
    heartbeat = daemon_heartbeat_status(interval_seconds)

    if system == "Darwin":
        plist_path = _launchd_plist_path()
        if not plist_path.exists():
            return {"platform": system, "installed": False, "targets": targets,
                    "heartbeat": heartbeat}
        proc = subprocess.run(["launchctl", "list", LABEL], capture_output=True, text=True)
        return {"platform": system, "installed": True, "loaded": proc.returncode == 0,
                "path": str(plist_path), "targets": targets, "heartbeat": heartbeat}

    if system == "Linux":
        timer_path = _systemd_unit_dir() / f"{SYSTEMD_UNIT_NAME}.timer"
        if not timer_path.exists():
            return {"platform": system, "installed": False, "targets": targets,
                    "heartbeat": heartbeat}
        proc = subprocess.run(["systemctl", "--user", "is-active", f"{SYSTEMD_UNIT_NAME}.timer"],
                              capture_output=True, text=True)
        return {"platform": system, "installed": True,
                "active": proc.stdout.strip() == "active", "path": str(timer_path),
                "targets": targets, "heartbeat": heartbeat}

    return {"platform": system, "installed": False, "targets": targets,
            "error": f"no daemon support for {system!r}"}
