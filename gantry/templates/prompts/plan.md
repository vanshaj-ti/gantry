# Implementation Plan Stage

You are running inside a coding agent with this project's normal context files
(CLAUDE.md / AGENTS.md / .cursorrules), skills, and settings available. Use those
project capabilities naturally.

Invoke the `gantry-stage-plan` skill for this stage's discipline (what a
plan must cover, required sections, per-step verification requirements).

Input files for this stage live in `.agent-runs/{RUN_ID}/`:
- `intake.md`
- `product-spec.md` if present
- `architecture-design.md` if present
- `investigation-report.md` if present
- optional `answers/plan.md` if this is a resumed run

Write the final plan to `.agent-runs/{RUN_ID}/implementation-plan.md` and
also summarize it in your final result.

In addition to the prose "Allowed files" section, also write
`.agent-runs/{RUN_ID}/allowed-files.json` with the same paths as a
structured glob list:

```json
{
  "allowed_globs": ["path/or/glob", "..."],
  "notes": {"path": "why"}
}
```

`allowed_globs` must be non-empty and cover every path/glob listed in the
prose section (the scope guard reads this file directly instead of
scraping prose backticks when it's present).
