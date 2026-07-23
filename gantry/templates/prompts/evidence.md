# Evidence Stage

You are running inside a coding agent with this project's normal context files
(CLAUDE.md / AGENTS.md / .cursorrules), skills, and settings available.

Invoke the `gantry-stage-evidence` skill for this stage's discipline (real
end-to-end evidence, AC-N verdict tagging, what NOT to do).

Input files for this stage live in `.agent-runs/{RUN_ID}/`:
- `intake.md`
- `product-spec.md` if present
- `architecture-design.md` if present
- `implementation-plan.md`
- `build-summary.md`
- `acceptance-criteria.json` if present
- optional `answers/evidence.md` if this is a resumed run

If `acceptance-criteria.json` is present, read it and explicitly address
EVERY `AC-N` id it lists in your report.

Write `.agent-runs/{RUN_ID}/evidence-report.md`. If the file already exists
(resumed run after a failed review), DO NOT overwrite it — **append** a new
`## Pass <N>` section at the bottom.

If blocked, write your question to `.agent-runs/{RUN_ID}/question.md` and
stop — do NOT put the question only in your final result text.
