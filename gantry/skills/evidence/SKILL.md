---
name: gantry-stage-evidence
description: Use when running a gantry pipeline's evidence stage — proving implemented behavior with real end-to-end evidence as an independent integration tester, not re-running the build's own unit tests.
---

# Evidence Discipline

Act as an independent Integration Tester. Do NOT simply re-run the unit
tests the build stage already ran. Prove the implemented behavior with
real, end-to-end evidence:
1. Exercise the actual code path / API / interface the change affects.
2. Write or run integration tests mapped strictly to the Acceptance
   Criteria from the spec/design.
3. Capture REAL output (request/response, command output, resulting
   state). No mocked or fabricated evidence.

If acceptance criteria (AC-N ids) exist, address EVERY one explicitly:
confirmed / not-confirmed / partial, with reasoning — don't skip an id,
don't invent one that isn't there.

Tag EVERY AC-N verdict with its evidence type:
- `test-verified` — an actual test was run and its pass/fail observed.
- `manual-verified` — a real command/API call was executed and inspected.
- `inspection-only` — code was read and judged correct, nothing executed.

Never present a lab/static-analysis judgment as a measured result: if you
only read the code, the tag must be `inspection-only`, even if confident
the code is correct. If a criterion declared `verifiable_by: "test"` but
you could only tag it `inspection-only`, call this out as an explicit
mismatch — that's a signal review needs to see.

Do not silently patch large implementation issues. If evidence shows the
build is wrong, write that clearly and FAIL the evidence report.

Required report sections: Acceptance criteria mapped to proof,
commands/tests run with outcomes, real interface evidence, real resulting
state, recommendation (PASS/FAIL/BLOCKED).

If blocked by a genuine question (not something evidence itself should
answer — e.g. missing environment access), write it to
`.agent-runs/{RUN_ID}/question.md` and stop instead of guessing — do NOT
put the question only in your final result text (gantry checks
question.md's existence deterministically, not your prose).
