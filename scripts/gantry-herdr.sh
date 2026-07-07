#!/usr/bin/env bash
# gantry-herdr — open a herdr dashboard workspace pre-wired for a Gantry
# target repo, three panes:
#
#   +----------------------------------------------------------+
#   |  gantry watch --live  (full width, thin — status is just |
#   |  info: title/status/updated-at, doesn't need real estate) |
#   +---------------------------------+--------------------------+
#   |                                 |  gantry docs --pick       |
#   |  claude --dangerously-skip-     |  (interactive: choose a   |
#   |  permissions  (your assistant   |  run, then a doc, Esc to  |
#   |  driving gantry runs)           |  go back a level)         |
#   +---------------------------------+--------------------------+
#
# Reuses the workspace if one for this repo is already open instead of
# spawning duplicates.
#
# Usage: gantry-herdr [target-repo-path]
#   Defaults to ~/edupaid if no path given.
set -euo pipefail

TARGET="${1:-$HOME/edupaid}"
TARGET="$(cd "$TARGET" && pwd)"   # normalize to absolute path
REPO_NAME="$(basename "$TARGET")"
LABEL="gantry: $REPO_NAME"
VENV_ACTIVATE="$HOME/gantry/.venv/bin/activate"

if [ ! -f "$TARGET/gantry.toml" ]; then
  echo "warning: $TARGET/gantry.toml not found — 'gantry doctor' will show config_present=false" >&2
fi

# Make sure the herdr server is actually up before we start driving the socket API.
# NB: use `grep ... >/dev/null` here, not `grep -q` — with `set -o pipefail`,
# `grep -q` exits immediately on first match without draining stdin, which
# SIGPIPEs `herdr status` and makes it exit non-zero, tripping pipefail even
# though the server IS up. `grep >/dev/null` drains the pipe fully first.
if ! herdr status 2>/dev/null | grep "status: running" >/dev/null; then
  echo "herdr server not running — starting it..." >&2
  # `herdr` bare launches/attaches to the persistent session, which also
  # brings the server up. We don't want to block here, so just nudge it via
  # a no-op status check loop instead of launching the full TUI ourselves.
  nohup herdr server >/tmp/herdr-server.log 2>&1 &
  for _ in $(seq 1 20); do
    herdr status 2>/dev/null | grep "status: running" >/dev/null && break
    sleep 0.5
  done
fi

# Reuse an existing workspace for this repo if one's already open, so
# re-running this command doesn't pile up duplicate tabs every time.
EXISTING_WS="$(herdr workspace list 2>/dev/null | python3 -c "
import json,sys
data = json.load(sys.stdin)
for ws in data.get('result', {}).get('workspaces', []):
    if ws.get('label') == '$LABEL':
        print(ws['workspace_id'])
        break
" 2>/dev/null || true)"

if [ -n "$EXISTING_WS" ]; then
  echo "Reusing existing workspace $EXISTING_WS for $REPO_NAME" >&2
  herdr workspace focus "$EXISTING_WS" >/dev/null
else
  echo "Creating new herdr workspace for $REPO_NAME" >&2
  CREATE_JSON="$(herdr workspace create --cwd "$TARGET" --label "$LABEL" --focus)"
  TOP_PANE="$(echo "$CREATE_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin)['result']['root_pane']['pane_id'])")"

  # Split off the top status bar first. NOTE: --ratio is the fraction kept by
  # the pane being split (the *original* pane), not the new one — so ratio
  # 0.12 here means TOP_PANE keeps 12% of the height and the new BOTTOM_PANE
  # gets the remaining 88%. (Verified empirically against an earlier version
  # of this script that split --direction right.)
  BOTTOM_PANE="$(herdr pane split "$TOP_PANE" --direction down --ratio 0.12 --no-focus | python3 -c "import json,sys; print(json.load(sys.stdin)['result']['pane']['pane_id'])")"

  # Split the bottom region into Claude (left, wider) and the docs viewer
  # (right, narrower) using the same original-pane-keeps-the-ratio rule.
  DOCS_PANE="$(herdr pane split "$BOTTOM_PANE" --direction right --ratio 0.65 --no-focus | python3 -c "import json,sys; print(json.load(sys.stdin)['result']['pane']['pane_id'])")"

  # Top: live state dashboard — full width, thin. It's just info (title,
  # status, updated-at), doesn't need a full pane's worth of screen.
  herdr pane run "$TOP_PANE" "source '$VENV_ACTIVATE' && export GANTRY_TARGET='$TARGET' && cd '$TARGET' && gantry watch --live"

  # Bottom-left: live Claude Code session, dropped straight into an
  # interactive chat cwd'd into the target repo with permissions bypassed —
  # this IS the assistant driving gantry runs. Venv activated + GANTRY_TARGET
  # exported first so any `gantry ...` shell-outs Claude runs actually resolve
  # the binary and default to the right repo.
  herdr pane run "$BOTTOM_PANE" "source '$VENV_ACTIVATE' && export GANTRY_TARGET='$TARGET' && cd '$TARGET' && claude --dangerously-skip-permissions"

  # Bottom-right: interactive docs viewer. --pick lets you fuzzy-choose a run
  # then a doc for it (Esc to go back a level, Esc again to return to the run
  # list); each doc waits on Enter before returning to the doc list, so it
  # behaves like a real nav stack rather than a one-shot dump.
  herdr pane run "$DOCS_PANE" "source '$VENV_ACTIVATE' && export GANTRY_TARGET='$TARGET' && cd '$TARGET' && gantry docs --pick"
fi

echo "Attaching to herdr..." >&2
exec herdr
