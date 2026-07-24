# Combined Definition Stage (medium features)

You are running inside a coding agent with this project's normal context files
(CLAUDE.md / AGENTS.md / .cursorrules), skills, and settings available. Use those
project capabilities naturally.

Invoke the `gantry-stage-definition` skill for this stage's discipline — produce
BOTH the product spec and architecture design in one pass.

Input files for this stage live in `.agent-runs/{RUN_ID}/`:
- `intake.md`
- optional `answers/definition.md` if this is a resumed run

Write BOTH final artifacts:
1. `.agent-runs/{RUN_ID}/product-spec.md` — WHAT and WHY (product perspective)
2. `.agent-runs/{RUN_ID}/architecture-design.md` — HOW (architecture perspective)

Also write `.agent-runs/{RUN_ID}/acceptance-criteria.json` capturing the SAME
acceptance criteria as structured data, one entry per criterion:

```json
{
  "criteria": [
    {"id": "AC-1", "text": "...", "verifiable_by": "test|manual|inspection"}
  ]
}
```

Optionally write `.agent-runs/{RUN_ID}/decision-log.json` for significant
architecture decisions.

This stage produces a human-review gate by default
(`gantry approve --stage definition` sends it to plan;
`gantry revise --stage definition` sends it back with comments).
