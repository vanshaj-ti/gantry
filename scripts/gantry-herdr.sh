#!/usr/bin/env bash
# gantry-herdr — open a herdr dashboard workspace pre-wired for a Gantry
# target repo, three panes:
#
#   +----------------------------------------------------------+
#   |  gantry watch --live  (full width, thin — status is just |
#   |  info: title/status/updated-at, doesn't need real estate) |
#   +---------------------------------+--------------------------+
#   |                                 |  gantry docs --pick       |
#   |  agent TUI (claude|codex|       |  (interactive: choose a   |
#   |  cursor-agent from              |  run, then a doc, Esc to  |
#   |  [agent].runner)                |  go back a level)         |
#   +---------------------------------+--------------------------+
#
# Reuses the workspace if one for this repo is already open instead of
# spawning duplicates.
#
# Usage: gantry-herdr [target-repo-path]
#   Target defaults to $GANTRY_TARGET if set; otherwise it's required.
set -euo pipefail

GANTRY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_ACTIVATE="$GANTRY_ROOT/.venv/bin/activate"

TARGET="${1:-${GANTRY_TARGET:-}}"
if [ -z "$TARGET" ]; then
  echo "Usage: gantry-herdr <target-repo-path>" >&2
  echo "  (or export GANTRY_TARGET and omit the argument)" >&2
  exit 1
fi
TARGET="$(cd "$TARGET" && pwd)"   # normalize to absolute path
REPO_NAME="$(basename "$TARGET")"
LABEL="gantry: $REPO_NAME"

if [ ! -f "$TARGET/gantry.toml" ]; then
  echo "warning: $TARGET/gantry.toml not found — 'gantry doctor' will show config_present=false" >&2
fi

# Resolve the interactive agent command from [agent].runner (same policy as
# gantry cockpit). Falls back to claude with skip-permissions if config/python
# is unavailable so the pane still opens something useful.
resolve_agent_cmd() {
  # Prefer the installed gantry package's runners module (venv first).
  local py_snippet
  py_snippet='
import shlex, sys
from pathlib import Path
try:
    from gantry.config import load_config
    from gantry.runners import interactive_command
    cfg = load_config(Path(sys.argv[1]))
    argv = interactive_command(cfg.agent.runner, skip_permissions=cfg.agent.skip_permissions)
except Exception:
    argv = ["claude", "--dangerously-skip-permissions"]
print(" ".join(shlex.quote(a) for a in argv))
'
  if [ -f "$VENV_ACTIVATE" ]; then
    # shellcheck disable=SC1090
    source "$VENV_ACTIVATE"
  fi
  if command -v python3 >/dev/null 2>&1; then
    python3 -c "$py_snippet" "$TARGET" 2>/dev/null && return 0
  fi
  printf '%s\n' "claude --dangerously-skip-permissions"
}

AGENT_CMD="$(resolve_agent_cmd)"
AGENT_PANE_SHELL="source '$VENV_ACTIVATE' && export GANTRY_TARGET='$TARGET' && cd '$TARGET' && $AGENT_CMD"

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

  # The three panes we set up earlier may have had their commands exit
  # (crash, Ctrl+C, `exit`, etc.) since this workspace was created — in
  # which case focusing it just shows three bare shells. Detect which of
  # the three roles (status/agent/docs) are sitting at a bare shell and
  # re-launch only those, identified by pane geometry (top / bottom-left /
  # bottom-right) rather than assuming pane IDs are stable across runs.
  ROLE_PANES="$(python3 -c "
import json, subprocess, sys

ws = '$EXISTING_WS'
panes = json.loads(subprocess.run(['herdr', 'pane', 'list', '--workspace', ws],
                                   capture_output=True, text=True).stdout)['result']['panes']

# Group panes by tab, then find the tab that has exactly our 3-pane layout
# (the workspace may have extra user-created tabs we shouldn't touch).
by_tab = {}
for p in panes:
    by_tab.setdefault(p['tab_id'], []).append(p)
tab_id, tab_panes = next(((t, ps) for t, ps in by_tab.items() if len(ps) == 3), (None, None))
if not tab_panes:
    sys.exit(0)

layout = json.loads(subprocess.run(['herdr', 'pane', 'layout', '--pane', tab_panes[0]['pane_id']],
                                    capture_output=True, text=True).stdout)['result']['layout']
rects = {p['pane_id']: p['rect'] for p in layout['panes']}

top_id = min(rects, key=lambda pid: rects[pid]['y'])
bottom = [pid for pid in rects if pid != top_id]
left_id, right_id = sorted(bottom, key=lambda pid: rects[pid]['x'])

for role, pid in (('top', top_id), ('agent', left_id), ('docs', right_id)):
    info = json.loads(subprocess.run(['herdr', 'pane', 'process-info', '--pane', pid],
                                      capture_output=True, text=True).stdout)
    fg = info['result']['process_info']['foreground_processes']
    is_bare_shell = len(fg) == 1 and fg[0]['name'] in ('zsh', 'bash', 'sh')
    print(f'{role} {pid} {1 if is_bare_shell else 0}')
" 2>/dev/null || true)"

  while read -r ROLE PANE_ID DEAD; do
    [ -z "$ROLE" ] && continue
    [ "$DEAD" = "1" ] || continue
    case "$ROLE" in
      top)
        echo "Re-launching gantry watch in $PANE_ID (previous process had exited)" >&2
        herdr pane run "$PANE_ID" "source '$VENV_ACTIVATE' && export GANTRY_TARGET='$TARGET' && cd '$TARGET' && gantry watch --live"
        ;;
      agent)
        echo "Re-launching agent ($AGENT_CMD) in $PANE_ID (previous process had exited)" >&2
        herdr pane run "$PANE_ID" "$AGENT_PANE_SHELL"
        ;;
      docs)
        echo "Re-launching gantry docs --pick in $PANE_ID (previous process had exited)" >&2
        herdr pane run "$PANE_ID" "source '$VENV_ACTIVATE' && export GANTRY_TARGET='$TARGET' && cd '$TARGET' && gantry docs --pick"
        ;;
    esac
  done <<< "$ROLE_PANES"
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

  # Split the bottom region into agent (left, wider) and the docs viewer
  # (right, narrower) using the same original-pane-keeps-the-ratio rule.
  DOCS_PANE="$(herdr pane split "$BOTTOM_PANE" --direction right --ratio 0.65 --no-focus | python3 -c "import json,sys; print(json.load(sys.stdin)['result']['pane']['pane_id'])")"

  # Top: live state dashboard — full width, thin. It's just info (title,
  # status, updated-at), doesn't need a full pane's worth of screen.
  herdr pane run "$TOP_PANE" "source '$VENV_ACTIVATE' && export GANTRY_TARGET='$TARGET' && cd '$TARGET' && gantry watch --live"

  # Bottom-left: live agent session for [agent].runner, cwd'd into the target
  # repo with permissions bypassed per skip_permissions — this IS the
  # assistant driving gantry runs. Venv activated + GANTRY_TARGET exported
  # first so any `gantry ...` shell-outs resolve the binary and default to
  # the right repo.
  herdr pane run "$BOTTOM_PANE" "$AGENT_PANE_SHELL"

  # Bottom-right: interactive docs viewer. --pick lets you fuzzy-choose a run
  # then a doc for it (Esc to go back a level, Esc again to return to the run
  # list); each doc waits on Enter before returning to the doc list, so it
  # behaves like a real nav stack rather than a one-shot dump.
  herdr pane run "$DOCS_PANE" "source '$VENV_ACTIVATE' && export GANTRY_TARGET='$TARGET' && cd '$TARGET' && gantry docs --pick"
fi

echo "Attaching to herdr..." >&2
exec herdr
