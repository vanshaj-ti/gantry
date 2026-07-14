"""Gantry CLI.

Verbs:
  gantry init                        scaffold gantry.toml + .gantry/prompts in the repo
  gantry run --title T --request R   create a run and start the pipeline
  gantry stage <stage> --run ID      run one agent stage (plan/build/evidence)
  gantry checks --run ID             run scope guard + repo checks
  gantry review --run ID             run the independent LLM review
  gantry approve --run ID --stage S  pass a human-review gate, advance
  gantry revise --run ID --stage S "comments"   send a stage back
  gantry ship --run ID                commit + push + open a PR (review_approved only)
  gantry status [--run ID]           show run(s) status
  gantry doctor                      check the environment (runners, git, config)
  gantry listen                      poll Telegram replies, act on the pending run
  gantry docs --run ID                render a run's spec/design/plan/evidence/review docs
  gantry cockpit                      open a tmux workspace pre-wired for this repo
  gantry update                       git pull + reinstall this gantry checkout

Target repo is $GANTRY_TARGET or the current working directory.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from .. import __version__
from .docs import cmd_doctor, cmd_docs
from .run_commands import (
    cmd_advance, cmd_approve, cmd_checks, cmd_init, cmd_loop, cmd_review,
    cmd_revise, cmd_run, cmd_ship, cmd_stage, cmd_status,
)
from .system import cmd_cockpit, cmd_daemon, cmd_mcp, cmd_update
from .watch import cmd_listen, cmd_watch


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gantry", description="Project-agnostic autonomous build pipeline")
    p.add_argument("--version", action="version", version=f"gantry {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("init", help="scaffold gantry.toml + prompts")
    s.add_argument("--force", action="store_true")
    s.add_argument("--with-skills", action="store_true",
                   help="also install enabled skills (e.g. superpowers) for the active runner")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("run", help="create a run and start the pipeline")
    s.add_argument("--title", required=True)
    s.add_argument("--request", default="")
    s.add_argument("--run", help="explicit run id")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("stage", help="run one stage (spec/design/plan/build/evidence)")
    s.add_argument("stage", choices=["spec", "design", "plan", "build", "evidence"])
    s.add_argument("--run", required=True)
    s.add_argument("--resume", action="store_true")
    s.set_defaults(func=cmd_stage)

    s = sub.add_parser("checks", help="scope guard + repo checks")
    s.add_argument("--run", required=True)
    s.set_defaults(func=cmd_checks)

    s = sub.add_parser("review", help="independent LLM review")
    s.add_argument("--run", required=True)
    s.set_defaults(func=cmd_review)

    s = sub.add_parser("approve", help="pass a human-review gate")
    s.add_argument("--run", required=True)
    s.add_argument("--stage", required=True)
    s.set_defaults(func=cmd_approve)

    s = sub.add_parser("revise", help="send a stage back with comments")
    s.add_argument("--run", required=True)
    s.add_argument("--stage", required=True)
    s.add_argument("comments")
    s.set_defaults(func=cmd_revise)

    s = sub.add_parser("ship", help="commit + push + open a PR (requires review_approved)")
    s.add_argument("--run", required=True)
    s.add_argument("--force", action="store_true", help="ship even if status isn't review_approved")
    s.set_defaults(func=cmd_ship)

    s = sub.add_parser("status", help="show run status")
    s.add_argument("--run")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("advance", help="drive the pipeline forward one tick")
    s.add_argument("--run", help="advance a single run")
    s.add_argument("--all", action="store_true", help="tick every run (poller mode); notifies on change")
    s.set_defaults(func=cmd_advance)

    s = sub.add_parser("loop", help="repeatedly tick the pipeline until terminal/human-gated "
                                     "(foreground alternative to an external cron)")
    s.add_argument("--run", help="loop a single run only (stops once it needs a human or finishes); "
                                 "omit to loop every run like the cron poller does")
    s.add_argument("--interval", type=int, default=15, help="seconds between ticks (default 15)")
    s.add_argument("--max-ticks", type=int, default=0,
                   help="stop after this many ticks regardless of state (0 = unbounded)")
    s.set_defaults(func=cmd_loop)

    s = sub.add_parser("doctor", help="check environment")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("listen", help="poll Telegram replies, act on the pending run")
    s.add_argument("--run", help="always apply replies to this run (default: the most recent needs-input run)")
    s.set_defaults(func=cmd_listen)

    s = sub.add_parser("docs", help="render a run's spec/design/plan/evidence/review docs (via glow if installed)")
    s.add_argument("--run", help="default: the most recently touched run")
    s.add_argument("--doc", help="a specific artifact filename (e.g. architecture-design.md); default: all")
    s.add_argument("--pick", action="store_true", help="interactive fzf nav: pick a run, then a doc, Esc to go back")
    s.add_argument("--follow", action="store_true", help="auto-refresh to whichever run is most recently touched")
    s.add_argument("--nav", action="store_true",
                   help="persistent arrow-key navigator (curses): run list -> doc list -> content")
    s.set_defaults(func=cmd_docs)

    s = sub.add_parser("watch", help="dashboard of all runs")
    s.add_argument("--live", action="store_true", help="refresh every 2s (default: one-shot)")
    s.set_defaults(func=cmd_watch)

    s = sub.add_parser("mcp", help="register/list MCP servers for the active runner")
    s.add_argument("--list", action="store_true", help="show enabled/available servers (default: register)")
    s.set_defaults(func=cmd_mcp)

    s = sub.add_parser("daemon", help="install/uninstall a 24/7 background job "
                                       "running `gantry advance --all` (launchd on "
                                       "macOS, systemd user timer on Linux)")
    s.add_argument("daemon_action", choices=["install", "uninstall", "status"])
    s.add_argument("--interval", type=int, default=60,
                   help="seconds between ticks for a new install (default 60)")
    s.set_defaults(func=cmd_daemon)

    s = sub.add_parser("cockpit", help="open a tmux workspace pre-wired for this repo "
                                        "(status bar + doc viewer + live claude session)")
    s.add_argument("--kill", action="store_true",
                    help="kill this repo's existing cockpit tmux session instead of opening it")
    s.set_defaults(func=cmd_cockpit)

    s = sub.add_parser("update", help="git pull + reinstall the gantry checkout this install runs from")
    s.set_defaults(func=cmd_update)
    return p


def _ensure_notify_env() -> None:
    """Auto-load ~/.config/gantry/env.sh's telegram bridge if the caller's shell
    never sourced it. Without this, every stage/advance/ship call silently no-ops
    notifications (TelegramNotifier.send returns {"sent": False, ...} — no
    exception, no visible error) whenever gantry runs from a shell that skipped
    the documented `source ~/.config/gantry/env.sh` setup step. Since agents and
    scripts routinely invoke `gantry` directly without that step, load it here
    once, unconditionally, instead of depending on caller discipline."""
    import os
    if os.environ.get("GANTRY_TELEGRAM_BOT_TOKEN") and os.environ.get("GANTRY_TELEGRAM_CHAT_ID"):
        return
    env_sh = Path.home() / ".config" / "gantry" / "env.sh"
    if not env_sh.exists():
        return
    try:
        out = subprocess.run(
            ["bash", "-c", f"source {env_sh} && env"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout
    except Exception:
        return
    for line in out.splitlines():
        if line.startswith("GANTRY_TELEGRAM_BOT_TOKEN=") or line.startswith("GANTRY_TELEGRAM_CHAT_ID="):
            key, _, val = line.partition("=")
            os.environ[key] = val


def main(argv=None) -> int:
    _ensure_notify_env()
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:  # surface a clean error, non-zero exit
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
