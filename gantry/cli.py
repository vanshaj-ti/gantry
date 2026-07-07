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
from .git import branch_name, commit_all, create_pr, push
from .notify import fetch_telegram_replies, get_notifier
from .review import run_review
from .runners import _RUNNERS
from .state import RunStore

# Statuses where a run is actually waiting on a human decision — the set
# `gantry listen` matches replies against.
NEEDS_INPUT_STATUSES = {
    "blocked", "review_escalated",
    "spec_complete", "design_complete",  # always human-gated — never auto-advanced
    "spec_failed", "design_failed", "plan_failed", "build_failed", "evidence_failed",
}

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"


def _notify(store, notifier, run_id: str, text: str, meta: dict | None = None) -> dict:
    """Send a notification and record which run it belongs to, so a Telegram
    *reply* to this exact message resolves unambiguously to this run — even
    with several runs stuck at once. See RunStore.record_telegram_message."""
    res = notifier.send(text, meta=meta)
    if res.get("sent") and res.get("message_id") is not None:
        store.record_telegram_message(res["message_id"], run_id)
    return res


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
    from .advance import notify_message
    eng = _engine()
    res = eng.run_agent_stage(args.run, args.stage, resume=args.resume)
    # Notify regardless of trigger (manual `gantry stage` or the `advance` cron) —
    # a human watching a terminal for a 10+ minute stage is not a safe assumption.
    notifier = get_notifier(eng.cfg.notify)
    status = eng.store.state(args.run).get("status", "")
    _notify(eng.store, notifier, args.run, notify_message(eng.store, args.run, status, res), meta=res)
    return _out(res)


def cmd_checks(args) -> int:
    from .advance import notify_message
    eng = _engine()
    res = eng.run_checks(args.run)
    notifier = get_notifier(eng.cfg.notify)
    status = eng.store.state(args.run).get("status", "")
    _notify(eng.store, notifier, args.run, notify_message(eng.store, args.run, status, res), meta=res)
    return _out(res)


def cmd_review(args) -> int:
    from .advance import notify_message
    eng = _engine()
    res = run_review(eng.store, args.run, eng.cfg, eng.work_dir(args.run))
    notifier = get_notifier(eng.cfg.notify)
    status = eng.store.state(args.run).get("status", "")
    _notify(eng.store, notifier, args.run, notify_message(eng.store, args.run, status, res), meta=res)
    return _out(res)


def cmd_approve(args) -> int:
    nxt = _engine().approve(args.run, args.stage)
    return _out({"ok": True, "advanced_to": nxt})


def cmd_revise(args) -> int:
    _engine().revise(args.run, args.stage, args.comments)
    return _out({"ok": True, "stage": args.stage, "status": "changes_requested"})


def cmd_ship(args) -> int:
    """Commit the worktree, push the branch, open a PR. Only valid once review
    has approved — never fires automatically; requires an explicit human call."""
    eng = _engine()
    run_id = args.run
    state = eng.store.state(run_id)
    if state.get("status") != "review_approved" and not args.force:
        return _out({"ok": False, "error": f"run status is {state.get('status')!r}, "
                    f"not review_approved (use --force to override)"})

    wt = eng.work_dir(run_id)
    branch = branch_name(run_id)
    title = state.get("title", run_id)

    commit_res = commit_all(wt, f"{title}\n\ngantry run {run_id}")
    if not commit_res["ok"]:
        return _out({"ok": False, "stage": "commit", **commit_res})

    push_res = push(wt, branch)
    if not push_res["ok"]:
        return _out({"ok": False, "stage": "push", **push_res})

    body = f"Automated PR from Gantry run `{run_id}`.\n\nSee `.agent-runs/{run_id}/` for the full trail (plan, build summary, evidence report, independent review verdict)."
    pr_res = create_pr(wt, branch, eng.cfg.git.base_branch, title, body)
    eng.store.update_state(run_id, status="shipped" if pr_res["ok"] else "ship_failed",
                           pr_url=pr_res.get("url"))
    return _out({"ok": pr_res["ok"], "commit": commit_res, "push": push_res, "pr": pr_res})


def cmd_status(args) -> int:
    store = RunStore(_target())
    if args.run:
        return _out(store.state(args.run))
    return _out(store.list_runs())


def cmd_advance(args) -> int:
    from .advance import advance_all, advance_run, notify_message
    tgt = _target()
    cfg = load_config(tgt)
    if args.all:
        results = advance_all(tgt, cfg)
        # Notify on every result, not just successful advances — a run that
        # stalled at blocked/checks_failed/review_escalated needs a human to
        # see it just as much as one that progressed, arguably more.
        if results:
            notifier = get_notifier(cfg.notify)
            store = RunStore(tgt)
            for r in results:
                if r.get("action") == "skipped_locked":
                    continue
                st = store.state(r["run_id"]).get("status", "")
                _notify(store, notifier, r["run_id"], notify_message(store, r["run_id"], st, r), meta=r)
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

    def trunc(s: str, width: int) -> str:
        """Fixed-width truncation with ellipsis. Plain `{s:<width}` only pads
        short strings — it doesn't truncate long ones, so a long run_id (they
        embed the full slugified title, e.g. `<ts>-change-resume-date-while-
        subscription-is-paused`) blows past the column and misaligns every
        column after it. Truncate first, then pad."""
        return (s[: width - 1] + "…") if len(s) > width else s.ljust(width)

    def age(mtime: float) -> str:
        """Relative age since state.json last changed — more actionable at a
        glance than an absolute timestamp for spotting a run that's been
        silently stuck (e.g. evidence_running for 3h is a real signal;
        the wall-clock time it started is not, without doing the subtraction
        yourself)."""
        secs = max(0, time.time() - mtime)
        if secs < 60:
            return f"{int(secs)}s ago"
        if secs < 3600:
            return f"{int(secs // 60)}m ago"
        if secs < 86400:
            return f"{int(secs // 3600)}h ago"
        return f"{int(secs // 86400)}d ago"

    def render() -> None:
        runs = store.list_runs()
        print("\033[2J\033[H" if args.live else "", end="")
        print(f"GANTRY — {len(runs)} run(s)\n")
        print(f"{trunc('TITLE', 40)} {trunc('STATUS', 24)} UPDATED")
        print("-" * 90)
        for r in runs:
            title = r["title"] or r["id"]
            print(f"{trunc(title, 40)} {trunc(r['status'], 24)} {age(r['mtime'])}")

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


def cmd_listen(args) -> int:
    """Poll Telegram for replies and act on the run each reply targets.

    Resolution order: (1) if the message is a Telegram *reply* to one of our
    own notifications, resolve to that exact run — this is what makes replying
    to an older stuck notification work correctly even with several runs
    blocked at once; (2) --run if passed; (3) fall back to the single most-
    recently-touched run in a needs-input state, for a bare "1"/"2" typed
    without using Telegram's reply feature.
    """
    tgt = _target()
    cfg = load_config(tgt)
    store = RunStore(tgt)
    notifier = get_notifier(cfg.notify)
    offset = None
    print(json.dumps({"listening": True, "chat_scope": "configured GANTRY_TELEGRAM_CHAT_ID"}))
    try:
        while True:
            messages, offset = fetch_telegram_replies(offset)
            for m in messages:
                reply_to = m.get("reply_to_message_id")
                target_run = (store.run_for_telegram_message(reply_to) if reply_to else None) or args.run
                if not target_run:
                    pending = [r for r in store.list_runs() if r["status"] in NEEDS_INPUT_STATUSES]
                    if not pending:
                        notifier.send("No run is currently waiting on input — nothing to apply that reply to.")
                        continue
                    target_run = pending[0]["id"]
                _handle_reply(store, cfg, notifier, target_run, m["text"].strip())
    except KeyboardInterrupt:
        return 0


def _handle_reply(store, cfg, notifier, run_id: str, text: str) -> None:
    from .advance import label
    from .engine import Engine
    st = store.state(run_id)
    status = st.get("status", "")
    eng = Engine(store.target, cfg)
    lowered = text.lower().strip()

    if status in ("spec_complete", "design_complete"):
        stage = status.removesuffix("_complete")
        if lowered.startswith("1") or lowered in ("approve", "yes", "y"):
            nxt = eng.approve(run_id, stage)
            _notify(store, notifier, run_id, f"Approved *{run_id}* {stage} — moved to `{nxt}`.")
        else:
            # spec/design have no auto-resume transition (they're always
            # human-gated), so write straight to answers/<stage>.md — the file
            # run_agent_stage's resume path actually reads — and resume now,
            # rather than calling revise() (which writes review-comments.md,
            # a file only the build-resume auto-transition happens to consume).
            comment = text[1:].strip() if lowered.startswith("2") else text
            answer_path = store.artifact_path(run_id, f"answers/{stage}.md")
            answer_path.parent.mkdir(parents=True, exist_ok=True)
            answer_path.write_text(comment or "See Telegram reply.")
            _notify(store, notifier, run_id, f"Rewriting *{run_id}* {stage} with your feedback…")
            eng.run_agent_stage(run_id, stage, resume=True)
            new_status = store.state(run_id).get("status", "")
            _notify(store, notifier, run_id, f"*{run_id}* {stage}: {label(new_status)}")
        return

    if status == "blocked":
        if lowered.startswith("1"):
            eng.run_checks(run_id)
            new_status = store.state(run_id).get("status", "")
            _notify(store, notifier, run_id, f"Re-checked *{run_id}* — now: {label(new_status)}")
        else:
            comment = text[1:].strip() if lowered.startswith("2") else text
            eng.revise(run_id, "build", comment or "See Telegram reply.")
            _notify(store, notifier, run_id, f"Sent *{run_id}* back to build with your comment.")
        return

    if status.endswith("_failed"):
        stage = status.removesuffix("_failed")
        if lowered.startswith("1") or lowered in ("retry", "yes", "y"):
            _notify(store, notifier, run_id, f"Resuming *{run_id}* stage `{stage}`…")
            eng.run_agent_stage(run_id, stage, resume=True)
            new_status = store.state(run_id).get("status", "")
            _notify(store, notifier, run_id, f"*{run_id}* stage `{stage}`: {label(new_status)}")
        else:
            _notify(store, notifier, run_id, f"Noted — *{run_id}* left as-is for you to inspect manually.")
        return

    if status == "review_escalated":
        if lowered.startswith("1") or lowered in ("approve", "yes", "y"):
            eng.approve(run_id, "review")
            _notify(store, notifier, run_id, f"Approved *{run_id}* — proceeding.")
        else:
            comment = text[1:].strip() if lowered.startswith("2") else text
            eng.revise(run_id, "build", comment or "See Telegram reply.")
            _notify(store, notifier, run_id, f"Sent *{run_id}* back to build with your comment.")
        return

    # Fallback: treat the reply as the answer to whatever the agent asked mid-stage
    # (the "clarifying question" branch of notify_message). We don't know which
    # exact stage without re-deriving it from status — best-effort from current_stage.
    stage = st.get("current_stage", "build")
    answer_path = store.artifact_path(run_id, f"answers/{stage}.md")
    answer_path.parent.mkdir(parents=True, exist_ok=True)
    answer_path.write_text(text)
    _notify(store, notifier, run_id, f"Recorded your answer for *{run_id}* stage `{stage}`, resuming…")
    eng.run_agent_stage(run_id, stage, resume=True)
    new_status = store.state(run_id).get("status", "")
    _notify(store, notifier, run_id, f"*{run_id}* stage `{stage}`: {label(new_status)}")


# Doc-worthy artifacts in pipeline order — the exact list a stage's own review
# prompt is told to read (see review.py). review-result.json is JSON, not
# markdown; rendered separately by extracting its "result" text field.
DOC_ARTIFACTS = [
    ("intake.md", "Intake"),
    ("product-spec.md", "Spec"),
    ("architecture-design.md", "Design"),
    ("implementation-plan.md", "Plan"),
    ("build-summary.md", "Build summary"),
    ("evidence-report.md", "Evidence"),
]


def _render_run_docs(store: RunStore, run_id: str, glow: str | None) -> None:
    found_any = False
    for filename, label_text in DOC_ARTIFACTS:
        content = store.read_artifact(run_id, filename)
        if content is None:
            continue
        found_any = True
        _render_doc(f"{label_text} ({filename})", content, glow)
    review = store.read_result(run_id, "review-result.json")
    if review:
        found_any = True
        verdict = review.get("verdict", "?")
        body = f"**Verdict: {verdict}**\n\n{review.get('result', '')}"
        _render_doc(f"Review (verdict: {verdict})", body, glow)
    if not found_any:
        print(f"No docs written yet for {run_id} — its current stage is "
              f"{store.state(run_id).get('status', 'unknown')}.")


def _run_doc_list(store: RunStore, run_id: str) -> list[tuple[str, str]]:
    """(label, filename) pairs for whichever docs this run has actually written,
    plus a synthetic "All docs" entry to render everything at once."""
    out = [("All docs", "")]
    for filename, label_text in DOC_ARTIFACTS:
        if store.read_artifact(run_id, filename) is not None:
            out.append((f"{label_text} ({filename})", filename))
    if store.read_result(run_id, "review-result.json"):
        out.append(("Review", "review-result.json"))
    return out


def _fzf_pick(options: list[str], prompt: str) -> str | None:
    """Run fzf over a list of lines, return the picked line or None (Esc/no match/no fzf)."""
    fzf = shutil.which("fzf")
    if not fzf or not options:
        return None
    try:
        proc = subprocess.run([fzf, "--prompt", prompt, "--height", "40%", "--layout=reverse"],
                              input="\n".join(options), text=True, capture_output=True)
        picked = proc.stdout.strip()
        return picked or None
    except Exception:
        return None


def cmd_docs(args) -> int:
    """Render docs a run has produced so far (spec, design, plan, evidence,
    review) — the human-facing artifacts, never the implementation diff itself.
    Pipes through `glow` if installed (falls back to plain text).

    --run + --doc: render exactly that doc (or all, if --doc omitted) and exit.
    --run omitted, no --pick/--follow: renders all docs for the most-recently-
    touched run and exits.
    --pick: interactive nav via fzf — pick a run, then a doc for that run, Esc
    to go back a level, Esc again to quit. Requires fzf on PATH.
    --follow: auto-refreshes to whichever run is most recently touched,
    whenever that run's updated_at changes — no interaction, for a
    docs-viewer pane that should just always show what's currently happening.
    """
    import time
    store = RunStore(_target())
    glow = shutil.which("glow")

    def resolve_run() -> str | None:
        if args.run:
            return args.run if store.exists(args.run) else None
        runs = store.list_runs()
        return runs[0]["id"] if runs else None

    if args.pick:
        if not shutil.which("fzf"):
            return _out({"ok": False, "error": "fzf not found on PATH — required for --pick"})
        while True:
            runs = store.list_runs()
            if not runs:
                print("No runs exist yet.")
                return 0
            run_lines = [f"{r['id']}  [{r['status']}]  {r['title']}" for r in runs]
            picked_run = _fzf_pick(run_lines, "run> ")
            if picked_run is None:
                return 0  # Esc at the top level: quit
            run_id = picked_run.split("  ", 1)[0]
            while True:
                docs = _run_doc_list(store, run_id)
                doc_lines = [label for label, _ in docs]
                picked_doc = _fzf_pick(doc_lines, f"{run_id} doc> ")
                if picked_doc is None:
                    break  # Esc: back to run picker
                filename = dict(docs)[picked_doc]
                print("\033[2J\033[H", end="")
                if filename:
                    content = store.read_artifact(run_id, filename)
                    if filename == "review-result.json":
                        review = store.read_result(run_id, filename)
                        content = f"**Verdict: {review.get('verdict', '?')}**\n\n{review.get('result', '')}"
                    _render_doc(f"{picked_doc}", content or "(empty)", glow)
                else:
                    _render_run_docs(store, run_id, glow)
                input("\n[Enter to go back] ")

    if args.doc:
        run_id = resolve_run()
        if not run_id:
            return _out({"ok": False, "error": f"run not found: {args.run}" if args.run else "no runs exist yet"})
        content = store.read_artifact(run_id, args.doc)
        if content is None:
            return _out({"ok": False, "error": f"{args.doc} not found for {run_id}"})
        _render_doc(args.doc, content, glow)
        return 0

    if not args.follow:
        run_id = resolve_run()
        if not run_id:
            return _out({"ok": False, "error": f"run not found: {args.run}" if args.run else "no runs exist yet"})
        _render_run_docs(store, run_id, glow)
        return 0

    last_key = None
    try:
        while True:
            run_id = resolve_run()
            key = (run_id, store.state(run_id).get("updated_at") if run_id else None)
            if key != last_key:
                last_key = key
                print("\033[2J\033[H", end="")  # clear screen, home cursor
                if run_id:
                    title = store.state(run_id).get("title", "")
                    print(f"Following: {run_id}" + (f" ({title})" if title else "") + "\n")
                    _render_run_docs(store, run_id, glow)
                else:
                    print("No runs exist yet.")
            time.sleep(3)
    except KeyboardInterrupt:
        return 0


def _render_doc(heading: str, content: str, glow_path: str | None) -> None:
    print(f"\n{'=' * 70}\n{heading}\n{'=' * 70}\n")
    if glow_path:
        try:
            subprocess.run([glow_path, "-"], input=content, text=True, timeout=30)
            return
        except Exception:
            pass  # fall through to plain print if glow itself misbehaves
    print(content)


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
    s.set_defaults(func=cmd_docs)

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
