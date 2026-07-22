# Gantry

**Point it at a repo, describe a task, get a reviewed pull request — with as much or as little human involvement as you want.**

Gantry drives a coding agent (Claude Code, Cursor, or Codex) through a staged pipeline — spec, design, plan, build, checks, evidence, review, ship — in an isolated git worktree, with deterministic guardrails and an independent LLM review before anything reaches a PR. Nothing is hardcoded: the agent, the model, the repo's own lint/test commands, and how much autonomy you allow all live in one config file per project.

```
spec ──▶ design ──▶ plan ──▶ build ──▶ checks ──▶ evidence ──▶ review ──▶ ship
 │gate     │gate            │          │                        │          │
 human     human         (agent)   (deterministic)          (2 axes,   (PR + auto
                                                              LLM)      -CI-safe)
```

- **Spec/design** are human-gated by default — read them, approve or send back with feedback.
- **Plan/build/evidence** are agent-driven, working inside a disposable worktree that never touches your checkout.
- **Checks** are yours: Gantry runs your repo's own lint/test/build commands and gates on exit code — no rule logic duplicated inside Gantry.
- **Review** is a genuinely independent two-axis investigation (does it do what was asked? does it follow this repo's conventions?) — never a rubber stamp on the agent's own work.
- **Ship** re-verifies everything one more time before pushing, drafts a PR that reads like normal engineering work, and attaches a rollback plan.

Every gate is a config flag. Leave them all off and you get a supervised pipeline you approve step by step. Turn them on and a request goes from `gantry run --request "..."` straight to an open PR with nobody watching.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/vanshaj-ti/gantry/main/install.sh | bash
```

One line, macOS/Linux. Clones to `~/gantry`, sets up a venv, puts `gantry` on your PATH, and installs the tools the pipeline actually needs (`gh`, `tmux`, `fzf`, `glow`) if it can. Finishes by running `gantry doctor` so you see exactly what's ready.

<details>
<summary>Manual install</summary>

```bash
git clone https://github.com/vanshaj-ti/gantry.git ~/gantry
cd ~/gantry
python3 -m venv .venv
source .venv/bin/activate
pip install -e .        # publishes as `gantry-cli`, binary `gantry`
```

The `gantry` binary lives in `~/gantry/.venv/bin` — only on PATH inside that activated venv. To make it available in every new shell:

```bash
echo 'export PATH="$HOME/gantry/.venv/bin:$PATH"' >> ~/.zshrc   # or ~/.bashrc
source ~/.zshrc
gantry --version
```

</details>

**Requirements:** Python ≥ 3.11, and at least one agent runner CLI on PATH — `claude` (Claude Code), `cursor-agent` (Cursor), or `codex` (Codex). Run `gantry doctor` any time to check.

**Already have some tools, added another one later?** `gantry doctor --fix` notices a runner CLI that showed up on your PATH after the fact and offers to register it in `gantry.toml` — no need to hand-edit config just because you installed Cursor last week.

## Quickstart

```bash
cd /path/to/your/repo
gantry setup                     # scaffold config, build the Docker sandbox, start it ticking
gantry run --title "add health endpoint" --request "add GET /health -> 200"
gantry watch                     # live dashboard of every run, including running cost
```

That's it. The container drives the run through every stage on its own, pausing only where a human gate exists (skippable — see [Autonomy dials](#autonomy-dials) below).

<details>
<summary>No Docker? Run directly on the host</summary>

```bash
cd /path/to/your/repo
gantry init                      # scaffold gantry.toml + .gantry/prompts/
gantry doctor                    # verify runners, git, config
gantry run --title "add health endpoint" --request "add GET /health -> 200"

# human doc gates (only if "spec"/"design" are in [stages]):
gantry approve --run <id> --stage spec
gantry approve --run <id> --stage design

# agent stages — or just let `gantry advance --all` drive all of this:
gantry stage plan --run <id>
gantry stage build --run <id>
gantry checks --run <id>
gantry stage evidence --run <id>
gantry review --run <id>
gantry ship --run <id>

gantry watch
```

</details>

## The pipeline, stage by stage

**Spec & design** *(human-gated, opt-in)* — the agent drafts what to build and why (`product-spec.md` + a structured `acceptance-criteria.json`), then how (`architecture-design.md` + a `decision-log.json` of the choices made and alternatives rejected). Both pause for `gantry approve`/`gantry revise` by default. Skip these two stages entirely if you'd rather write the spec directly into `--request`; add `"spec"`, `"design"` to `[stages]` to turn them on.

**Plan** — turns the spec/design into an ordered, independently-verifiable implementation plan: each step names its own check, not just one test plan at the end. Declares its file scope as structured `allowed-files.json`, not just prose — that's what the scope guard actually reads.

**Build** — executes the plan inside its worktree, one step at a time: implement, verify, commit *only that step's files*, move to the next. A `max_turns` cutoff or crash leaves clean, verified partial progress behind, not one big uncommitted blob.

**Checks** *(deterministic, yours)* — your repo's own lint/build/test commands, gated on exit code, plus Gantry's built-in scope guard (did the build touch only what the plan declared?). A flaky failure gets a bare re-run before anything escalates to an agent-involved retry; a file matching a project-declared "high-risk" glob (auth, migrations, whatever you name) forces a human gate regardless of every other autonomy flag.

**Evidence** — an independent integration-test pass, not a re-run of build's own unit tests. Maps each acceptance criterion to real proof, and tags *how* it was verified (test-run vs. inspection) so a claim of "confirmed" actually means something.

**Review** — two fully independent investigations, run in parallel, that must **both** approve: does the diff satisfy the spec (with no scope creep), and does it follow this repo's own conventions? A change can pass one and fail the other — a single blended verdict would hide that. Findings are severity-tagged and never silently dropped; an unclassified finding always defaults to "ask a human," never to "ignore it."

**Ship** — re-runs your checks one final time (state can go stale between review and ship — Gantry never trusts a result that's minutes old), drafts a title/body/branch that reads like a human wrote it, attaches a concrete rollback plan, and opens the PR. A conflict with the base branch gets resolved by understanding *intent* (what was this branch actually trying to do), not a blind `--ours`/`--theirs`.

## Autonomy dials

Every gate above is opt-in-to-skip, in `gantry.toml`. Turn on exactly as much unattended operation as you're comfortable with:

| Flag | Effect |
|---|---|
| `[git].auto_approve_docs` | spec/design approve themselves instead of waiting for a human |
| `[checks].auto_resolve` | a stuck checks failure spawns a dedicated fix-it agent instead of escalating to you (its fix is always re-verified for real, never trusted on its own word) |
| `[git].auto_ship` | the moment review approves, `ship_run` fires — no `gantry ship` call needed |
| `[git].auto_merge` | (needs `auto_ship`) squash-merges the PR Gantry just opened, too |

All four together: `gantry run --request "..."` goes to a *merged* PR with zero human touch. Any subset: you keep exactly the gates you want and automate the rest.

One thing autonomy can't skip: a file matching `[scope].high_risk_paths` always stops the run for a human, no matter what else is turned on.

## Configuration (`gantry.toml`)

One file per repo, scaffolded by `gantry init`/`gantry setup`. Nothing here is required — every field defaults to today's behavior until you touch it. Full reference with inline comments: `gantry/templates/gantry.toml`; authoritative types: `gantry/config.py`.

| Section | What it controls |
|---|---|
| `stages` | which stages run and in what order — leave a stage out and its side effects go too |
| `[agent]` | which runner drives the pipeline, headless auto-approve, concurrent-run cap |
| `[models.<stage>]` | model + turn budget per stage; `[models.resolve]` for the fix-it agent specifically |
| `[plan]` | context injected into the plan prompt, `"detailed"` vs `"brief"` depth |
| `[build]` | a pre-hook shell command (install deps, seed a DB) before the first build call |
| `[evidence]` | prose or structured (parseable) evidence output |
| `[review]` | reviewer model, two-axis toggle, checklists, verdict-keyword matching |
| `[scope]` | forbidden paths, high-risk paths, how strictly plan-scope is enforced |
| `[checks]` | your check commands, parallelism, retry/escalation limits, flaky-retry |
| `[e2e]` | a deterministic e2e pass between checks and evidence, per touched app |
| `[git]` | base branch, the four autonomy flags above, ship retry cap |
| `[notify]` | none / Telegram / webhook |
| `[skills]` | agent skill libraries mandated for build/evidence (e.g. `superpowers`) |
| `[mcp]` | MCP servers registered per stage (a code-intelligence server, Chrome DevTools, or your own) |
| `[proxy]` | org gateway/proxy overrides for claude-code and codex-cli |
| `[daemon]` | per-target time budget for the background auto-advance tick |
| `[herdr]` | optional [herdr](https://herdr.dev) sidebar integration, auto-detected |

## Running it hands-off

**One tick, on demand:**
```bash
gantry advance --all      # fire the next stage for every run that's ready
```

**A cron/systemd/launchd job that runs forever:**
```bash
gantry daemon install --interval 60
gantry daemon status       # shows install state + last-tick health
```
Self-healing: the OS relaunches it if it crashes, and Gantry itself notices and notifies if a tick goes silent for too long.

**Or inside its own Docker container**, isolated from your host machine entirely (installed by `gantry setup`, or drive it yourself):
```bash
gantry docker up --interval 60
gantry docker status
gantry docker down
```

## Watching runs

```bash
gantry watch [--live] [--tag T]      # dashboard: title, status, agent/model, cost, retry detail
gantry cost [--run ID]               # repo-wide total, or one run's per-stage breakdown
gantry docs --run ID [--nav]         # render a run's artifacts; --nav for a full-screen browser
```

**`gantry cockpit`** opens a ready-made tmux workspace — no manual pane setup:

```
+----------------------------------------------------------+
|  gantry watch --live — thin status bar, full width        |
+-----------------------+------------------------------------+
|  doc viewer            |  live claude session (larger)      |
|  gantry docs --nav     |  your assistant for driving runs    |
+-----------------------+------------------------------------+
```

```bash
gantry cockpit          # uses $GANTRY_TARGET, or cwd if inside the repo
```

Mouse-enabled (drag to resize panes, click to focus, scroll) without touching your global tmux config. Re-running against the same repo reattaches instead of spawning a duplicate session.

<details>
<summary>Optional: herdr integration</summary>

[herdr](https://herdr.dev) is a terminal-native agent multiplexer — an alternative to `gantry cockpit` for anyone who already uses it, with SSH-friendly detach/reattach and a working/blocked/done sidebar. Fully auto-detected (`HERDR_ENV=1`); zero effect when absent, so it's safe to leave `[herdr].enabled = true` on for headless/CI/cron runs. When active, Gantry reports its real pipeline stage (not just working/done) to the sidebar.

```bash
ln -s /path/to/gantry/scripts/gantry-herdr.sh ~/.local/bin/gantry-herdr
GANTRY_TARGET=~/some-repo gantry-herdr
```

</details>

<details>
<summary>Claude Code skill: gantry-pipeline</summary>

`claude-skills/gantry-pipeline/` teaches Claude Code the full CLI surface, the worktree isolation model, and how to recover a blocked/stuck run. Install once, globally:

```bash
ln -s ~/gantry/claude-skills/gantry-pipeline ~/.claude/skills/gantry-pipeline
```

Auto-triggers on Gantry-related requests — no slash command needed.

</details>

## Coordinating runs

- **`gantry run --depends-on ID,...`** queues a run behind others; it only starts once every dependency is *actually merged*, not merely approved or shipped — otherwise it'd build against code that isn't on the base branch yet. `gantry mark-merged --run ID` tells Gantry a dependency landed (skip this if `[git].auto_merge` did it for you).
- **`gantry run --tag NAME`** labels a run for filtering — `gantry watch --tag NAME`, `gantry advance --all --tag NAME` — with no effect on the run's own execution.
- **`gantry hold --run ID`** pauses a run so nothing (poller, auto-retry, auto-resolve) touches it while you work in the worktree by hand; `gantry resume --run ID` hands it back.
- **`[agent].max_concurrent`** lets independent runs' agent stages execute in parallel instead of one at a time — each run's own file lock still guarantees two overlapping ticks never double-process the same run.

## Design notes

- **Runner-agnostic core.** The engine never names a specific vendor, model, or project. Swapping runners, or running different stages on different runners, is one config line.
- **Determinism where it counts.** Guardrails (scope, checks) are globs and exit codes — only review and evidence use model judgment, and review's two axes are independent specifically so one can't paper over the other.
- **The repo owns its rules.** House style lives in your repo's own linters, invoked via `[checks]`. Gantry doesn't re-encode them, so they can't drift out of sync.
- **Stateless engine, stateful repo.** Everything about a run — artifacts, logs, session ids — lives under `.agent-runs/<run_id>/` in the target repo itself. Gantry holds nothing in memory between invocations; runs survive across machines and restarts.
- **Real git isolation.** Each run gets its own worktree and local branch, off your configured base branch — agent stages, checks, and review all execute there, never in your main checkout. Shipped PRs get a real branch name and description drawn from the run's own artifacts, with no trace of the pipeline that produced them.

## Environment variables

| Variable | Used for | Required? |
|---|---|---|
| `GANTRY_TARGET` | which repo Gantry operates on (falls back to cwd) | No |
| `GANTRY_TELEGRAM_BOT_TOKEN` / `GANTRY_TELEGRAM_CHAT_ID` | `[notify].backend = "telegram"` | Only with the telegram backend |

## Full CLI reference

```
gantry setup [--force] [--interval S]     one-command bring-up: init + docker build + docker up
gantry init [--force] [--with-skills]     scaffold config + prompts (+ install skills)
gantry run --title T --request R [--depends-on ID,...] [--tag T]
gantry stage {spec|design|plan|build|evidence} --run ID [--resume]
gantry retry {plan|build|evidence} --run ID   re-run a stage fresh, no resume/feedback
gantry checks --run ID                    scope guard + repo checks
gantry review --run ID                    independent two-axis LLM review
gantry approve --run ID --stage S         pass a human-review gate, advance
gantry revise --run ID --stage S "…"      send a stage back with comments
gantry ship --run ID [--force]            commit + push + open a PR
gantry mark-shipped --run ID [--force]     record a run shipped outside `gantry ship`
gantry mark-merged --run ID               record a shipped run's PR as actually merged
gantry hold --run ID                      pause a run so nothing auto-advances it
gantry resume --run ID                    un-pause a held run
gantry cancel --run ID [--force] [--cleanup]
gantry cleanup [--status S ...] [--older-than-days N] [--yes] [--purge-state]
gantry advance [--run ID | --all] [--tag T]   drive the pipeline forward one tick
gantry loop [--run ID] [--interval S] [--max-ticks N] [--tag T]
gantry status [--run ID]                  run state (json)
gantry watch [--live] [--tag T]           dashboard of all runs, incl. running cost
gantry cost [--run ID]                    repo-wide total, or one run's per-stage breakdown
gantry docs [--run ID] [--pick] [--doc D] [--follow] [--nav]
gantry listen [--run ID]                  poll Telegram replies, act on the pending run
gantry mcp [--list]                       register/list MCP servers for the active runner
gantry cockpit [--kill]                   open a tmux workspace pre-wired for this repo
gantry daemon {install|uninstall|status} [--interval S]
gantry docker {build|up|down|status} [--interval S]
gantry doctor [--fix] [--yes]             environment/config health; --fix registers new runners
gantry update                             git pull + reinstall this gantry checkout
```

`$GANTRY_TARGET`, or the current working directory, is always the target repo.
