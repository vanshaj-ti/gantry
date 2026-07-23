# Build Stage

You are running inside a coding agent with this project's normal context files
(CLAUDE.md / AGENTS.md / .cursorrules), skills, and settings available. Use those
project capabilities naturally.

Invoke the `gantry-stage-build` skill for this stage's discipline (verified
incremental commits, scope-addition rules, what NOT to do).

Input files for this stage live in `.agent-runs/{RUN_ID}/`:
- `intake.md`
- `product-spec.md` if present
- `architecture-design.md` if present
- `implementation-plan.md`
- optional `answers/build.md` if this is a resumed run

Write `.agent-runs/{RUN_ID}/build-summary.md`. If the file already exists
(because this is a resumed run fixing review feedback), DO NOT overwrite it.
Instead, **append** a new top-level section at the bottom starting with
`## Pass <N>` (e.g. `## Pass 2`) so iteration history is preserved.

Note: the per-step commit loop (see the skill) happens WITHIN this single
build invocation. That's a different level from the "## Pass N" convention
here, which is about a build-summary.md appended across SEPARATE, resumed
build invocations — do not conflate the two.

In your summary (or appended section), include:
1. Files changed in this pass
2. Plan steps completed or review feedback addressed
3. Tests/commands run with outcomes
4. Deviations from plan
5. Remaining risks or questions
6. Which steps got their own verified commit, and which did not (and why)

If blocked, write your question to `.agent-runs/{RUN_ID}/question.md` and
stop — do NOT put the question only in your final result text.
