# Architecture Design Stage

You are running inside a coding agent with this project's normal context files
(CLAUDE.md / AGENTS.md / .cursorrules), skills, and settings available. Use those
project capabilities naturally.

Input files for this stage live in `.agent-runs/{RUN_ID}/`:
- `intake.md`
- `product-spec.md`
- optional `answers/design.md` if this is a resumed run

Your job: turn the product spec into an architecture design — HOW it will be
built, at the level of components, data flow, and integration points. Do NOT
write an implementation plan, file list, or step-by-step build order — that's
the plan stage's job. Reuse existing patterns and utilities in this codebase
wherever they fit; do not propose a new abstraction where an existing one
already covers the need.

If a design decision genuinely can't be made without more product input, ask
exactly one concise inline question in your final result and stop.

Write the final design to `.agent-runs/{RUN_ID}/architecture-design.md` and
also summarize it in your final result.

Required design sections:
1. Approach — the chosen architecture, in prose
2. Components affected — existing modules/files touched or extended
3. New components, if any — and why an existing one doesn't fit
4. Data flow / integration points
5. Alternatives considered — and why they were rejected
6. Risks and trade-offs
7. Open questions, if any

In addition to the prose design above, also write (best-effort, optional)
`.agent-runs/{RUN_ID}/decision-log.json` capturing each significant decision
from section 5 as structured data:

```json
{
  "decisions": [
    {"decision": "...", "rationale": "...", "alternatives_considered": ["..."]}
  ]
}
```

One entry per significant architectural decision — `decision` is what was
chosen, `rationale` is why, `alternatives_considered` lists the options that
were rejected. This file is not gate-checked; write it as a structured
companion to the prose "Alternatives considered" section.

This stage produces a human-review gate by default (`gantry approve --stage design`
sends it to plan; `gantry revise --stage design` sends it back with comments).
