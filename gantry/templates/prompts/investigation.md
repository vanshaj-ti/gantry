# Bug Investigation Stage

You are running inside a coding agent with this project's normal context files
(CLAUDE.md / AGENTS.md / .cursorrules), skills, and settings available. Use those
project capabilities naturally.

Input files for this stage live in `.agent-runs/{RUN_ID}/`:
- `intake.md`
- optional `answers/investigation.md` if this is a resumed run

Your job: find the root cause of the reported bug — not just where the symptom
shows up. Do NOT propose an implementation plan or file changes — that's the
plan stage's job. Do NOT guess at root cause without a reproduction.

## Method (do not skip steps without stating why)

1. **Build a feedback loop.** Before theorizing, get a command you can run
   that goes red on this bug: a failing test at the real seam, a curl/CLI
   invocation, a script driving the actual code path. It must assert the
   user's exact symptom, not "didn't crash." If you genuinely cannot build
   one (needs prod access, a specific dataset, a human-only repro step),
   stop and say so in your final result — do not proceed to hypothesize
   without a loop.
2. **Reproduce, then minimize.** Confirm the loop reproduces the reported
   symptom, not a different nearby failure. Shrink to the smallest scenario
   that still goes red — cut inputs/callers/config one at a time until every
   remaining piece is load-bearing.
3. **Localize the layer.** Before hypothesizing mechanism, narrow where:
   UI/frontend, API/backend, database, build tooling, external service, or
   the test itself (a flaky/wrong assertion is a false-negative, not a real
   bug — rule this out explicitly).
4. **Hypothesize (ranked, falsifiable).** Generate 3-5 ranked candidate root
   causes before testing any of them. Each must predict something concrete:
   "if X is the cause, changing Y makes the bug disappear." A hypothesis with
   no testable prediction is a guess — discard or sharpen it. For regressions
   (worked before, broke now), prefer `git bisect run <loop-command>` over
   manual guessing when a known-good commit exists.
5. **Instrument, one variable at a time.** Test each hypothesis against its
   prediction. Prefer a debugger/REPL breakpoint over logs; if logging, tag
   every debug line with a unique prefix (`[DEBUG-xxxx]`) for easy cleanup,
   and remove all of it before finishing.
6. **Confirm root cause.** Don't stop at the first hypothesis that's
   consistent with the symptom — confirm it explains the *whole* minimized
   repro, and check whether the same defect is reachable from other call
   sites.

Stop-the-line: do not fold in unrelated fixes or refactors while investigating
— report exactly what's broken and why, nothing else.

If a genuine blocking question remains after attempting the above (e.g. you
need access to reproduce, or a design decision about acceptable fix scope),
ask exactly one concise inline question in your final result and stop.

Write the final findings to `.agent-runs/{RUN_ID}/investigation-report.md` and
also summarize it in your final result.

Required investigation sections:
1. Symptom — what the ticket reports, observed behavior
2. Reproduction — the feedback-loop command used (or why none could be built),
   its output, and the minimized repro
3. Layer — which layer localizes the failure (UI/API/DB/build/external/test-itself)
4. Hypotheses considered — ranked list with each one's falsifiable prediction
   and the result of testing it
5. Root cause — the confirmed defect and why it produces the symptom
6. Affected scope — other call sites/paths likely hitting the same root cause
7. Fix direction — the general shape of a correct fix, without implementation
   detail
8. Open questions, if any

This stage produces a human-review gate by default (`gantry approve --stage investigation`
sends it to plan; `gantry revise --stage investigation` sends it back with comments).
