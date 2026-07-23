---
name: gantry-stage-investigation
description: Use when running a gantry pipeline's bug investigation stage — finding root cause before proposing any fix. Trigger words — root cause, reproduce, bisect, feedback loop, hypothesis.
---

# Bug Investigation Discipline

Find the root cause of the reported bug — not just where the symptom shows
up. Do NOT propose an implementation plan or file changes here. Do NOT
guess at root cause without a reproduction.

## Method (do not skip steps without stating why)

1. **Build a feedback loop.** Before theorizing, get a command you can run
   that goes red on this bug: a failing test at the real seam, a curl/CLI
   invocation, a script driving the actual code path. It must assert the
   user's exact symptom, not "didn't crash." If you genuinely cannot build
   one, stop and say so — do not proceed to hypothesize without a loop.
2. **Reproduce, then minimize.** Confirm the loop reproduces the reported
   symptom, not a different nearby failure. Shrink to the smallest scenario
   that still goes red.
3. **Localize the layer.** UI/frontend, API/backend, database, build
   tooling, external service, or the test itself (a flaky/wrong assertion
   is a false-negative, not a real bug — rule this out explicitly).
4. **Hypothesize (ranked, falsifiable).** Generate 3-5 ranked candidate
   root causes before testing any of them. Each must predict something
   concrete: "if X is the cause, changing Y makes the bug disappear." For
   regressions (worked before, broke now), prefer `git bisect run
   <loop-command>` over manual guessing when a known-good commit exists.
5. **Instrument, one variable at a time.** Prefer a debugger/REPL
   breakpoint over logs; if logging, tag every debug line with a unique
   prefix (`[DEBUG-xxxx]`) and remove all of it before finishing.
6. **Confirm root cause.** Confirm it explains the *whole* minimized repro,
   and check whether the same defect is reachable from other call sites.

Stop-the-line: do not fold in unrelated fixes or refactors while
investigating — report exactly what's broken and why, nothing else.

If you genuinely cannot build a feedback loop, or hit a real blocking
question you can't resolve yourself, write it to
`.agent-runs/{RUN_ID}/question.md` and stop — do NOT write the
investigation report in this case, and do NOT put the question only in
your final result text (gantry checks question.md's existence
deterministically, not your prose).

Required report sections: Symptom, Reproduction (loop command + output +
minimized repro), Layer, Hypotheses considered (ranked, with predictions
and results), Root cause, Affected scope, Fix direction, Open questions.
