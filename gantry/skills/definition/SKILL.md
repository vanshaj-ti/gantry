---
name: gantry-stage-definition
description: Use when writing a gantry pipeline's combined definition stage — product WHAT/WHY plus architecture HOW in one medium-feature pass.
---

# Combined Definition Discipline

For medium features, produce both the product spec and architecture design
in a single stage so the human reviews one coherent definition pack.

## Product spec (WHAT / WHY)

Cover:
1. Problem — what's broken or missing today, for whom
2. Goal — the outcome that defines success
3. Non-goals — explicitly out of scope
4. User-facing behavior
5. Acceptance criteria — concrete, checkable, with unique `AC-N` ids
6. Open questions, if any

Do not invent product intent. If the intake is ambiguous, write the question
to `.agent-runs/{RUN_ID}/question.md` and stop — do NOT write either artifact.

## Architecture design (HOW)

Given the same intake (and the spec you are writing), cover:
1. Approach — the architecture shape that satisfies the acceptance criteria
2. Key components / boundaries
3. Data / API / control-flow changes
4. Risks and mitigations
5. Alternatives considered (brief)
6. Explicit non-goals for implementation detail that belongs in plan

Do NOT write the ordered implementation plan here — that is the plan stage.

## Required outputs

- `product-spec.md`
- `architecture-design.md`
- `acceptance-criteria.json` (structural gate)
- optional `decision-log.json`
