"""24/7 auto-advance daemon: install/uninstall a per-OS background job that
runs `gantry advance --all` on a fixed interval against a target repo.

Without this, hands-off pipeline progression only exists as long as someone
manually re-runs `gantry advance --all` (or the broken-until-recently
`gantry loop`) in a foreground shell. This generates the OS-native background
job (launchd on macOS, a systemd user timer on Linux) so it survives reboots
and terminal closes, without hardcoding any machine's paths.
"""
from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from pathlib import Path

LABEL = "ai.gantry.advance"
SYSTEMD_UNIT_NAME = "gantry-advance"


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


def _macos_plist_xml(target: Path, interval_seconds: int, log_dir: Path) -> str:
    gantry_bin = _gantry_bin()
    venv_bin_dir = str(Path(gantry_bin).parent)
    path_env = f"{venv_bin_dir}:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    out_log = log_dir / "advance.log"
    err_log = log_dir / "advance.error.log"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LABEL}</string>

    <key>ProgramArguments</key>
    <array>
        <string>{gantry_bin}</string>
        <string>advance</string>
        <string>--all</string>
    </array>

    <key>WorkingDirectory</key>
    <string>{target}</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>{path_env}</string>
        <key>GANTRY_TARGET</key>
        <string>{target}</string>
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


def _systemd_service_ini(target: Path, log_dir: Path) -> str:
    gantry_bin = _gantry_bin()
    return f"""[Unit]
Description=Gantry auto-advance ({target})

[Service]
Type=oneshot
WorkingDirectory={target}
Environment=GANTRY_TARGET={target}
ExecStart={gantry_bin} advance --all
StandardOutput=append:{log_dir / "advance.log"}
StandardError=append:{log_dir / "advance.error.log"}
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
    system = platform.system()
    log_dir = target / ".agent-runs" / "_daemon-logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    if system == "Darwin":
        plist_path = _launchd_plist_path()
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        plist_path.write_text(_macos_plist_xml(target, interval_seconds, log_dir))
        subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
        proc = subprocess.run(["launchctl", "load", str(plist_path)],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            return {"ok": False, "platform": system, "error": proc.stderr.strip(),
                    "path": str(plist_path)}
        return {"ok": True, "platform": system, "path": str(plist_path),
                "interval_seconds": interval_seconds}

    if system == "Linux":
        unit_dir = _systemd_unit_dir()
        unit_dir.mkdir(parents=True, exist_ok=True)
        service_path = unit_dir / f"{SYSTEMD_UNIT_NAME}.service"
        timer_path = unit_dir / f"{SYSTEMD_UNIT_NAME}.timer"
        service_path.write_text(_systemd_service_ini(target, log_dir))
        timer_path.write_text(_systemd_timer_ini(interval_seconds))
        proc = subprocess.run(
            ["systemctl", "--user", "enable", "--now", f"{SYSTEMD_UNIT_NAME}.timer"],
            capture_output=True, text=True)
        if proc.returncode != 0:
            return {"ok": False, "platform": system, "error": proc.stderr.strip(),
                    "path": str(timer_path)}
        return {"ok": True, "platform": system, "path": str(timer_path),
                "interval_seconds": interval_seconds}

    return {"ok": False, "platform": system,
            "error": f"no daemon support for {system!r} yet — run "
                     f"`gantry advance --all` on a cron/scheduled task manually."}


def uninstall_daemon() -> dict:
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

    if system == "Darwin":
        plist_path = _launchd_plist_path()
        if not plist_path.exists():
            return {"platform": system, "installed": False}
        proc = subprocess.run(["launchctl", "list", LABEL], capture_output=True, text=True)
        return {"platform": system, "installed": True, "loaded": proc.returncode == 0,
                "path": str(plist_path)}

    if system == "Linux":
        timer_path = _systemd_unit_dir() / f"{SYSTEMD_UNIT_NAME}.timer"
        if not timer_path.exists():
            return {"platform": system, "installed": False}
        proc = subprocess.run(["systemctl", "--user", "is-active", f"{SYSTEMD_UNIT_NAME}.timer"],
                              capture_output=True, text=True)
        return {"platform": system, "installed": True,
                "active": proc.stdout.strip() == "active", "path": str(timer_path)}

    return {"platform": system, "installed": False,
            "error": f"no daemon support for {system!r}"}
