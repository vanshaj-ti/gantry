"""Gantry CLI.

Verbs:
  gantry init                        scaffold gantry.toml + .gantry/prompts in the repo
  gantry run --title T --request R   create a run and start the pipeline
  gantry stage <stage> --run ID      run one agent stage (plan/build/evidence)
  gantry checks --run ID             run scope guard + repo checks
  gantry review --run ID             run the independent LLM review
  gantry approve --run ID --stage S  pass a human-review gate, advance
  gantry revise --run ID --stage S "comments"   send a stage back
  gantry status [--run ID]           show run(s) status
  gantry doctor                      check the environment (runners, git, config)

Target repo is $GANTRY_TARGET or the current working directory.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__
from .config import CONFIG_FILENAME, load_config
from .engine import Engine
from .notify import get_notifier
from .review import run_review
from .runners import _RUNNERS
from .state import RunStore

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"


def _target() -> Path:
    return Path(os.environ.get("GANTRY_TARGET", os.getcwd())).resolve()


def _engine() -> Engine:
    tgt = _target()
    return Engine(tgt, load_config(tgt))


def _out(obj) -> int:
    print(json.dumps(obj, indent=2, default=str))
    return 0


# --- verbs ---
def cmd_init(args) -> int:
    tgt = _target()
    cfg_path = tgt / CONFIG_FILENAME
    if cfg_path.exists() and not args.force:
        return _out({"ok": False, "error": f"{CONFIG_FILENAME} already exists (use --force)"})
    tmpl = TEMPLATE_DIR / "gantry.toml"
    cfg_path.write_text(tmpl.read_text() if tmpl.exists() else "project_id = \"project\"\n")
    prompts = tgt / ".gantry" / "prompts"
    prompts.mkdir(parents=True, exist_ok=True)
    for stage in ["plan", "build", "evidence", "review"]:
        src = TEMPLATE_DIR / "prompts" / f"{stage}.md"
        dst = prompts / f"{stage}.md"
        if src.exists() and not dst.exists():
            shutil.copy(src, dst)
    result = {"ok": True, "config": str(cfg_path), "prompts_dir": str(prompts)}
    if args.with_skills:
        result["skills_install"] = _install_skills(load_config(tgt))
    return _out(result)


def _install_skills(cfg) -> list[dict]:
    """Run the declared per-runner install command for each enabled skill.
    Uses only the inspectable commands in config — never a piped remote script."""
    runner = cfg.agent.runner
    out = []
    for skill in cfg.skills.enabled:
        cmd = cfg.skills.install_command(skill, runner)
        if not cmd:
            out.append({"skill": skill, "runner": runner, "ok": False, "error": "no install command for this runner"})
            continue
        proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=300)
        out.append({"skill": skill, "runner": runner, "command": cmd,
                    "ok": proc.returncode == 0, "output_tail": (proc.stdout + proc.stderr)[-500:]})
    return out


def cmd_run(args) -> int:
    eng = _engine()
    rid = eng.create_run(args.title, args.request or args.title, args.run)
    return _out({"ok": True, "run_id": rid, "first_stage": eng.cfg.stages[0]})


def cmd_stage(args) -> int:
    eng = _engine()
    res = eng.run_agent_stage(args.run, args.stage, resume=args.resume)
    return _out(res)


def cmd_checks(args) -> int:
    return _out(_engine().run_checks(args.run))


def cmd_review(args) -> int:
    eng = _engine()
    return _out(run_review(eng.store, args.run, eng.cfg, eng.target))


def cmd_approve(args) -> int:
    nxt = _engine().approve(args.run, args.stage)
    return _out({"ok": True, "advanced_to": nxt})


def cmd_revise(args) -> int:
    _engine().revise(args.run, args.stage, args.comments)
    return _out({"ok": True, "stage": args.stage, "status": "changes_requested"})


def cmd_status(args) -> int:
    store = RunStore(_target())
    if args.run:
        return _out(store.state(args.run))
    return _out(store.list_runs())


def cmd_advance(args) -> int:
    from .advance import advance_all, advance_run, label
    tgt = _target()
    cfg = load_config(tgt)
    if args.all:
        results = advance_all(tgt, cfg)
        # notify on any state change
        if results:
            notifier = get_notifier(cfg.notify)
            store = RunStore(tgt)
            for r in results:
                if r.get("advanced"):
                    st = store.state(r["run_id"]).get("status", "")
                    notifier.send(f"[{r['run_id']}] {label(st)}", meta=r)
        return _out(results)
    eng = Engine(tgt, cfg)
    return _out(advance_run(eng, args.run))


def cmd_mcp(args) -> int:
    from .mcp import ensure_mcp_for_stage
    tgt = _target()
    cfg = load_config(tgt)
    if args.list:
        return _out({"enabled": cfg.mcp.enabled,
                     "available": sorted(cfg.mcp.servers.keys()),
                     "runner": cfg.agent.runner})
    seen, results = set(), []
    for stage in ["plan", "build", "evidence", "review"]:
        for r in ensure_mcp_for_stage(cfg, stage, tgt):
            key = (r.get("server"), r.get("status"))
            if key not in seen:
                seen.add(key)
                results.append(r)
    return _out(results)


def cmd_watch(args) -> int:
    """Live/one-shot dashboard of all runs in the target repo."""
    import time
    store = RunStore(_target())

    def render() -> None:
        runs = store.list_runs()
        print("\033[2J\033[H" if args.live else "", end="")
        print(f"GANTRY — {len(runs)} run(s)\n")
        print(f"{'RUN ID':<34} {'STATUS':<26} TITLE")
        print("-" * 90)
        for r in runs:
            print(f"{r['id']:<34} {r['status']:<26} {r['title'][:28]}")

    if not args.live:
        render()
        return 0
    try:
        while True:
            render()
            print("\n(Ctrl+C to exit — refreshing every 2s)")
            time.sleep(2)
    except KeyboardInterrupt:
        return 0


def cmd_doctor(args) -> int:
    tgt = _target()
    cfg = load_config(tgt)
    runners = {name: bool(shutil.which(cls().build_command(
        prompt="x", model="", session_id=None, plan_mode=False, skip_permissions=False,
        output_format="json", session_name="x", max_turns=1)[0]))
        for name, cls in _RUNNERS.items()}
    git_ok = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"],
                            cwd=str(tgt), capture_output=True, text=True).returncode == 0
    herdr_installed = bool(shutil.which("herdr"))
    inside_herdr = os.environ.get("HERDR_ENV") == "1"
    herdr_status = ("active (inside pane)" if (herdr_installed and inside_herdr)
                    else "installed (run Gantry inside a herdr pane to activate)" if herdr_installed
                    else "not installed — recommended cockpit: https://herdr.dev")
    return _out({
        "target": str(tgt),
        "config_present": (tgt / CONFIG_FILENAME).exists(),
        "active_runner": cfg.agent.runner,
        "runners_available": runners,
        "git_repo": git_ok,
        "base_branch": cfg.git.base_branch,
        "notify_backend": cfg.notify.backend,
        "stages": cfg.stages,
        "mandated_skills": cfg.skills.enabled,
        "mcp_enabled": cfg.mcp.enabled,
        "mcp_available": sorted(cfg.mcp.servers.keys()),
        "herdr": herdr_status,
    })


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

    s = sub.add_parser("stage", help="run one agent stage")
    s.add_argument("stage", choices=["plan", "build", "evidence"])
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

    s = sub.add_parser("status", help="show run status")
    s.add_argument("--run")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("advance", help="drive the pipeline forward one tick")
    s.add_argument("--run", help="advance a single run")
    s.add_argument("--all", action="store_true", help="tick every run (poller mode); notifies on change")
    s.set_defaults(func=cmd_advance)

    s = sub.add_parser("doctor", help="check environment")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("watch", help="dashboard of all runs")
    s.add_argument("--live", action="store_true", help="refresh every 2s (default: one-shot)")
    s.set_defaults(func=cmd_watch)

    s = sub.add_parser("mcp", help="register/list MCP servers for the active runner")
    s.add_argument("--list", action="store_true", help="show enabled/available servers (default: register)")
    s.set_defaults(func=cmd_mcp)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:  # surface a clean error, non-zero exit
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
