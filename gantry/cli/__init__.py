"""Gantry CLI.

Verbs:
  gantry init                        scaffold gantry.toml + .gantry/prompts in the repo
  gantry setup                       one-command bring-up: init + docker build + docker up
  gantry run --title T --request R   create a run and start the pipeline
  gantry stage <stage> --run ID      run one agent stage (plan/build/evidence)
  gantry retry <stage> --run ID       re-run a stage fresh (new session, no resume/feedback)
  gantry checks --run ID             run scope guard + repo checks
  gantry review --run ID             run the independent LLM review
  gantry approve --run ID --stage S  pass a human-review gate, advance
  gantry revise --run ID --stage S "comments"   send a stage back
  gantry ship --run ID                commit + push + open a PR (review_approved only)
  gantry hold --run ID                pause a run so nothing auto-advances it (manual takeover)
  gantry resume --run ID              un-pause a held run, restoring its prior status
  gantry mark-shipped --run ID        record that a run was shipped outside `gantry ship`
  gantry mark-merged --run ID         record that a shipped run's PR was actually merged
  gantry status [--run ID]           show run(s) status
  gantry doctor                      check the environment (runners, git, config)
  gantry listen                      poll Telegram replies, act on the pending run
  gantry linear-serve                 serve Linear issue webhooks: classify + create runs
  gantry cost [--run ID]               repo-wide cost total, or one run's per-stage breakdown
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
from ..config import AGENT_STAGES, DOC_STAGES
from .cost import cmd_cost
from .docs import cmd_doctor, cmd_docs
from .linear import cmd_linear_serve
from .run_commands import (
    cmd_advance, cmd_approve, cmd_cancel, cmd_checks, cmd_cleanup, cmd_hold, cmd_init,
    cmd_loop, cmd_mark_merged, cmd_mark_shipped, cmd_resume_hold, cmd_retry, cmd_review,
    cmd_revise, cmd_run, cmd_ship, cmd_stage, cmd_status,
)
from .system import (
    cmd_cockpit, cmd_daemon, cmd_daemon_tick, cmd_docker, cmd_mcp, cmd_setup, cmd_update,
)
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

    s = sub.add_parser("setup", help="one-command bring-up: scaffold gantry.toml + prompts, "
                                      "build the Docker image (claude + codex both installed), "
                                      "and start this project's container")
    s.add_argument("--force", action="store_true", help="overwrite an existing gantry.toml")
    s.add_argument("--interval", type=int, default=60,
                   help="seconds between ticks inside the container (default 60)")
    s.set_defaults(func=cmd_setup)

    s = sub.add_parser("run", help="create a run and start the pipeline")
    s.add_argument("--title", required=True)
    s.add_argument("--request", default="")
    s.add_argument("--run", help="explicit run id")
    s.add_argument("--depends-on", default="",
                   help="comma-separated run_ids this run is queued behind; "
                        "it stays in status=queued (not started) until every "
                        "listed run is actually shipped AND merged (see "
                        "`gantry mark-merged`). Poller/advance picks it up "
                        "automatically once prereqs clear.")
    s.add_argument("--tag", default="", help="filter label for gantry watch/advance --all/loop "
                                              "--tag; has no effect on the run's own execution")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("stage", help="run one stage (any DOC_STAGES or AGENT_STAGES stage)")
    s.add_argument("stage", choices=sorted(DOC_STAGES | AGENT_STAGES))
    s.add_argument("--run", required=True)
    s.add_argument("--resume", action="store_true")
    s.set_defaults(func=cmd_stage)

    s = sub.add_parser("retry", help="re-run a stage from scratch (new session, no resume/feedback) — "
                                      "for a stage that just flaked, not one that needs a replan")
    s.add_argument("stage", choices=["plan", "build", "evidence"])
    s.add_argument("--run", required=True)
    s.set_defaults(func=cmd_retry)

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

    s = sub.add_parser("hold", help="pause a run so Gantry stops advancing/auto-retrying it — "
                                     "for taking over the worktree by hand")
    s.add_argument("--run", required=True)
    s.set_defaults(func=cmd_hold)

    s = sub.add_parser("mark-shipped", help="record that a run was shipped outside `gantry ship` "
                                             "(e.g. shipped by hand after a hold)")
    s.add_argument("--run", required=True)
    s.add_argument("--force", action="store_true", help="mark shipped even if already shipped")
    s.set_defaults(func=cmd_mark_shipped)

    s = sub.add_parser("mark-merged", help="record that a shipped run's PR was actually merged — "
                                            "required for its dependents (depends_on) to start "
                                            "when [git].auto_merge is off")
    s.add_argument("--run", required=True)
    s.set_defaults(func=cmd_mark_merged)

    s = sub.add_parser("resume", help="un-pause a held run, restoring its prior status")
    s.add_argument("--run", required=True)
    s.set_defaults(func=cmd_resume_hold)

    s = sub.add_parser("cancel", help="cancel a run (mark cancelled, optionally clean up its worktree)")
    s.add_argument("--run", required=True)
    s.add_argument("--force", action="store_true", help="cancel even if already shipped")
    s.add_argument("--cleanup", action="store_true", help="also remove the run's worktree/branch now")
    s.set_defaults(func=cmd_cancel)

    s = sub.add_parser("cleanup", help="prune worktrees (and optionally state) for finished runs")
    s.add_argument("--status", action="append",
                   help="status to target (repeatable); default: shipped/shipped_manually/cancelled")
    s.add_argument("--older-than-days", type=int, default=0)
    s.add_argument("--yes", action="store_true", help="actually delete (default: dry-run listing only)")
    s.add_argument("--purge-state", action="store_true",
                   help="also delete .agent-runs/<run_id> (state+artifacts)")
    s.set_defaults(func=cmd_cleanup)

    s = sub.add_parser("status", help="show run status")
    s.add_argument("--run")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("advance", help="drive the pipeline forward one tick")
    s.add_argument("--run", help="advance a single run")
    s.add_argument("--all", action="store_true", help="tick every run (poller mode); notifies on change")
    s.add_argument("--tag", help="with --all, only tick runs created with this --tag")
    s.set_defaults(func=cmd_advance)

    s = sub.add_parser("loop", help="repeatedly tick the pipeline until terminal/human-gated "
                                     "(foreground alternative to an external cron)")
    s.add_argument("--run", help="loop a single run only (stops once it needs a human or finishes); "
                                 "omit to loop every run like the cron poller does")
    s.add_argument("--interval", type=int, default=15, help="seconds between ticks (default 15)")
    s.add_argument("--max-ticks", type=int, default=0,
                   help="stop after this many ticks regardless of state (0 = unbounded)")
    s.add_argument("--tag", help="without --run, only tick runs created with this --tag")
    s.set_defaults(func=cmd_loop)

    s = sub.add_parser("doctor", help="check environment")
    s.add_argument("--fix", action="store_true",
                   help="detect a PATH-available runner not registered in gantry.toml and "
                        "offer to configure it as [agent].runner")
    s.add_argument("--yes", action="store_true",
                   help="with --fix, apply the fix without an interactive y/n confirmation")
    s.add_argument("--live-sdk-smoke", action="store_true",
                   help="run credential-gated Cursor SDK live smoke "
                        "(requires CURSOR_API_KEY; same as "
                        "GANTRY_CURSOR_SDK_LIVE=1 unittest)")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("listen", help="poll Telegram replies, act on the pending run")
    s.add_argument("--run", help="always apply replies to this run (default: the most recent needs-input run)")
    s.set_defaults(func=cmd_listen)

    s = sub.add_parser("linear-serve", help="serve Linear issue webhooks: classify + create runs")
    s.add_argument("--port", type=int, default=8080)
    s.set_defaults(func=cmd_linear_serve)

    s = sub.add_parser("docs", help="render a run's spec/design/plan/evidence/review docs (via glow if installed)")
    s.add_argument("--run", help="default: the most recently touched run")
    s.add_argument("--doc", help="a specific artifact filename (e.g. architecture-design.md); default: all")
    s.add_argument("--pick", action="store_true", help="interactive fzf nav: pick a run, then a doc, Esc to go back")
    s.add_argument("--follow", action="store_true", help="auto-refresh to whichever run is most recently touched")
    s.add_argument("--nav", action="store_true",
                   help="persistent arrow-key navigator (curses): run list -> doc list -> content")
    s.set_defaults(func=cmd_docs)

    s = sub.add_parser("cost", help="repo-wide cost total, or one run's per-stage breakdown")
    s.add_argument("--run", help="show this run's per-stage cost breakdown (default: repo-wide total)")
    s.set_defaults(func=cmd_cost)

    s = sub.add_parser("watch", help="dashboard of all runs")
    s.add_argument("--live", action="store_true", help="refresh every 2s (default: one-shot)")
    s.add_argument("--tag", help="only show runs created with this --tag")
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

    s = sub.add_parser("daemon-tick", help="internal: advance every daemon-registered "
                                            "target once (invoked by the background job)")
    s.set_defaults(func=cmd_daemon_tick)

    s = sub.add_parser("docker", help="run gantry for this project inside its own "
                                       "Docker container (isolated from the host)")
    s.add_argument("docker_action", choices=["build", "up", "down", "status"])
    s.add_argument("--interval", type=int, default=60,
                   help="seconds between ticks inside the container (default 60)")
    s.set_defaults(func=cmd_docker)

    s = sub.add_parser("cockpit", help="open a tmux workspace pre-wired for this repo "
                                        "(status bar + doc viewer + live agent session)")
    s.add_argument("--kill", action="store_true",
                    help="kill this repo's existing cockpit tmux session instead of opening it")
    s.set_defaults(func=cmd_cockpit)

    s = sub.add_parser("update", help="git pull + reinstall the gantry checkout this install runs from")
    s.set_defaults(func=cmd_update)
    return p


def _ensure_notify_env() -> None:
    """Auto-load local env files so callers don't need to source them.

    Load order (later does not overwrite already-set vars):
      1. ~/.config/gantry/env.sh  (telegram + optional Cursor key)
      2. <gantry-checkout>/.env   (CURSOR_API_KEY for this install)
    """
    _load_config_env_sh()
    _load_dotenv_file(_gantry_checkout_env())
    _load_dotenv_file(Path.cwd() / ".env")


def _gantry_checkout_env() -> Path:
    # gantry/cli/__init__.py -> gantry/cli -> gantry -> checkout root
    return Path(__file__).resolve().parents[2] / ".env"


def _load_config_env_sh() -> None:
    import os
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
    wanted = (
        "GANTRY_TELEGRAM_BOT_TOKEN",
        "GANTRY_TELEGRAM_CHAT_ID",
        "CURSOR_API_KEY",
    )
    for line in out.splitlines():
        for key in wanted:
            if line.startswith(key + "=") and not os.environ.get(key):
                _, _, val = line.partition("=")
                os.environ[key] = val


def _load_dotenv_file(path: Path) -> None:
    """Minimal KEY=VALUE loader. Does not override existing environ."""
    import os
    if not path.is_file():
        return
    try:
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip("'").strip('"')
            if key and not os.environ.get(key):
                os.environ[key] = val
    except Exception:
        return


def main(argv=None) -> int:
    _ensure_notify_env()
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:  # surface a clean error, non-zero exit
        # An exception message can in principle quote a secret value (e.g. a
        # failed auth header, a subprocess error echoing its own args) — redact
        # known-sensitive env values (auth tokens; per-target proxy
        # secrets aren't resolvable here without a target config, so only the
        # always-sensitive env vars are covered) before this ever reaches stderr.
        from ..redact import redact_secrets
        print(json.dumps({"ok": False, "error": redact_secrets(str(exc))}, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
