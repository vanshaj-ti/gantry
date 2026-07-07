#!/usr/bin/env bash
# gantry-herdr — open a herdr dashboard workspace pre-wired for a Gantry
# target repo: left pane = live interactive `claude --dangerously-skip-
# permissions` session cwd'd into the repo (your assistant for driving
# gantry runs, with the global gantry-pipeline skill available), right
# pane = live `gantry watch --live` state dashboard. Reuses the workspace
# if one for this repo is already open instead of spawning duplicates.
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
  ROOT_PANE="$(echo "$CREATE_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin)['result']['root_pane']['pane_id'])")"

  # Right pane: live state dashboard. NOTE: --ratio is the fraction given to
  # the pane being split (ROOT_PANE / left / Claude), not the new pane — so
  # 0.7 here means Claude gets 70% of the width and the dashboard gets the
  # remaining 30%. (Verified empirically: split ratio=0.2 on a pane left it
  # at 20% width with the new pane taking the other 80%.)
  RIGHT_PANE="$(herdr pane split "$ROOT_PANE" --direction right --ratio 0.7 --no-focus | python3 -c "import json,sys; print(json.load(sys.stdin)['result']['pane']['pane_id'])")"

  # Left pane (root): live Claude Code session, dropped straight into an
  # interactive chat cwd'd into the target repo with permissions bypassed —
  # this IS the assistant driving gantry runs. Venv activated + GANTRY_TARGET
  # exported first so any `gantry ...` shell-outs Claude runs actually resolve
  # the binary and default to the right repo.
  herdr pane run "$ROOT_PANE" "source '$VENV_ACTIVATE' && export GANTRY_TARGET='$TARGET' && cd '$TARGET' && claude --dangerously-skip-permissions"

  # Right pane: live dashboard.
  herdr pane run "$RIGHT_PANE" "source '$VENV_ACTIVATE' && export GANTRY_TARGET='$TARGET' && cd '$TARGET' && gantry watch --live"
fi

echo "Attaching to herdr..." >&2
exec herdr
