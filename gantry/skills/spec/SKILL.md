---
name: gantry-stage-spec
description: Use when writing a gantry pipeline's product spec stage — turning an intake request into WHAT should exist and WHY, before any architecture or implementation decisions are made.
---

# Product Spec Discipline

Turn the intake request into a clear product spec — WHAT should exist and
WHY, from a user/product point of view. Do NOT propose architecture,
libraries, file layout, or implementation steps — that is the design and
plan stages' job, and reaching into it here fights the pipeline's own
stage separation.

If the request is ambiguous or underspecified, write your question to
`.agent-runs/{RUN_ID}/question.md` and stop — do NOT write the spec itself
in this case, and do NOT put the question only in your final result text
(gantry checks question.md's existence deterministically, not your prose).
Do not guess at product intent.

Required spec sections:
1. Problem — what's broken or missing today, for whom
2. Goal — the outcome that defines success
3. Non-goals — explicitly out of scope for this change
4. User-facing behavior — what changes from the user's perspective
5. Acceptance criteria — concrete, checkable statements a reviewer can verify
6. Open questions, if any

Each acceptance criterion needs a unique `AC-N` id, the same checkable
statement in prose, and a `verifiable_by` tag: `"test"` if provable by an
automated test, `"manual"` if it needs a human to check by hand,
`"inspection"` if verified by reading code/config rather than running
anything.
