# Gantry

**A project-agnostic, staged, autonomous build pipeline for coding agents.**

Gantry wraps a spec → design → plan → build → evidence → review pipeline into a
single CLI. You point it at any git repo, describe a task, and it drives a coding
agent (Claude Code, Cursor, or Codex) through the stages — pausing for human review
where it matters, running deterministic guardrails, and getting an independent LLM
review before anything ships.

The engine hardcodes no project, model, or tool. Everything specific to a repo
lives in that repo's `gantry.toml`. Agent tools are pluggable adapters.

```
spec ──▶ design ──▶ plan ──▶ build ──▶ evidence ──▶ review
 │gate     │gate            │          │            │
 human     human         (agent)    (agent)   (independent LLM)
```

## Install

```bash
git clone https://github.com/vanshaj-ti/gantry.git ~/gantry
cd ~/gantry
python3 -m venv .venv
source .venv/bin/activate
pip install -e .        # publishes as `gantry-cli`, binary `gantry`
```

The `gantry` binary now lives in `~/gantry/.venv/bin` — on PATH only inside
that activated venv. For `gantry` to work in every new shell without manually
activating first, add the venv's `bin/` to your shell profile once:

```bash
echo 'export PATH="$HOME/gantry/.venv/bin:$PATH"' >> ~/.zshrc   # or ~/.bashrc
source ~/.zshrc
gantry --version   # should now work in a fresh shell
```

Requires Python ≥ 3.11 and at least one agent runner CLI on PATH: `claude`
(Claude Code), `cursor-agent` (Cursor), or `codex` (Codex). Check with
`gantry doctor`.

**Other tools used by specific commands** (all optional — Gantry degrades
gracefully without them):

| Tool | Used by |
|---|---|
| `gh` | `gantry ship` (opens the PR) |
| `tmux` | `gantry cockpit` (the shipped-in-the-box workspace, see below) |
| `fzf` | `gantry docs --pick` (interactive picker; falls back to non-interactive without it) |
| `glow` | `gantry docs` (pretty-prints markdown; falls back to plain `print` without it) |
| `herdr` | optional enhanced cockpit alternative, see below |

## Environment variables

| Variable | Used for | Required? |
|---|---|---|
| `GANTRY_TARGET` | Which repo Gantry operates on. Falls back to the current working directory if unset. | No |
| `GANTRY_TELEGRAM_BOT_TOKEN` / `GANTRY_TELEGRAM_CHAT_ID` | `[notify] backend = "telegram"` — sending/receiving pipeline notifications via a Telegram bot. | Only if using the `telegram` notify backend |

```bash
export GANTRY_TARGET=~/my-project
gantry doctor
```

## Quickstart

```bash
cd /path/to/your/repo
gantry init                      # scaffold gantry.toml + .gantry/prompts/
gantry doctor                    # verify runners, git, config
gantry run --title "add health endpoint" --request "add GET /health -> 200"

# doc-stage gates (human):
gantry approve --run <id> --stage spec
gantry approve --run <id> --stage design

# agent stages (or let `gantry advance --all` drive them):
gantry stage plan --run <id>
gantry stage build --run <id>
gantry checks --run <id>         # scope guard + your repo's lint/build
gantry stage evidence --run <id>
gantry review --run <id>         # independent LLM verdict

gantry watch                     # dashboard of all runs
```

## How it works

**Stages.** Doc stages (`spec`, `design`) produce a markdown artifact and pause at
a human-review gate — advance with `gantry approve`, send back with
`gantry revise`. Agent stages (`plan`, `build`, `evidence`) invoke the configured
runner with the stage's prompt. The `review` stage feeds the diff + artifacts to an
independent LLM and parses an `APPROVE` / `REQUEST_CHANGES` / `ESCALATE` verdict.

**Runners (pluggable).** `claude-code`, `cursor-cli`, and `codex-cli` ship in v1.
Pick a runner globally via `[agent] runner = "..."`, or override per-stage via
`[models.<stage>] runner = "..."` — any of the three can drive any stage,
including an independent runner for `[review]` (e.g. build with `claude-code`,
review with `codex-cli`, for a genuinely independent second opinion). Add a new
runner by subclassing `AgentRunner`.

**Guardrails (layered, deterministic).**
- *Scope guard* (built-in): forbidden path globs + optional plan-scope enforcement
  (flags files changed outside the plan's stated "Allowed files").
- *Repo checks* (delegated): Gantry runs the commands you list in `[checks]`
  (`npm run lint`, `go vet`, `ruff`, …) and gates on exit code. Your repo owns its
  own house rules — no rule logic is duplicated inside Gantry.
- Semantic/architectural judgment is left to the LLM review stage, not regex.

**Auto-retry on checks failure.** A scope violation or a failing check doesn't
park the run forever — `advance_run` writes the concrete failure (the failing
command, or the specific out-of-scope files) as feedback and resumes the build
stage with it, up to `[checks].retry_checks` times (default 3). Exhausting
retries moves the run to `checks_escalated` and notifies a human with the same
concrete detail, instead of silently looping or dying.

**Auto-ship (opt-in).** By default, reaching `review_approved` still requires
an explicit `gantry ship --run ID` — a human decides when a real PR gets
opened. Set `[git].auto_ship = true` to have `advance_run` ship automatically
the moment review approves, closing the loop from doc-approval all the way to
an opened PR with zero human touch. A failed push/PR-create sets
`ship_failed` rather than retrying (almost always an auth/network issue, not
something to blindly resend).

**Skills (scoped mandate).** Enable agent skill libraries (e.g. `superpowers`) in
`[skills]`; Gantry injects a directive into the **build/evidence** stages only —
never spec/design/plan — telling the agent to use them for execution discipline
without restarting planning. Install per-runner with `gantry init --with-skills`.

**State.** Gantry is stateless. Everything about a run lives in the target repo
under `.agent-runs/<run_id>/` (artifacts, logs, `state.json`, sessions), so runs
survive across invocations and machines.

**Git isolation.** Each run gets its own worktree at
`.worktrees/gantry/<run_id>` on a local branch `gantry/<run_id>` off
`[git].base_branch` — agent stages, checks, and review all execute there, never
in the main checkout. `.agent-runs/` is symlinked into the worktree so stage
prompts see it at the expected relative path. `gantry ship --run ID` commits,
pushes, and opens a PR (via `gh`) once a run reaches `review_approved`; it never
fires automatically. The pushed/PR branch is NOT `gantry/<run_id>` — ship drafts
a real title, body, and short branch slug (e.g. `chore/remove-dead-webhook`)
from the run's own artifacts (spec, build summary, evidence) so the PR reads
like normal engineering work, with no mention of the pipeline that produced it.
The `gantry/<run_id>` name stays local-only, for worktree bookkeeping. Worktrees
are cleaned up the same way any other `.worktrees/`-based branch is in this
convention (e.g. a merged-branch prune cron) — Gantry does not delete them
itself.

## Cockpit: `gantry cockpit`

`gantry cockpit` opens a tmux workspace pre-wired for the target repo — no
manual pane setup, no extra tool to install beyond tmux (which most
developers already have). Ships with Gantry, works out of the box:

```bash
gantry cockpit                    # uses $GANTRY_TARGET, or run from inside the repo
```

```
+----------------------------------------------------------+
|  status bar (gantry watch --live) — full width, thin      |
+-----------------------+------------------------------------+
|                       |                                    |
|  doc viewer           |  claude session (larger)            |
|  gantry docs --nav    |  claude --dangerously-skip-...      |
|                       |                                    |
+-----------------------+------------------------------------+
```

- **Status bar** (top): `gantry watch --live` — colorized table with `TITLE`,
  `STATUS`, `AGENT`/`MODEL`/`SESSION` (which runner/model/session id is
  driving a `*_running` stage, blank otherwise), `DETAIL` (retry progress for
  `blocked`/`checks_escalated` runs), `UPDATED`. Plain text labels, no emoji —
  color (green/yellow/red by outcome family) is the at-a-glance signal.
- **Doc viewer** (bottom-left): `gantry docs --nav` — a persistent, full-screen
  arrow-key navigator (curses): run list → doc list → doc content.
  `↓`/`↑` (or mouse wheel) move/scroll, `→`/Enter drills in, `←`/Esc backs out
  one level (quits from the run list), `q` quits from anywhere. Auto-refreshes
  on a new run or doc appearing without resetting your current position.
  Every render is a clean full-screen redraw — no scroll-history leakage.
  Doc content is rendered through `glow` (run inside a pty so it emits its
  real 256-color output, not the flat bold-only text it downgrades to when
  piped — word-wrapped to the pane's actual width, headers/code/tables
  render in color like glow does in a normal terminal, not literal `**`/`#`
  characters) and falls back to plain unwrapped, unstyled text if `glow`
  isn't installed.
- **Claude session** (bottom-right, gets the larger share of the split): a
  live `claude --dangerously-skip-permissions` session cwd'd into the repo —
  your assistant for driving Gantry runs.

Mouse mode is enabled for the cockpit's tmux session only — click-drag pane
borders to resize, click to switch focus — without touching your global tmux
config.

Re-running `gantry cockpit` against the same repo reuses the existing tmux
session (named `gantry-<repo-name>`) instead of spawning a duplicate —
`tmux attach` picks up right where you left it.

`gantry doctor` reports whether `tmux` is available.

### Optional enhanced integration: herdr

[herdr](https://herdr.dev) — a terminal-native agent multiplexer — is an
**optional** alternative to `gantry cockpit`'s tmux workspace, for anyone who
already has it: detach/reattach over SSH (even from your phone), and a
sidebar that rolls each pane up to blocked / working / done.

The integration is **auto-detected** — Gantry works identically with no herdr
present (CI, cron, Docker, headless, or just using `gantry cockpit` instead),
and lights up extra behavior only when it detects `HERDR_ENV=1`:

- **Semantic stage in the sidebar.** Gantry reports its pipeline status
  (`evidence_running`, `review_approved`, …) to herdr via `pane report-agent`, so
  the sidebar shows *which stage* a run is in, not just working/done.
- **Event-driven advance.** When inside herdr, Gantry can `herdr wait` on a pane
  reaching `done` instead of polling.

Configure under `[herdr]` in `gantry.toml` (both flags default on; harmless
when herdr is absent). `scripts/gantry-herdr.sh` opens a herdr-based workspace
the same way `gantry cockpit` opens a tmux one, for anyone who prefers it:

```bash
ln -s /path/to/gantry/scripts/gantry-herdr.sh ~/.local/bin/gantry-herdr
export GANTRY_TARGET=~/some-repo
gantry-herdr
```

> Note: `gantry watch` (Gantry's own dashboard) shows *pipeline-stage* state
> across runs; herdr shows the *live terminals*. They complement each other.

### Claude Code skill: `gantry-pipeline`

`claude-skills/gantry-pipeline/` teaches Claude Code the Gantry CLI surface
(create/plan/build/checks/evidence/review/ship/advance/watch), the worktree
isolation model, and recovery patterns for blocked/stuck runs. It's a **global**
Claude Code skill — install once, works from any project:

```bash
ln -s ~/gantry/claude-skills/gantry-pipeline ~/.claude/skills/gantry-pipeline
```

It auto-triggers on Gantry-related requests (no slash command needed) as long
as the prompt mentions "gantry"/"pipeline run"/"stage"/etc. — matching the
skill's `description` frontmatter. Update `claude-skills/gantry-pipeline/` in
this repo when the CLI surface changes; the symlink keeps `~/.claude/skills/`
in sync automatically.


**Auto-advance.** `gantry advance --all` ticks every run once, firing the next
non-gated stage (build → checks → evidence → review, and re-build on
REQUEST_CHANGES). Run it on a 1-minute cron for hands-off progression; it notifies
via the configured backend on each state change.

## Configuration (`gantry.toml`)

Generated by `gantry init`. Key sections:

| Section | Purpose |
|---|---|
| `stages` | which stages run, in order |
| `[agent]` | runner (`claude-code` / `cursor-cli` / `codex-cli`), skip-permissions |
| `[models.<stage>]` | per-stage model + max_turns |
| `[review]` | reviewer runner/model + verdict keywords |
| `[scope]` | forbidden path globs, plan-scope enforcement |
| `[checks]` | your repo's own check commands, `retry_checks` (auto-retry cap) |
| `[git]` | diff base branch, `auto_ship` (ship automatically on review_approved) |
| `[notify]` | `none` / `telegram` / `webhook` |
| `[skills]` | mandated skill libraries + per-runner installers |
| `[mcp]` | MCP servers per stage (codebase-memory, chrome-devtools) |
| `[herdr]` | optional herdr sidebar integration (auto-detected) |

## CLI reference

```
gantry init [--force] [--with-skills]   scaffold config + prompts (+ install skills)
gantry run --title T --request R        create a run, start the pipeline
gantry stage {plan|build|evidence} --run ID [--resume]
gantry checks --run ID                  scope guard + repo checks
gantry review --run ID                  independent LLM review
gantry approve --run ID --stage S       pass a human-review gate, advance
gantry revise --run ID --stage S "…"    send a stage back with comments
gantry ship --run ID                    commit + push + open a PR (review_approved only)
gantry advance [--run ID | --all]       drive the pipeline forward one tick
gantry loop [--run ID] [--interval S] [--max-ticks N]
                                         repeatedly tick in-process (foreground
                                         alternative to an external cron)
gantry status [--run ID]                run state (json)
gantry watch [--live]                   dashboard of all runs
gantry docs [--run ID] [--pick] [--doc D] [--follow]
                                         render a run's stage docs (default: most recent run;
                                         via glow if installed)
gantry listen [--run ID]                poll Telegram replies, act on the pending run
gantry mcp [--list]                     register/list MCP servers for the active runner
gantry cockpit                          open a tmux workspace pre-wired for this repo
gantry daemon {install|uninstall|status} [--interval S]
                                         24/7 auto-advance background job (launchd/systemd)
gantry doctor                           environment / config health
```

The target repo is `$GANTRY_TARGET` or the current working directory.

## Design notes

- **Runner-agnostic core.** The engine never names `claude`, `cursor`, a model, or
  a project. Swapping runners is a config line.
- **Determinism where it counts.** Guardrails are deterministic (globs + exit
  codes); only the review stage uses model judgment. This keeps false-approvals
  from a chatty reviewer out of the gating path.
- **The repo owns its rules.** House rules live in the repo's own linters, invoked
  via `[checks]`. Gantry doesn't re-encode them, so they never drift.
