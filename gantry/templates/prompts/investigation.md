# Bug Investigation Stage

You are running inside a coding agent with this project's normal context files
(CLAUDE.md / AGENTS.md / .cursorrules), skills, and settings available. Use those
project capabilities naturally.

Invoke the `gantry-stage-investigation` skill for this stage's discipline
(feedback-loop-first debugging method, ranked falsifiable hypotheses,
required report sections).

Input files for this stage live in `.agent-runs/{RUN_ID}/`:
- `intake.md`
- optional `answers/investigation.md` if this is a resumed run

Write the final findings to `.agent-runs/{RUN_ID}/investigation-report.md`
and also summarize it in your final result.

This stage produces a human-review gate by default (`gantry approve --stage investigation`
sends it to plan; `gantry revise --stage investigation` sends it back with comments).
