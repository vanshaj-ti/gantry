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
import io
import re
import time
from dataclasses import dataclass, field
from typing import Any

from rich.console import Console
from rich.markdown import Markdown
from rich.theme import Theme

from .advance import label
from .state import RunStore

# rich's default markdown theme distinguishes heading levels mostly by
# weight/underline (all one hue) — too subtle to tell apart at a glance in
# a terminal pane. Force a distinct, high-contrast color per level instead.
_MARKDOWN_THEME = Theme({
    "markdown.h1": "bold cyan",
    "markdown.h2": "bold yellow",
    "markdown.h3": "bold green",
    "markdown.h4": "bold blue",
    "markdown.h5": "bold magenta",
    "markdown.h6": "bold red",
})

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
        out.append(("Review (review-result.json)", "review-result.json"))
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


_ANSI_SGR_RE = re.compile(r"\x1b\[([0-9;]*)m")

# glow's "dark" glamour style emits only these style bits for markdown
# emphasis (verified against real pty-captured output) — bold/italic/
# underline, plus indexed 256-color foreground (38;5;N) and background
# (48;5;N) for headers/code/tables. No truecolor (38;2;r;g;b) in this
# version. A small, fully-known set, not a general ANSI interpreter —
# unknown codes are silently ignored (forward compatible: a future glow
# version adding a code we don't map just renders as A_NORMAL for that run,
# not a crash).
_SGR_ATTR = {"1": curses.A_BOLD, "3": curses.A_ITALIC, "4": curses.A_UNDERLINE}

# Basic 16-color SGR codes -> curses color index. Standard ANSI ordering
# (black/red/green/yellow/blue/magenta/cyan/white); the "bright" 90-97/
# 100-107 range has no distinct curses base-16 equivalent without extended
# color support (which the 256-color path above already provides), so
# bright variants alias to their base color — a small fidelity loss, not a
# correctness bug. Verified rich's Console emits this range alongside the
# 256-indexed form (e.g. "1;35" for bold magenta), not just one or the other.
_BASE16 = [curses.COLOR_BLACK, curses.COLOR_RED, curses.COLOR_GREEN, curses.COLOR_YELLOW,
           curses.COLOR_BLUE, curses.COLOR_MAGENTA, curses.COLOR_CYAN, curses.COLOR_WHITE]
_BASIC_FG = {str(30 + i): c for i, c in enumerate(_BASE16)}
_BASIC_FG.update({str(90 + i): c for i, c in enumerate(_BASE16)})  # bright fg
_BASIC_BG = {str(40 + i): c for i, c in enumerate(_BASE16)}
_BASIC_BG.update({str(100 + i): c for i, c in enumerate(_BASE16)})  # bright bg

# (fg, bg) 256-color index pair -> registered curses color-pair number.
# Populated lazily via _color_pair_for() the first time each combination is
# seen — a single markdown style's palette is small (headers/code/quotes/
# links, roughly a dozen combinations), well within curses.COLOR_PAIRS.
_COLOR_PAIR_CACHE: dict[tuple[int, int], int] = {}
_next_pair_number = 1


def _color_pair_for(fg: int, bg: int) -> int:
    """Register (or reuse) a curses color pair for this 256-color (fg, bg)
    combination, returning the curses.color_pair() attribute bitmask.
    Requires curses.start_color() to have already been called (done once in
    _run_loop). No-op-safe if curses isn't initialized (e.g. under
    unittest without a real screen) — falls back to A_NORMAL rather than
    raising, since color is a visual nicety, not correctness-critical."""
    global _next_pair_number
    key = (fg, bg)
    if key in _COLOR_PAIR_CACHE:
        return curses.color_pair(_COLOR_PAIR_CACHE[key])
    # COLOR_PAIRS only exists once initscr()/start_color() have run (e.g.
    # inside curses.wrapper) — absent entirely under plain unittest, which
    # has no real screen. Treat that the same as "no color available."
    max_pairs = getattr(curses, "COLOR_PAIRS", 0)
    if _next_pair_number >= max_pairs:
        return curses.A_NORMAL  # no color support / palette exhausted
    try:
        curses.init_pair(_next_pair_number, fg, bg)
    except curses.error:
        return curses.A_NORMAL  # e.g. color_content unsupported in this term
    _COLOR_PAIR_CACHE[key] = _next_pair_number
    pair_attr = curses.color_pair(_next_pair_number)
    _next_pair_number += 1
    return pair_attr


def _parse_ansi_line(line: str) -> list[tuple[str, int]]:
    """Split one line of glow's ANSI output into (text, curses_attr) segments,
    tracking bold/italic/underline/256-color foreground+background state
    across SGR codes within the line. Pure function apart from the color-pair
    side table (curses-dependent, but degrades to A_NORMAL when curses isn't
    initialized) — unit-testable directly."""
    segments: list[tuple[str, int]] = []
    style_bits = curses.A_NORMAL
    fg = bg = -1  # -1 == default/unset (curses convention for "terminal default")
    pos = 0

    def current_attr() -> int:
        if fg == -1 and bg == -1:
            return style_bits
        return style_bits | _color_pair_for(fg, bg)

    for m in _ANSI_SGR_RE.finditer(line):
        text = line[pos:m.start()]
        if text:
            segments.append((text, current_attr()))
        pos = m.end()
        codes = m.group(1).split(";") if m.group(1) else ["0"]
        i = 0
        while i < len(codes):
            code = codes[i]
            if code in ("", "0"):
                style_bits, fg, bg = curses.A_NORMAL, -1, -1
            elif code in _SGR_ATTR:
                style_bits |= _SGR_ATTR[code]
            elif code == "38" and i + 2 < len(codes) and codes[i + 1] == "5":
                fg = int(codes[i + 2]) if codes[i + 2].isdigit() else -1
                i += 2
            elif code == "48" and i + 2 < len(codes) and codes[i + 1] == "5":
                bg = int(codes[i + 2]) if codes[i + 2].isdigit() else -1
                i += 2
            elif code in _BASIC_FG:
                fg = _BASIC_FG[code]
            elif code in _BASIC_BG:
                bg = _BASIC_BG[code]
            i += 1
    tail = line[pos:]
    if tail:
        segments.append((tail, current_attr()))
    return segments or [("", curses.A_NORMAL)]


def _render_markdown(content: str, width: int) -> list[list[tuple[str, int]]]:
    """Render markdown content at `width` via rich, in-process, parsed into
    (text, curses_attr) segments per line so headers/bold/italic/underline/
    code/tables render with matching curses attributes instead of literal
    `**`/`#` characters.

    Previously this shelled out to `glow` through a manually-managed pty
    (glow silently downgrades to flat bold-only output when its stdout
    isn't a real terminal, which piping through subprocess.run always is) —
    replaced with rich's Console/Markdown, which renders correctly with no
    subprocess, no pty, no deadlock-prone drain loop, and no external binary
    dependency at all."""
    buf = io.StringIO()
    console = Console(file=buf, width=max(20, width), force_terminal=True,
                      color_system="256", legacy_windows=False, theme=_MARKDOWN_THEME)
    try:
        console.print(Markdown(content))
    except Exception:
        return [[(ln, curses.A_NORMAL)] for ln in (content.splitlines() or [""])]
    lines = buf.getvalue().splitlines() or [""]
    return [_parse_ansi_line(ln) for ln in lines]


def _draw_content(stdscr, title: str, lines: list[list[tuple[str, int]]], scroll: int) -> int:
    """Renders pre-rendered segment `lines` starting at line `scroll`;
    returns the clamped scroll actually used (so the caller's state stays
    in bounds)."""
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    max_scroll = max(0, len(lines) - (h - 3))
    scroll = max(0, min(scroll, max_scroll))
    stdscr.addnstr(0, 0, title, w - 1, curses.A_BOLD)
    stdscr.addnstr(1, 0, "-" * min(w - 1, len(title)), w - 1)
    for i, segments in enumerate(lines[scroll:scroll + (h - 3)]):
        row = 2 + i
        col = 0
        for text, attr in segments:
            if col >= w - 1:
                break
            stdscr.addnstr(row, col, text, w - 1 - col, attr)
            col += len(text)
    footer = "up/down scroll  left/esc back  q quit"
    stdscr.addnstr(h - 1, 0, footer, w - 1, curses.A_DIM)
    stdscr.refresh()
    return scroll


def _run_loop(stdscr, store: RunStore) -> None:
    curses.curs_set(0)
    # Needed before init_pair()/color_pair() work at all (_color_pair_for,
    # used by the glow-ANSI parser to render real 256-color markdown
    # styling). Must come before use_default_colors() below.
    try:
        curses.start_color()
    except curses.error:
        pass  # terminal doesn't support color — glow rendering falls back
              # to style bits only (bold/italic/underline), no crash
    # Without this, ncurses paints its own default background (often a flat
    # grey/black block) instead of inheriting the real terminal/tmux pane
    # background — reads as a mismatched "overlay" against the rest of the
    # pane. -1 means "whatever the terminal's actual bg/fg already is".
    try:
        curses.use_default_colors()
    except curses.error:
        pass  # terminal doesn't support it — harmless no-op
    # Scroll wheel arrives as BUTTON4 (up) / BUTTON5 (down) — tmux passes
    # these through once the pane's mouse mode is on (cockpit.py enables it
    # for the whole session). Without registering the mask, wheel events are
    # either swallowed or misread as raw escape bytes by getkey().
    try:
        curses.mousemask(curses.BUTTON4_PRESSED | curses.BUTTON5_PRESSED)
    except curses.error:
        pass  # terminal doesn't report mouse events — harmless no-op
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
                rendered_lines = _render_markdown(content, width)
            title = f"{state.doc_filename} — {state.run_id}"
            state.content_scroll = _draw_content(stdscr, title, rendered_lines, state.content_scroll)

        try:
            key = stdscr.getkey()
        except curses.error:
            continue  # no input this tick — just re-poll/redraw

        if key == "KEY_MOUSE":
            try:
                _, _, _, _, bstate = curses.getmouse()
            except curses.error:
                continue
            # Wheel scroll becomes the same up/down keys that already work
            # at every level — no new state-transition logic needed, mouse
            # is just another input source feeding the existing pure
            # _next_state function.
            if bstate & curses.BUTTON4_PRESSED:
                key = "KEY_UP"
            elif bstate & curses.BUTTON5_PRESSED:
                key = "KEY_DOWN"
            else:
                continue

        state = _next_state(state, key, runs, docs)
        if state.level == LEVEL_DOCS and state.run_id and not docs:
            docs = run_doc_list(store, state.run_id)
            last_docs_key = tuple(docs)


def run_navigator(store: RunStore) -> None:
    curses.wrapper(_run_loop, store)
