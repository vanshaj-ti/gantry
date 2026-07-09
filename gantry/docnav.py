"""Arrow-key doc navigator for `gantry docs --nav` — a persistent, always-on
curses TUI for browsing a run's stage docs, meant to live in a cockpit pane.

Three levels: run list -> doc list (for the selected run) -> doc content.
Right/Enter drills in, Left/Esc backs out one level (quits from the run
list). Every render is a full curses erase+redraw — no scroll-history
leakage, unlike a print()-based loop (curses uses the terminal's alternate
screen buffer, which tmux panes support natively).

The pure state-transition logic (`_next_state`) is separated from the
curses I/O loop (`run_navigator`) so it's testable without a real tty —
curses needs one, so CI can't exercise the loop itself.
"""
from __future__ import annotations

import curses
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any

from .advance import label
from .state import RunStore

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

DOC_ARTIFACTS = [
    ("intake.md", "Intake"),
    ("product-spec.md", "Spec"),
    ("architecture-design.md", "Design"),
    ("implementation-plan.md", "Plan"),
    ("build-summary.md", "Build summary"),
    ("evidence-report.md", "Evidence"),
]

LEVEL_RUNS = 0
LEVEL_DOCS = 1
LEVEL_CONTENT = 2

KEY_UP = {"KEY_UP", "k"}
KEY_DOWN = {"KEY_DOWN", "j"}
KEY_IN = {"KEY_RIGHT", "l", "\n", "\r"}
KEY_OUT = {"KEY_LEFT", "h", "\x1b"}  # \x1b == Esc
KEY_QUIT = {"q", "Q"}


@dataclass
class NavState:
    level: int = LEVEL_RUNS
    run_selected: int = 0
    doc_selected: int = 0
    content_scroll: int = 0
    run_id: str | None = None
    doc_filename: str | None = None
    quit: bool = False


def run_doc_list(store: RunStore, run_id: str) -> list[tuple[str, str]]:
    """(label, filename) pairs for whichever docs this run has actually
    written. No synthetic "All docs" entry here — --nav shows one doc at a
    time, unlike --pick's fzf flow."""
    out = []
    for filename, label_text in DOC_ARTIFACTS:
        if store.read_artifact(run_id, filename) is not None:
            out.append((f"{label_text} ({filename})", filename))
    if store.read_result(run_id, "review-result.json"):
        out.append(("Review", "review-result.json"))
    return out


def read_doc_content(store: RunStore, run_id: str, filename: str) -> str:
    if filename == "review-result.json":
        review = store.read_result(run_id, filename)
        return f"Verdict: {review.get('verdict', '?')}\n\n{review.get('result', '')}"
    return store.read_artifact(run_id, filename) or "(empty)"


def _next_state(state: NavState, key: str, runs: list[dict], docs: list[tuple[str, str]]) -> NavState:
    """Pure state transition: given the current state, a keypress, and the
    current run/doc lists, return the next state. No I/O — testable without
    curses."""
    if key in KEY_QUIT:
        return NavState(**{**state.__dict__, "quit": True})

    if state.level == LEVEL_RUNS:
        if not runs:
            return state
        if key in KEY_UP:
            return NavState(**{**state.__dict__, "run_selected": max(0, state.run_selected - 1)})
        if key in KEY_DOWN:
            return NavState(**{**state.__dict__, "run_selected": min(len(runs) - 1, state.run_selected + 1)})
        if key in KEY_IN:
            run_id = runs[state.run_selected]["id"]
            return NavState(level=LEVEL_DOCS, run_selected=state.run_selected, run_id=run_id)
        return state

    if state.level == LEVEL_DOCS:
        if key in KEY_OUT:
            return NavState(level=LEVEL_RUNS, run_selected=state.run_selected)
        if not docs:
            return state
        if key in KEY_UP:
            return NavState(**{**state.__dict__, "doc_selected": max(0, state.doc_selected - 1)})
        if key in KEY_DOWN:
            return NavState(**{**state.__dict__, "doc_selected": min(len(docs) - 1, state.doc_selected + 1)})
        if key in KEY_IN:
            filename = docs[state.doc_selected][1]
            return NavState(level=LEVEL_CONTENT, run_selected=state.run_selected,
                            doc_selected=state.doc_selected, run_id=state.run_id,
                            doc_filename=filename)
        return state

    if state.level == LEVEL_CONTENT:
        if key in KEY_OUT:
            return NavState(level=LEVEL_DOCS, run_selected=state.run_selected,
                            doc_selected=state.doc_selected, run_id=state.run_id)
        if key in KEY_UP:
            return NavState(**{**state.__dict__, "content_scroll": max(0, state.content_scroll - 1)})
        if key in KEY_DOWN:
            return NavState(**{**state.__dict__, "content_scroll": state.content_scroll + 1})
        return state

    return state


def _draw_list(stdscr, title: str, items: list[str], selected: int) -> None:
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    stdscr.addnstr(0, 0, title, w - 1, curses.A_BOLD)
    stdscr.addnstr(1, 0, "-" * min(w - 1, len(title)), w - 1)
    for i, item in enumerate(items):
        row = i + 3
        if row >= h - 1:
            break
        attr = curses.A_REVERSE if i == selected else curses.A_NORMAL
        stdscr.addnstr(row, 0, item, w - 1, attr)
    footer = "up/down move  right/enter open  left/esc back  q quit"
    stdscr.addnstr(h - 1, 0, footer, w - 1, curses.A_DIM)
    stdscr.refresh()


def _render_via_glow(content: str, width: int) -> list[str]:
    """Word-wrap markdown content at `width` via glow, plain (no ANSI) output
    — `-s notty` renders without color/style codes, which is what a curses
    window needs (compositing real ANSI into curses attrs would need a full
    escape-sequence parser; not worth it for what was actually asked here,
    which is correct wrapping). Falls back to raw splitlines (unwrapped) if
    glow isn't on PATH or the call fails for any reason."""
    glow = shutil.which("glow")
    if not glow:
        return content.splitlines() or [""]
    try:
        proc = subprocess.run([glow, "-w", str(max(20, width)), "-s", "notty", "-"],
                              input=content, text=True, capture_output=True, timeout=10)
        if proc.returncode != 0 or not proc.stdout:
            return content.splitlines() or [""]
        return _ANSI_RE.sub("", proc.stdout).splitlines() or [""]
    except Exception:
        return content.splitlines() or [""]


def _draw_content(stdscr, title: str, lines: list[str], scroll: int) -> int:
    """Renders pre-rendered `lines` starting at line `scroll`; returns the
    clamped scroll actually used (so the caller's state stays in bounds)."""
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    max_scroll = max(0, len(lines) - (h - 3))
    scroll = max(0, min(scroll, max_scroll))
    stdscr.addnstr(0, 0, title, w - 1, curses.A_BOLD)
    stdscr.addnstr(1, 0, "-" * min(w - 1, len(title)), w - 1)
    for i, line in enumerate(lines[scroll:scroll + (h - 3)]):
        stdscr.addnstr(2 + i, 0, line, w - 1)
    footer = "up/down scroll  left/esc back  q quit"
    stdscr.addnstr(h - 1, 0, footer, w - 1, curses.A_DIM)
    stdscr.refresh()
    return scroll


def _run_loop(stdscr, store: RunStore) -> None:
    curses.curs_set(0)
    # Without this, ncurses paints its own default background (often a flat
    # grey/black block) instead of inheriting the real terminal/tmux pane
    # background — reads as a mismatched "overlay" against the rest of the
    # pane. -1 means "whatever the terminal's actual bg/fg already is".
    try:
        curses.use_default_colors()
    except curses.error:
        pass  # terminal doesn't support it — harmless no-op
    stdscr.nodelay(True)
    stdscr.timeout(200)  # ms — poll interval for both keypress and refresh

    state = NavState()
    last_runs_key: Any = None
    last_docs_key: Any = None
    runs: list[dict] = []
    docs: list[tuple[str, str]] = []
    last_poll = 0.0

    last_content_key: Any = None
    rendered_lines: list[str] = [""]

    while not state.quit:
        now = time.time()
        if now - last_poll >= 1.0:
            last_poll = now
            new_runs = store.list_runs()
            runs_key = tuple((r["id"], r["mtime"]) for r in new_runs)
            if runs_key != last_runs_key:
                last_runs_key = runs_key
                runs = new_runs
            if state.run_id:
                new_docs = run_doc_list(store, state.run_id)
                docs_key = tuple(new_docs)
                if docs_key != last_docs_key:
                    last_docs_key = docs_key
                    docs = new_docs

        if state.level == LEVEL_RUNS:
            items = [f"{(r['title'] or r['id'])}  [{label(r['status'])}]" for r in runs]
            _draw_list(stdscr, f"GANTRY RUNS ({len(runs)})", items, state.run_selected)
        elif state.level == LEVEL_DOCS:
            items = [d[0] for d in docs] or ["(no docs yet for this run)"]
            title = f"DOCS — {state.run_id}"
            _draw_list(stdscr, title, items, state.doc_selected if docs else 0)
        else:
            content = read_doc_content(store, state.run_id, state.doc_filename)
            width = stdscr.getmaxyx()[1]
            content_key = (state.doc_filename, width, content)
            if content_key != last_content_key:
                last_content_key = content_key
                rendered_lines = _render_via_glow(content, width)
            title = f"{state.doc_filename} — {state.run_id}"
            state.content_scroll = _draw_content(stdscr, title, rendered_lines, state.content_scroll)

        try:
            key = stdscr.getkey()
        except curses.error:
            continue  # no input this tick — just re-poll/redraw

        state = _next_state(state, key, runs, docs)
        if state.level == LEVEL_DOCS and state.run_id and not docs:
            docs = run_doc_list(store, state.run_id)
            last_docs_key = tuple(docs)


def run_navigator(store: RunStore) -> None:
    curses.wrapper(_run_loop, store)
