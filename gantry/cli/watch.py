"""Dashboard and Telegram-reply handling: watch, listen."""
from __future__ import annotations

import json
import shutil
import sys
import time

from ..config import load_config
from ..notify import fetch_telegram_replies, get_notifier
from ..state import RunStore
from ._shared import NEEDS_INPUT_STATUSES, _notify, _target

_WATCH_COLOR = {
    "green": "\033[32m", "yellow": "\033[33m", "red": "\033[31m", "reset": "\033[0m",
}


def _watch_color_family(status: str) -> str:
    if status in ("shipped", "shipped_manually") or status.endswith("_complete") or status == "review_approved":
        return "green"
    if status in ("blocked",) or status.endswith("_escalated") or status.endswith("_failed"):
        return "red"
    if status.endswith("_running"):
        return "yellow"
    if status == "held":
        return "yellow"
    return ""


def cmd_watch(args) -> int:
    """Live/one-shot dashboard of all runs in the target repo."""
    from ..advance import short_label as label
    store = RunStore(_target())
    colorize = sys.stdout.isatty()

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

    def running_session(run_id: str, status: str) -> tuple[str, str, str]:
        """(runner, model, short_session_id) for a *_running stage, all
        blank if the run isn't currently running an agent stage."""
        if not status.endswith("_running"):
            return "", "", ""
        stage = status.removesuffix("_running")
        session = store.get_session(run_id, stage)
        runner = session.get("runner", "")
        model = session.get("model") or "default"
        sid = session.get("session_id", "")
        sid_short = f"{sid[:8]}…" if sid else ""
        return runner, model, sid_short

    def detail_for(run_id: str, status: str) -> str:
        """Retry/blocked context only — agent/model/session now have their
        own columns (see running_session), so this stays scoped to what a
        stuck run is actually blocked on."""
        if status not in ("blocked", "checks_escalated", "resolve_escalated", "held",
                          "shipped", "shipped_manually"):
            return ""
        st = store.state(run_id)
        if status == "held":
            return f"was: {st.get('held_from_status', '')}"
        if status in ("shipped", "shipped_manually"):
            # `merged` is otherwise an invisible flag — shipped (PR opened)
            # and actually-merged look identical without this, and
            # dependents (depends_on) only start once a run is BOTH shipped
            # AND merged (see Engine._prereqs_met), so this is exactly the
            # state a human watching the dashboard needs to see at a glance.
            return "merged" if st.get("merged") is True else "not yet merged"
        blocked_on = st.get("blocked_on", "")
        if status == "resolve_escalated":
            attempts = st.get("resolve_attempt_count")
            cap = load_config(_target()).checks.resolve_attempts
            if attempts is not None and blocked_on:
                return f"{blocked_on} (resolve {attempts}/{cap})"
            return blocked_on
        retry = st.get("checks_retry_count")
        cfg_cap = load_config(_target()).checks.retry_checks
        if retry is not None and blocked_on:
            return f"{blocked_on} (retry {retry}/{cfg_cap})"
        return blocked_on

    def paint(text: str, status: str) -> str:
        if not colorize:
            return text
        family = _watch_color_family(status)
        if not family:
            return text
        return f"{_WATCH_COLOR[family]}{text}{_WATCH_COLOR['reset']}"

    def cost_for(run_id: str) -> str:
        cost = store.state(run_id).get("total_cost_usd")
        return f"${cost:.2f}" if cost is not None else ""

    def render() -> None:
        cols = shutil.get_terminal_size().columns
        runs = store.list_runs()
        tag_filter = getattr(args, "tag", None)
        if tag_filter:
            runs = [r for r in runs if r.get("tag") == tag_filter]
        lines = [f"GANTRY — {len(runs)} run(s)" + (f" (tag={tag_filter})" if tag_filter else ""), ""]

        status_w, agent_w, model_w, session_w, cost_w, detail_w, updated_w = 20, 12, 16, 10, 8, 20, 10
        fixed = status_w + agent_w + model_w + session_w + cost_w + detail_w + updated_w
        # Titles are short slugs in practice (run_id-derived) — absorbing
        # every leftover column in a wide status bar just leaves a huge
        # empty gap, not more useful information. Cap it well below "all
        # remaining space".
        title_w = max(20, min(40, cols - fixed - 6))

        headers = ("TITLE", "STATUS", "AGENT", "MODEL", "SESSION", "COST", "DETAIL", "UPDATED")
        widths = (title_w, status_w, agent_w, model_w, session_w, cost_w, detail_w, updated_w)
        lines.append(" ".join(trunc(h, w) for h, w in zip(headers, widths)))
        lines.append("-" * min(cols, sum(widths) + 6))
        for r in runs:
            title = r["title"] or r["id"]
            status_text = label(r["status"])
            runner, model, sid = running_session(r["id"], r["status"])
            cost_text = cost_for(r["id"])
            detail_text = detail_for(r["id"], r["status"])
            row = " ".join(trunc(v, w) for v, w in zip(
                (title, status_text, runner, model, sid, cost_text, detail_text, age(r["mtime"])), widths))
            lines.append(paint(row, r["status"]))

        if args.live:
            lines.append("\n(Ctrl+C to exit — refreshing every 2s)")

        # Single write, not one print() per line: on a --live refresh the clear
        # sequence and the new frame must land in the terminal as one unit —
        # otherwise a slow pipe (SSH, tmux over a laggy link) can flush the
        # clear before the content is fully written, showing a blank/partial
        # pane for a frame. The footer line above must be part of THIS same
        # write too — a separate print() after render() returns is a second,
        # un-cleared stdout write every tick, which tmux/the terminal treats
        # as new scrollback content rather than part of the repainted frame,
        # making the pane scroll upward by one line on every refresh forever.
        clear = "\033[2J\033[H" if args.live else ""
        sys.stdout.write(clear + "\n".join(lines) + "\n")
        sys.stdout.flush()

    if not args.live:
        render()
        return 0
    try:
        while True:
            render()
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
    from ..advance import label
    from ..engine import Engine
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
