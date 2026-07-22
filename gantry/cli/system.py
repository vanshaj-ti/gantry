"""System/environment commands: cockpit, update, daemon, mcp, setup."""
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
    tgt = _target()
    if args.daemon_action == "install":
        return _out(install_daemon(tgt, interval_seconds=args.interval))
    if args.daemon_action == "uninstall":
        return _out(uninstall_daemon(tgt))
    return _out(daemon_status())


def cmd_daemon_tick(args) -> int:
    """Entrypoint the background job execs each interval — not meant to be
    run by hand. Loops every registered target (see `daemon.add_target`)
    and advances each once."""
    from ..daemon import run_tick
    run_tick()
    return 0


def _runner_availability() -> dict:
    from ..runners import _RUNNERS
    return {name: bool(shutil.which(cls().build_command(
        prompt="x", model="", session_id=None, plan_mode=False, skip_permissions=False,
        output_format="json", session_name="x", max_turns=1)[0]))
        for name, cls in _RUNNERS.items()}


def cmd_setup(args) -> int:
    """One-command project bring-up: scaffold gantry.toml + prompts, build the
    Docker image (claude + codex both installed, see Dockerfile), start the
    per-project container. Composes the three existing steps a user would
    otherwise run by hand (`init`, `docker build`, `docker up`) — no new
    behavior beyond that, so failures are attributable to the exact
    underlying command that produced them."""
    from .run_commands import scaffold
    from .. import docker as _docker
    if not shutil.which("docker"):
        return _out({"ok": False, "error": "docker not found on PATH — required for `gantry setup`"})
    tgt = _target()
    result: dict = {"ok": True, "runners_available": _runner_availability()}
    result["init"] = scaffold(tgt, force=getattr(args, "force", False))
    if not result["init"]["ok"] and not getattr(args, "force", False):
        # gantry.toml already existed — not fatal, setup still proceeds to
        # build/up against whatever config is already there.
        pass
    result["docker_build"] = _docker.build_image()
    if not result["docker_build"]["ok"]:
        result["ok"] = False
        return _out(result)
    result["docker_up"] = _docker.up(tgt, interval_seconds=getattr(args, "interval", 60))
    if not result["docker_up"]["ok"]:
        result["ok"] = False
    return _out(result)


def cmd_docker(args) -> int:
    """Run gantry for the target project (GANTRY_TARGET / cwd, same
    resolution as every other command) inside its own Docker container —
    isolates its spawned claude/codex subprocesses from the host and any
    other active session. See docker.py and the repo Dockerfile."""
    from .. import docker as _docker
    if args.docker_action == "build":
        return _out(_docker.build_image())
    if args.docker_action == "status":
        return _out(_docker.status())
    tgt = _target()
    if args.docker_action == "up":
        return _out(_docker.up(tgt, interval_seconds=args.interval))
    return _out(_docker.down(tgt))
