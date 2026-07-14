"""System/environment commands: cockpit, update, daemon, mcp."""
from __future__ import annotations

import shutil

from ..config import load_config
from ._shared import _target, _out


def cmd_mcp(args) -> int:
    from ..mcp import ensure_mcp_for_stage
    tgt = _target()
    cfg = load_config(tgt)
    if args.list:
        return _out({"enabled": cfg.mcp.enabled,
                     "available": sorted(cfg.mcp.servers.keys()),
                     "runner": cfg.agent.runner})
    seen, results = set(), []
    for stage in ["plan", "build", "evidence", "review"]:
        runner = cfg.runner_for(stage)
        for r in ensure_mcp_for_stage(cfg, stage, runner, tgt):
            key = (r.get("server"), r.get("status"))
            if key not in seen:
                seen.add(key)
                results.append(r)
    return _out(results)


def cmd_cockpit(args) -> int:
    """Open (or reuse) a tmux workspace pre-wired for this target repo:
    status bar on top, doc viewer + live claude session below.
    --kill tears down an existing cockpit session instead (fresh start)."""
    from ..cockpit import attach, build_cockpit, kill_cockpit, session_name
    if not shutil.which("tmux"):
        return _out({"ok": False, "error": "tmux not found on PATH — required for `gantry cockpit`"})
    tgt = _target()
    if getattr(args, "kill", False):
        return _out(kill_cockpit(session_name(tgt)))
    result = build_cockpit(tgt)
    if not result["ok"]:
        return _out(result)
    attach(session_name(tgt))
    return 0  # unreachable on success — attach() execs into tmux


def cmd_update(args) -> int:
    """git pull + reinstall the gantry checkout this install runs from.
    Prints a plain status line, not JSON — this is a command run
    interactively by a person, unlike the rest of the CLI (which stays
    JSON for scriptability)."""
    from ..update import update_gantry
    result = update_gantry()
    if not result["ok"]:
        print(f"gantry update failed: {result.get('error') or result.get('output', 'unknown error')}")
        if result.get("dirty_files"):
            print("Dirty files:")
            for f in result["dirty_files"]:
                print(f"  {f}")
        return 1
    if not result["updated"]:
        print(f"Already up to date ({result['commit'][:8]}).")
    else:
        print(f"Updated {result['from_commit'][:8]} -> {result['to_commit'][:8]} "
              f"in {result['repo']}. Reinstalled.")
    return 0


def cmd_daemon(args) -> int:
    from ..daemon import daemon_status, install_daemon, uninstall_daemon
    if args.daemon_action == "install":
        tgt = _target()
        return _out(install_daemon(tgt, interval_seconds=args.interval))
    if args.daemon_action == "uninstall":
        return _out(uninstall_daemon())
    return _out(daemon_status())
