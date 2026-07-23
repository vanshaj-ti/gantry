---
name: gantry-stage-plan
description: Use when running a gantry pipeline's plan stage — writing an implementation plan precise enough for a cheaper build agent to execute without making product or architecture decisions.
---

# Implementation Plan Discipline

Produce an implementation plan only. Do NOT modify application/source
files. The plan must be precise enough for a cheaper build agent to
execute without making product or architecture decisions.

If you need clarification, ask exactly one concise inline question and
stop. Do not guess.

Required plan sections:
1. Goal
2. Scope and risk level
3. Allowed files — list every path you intend to touch, in backticks (the
   scope guard reads these; anything changed outside this list is flagged).
4. Forbidden files / non-goals
5. Ordered implementation steps — each step must name its own specific
   verification (a command, a test name, or a concrete check) that proves
   THAT step is done correctly. Don't defer all verification to the "Test
   plan" section — that section is for overall/final verification once
   every step is complete; per-step verification is what lets the build
   agent confirm and commit each step independently before moving on.
6. Test plan and exact commands
7. Evidence requirements
8. Rollback / safety notes
9. Open questions, if any

Also write the "Allowed files" list as structured data (`allowed_globs`
JSON) alongside the prose — the scope guard reads that file directly
instead of scraping prose backticks when it's present.
