# Product Spec Stage

You are running inside a coding agent with this project's normal context files
(CLAUDE.md / AGENTS.md / .cursorrules), skills, and settings available. Use those
project capabilities naturally.

Invoke the `gantry-stage-spec` skill for this stage's discipline (what a
spec must cover, what NOT to do, required sections and acceptance-criteria
format).

Input files for this stage live in `.agent-runs/{RUN_ID}/`:
- `intake.md`
- optional `answers/spec.md` if this is a resumed run

Write the final spec to `.agent-runs/{RUN_ID}/product-spec.md` and also
summarize it in your final result.

In addition to the prose spec, also write
`.agent-runs/{RUN_ID}/acceptance-criteria.json` capturing the SAME
acceptance criteria as structured data, one entry per criterion:

```json
{
  "criteria": [
    {"id": "AC-1", "text": "...", "verifiable_by": "test|manual|inspection"}
  ]
}
```

This file is a structural requirement for this stage to complete — write it
even if it feels redundant with the prose section.

This stage produces a human-review gate by default (`gantry approve --stage spec`
sends it to design; `gantry revise --stage spec` sends it back with comments).
