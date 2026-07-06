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
    return _out({"ok": True, "config": str(cfg_path), "prompts_dir": str(prompts)})


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


def cmd_doctor(args) -> int:
    tgt = _target()
    cfg = load_config(tgt)
    runners = {name: bool(shutil.which(cls().build_command(
        prompt="x", model="", session_id=None, plan_mode=False, skip_permissions=False,
        output_format="json", session_name="x", max_turns=1)[0]))
        for name, cls in _RUNNERS.items()}
    git_ok = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"],
                            cwd=str(tgt), capture_output=True, text=True).returncode == 0
    return _out({
        "target": str(tgt),
        "config_present": (tgt / CONFIG_FILENAME).exists(),
        "active_runner": cfg.agent.runner,
        "runners_available": runners,
        "git_repo": git_ok,
        "base_branch": cfg.git.base_branch,
        "notify_backend": cfg.notify.backend,
        "stages": cfg.stages,
    })


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gantry", description="Project-agnostic autonomous build pipeline")
    p.add_argument("--version", action="version", version=f"gantry {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("init", help="scaffold gantry.toml + prompts")
    s.add_argument("--force", action="store_true")
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

    s = sub.add_parser("doctor", help="check environment")
    s.set_defaults(func=cmd_doctor)
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
