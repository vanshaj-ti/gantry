"""Run lifecycle commands: init, run, stage, checks, review, approve, revise,
ship, status, advance, loop."""
from __future__ import annotations

import shutil
import subprocess
import time

from ..config import AGENT_STAGES, CONFIG_FILENAME, load_config
from ..engine import Engine
from ..git import remove_worktree
from ..notify import get_notifier
from ..review import run_review
from ..state import RunStore, now_iso
from ._shared import TEMPLATE_DIR, _engine, _notify, _out, _target


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
    depends_on = [d.strip() for d in (args.depends_on or "").split(",") if d.strip()] or None
    tag = getattr(args, "tag", None) or None
    try:
        rid = eng.create_run(args.title, args.request or args.title, args.run,
                             depends_on=depends_on, tag=tag)
    except ValueError as e:
        return _out({"ok": False, "error": str(e)})
    out = {"ok": True, "run_id": rid, "first_stage": eng.cfg.stages[0]}
    if depends_on:
        out["queued_behind"] = depends_on
    if tag:
        out["tag"] = tag
    return _out(out)


def cmd_stage(args) -> int:
    from ..advance import notify_message
    eng = _engine()
    res = eng.run_agent_stage(args.run, args.stage, resume=args.resume)
    # Notify regardless of trigger (manual `gantry stage` or the `advance` cron) —
    # a human watching a terminal for a 10+ minute stage is not a safe assumption.
    notifier = get_notifier(eng.cfg.notify)
    status = eng.store.state(args.run).get("status", "")
    _notify(eng.store, notifier, args.run, notify_message(eng.store, args.run, status, res), meta=res)
    return _out(res)


def cmd_retry(args) -> int:
    """Re-run a stage from scratch with a brand-new agent session — same
    rendered prompt, no stored session resumed, no feedback/comments injected.
    Distinct from `gantry revise` (sends feedback + expects a replan) and from
    resuming a `_failed` stage via `gantry stage --resume` (carries the dead
    session's context forward): this is for a stage that simply flaked
    (network blip, rate limit, transient tool error) and just needs an
    identical do-over."""
    from ..advance import notify_message
    eng = _engine()
    res = eng.run_agent_stage(args.run, args.stage, resume=False)
    notifier = get_notifier(eng.cfg.notify)
    status = eng.store.state(args.run).get("status", "")
    _notify(eng.store, notifier, args.run, notify_message(eng.store, args.run, status, res), meta=res)
    return _out(res)


def cmd_checks(args) -> int:
    from ..advance import notify_message
    eng = _engine()
    res = eng.run_checks(args.run)
    notifier = get_notifier(eng.cfg.notify)
    status = eng.store.state(args.run).get("status", "")
    _notify(eng.store, notifier, args.run, notify_message(eng.store, args.run, status, res), meta=res)
    return _out(res)


def cmd_review(args) -> int:
    from ..advance import notify_message
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
    has approved — requires an explicit human call, UNLESS [git].auto_ship is
    enabled, in which case advance_run (advance.py) calls ship_run directly
    on review_approved without this command being invoked at all."""
    from ..ship import ship_run
    eng = _engine()
    run_id = args.run
    state = eng.store.state(run_id)
    if state.get("status") != "review_approved" and not args.force:
        return _out({"ok": False, "error": f"run status is {state.get('status')!r}, "
                    f"not review_approved (use --force to override)"})
    return _out(ship_run(eng, run_id))


def cmd_mark_shipped(args) -> int:
    """Tell Gantry a run was shipped outside its own `ship` command — e.g. the
    human took over via `gantry hold`, made the PR/merge by hand, and now
    wants the run to stop showing as active in `watch`/`cleanup`/`cancel`'s
    "already shipped" guard. Distinct from `gantry ship` (which actually
    commits/pushes/opens the PR itself) — this only records that shipping
    already happened, matching how `shipped_manually` is already read
    everywhere else in the codebase (watch's green coloring, cleanup's
    default targets, cancel's already-shipped guard) despite nothing ever
    having set it until now."""
    store = RunStore(_target())
    run_id = args.run
    state = store.state(run_id)
    if state.get("status") in ("shipped", "shipped_manually") and not args.force:
        return _out({"ok": False, "error": f"run status is {state.get('status')!r}, "
                    f"already marked shipped (use --force to override)"})
    store.update_state(run_id, status="shipped_manually", shipped_at=now_iso())
    return _out({"ok": True, "run_id": run_id, "status": "shipped_manually"})


def cmd_mark_merged(args) -> int:
    """Record that a shipped run's PR has actually been merged into
    base_branch. Only matters when [git].auto_merge is off (the common case
    — auto_merge itself already sets `merged` as part of ship_run): a run's
    dependents (via `depends_on`) only start once their prereq is actually
    merged, not merely shipped (PR opened but possibly still under review,
    possibly never merged at all) — see Engine._prereqs_met. Without this
    command a human merging a PR by hand via GitHub's UI has no way to tell
    Gantry it happened, and every dependent run would wait forever."""
    store = RunStore(_target())
    run_id = args.run
    state = store.state(run_id)
    if state.get("status") not in ("shipped", "shipped_manually"):
        return _out({"ok": False, "error": f"run status is {state.get('status')!r}, "
                    f"not shipped/shipped_manually — nothing to mark merged"})
    store.update_state(run_id, merged=True, merged_at=now_iso())
    return _out({"ok": True, "run_id": run_id, "merged": True})


def cmd_hold(args) -> int:
    """Pause a run so nothing in Gantry touches it — `advance --all`/`loop`
    skip it, and the stale-running repair sweep leaves it alone — while a
    human works on the worktree by hand. Distinct from `cancel`: hold is
    reversible (`gantry resume` restores whatever status was active when
    held), cancel is a terminal dead end.

    Refuses on an already-*_running stage: holding mid-agent-invocation would
    still leave that subprocess running unsupervised in the worktree (nothing
    kills it), so the human would be editing files out from under a live
    agent. Let that stage finish (or fail/timeout) first, then hold."""
    store = RunStore(_target())
    run_id = args.run
    state = store.state(run_id)
    current_status = state.get("status", "")
    if current_status == "held":
        return _out({"ok": False, "error": "run is already held"})
    if current_status.endswith("_running"):
        return _out({"ok": False, "error": f"run status is {current_status!r} — an agent stage "
                    f"is actively running; wait for it to finish before holding"})
    store.update_state(run_id, status="held", held_from_status=current_status, held_at=now_iso())
    return _out({"ok": True, "run_id": run_id, "status": "held", "held_from_status": current_status})


def cmd_resume_hold(args) -> int:
    """Restore a held run to whatever status it was in before `gantry hold`,
    handing it back to the normal auto-advance machinery. Named
    resume_hold/`gantry resume` (not `unhold`) to read naturally as the
    counterpart to a human pausing work and then resuming it."""
    store = RunStore(_target())
    run_id = args.run
    state = store.state(run_id)
    if state.get("status") != "held":
        return _out({"ok": False, "error": f"run status is {state.get('status')!r}, not held"})
    restored = state.get("held_from_status", "blocked")
    store.update_state(run_id, status=restored, held_from_status=None)
    return _out({"ok": True, "run_id": run_id, "status": restored})


def cmd_cancel(args) -> int:
    """Mark a run cancelled — stops it from ever being auto-advanced or
    manually stage'd further. Doesn't touch the worktree unless --cleanup is
    passed; a bare cancel is meant to be fast and safe (e.g. cancelling by
    mistake shouldn't have already deleted anything)."""
    store = RunStore(_target())
    run_id = args.run
    state = store.state(run_id)
    if state.get("status") in ("shipped", "shipped_manually") and not args.force:
        return _out({"ok": False, "error": f"run status is {state.get('status')!r}, "
                    f"already shipped (use --force to cancel anyway)"})
    store.update_state(run_id, status="cancelled", cancelled_at=now_iso())
    result = {"ok": True, "run_id": run_id, "status": "cancelled"}
    if args.cleanup:
        result["worktree"] = remove_worktree(_target(), run_id)
    return _out(result)


_CLEANUP_DEFAULT_STATUSES = {"shipped", "shipped_manually", "cancelled"}


def cmd_cleanup(args) -> int:
    """Prune worktrees (and optionally state/artifacts) for finished runs.
    Dry-run by default — this deletes worktrees and, with --purge-state, a
    run's entire history, so a candidate listing comes back unless the
    caller explicitly opts into --yes."""
    tgt = _target()
    store = RunStore(tgt)
    statuses = set(args.status) if args.status else _CLEANUP_DEFAULT_STATUSES
    cutoff = time.time() - args.older_than_days * 86400 if args.older_than_days else None

    candidates = [
        r for r in store.list_runs()
        if r["status"] in statuses and (cutoff is None or r["mtime"] <= cutoff)
    ]

    if not args.yes:
        return _out({"ok": True, "dry_run": True, "count": len(candidates), "runs": candidates})

    results = []
    for r in candidates:
        run_id = r["id"]
        entry = {"run_id": run_id, "status": r["status"],
                  "worktree": remove_worktree(tgt, run_id)}
        if args.purge_state:
            shutil.rmtree(store.run_dir(run_id), ignore_errors=True)
            entry["state_purged"] = True
        results.append(entry)
    return _out({"ok": True, "dry_run": False, "count": len(results), "runs": results})


def cmd_status(args) -> int:
    store = RunStore(_target())
    if args.run:
        return _out(store.state(args.run))
    return _out(store.list_runs())


def cmd_advance(args) -> int:
    from ..advance import advance_all, advance_run, notify_message
    tgt = _target()
    cfg = load_config(tgt)
    if args.all:
        results = advance_all(tgt, cfg, tag=getattr(args, "tag", None))
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
    try:
        return _out(advance_run(eng, args.run))
    except Exception as exc:
        # Any uncaught exception here (e.g. a deterministic step like e2e
        # raising instead of being caught internally) would otherwise leave
        # state.json at whatever "{stage}_running" it was already showing —
        # main()'s top-level handler only prints the error, it never touches
        # run state. Mark the in-flight stage failed so `gantry watch`/status
        # doesn't lie about a run still being alive after this process exits.
        st = eng.store.state(args.run)
        current = st.get("current_stage") or st.get("status", "").removesuffix("_running")
        if current and st.get("status", "").endswith("_running"):
            eng._set_status(args.run, f"{current}_failed")
        return _out({"ok": False, "error": str(exc)})


# Statuses where a single-run loop should stop: the run is done, needs a human,
# or has errored. Mirrors advance.py's AUTO_TRANSITIONS gate but from the other
# side — these are exactly the statuses NOT in AUTO_TRANSITIONS that a bare
# `advance --run` tick would refuse to touch, plus terminal ship states.
_LOOP_STOP_SUFFIXES = ("_failed", "_approved", "_escalated", "_shipped")
_LOOP_STOP_PREFIXES = ("shipped",)
_LOOP_STOP_EXACT = {"blocked", "cancelled", "held"}


def _is_loop_terminal(status: str, cfg=None) -> bool:
    """review_approved and checks_escalated are only REAL terminal states
    for a project that hasn't opted into auto_ship/auto_resolve — same
    conditional-gate logic as advance_all's own AUTO_TRANSITIONS handling
    (advance.py). Without this, `gantry loop --run ID` on a project with
    auto_ship=true stops the moment review approves instead of continuing
    to watch the run through to ship_run actually firing — auto_ship never
    gets a chance to act under `loop`, exactly the bug auto_ship exists to
    avoid under `advance --all`.

    awaiting_spec/awaiting_design are real human gates (DOC_STAGES) and stop
    the loop; awaiting_plan/awaiting_build/awaiting_evidence (AGENT_STAGES)
    are not — they just mean "approved to start, not yet kicked off" (see
    advance.py's AUTO_TRANSITIONS comment), so the loop must fire the stage
    itself instead of treating a freshly-created run as already terminal."""
    if status.startswith("awaiting_"):
        stage = status.removeprefix("awaiting_")
        return stage not in AGENT_STAGES
    if cfg is not None:
        if status == "review_approved" and cfg.git.auto_ship:
            return False
        if status == "checks_escalated" and cfg.checks.auto_resolve:
            return False
    if status in _LOOP_STOP_EXACT:
        return True
    if any(status.endswith(suf) for suf in _LOOP_STOP_SUFFIXES):
        return True
    if any(status.startswith(pre) for pre in _LOOP_STOP_PREFIXES):
        return True
    return False


def cmd_loop(args) -> int:
    """Repeatedly tick the pipeline (in-process, foreground) instead of relying
    on an external cron calling `gantry advance --all` on a timer. With --run,
    stops as soon as that run reaches a human-gated or terminal state; without
    it, loops every run forever (Ctrl-C to stop), same transitions `--all` drives."""
    from ..advance import advance_all, advance_run, notify_message
    tgt = _target()
    cfg = load_config(tgt)
    store = RunStore(tgt)
    ticks = 0
    print(f"gantry loop: interval={args.interval}s"
          + (f" run={args.run}" if args.run else " (all runs)"), flush=True)
    try:
        while True:
            ticks += 1
            if args.run:
                status = store.state(args.run).get("status", "")
                print(f"[tick {ticks}] {args.run}: {status}", flush=True)
                if _is_loop_terminal(status, cfg):
                    print(f"gantry loop: stopping, reached {status}", flush=True)
                    return 0
                eng = Engine(tgt, cfg)
                result = advance_run(eng, args.run)
                print(f"[tick {ticks}] advance -> {result}", flush=True)
            else:
                results = advance_all(tgt, cfg, tag=getattr(args, "tag", None))
                if results:
                    notifier = get_notifier(cfg.notify)
                    for r in results:
                        if r.get("action") == "skipped_locked":
                            continue
                        st = store.state(r["run_id"]).get("status", "")
                        _notify(store, notifier, r["run_id"],
                               notify_message(store, r["run_id"], st, r), meta=r)
                print(f"[tick {ticks}] advance --all -> {len(results)} run(s) touched", flush=True)
            if args.max_ticks and ticks >= args.max_ticks:
                print(f"gantry loop: stopping, reached max-ticks={args.max_ticks}", flush=True)
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\ngantry loop: interrupted", flush=True)
        return 130
