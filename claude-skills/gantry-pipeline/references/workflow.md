# Standard Operating Loop

1. `gantry doctor` — confirm target repo, config, runner CLI are all healthy
   before starting anything new.
2. `gantry watch` — see current runs and their state before creating a new one.
   Avoid duplicating an in-flight run for the same task.
3. Create: `gantry run --title "..." --request "clear, specific description of
   the change"`. The request text IS the spec — there's no separate spec/design
   stage, so front-load all constraints and acceptance criteria here.
4. Drive forward with `gantry advance --run RUN` repeatedly (check status between
   calls), or `gantry stage <name> --run RUN` for one manual step at a time when
   the user wants to inspect each stage's output before proceeding.
5. If status becomes `blocked`: read `.agent-runs/RUN/checks.json` (or scope.json)
   to see what failed, explain it to the user in plain language, fix the root
   cause if it's a real code/config issue (not a gantry bug), then re-run
   `gantry checks --run RUN` to clear the block, then resume `gantry advance`.
6. If status becomes `review_escalated`: this needs a human judgment call.
   Summarize the reviewer's concern to the user; do not auto-resolve it yourself.
7. Once `review_approved`: tell the user the run is ready to ship. Do NOT run
   `gantry ship` without an explicit go-ahead for that specific run.

## Recovery & troubleshooting

- **Stuck on a `*_running` status for a long time (check `gantry watch`'s UPDATED
  column):** the underlying subprocess may have been killed (e.g. a shell timeout)
  without properly finishing. Check for a stale lockfile at
  `.agent-runs/RUN/.advance.lock` and remove it if the process is confirmed dead
  (no matching `claude` process running). Reset status to the prior `*_complete`
  state via `gantry status --run RUN` inspection, then retry the stage.
- **Fresh repo, `gantry run` immediately raises ValueError about spec/design:**
  the repo's `gantry.toml` still has the full default `stages` list. Edit
  `gantry.toml` to `stages = ["plan", "build", "evidence", "review"]`.
- **`gantry checks` fails on every run with a command-not-found style error:**
  `[checks].commands` in gantry.toml doesn't match the repo's actual toolchain
  (e.g. npm scripts on a Python repo). Fix the commands list to match reality,
  don't just skip checks.
- **Long-running commands (build/evidence stages, `gantry advance`) should run in
  the background, not foreground, if your own execution environment has a short
  foreground timeout.** A killed-mid-flight foreground call leaves the run's
  status stuck at a `*_running` value and the lockfile held — always prefer
  backgrounding + polling `gantry status` for anything that might take over a
  minute.

## What NOT to do

- Never run `gantry ship` automatically as part of a routine advance loop —
  it's an explicit, separately-confirmed action per run.
- Never hand-edit files inside `.worktrees/gantry/<run_id>/` directly as a
  shortcut around the pipeline — that defeats the point of having an agent
  stage produce and own the diff. If the agent's output is wrong, use
  `gantry revise` to send it back with comments instead.
- Never delete or bypass the isolated worktree to "just fix it faster" on the
  main branch — main/staging must stay untouched by in-flight runs.
- Never assume a stage finished just because a command returned — always
  confirm via `gantry status --run RUN` or the JSON output's actual status
  field before reporting success to the user.
