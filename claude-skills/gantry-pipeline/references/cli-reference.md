# Gantry CLI Reference

All commands require `GANTRY_TARGET` set (or run from inside the target repo).

## gantry doctor
Checks environment: runner CLI present, git repo valid, config present, herdr/mcp status.
Run this first if anything seems broken.

## gantry watch [--live]
Dashboard of all runs: RUN ID | STATUS | UPDATED | TITLE. `--live` refreshes every 2s.
UPDATED shows relative staleness ("3d ago") — treat anything stuck for hours on a
`*_running` status as suspicious, not normal.

## gantry run --title "TITLE" --request "REQUEST" [--run RUN_ID]
Creates a new run. Auto-generates a run_id from timestamp+slugified title unless
--run is given. Fails loudly (ValueError) if the configured stages[0] is "spec" or
"design" — those have no execution path yet. First real stage is normally "plan".

## gantry stage {plan,build,evidence} --run RUN [--resume]
Runs one agent stage manually. Executes inside the run's isolated git worktree
(`.worktrees/gantry/<run_id>`), never against the main checkout. Use --resume to
continue a stage's existing Claude session instead of starting fresh.

## gantry checks --run RUN
Runs scope guard (did the agent only touch files within the declared plan) + repo
checks (`[checks].commands` from gantry.toml, e.g. lint/build/test). This is also
the RECOVERY command after a `blocked` status — fixing the underlying issue and
re-running `gantry checks --run RUN` clears the block if it now passes.

## gantry review --run RUN
Independent LLM review of the diff + artifacts. Verdict: review_approved,
review_changes_requested, or review_escalated (needs human judgment call).

## gantry approve --run RUN --stage STAGE
Passes a human-review gate (used for doc-stage artifacts, not needed in the
default plan/build/evidence/review pipeline).

## gantry revise --run RUN --stage STAGE "comments"
Sends a stage back with review comments for rework.

## gantry ship --run RUN [--force]
Commits all changes in the run's worktree, pushes the branch, opens a GitHub PR
via `gh pr create`. REFUSES to run unless status is review_approved (bypass with
--force only on explicit human instruction). NEVER call this automatically as
part of routine advancement — shipping is a deliberate, explicit, human-gated
action. Always tell the user before shipping and confirm they want a PR opened.

## gantry status [--run RUN]
Shows one run's full state, or lists all runs if --run omitted.

## gantry advance [--run RUN] [--all]
Auto-advances one or all runs by one tick each: plan_complete->build,
build_complete->checks->evidence, evidence_complete->review,
review_changes_requested->resume build. Does NOT auto-advance human-gated states
(awaiting_*, review_escalated, blocked) or auto-ship. This is the same command
the cron uses — running it manually just does an extra tick immediately.

IMPORTANT: build/evidence stages routinely take 1-15+ minutes (they invoke a real
Claude Code subprocess). Never run `gantry advance` expecting instant completion —
run it, then check `gantry status --run RUN` after a wait, or poll `gantry watch`.
