# Product Spec Stage

You are running inside a coding agent with this project's normal context files
(CLAUDE.md / AGENTS.md / .cursorrules), skills, and settings available. Use those
project capabilities naturally.

Input files for this stage live in `.agent-runs/{RUN_ID}/`:
- `intake.md`
- optional `answers/spec.md` if this is a resumed run

Your job: turn the intake request into a clear product spec — WHAT should exist
and WHY, from a user/product point of view. Do NOT propose architecture,
libraries, file layout, or implementation steps — that's the design and plan
stages' job.

If the request is ambiguous or underspecified, ask exactly one concise inline
question in your final result and stop. Do not guess at product intent.

Write the final spec to `.agent-runs/{RUN_ID}/product-spec.md` and also
summarize it in your final result.

Required spec sections:
1. Problem — what's broken or missing today, for whom
2. Goal — the outcome that defines success
3. Non-goals — explicitly out of scope for this change
4. User-facing behavior — what changes from the user's perspective
5. Acceptance criteria — concrete, checkable statements a reviewer can verify
6. Open questions, if any

In addition to the prose spec above, also write
`.agent-runs/{RUN_ID}/acceptance-criteria.json` capturing the SAME acceptance
criteria from section 5 as structured data, one entry per criterion:

```json
{
  "criteria": [
    {"id": "AC-1", "text": "...", "verifiable_by": "test|manual|inspection"}
  ]
}
```

Each criterion needs a unique `AC-N` id (matching what you write in prose so a
reader can cross-reference the two), the same checkable statement as `text`,
and a `verifiable_by` tag: `"test"` if it can be proven by an automated test,
`"manual"` if it needs a human to check it by hand, `"inspection"` if it's
verified by reading code/config rather than running anything. This file is a
structural requirement for this stage to complete — write it even if it feels
redundant with the prose section.

This stage produces a human-review gate by default (`gantry approve --stage spec`
sends it to design; `gantry revise --stage spec` sends it back with comments).
