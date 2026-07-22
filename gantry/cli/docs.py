"""Doc rendering commands: docs, doctor."""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from ..config import CONFIG_FILENAME, load_config
from ..state import RunStore
from ._shared import _target, _out

logger = logging.getLogger(__name__)

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
        out.append(("Review (review-result.json)", "review-result.json"))
    return out


def _docs_fingerprint(store: RunStore, run_id: str) -> tuple:
    """A cheap signature of "everything that could change what --follow
    should show right now": each existing doc's mtime, sorted. A new doc
    appearing (or an existing one being rewritten) changes this even when it
    doesn't happen to coincide with a state.json write — the previous
    (run_id, updated_at) key could miss that."""
    mtimes = []
    for filename, _ in DOC_ARTIFACTS:
        p = store.artifact_path(run_id, filename)
        if p.exists():
            mtimes.append((filename, p.stat().st_mtime))
    review_path = store.run_dir(run_id) / "review-result.json"
    if review_path.exists():
        mtimes.append(("review-result.json", review_path.stat().st_mtime))
    return tuple(sorted(mtimes))


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
    --nav: persistent full-screen arrow-key navigator (curses) — run list ->
    doc list -> doc content, →/Enter drills in, ←/Esc backs out, q quits.
    Auto-refreshes in place without resetting your position. This is what
    `gantry cockpit`'s doc-viewer pane runs.
    """
    store = RunStore(_target())
    glow = shutil.which("glow")

    if args.nav:
        from ..docnav import run_navigator
        run_navigator(store)
        return 0

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
            width = shutil.get_terminal_size().columns
            # Re-render on: a different run being followed, a state.json write
            # (updated_at), a new/rewritten doc appearing (fingerprint — catches
            # a doc written mid-stage that doesn't coincide with a state write),
            # or the terminal being resized (width) — glow re-wraps correctly
            # per invocation, but the loop must actually decide to re-invoke it.
            key = (
                run_id,
                store.state(run_id).get("updated_at") if run_id else None,
                _docs_fingerprint(store, run_id) if run_id else None,
                width,
            )
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
            logger.debug("glow rendering failed, falling back to plain print", exc_info=True)
    print(content)


_AGENT_RUNNER_LINE_RE = re.compile(r'^(\s*runner\s*=\s*)"([^"]*)"(\s*.*)$', re.MULTILINE)


def _fix_agent_runner(cfg_path: Path, new_runner: str) -> dict:
    """Text-level edit of gantry.toml's [agent] runner line (gantry.toml is
    hand-authored template text, not dataclass-dumped — see scaffold()).
    Only rewrites the FIRST `runner = "..."` line, which is [agent].runner
    (models.<stage>.runner lines live under their own [models.<stage>]
    tables further down and are never the first match)."""
    text = cfg_path.read_text()
    m = _AGENT_RUNNER_LINE_RE.search(text)
    if not m:
        return {"ok": False, "error": "could not find an [agent] runner line to edit"}
    new_text = text[:m.start()] + f'{m.group(1)}"{new_runner}"{m.group(3)}' + text[m.end():]
    cfg_path.write_text(new_text)
    return {"ok": True, "runner": new_runner}


def cmd_doctor(args) -> int:
    from .system import _runner_availability
    tgt = _target()
    cfg = load_config(tgt)
    runners = _runner_availability()
    git_ok = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"],
                            cwd=str(tgt), capture_output=True, text=True).returncode == 0
    herdr_installed = bool(shutil.which("herdr"))
    inside_herdr = os.environ.get("HERDR_ENV") == "1"
    herdr_status = ("active (inside pane)" if (herdr_installed and inside_herdr)
                    else "installed (run Gantry inside a herdr pane to activate)" if herdr_installed
                    else "not installed (optional enhanced integration — see README)")

    required_tools = {
        name: {"available": bool(shutil.which(name))}
        for name in ("gh", "tmux", "fzf", "glow")
    }

    out = {
        "target": str(tgt),
        "config_present": (tgt / CONFIG_FILENAME).exists(),
        "active_runner": cfg.agent.runner,
        "runners_available": runners,
        "git_repo": git_ok,
        "base_branch": cfg.git.base_branch,
        "notify_backend": cfg.notify.backend,
        "notify_ready": (
            bool(os.environ.get("GANTRY_TELEGRAM_BOT_TOKEN") and os.environ.get("GANTRY_TELEGRAM_CHAT_ID"))
            if cfg.notify.backend == "telegram" else True
        ),
        "stages": cfg.stages,
        "mandated_skills": cfg.skills.enabled,
        "mcp_enabled": cfg.mcp.enabled,
        "mcp_available": sorted(cfg.mcp.servers.keys()),
        "required_tools": required_tools,
        "herdr": herdr_status,
    }

    if getattr(args, "fix", False):
        out["fix"] = _run_doctor_fix(tgt, cfg, runners, getattr(args, "yes", False))

    return _out(out)


def _run_doctor_fix(tgt: Path, cfg, runners: dict, auto_yes: bool) -> dict:
    """`gantry doctor --fix`: if a runner CLI is available on PATH but isn't
    the configured [agent].runner (and no [models.<stage>].runner override
    already references it), offer to register it as [agent].runner.

    Never silently overrides an explicit existing runner choice without
    --yes: gantry.toml's template always writes an [agent] runner line, so
    "no runner is currently meaningfully configured" is treated narrowly —
    only a gantry.toml with no [agent] runner line at all (or no gantry.toml
    to edit) counts as unconfigured and gets fixed without confirmation.
    Everything else requires --yes, or an interactive y/n confirmation when
    stdin is a tty; if stdin isn't a tty and --yes wasn't passed, this only
    reports, it never modifies gantry.toml.
    """
    configured = {cfg.agent.runner} | {sm.runner for sm in cfg.models.values() if sm.runner}
    candidates = [name for name, available in runners.items() if available and name not in configured]
    if not candidates:
        return {"ok": True, "action": "none", "detail": "no unregistered PATH-available runner found"}

    detected = candidates[0]
    cfg_path = tgt / CONFIG_FILENAME
    report = {"detected_but_not_registered": detected, "currently_configured": cfg.agent.runner}

    if not cfg_path.exists():
        report["action"] = "reported_only"
        report["note"] = f"no {CONFIG_FILENAME} to edit"
        return {"ok": True, **report}

    has_explicit_runner_line = bool(_AGENT_RUNNER_LINE_RE.search(cfg_path.read_text()))

    apply = auto_yes or not has_explicit_runner_line
    if not apply:
        if sys.stdin.isatty():
            answer = input(
                f"Detected {detected!r} on PATH but gantry.toml's [agent].runner is "
                f"{cfg.agent.runner!r}. Update it to {detected!r}? [y/N] ").strip().lower()
            apply = answer in ("y", "yes")
        else:
            report["action"] = "reported_only"
            report["note"] = "stdin is not a tty and --yes was not passed — not modifying gantry.toml"
            return {"ok": True, **report}

    if not apply:
        report["action"] = "declined"
        return {"ok": True, **report}

    result = _fix_agent_runner(cfg_path, detected)
    report["action"] = "updated" if result["ok"] else "failed"
    report.update(result)
    return {"ok": result["ok"], **report}
