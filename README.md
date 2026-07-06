# Gantry

**A project-agnostic, staged, autonomous build pipeline for coding agents.**

Gantry wraps a spec → design → plan → build → evidence → review pipeline into a
single CLI. You point it at any git repo, describe a task, and it drives a coding
agent (Claude Code or Cursor) through the stages — pausing for human review where
it matters, running deterministic guardrails, and getting an independent LLM review
before anything ships.

The engine hardcodes no project, model, or tool. Everything specific to a repo
lives in that repo's `gantry.toml`. Agent tools are pluggable adapters.

```
spec ──▶ design ──▶ plan ──▶ build ──▶ evidence ──▶ review
 │gate     │gate            │          │            │
 human     human         (agent)    (agent)   (independent LLM)
```

## Install

```bash
pip install -e .        # from a clone; publishes as `gantry-cli`, binary `gantry`
```

Requires Python ≥ 3.11 and at least one agent runner on PATH (`claude` or
`cursor-agent`). Check with `gantry doctor`.

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

**Runners (pluggable).** `claude-code` and `cursor-cli` ship in v1. They share a
near-identical command surface; the adapter is a flag-mapping table plus shared
JSON-result parsing. Add one by subclassing `AgentRunner`.

**Guardrails (layered, deterministic).**
- *Scope guard* (built-in): forbidden path globs + optional plan-scope enforcement
  (flags files changed outside the plan's stated "Allowed files").
- *Repo checks* (delegated): Gantry runs the commands you list in `[checks]`
  (`npm run lint`, `go vet`, `ruff`, …) and gates on exit code. Your repo owns its
  own house rules — no rule logic is duplicated inside Gantry.
- Semantic/architectural judgment is left to the LLM review stage, not regex.

**Skills (scoped mandate).** Enable agent skill libraries (e.g. `superpowers`) in
`[skills]`; Gantry injects a directive into the **build/evidence** stages only —
never spec/design/plan — telling the agent to use them for execution discipline
without restarting planning. Install per-runner with `gantry init --with-skills`.

**State.** Gantry is stateless. Everything about a run lives in the target repo
under `.agent-runs/<run_id>/` (artifacts, logs, `state.json`, sessions), so runs
survive across invocations and machines.

**Git isolation.** Each run gets its own worktree at
`.worktrees/gantry/<run_id>` on a fresh branch `gantry/<run_id>` off
`[git].base_branch` — agent stages, checks, and review all execute there, never
in the main checkout. `.agent-runs/` is symlinked into the worktree so stage
prompts see it at the expected relative path. `gantry ship --run ID` commits,
pushes, and opens a PR (via `gh`) once a run reaches `review_approved`; it never
fires automatically. Worktrees are cleaned up the same way any other
`.worktrees/`-based branch is in this convention (e.g. a merged-branch prune
cron) — Gantry does not delete them itself.

## Recommended cockpit: herdr

Gantry drives one task through quality stages; [herdr](https://herdr.dev) — a
terminal-native agent multiplexer — is the recommended way to *watch* your fleet
while it does. Run Gantry inside a herdr pane and you get a real terminal per
run, detach/reattach over SSH (even from your phone), and a sidebar that rolls
each pane up to blocked / working / done.

The integration is **optional and auto-detected** — Gantry works identically with
no herdr present (CI, cron, Docker, headless), and lights up extra behavior only
when it detects `HERDR_ENV=1`:

- **Semantic stage in the sidebar.** Gantry reports its pipeline status
  (`evidence_running`, `review_approved`, …) to herdr via `pane report-agent`, so
  the sidebar shows *which stage* a run is in, not just working/done.
- **Event-driven advance.** When inside herdr, Gantry can `herdr wait` on a pane
  reaching `done` instead of polling.

`gantry doctor` reports whether herdr is installed and active. Configure under
`[herdr]` in `gantry.toml` (both flags default on; harmless when herdr is absent).

> Note: `gantry watch` (Gantry's own dashboard) shows *pipeline-stage* state
> across runs; herdr shows the *live terminals*. They complement each other.

### One-command dashboard: `gantry-herdr`

`scripts/gantry-herdr.sh` opens a herdr workspace pre-wired for a target repo in
one command — no manual pane setup. Install once:

```bash
ln -s ~/gantry/scripts/gantry-herdr.sh ~/.local/bin/gantry-herdr
chmod +x ~/.local/bin/gantry-herdr
```

Then:

```bash
gantry-herdr ~/some-repo   # defaults to ~/edupaid if omitted
```

Opens (or refocuses, if one already exists for that repo) a workspace with two
panes: **left** = a live `claude --dangerously-skip-permissions` session cwd'd
into the repo — this is your assistant for driving Gantry runs, with the
`gantry-pipeline` Claude Code skill (see below) available; **right** =
`gantry watch --live`, the auto-refreshing run-state dashboard. Reuses an
existing workspace by label instead of spawning duplicates on repeat runs.

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
| `[agent]` | runner (`claude-code` / `cursor-cli`), skip-permissions |
| `[models.<stage>]` | per-stage model + max_turns |
| `[review]` | reviewer runner/model + verdict keywords |
| `[scope]` | forbidden path globs, plan-scope enforcement |
| `[checks]` | your repo's own check commands |
| `[git]` | diff base branch |
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
gantry status [--run ID]                run state (json)
gantry watch [--live]                   dashboard of all runs
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
