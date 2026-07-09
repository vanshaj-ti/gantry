"""gantry cockpit: a tmux workspace pre-wired for a target repo, shipped
inherently with gantry (no external tool dependency, unlike the optional
herdr integration — see scripts/gantry-herdr.sh and README's herdr section).

Layout:

    +----------------------------------------------------------+
    |  status bar (gantry watch --live) — full width, thin     |
    +---------------------------+--------------------------------+
    |                           |                                |
    |  doc viewer (left)        |  claude session (right)         |
    |  gantry docs --follow     |  claude --dangerously-skip-...  |
    |                           |                                |
    +---------------------------+--------------------------------+

Re-running `gantry cockpit` against the same target reuses the existing tmux
session (by name) instead of spawning a duplicate.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

STATUS_BAR_HEIGHT = 6


def session_name(target: Path) -> str:
    return f"gantry-{target.name}"


def _tmux(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["tmux", *args], capture_output=True, text=True, check=False)


def session_exists(name: str) -> bool:
    return _tmux("has-session", "-t", name).returncode == 0


def _pane_cmd(target: Path, cmd: str) -> str:
    return f"export GANTRY_TARGET={target}; cd {target}; {cmd}"


def build_cockpit(target: Path) -> dict:
    """Create (or reuse) the tmux workspace for `target`. Returns
    {ok, session, reused} — never raises; tmux errors surface in `ok`/`error`."""
    name = session_name(target)

    if session_exists(name):
        return {"ok": True, "session": name, "reused": True}

    proc = _tmux("new-session", "-d", "-s", name, "-c", str(target))
    if proc.returncode != 0:
        return {"ok": False, "error": proc.stderr.strip() or "tmux new-session failed"}

    # Split off the top status bar: `-l N` on the ORIGINAL pane (index 0)
    # gives the NEW pane N lines and leaves the remainder with the original
    # pane — so the split target keeps pane 0 as the (large) bottom area and
    # the newly created pane becomes the thin status bar on top.
    proc = _tmux("split-window", "-t", f"{name}.0", "-v", "-b",
                 "-l", str(STATUS_BAR_HEIGHT))
    if proc.returncode != 0:
        _tmux("kill-session", "-t", name)
        return {"ok": False, "error": proc.stderr.strip() or "tmux split-window (status) failed"}

    # Split the bottom pane left/right: doc viewer left, claude session right.
    proc = _tmux("split-window", "-t", f"{name}.1", "-h", "-p", "50")
    if proc.returncode != 0:
        _tmux("kill-session", "-t", name)
        return {"ok": False, "error": proc.stderr.strip() or "tmux split-window (left/right) failed"}

    panes = _tmux("list-panes", "-t", name, "-F", "#{pane_id} #{pane_top} #{pane_left}")
    pane_lines = [ln.split() for ln in panes.stdout.strip().splitlines() if ln.strip()]
    # Identify roles by position, not creation order (tmux pane indices can
    # renumber): smallest top = status bar; of the remaining two, smaller
    # left = doc viewer, larger left = claude session.
    by_top = sorted(pane_lines, key=lambda p: int(p[1]))
    status_pane = by_top[0][0]
    bottom = sorted(by_top[1:], key=lambda p: int(p[2]))
    docs_pane, claude_pane = bottom[0][0], bottom[1][0]

    _tmux("send-keys", "-t", status_pane,
          _pane_cmd(target, "gantry watch --live"), "Enter")
    _tmux("send-keys", "-t", docs_pane,
          _pane_cmd(target, "gantry docs --follow"), "Enter")
    _tmux("send-keys", "-t", claude_pane,
          _pane_cmd(target, "claude --dangerously-skip-permissions"), "Enter")
    _tmux("select-pane", "-t", claude_pane)

    return {"ok": True, "session": name, "reused": False}


def attach(name: str) -> int:
    """Replace this process with `tmux attach`. Does not return on success."""
    import os
    os.execvp("tmux", ["tmux", "attach", "-t", name])
