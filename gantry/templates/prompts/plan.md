# Implementation Plan Stage

You are running inside a coding agent with this project's normal context files
(CLAUDE.md / AGENTS.md / .cursorrules), skills, and settings available. Use those
project capabilities naturally.

Input files for this stage live in `.agent-runs/{RUN_ID}/`:
- `intake.md`
- `product-spec.md` if present
- `architecture-design.md` if present
- `investigation-report.md` if present
- optional `answers/plan.md` if this is a resumed run

Your job: produce an implementation plan only. Do NOT modify application/source files.

The plan must be precise enough for a cheaper build agent to execute without making
product or architecture decisions.

If you need clarification, ask exactly one concise inline question in your final
result and stop. Do not guess.

Write the final plan to `.agent-runs/{RUN_ID}/implementation-plan.md` and also
summarize it in your final result.

Required plan sections:
1. Goal
2. Scope and risk level
3. Allowed files — list every path you intend to touch, in backticks (the scope
   guard reads these; anything changed outside this list is flagged).
4. Forbidden files / non-goals
5. Ordered implementation steps — each step must name its own specific
   verification (a command, a test name, or a concrete check) that proves
   THAT step is done correctly, e.g. "Step 3: add `validate_email` to
   `auth/validators.py` — verify: `pytest tests/test_validators.py::test_validate_email`".
   Don't defer all verification to the separate "Test plan and exact
   commands" section below — that section is for the overall/final
   verification once every step is complete, this per-step verification is
   what lets the build agent confirm and commit each step independently
   before moving to the next.
6. Test plan and exact commands
7. Evidence requirements
8. Rollback / safety notes
9. Open questions, if any

In addition to the prose "Allowed files" section above, also write
`.agent-runs/{RUN_ID}/allowed-files.json` with the same paths as a structured
glob list:

```json
{
  "allowed_globs": ["path/or/glob", "..."],
  "notes": {"path": "why"}
}
```

`allowed_globs` must be non-empty and cover every path/glob you listed in the
prose section above (the scope guard reads this file directly instead of
scraping the prose backticks when it's present). `notes` is optional —
a short one-line reason per path/glob, keyed by the same string used in
`allowed_globs`.
