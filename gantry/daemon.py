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

import json
import logging
import platform
import shutil
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

LABEL = "ai.gantry.advance"
SYSTEMD_UNIT_NAME = "gantry-advance"

_CONFIG_DIR = Path.home() / ".config" / "gantry"
_TARGETS_FILE = _CONFIG_DIR / "daemon-targets.json"
_LOG_DIR = _CONFIG_DIR / "daemon-logs"


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


def run_tick() -> list[dict]:
    """What the background job actually execs each interval: advance every
    registered target once. A broken target (deleted repo, bad gantry.toml)
    is caught and reported per-target so it can't stop the rest from
    advancing — this runs unattended, so one bad repo silently blocking
    every other project's pipeline would be far worse than a single logged
    error line."""
    from .advance import advance_all
    from .config import load_config

    if not _tick_lock_acquire():
        print("skipped — previous tick still running")
        return []
    try:
        results = []
        for target in _load_targets():
            try:
                cfg = load_config(target)
                advanced = advance_all(target, cfg)
                results.append({"target": str(target), "ok": True, "advanced": len(advanced)})
            except Exception as exc:
                results.append({"target": str(target), "ok": False, "error": str(exc)})
        for r in results:
            if r["ok"]:
                print(f"{r['target']}: advanced {r['advanced']} run(s)")
            else:
                print(f"{r['target']}: ERROR {r['error']}")
        return results
    finally:
        _tick_lock_release()


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

    <key>StandardOutPath</key>
    <string>{out_log}</string>

    <key>StandardErrorPath</key>
    <string>{err_log}</string>
</dict>
</plist>
"""


def _systemd_service_ini() -> str:
    gantry_bin = _gantry_bin()
    return f"""[Unit]
Description=Gantry auto-advance (all registered targets)

[Service]
Type=oneshot
ExecStart={gantry_bin} daemon-tick
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


def daemon_status() -> dict:
    system = platform.system()
    targets = [str(t) for t in _load_targets()]

    if system == "Darwin":
        plist_path = _launchd_plist_path()
        if not plist_path.exists():
            return {"platform": system, "installed": False, "targets": targets}
        proc = subprocess.run(["launchctl", "list", LABEL], capture_output=True, text=True)
        return {"platform": system, "installed": True, "loaded": proc.returncode == 0,
                "path": str(plist_path), "targets": targets}

    if system == "Linux":
        timer_path = _systemd_unit_dir() / f"{SYSTEMD_UNIT_NAME}.timer"
        if not timer_path.exists():
            return {"platform": system, "installed": False, "targets": targets}
        proc = subprocess.run(["systemctl", "--user", "is-active", f"{SYSTEMD_UNIT_NAME}.timer"],
                              capture_output=True, text=True)
        return {"platform": system, "installed": True,
                "active": proc.stdout.strip() == "active", "path": str(timer_path),
                "targets": targets}

    return {"platform": system, "installed": False, "targets": targets,
            "error": f"no daemon support for {system!r}"}
