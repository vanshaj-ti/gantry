---
name: gantry-stage-design
description: Use when writing a gantry pipeline's architecture design stage — turning an approved product spec into HOW it will be built (components, data flow, integration points), before any implementation plan is written.
---

# Architecture Design Discipline

Turn the product spec into an architecture design — HOW it will be built,
at the level of components, data flow, and integration points. Do NOT
write an implementation plan, file list, or step-by-step build order —
that's the plan stage's job. Reuse existing patterns and utilities in this
codebase wherever they fit; do not propose a new abstraction where an
existing one already covers the need.

If a design decision genuinely can't be made without more product input,
write your question to `.agent-runs/{RUN_ID}/question.md` and stop — do NOT
write the design itself in this case, and do NOT put the question only in
your final result text (gantry checks question.md's existence
deterministically, not your prose).

Required design sections:
1. Approach — the chosen architecture, in prose
2. Components affected — existing modules/files touched or extended
3. New components, if any — and why an existing one doesn't fit
4. Data flow / integration points
5. Alternatives considered — and why they were rejected
6. Risks and trade-offs
7. Open questions, if any
