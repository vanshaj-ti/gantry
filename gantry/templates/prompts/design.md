# Architecture Design Stage

You are running inside a coding agent with this project's normal context files
(CLAUDE.md / AGENTS.md / .cursorrules), skills, and settings available. Use those
project capabilities naturally.

Invoke the `gantry-stage-design` skill for this stage's discipline (what a
design must cover, what NOT to do, required sections).

Input files for this stage live in `.agent-runs/{RUN_ID}/`:
- `intake.md`
- `product-spec.md`
- optional `answers/design.md` if this is a resumed run

Write the final design to `.agent-runs/{RUN_ID}/architecture-design.md` and
also summarize it in your final result.

In addition to the prose design, also write (best-effort, optional)
`.agent-runs/{RUN_ID}/decision-log.json` capturing each significant decision
as structured data:

```json
{
  "decisions": [
    {"decision": "...", "rationale": "...", "alternatives_considered": ["..."]}
  ]
}
```

Not gate-checked; a structured companion to the prose "Alternatives
considered" section.

This stage produces a human-review gate by default (`gantry approve --stage design`
sends it to plan; `gantry revise --stage design` sends it back with comments).
