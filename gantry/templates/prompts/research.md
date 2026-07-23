# Research Stage

You are running inside a coding agent with this project's normal context files
(CLAUDE.md / AGENTS.md / .cursorrules), skills, and settings available. Use those
project capabilities naturally.

Invoke the `gantry-stage-research` skill for this stage's discipline (what
counts as a deliverable, what NOT to do, required report sections).

Input files for this stage live in `.agent-runs/{RUN_ID}/`:
- `intake.md`
- optional `answers/research.md` if this is a resumed run

Write the final deliverable to `.agent-runs/{RUN_ID}/research-report.md` and
also summarize it in your final result.

This stage is the last stage in the research queue: there is no plan/build/
evidence/review after it. `gantry approve --stage research` closes out the
run; `gantry revise --stage research` sends it back with comments.
