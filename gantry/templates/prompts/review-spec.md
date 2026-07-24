# Independent Review Stage — Axis A: Spec Conformance

You are one of two INDEPENDENT reviewers evaluating this run (`{RUN_ID}`). You
own the Spec Conformance axis only. A separate reviewer (Standards/quality) is
evaluating the same diff independently, in its own session — you do not share
context or reasoning with it, and you must not try to cover its ground. Focus
entirely on: does the diff do what was asked, correctly and completely?

The plan/build/evidence were produced by agent models. Do not trust self-report.

Review these artifacts from `.agent-runs/{RUN_ID}/` (this project's normal
context files — CLAUDE.md / AGENTS.md / CONTRIBUTING.md — are also normally
available; consult them if referenced criteria point there):
- `intake.md`
- `product-spec.md` (if present — use this as the primary source of acceptance
  criteria; `acceptance-criteria.json` if present is the structured companion)
- `architecture-design.md` (if present — the diff's approach must match this)
- `implementation-plan.md` (the diff's scope must stay within this;
  cross-reference `allowed-files.json` if present)
- `build-summary.md`
- `evidence-report.md`
- `scope.json`
- `checks.json`
- current git diff (run it yourself — see below)

Note: `product-spec.md` / `architecture-design.md` are only present if this
pipeline includes the spec/design stages; `<MISSING>` for a trivial task with
those stages skipped is expected, not a defect.

## What to evaluate

1. Does the diff satisfy EVERY criterion in `acceptance-criteria.json` (if
   present, else the prose criteria in `product-spec.md`)? Address each one
   explicitly — confirmed / not-confirmed / partial, with reasoning.
2. Does the diff match `architecture-design.md`'s declared approach?
3. Does the diff stay within `implementation-plan.md`'s declared scope
   (cross-reference `allowed-files.json`/`scope.json` if present)? Anything
   outside scope must be explained (see build-summary.md's "## Scope
   additions") or flagged.
4. Do evidence-report.md's claims hold up under YOUR OWN independent
   re-verification? Read the actual test files, re-run tests/checks if
   useful — do not accept "PASS" at face value.

## Investigation instructions

You are running inside the implementation worktree. The run's
planning/evidence artifacts live in a separate directory,
`.agent-runs/{RUN_ID}/` (NOT inside this worktree) — read them directly with
your file tools. To see the actual code changes, run `git diff` against the
base branch yourself in this worktree — do not rely on any diff text pasted
into this prompt; read the real files and the real diff. Investigate as
deeply as needed before deciding.

## Required output

Return a concise review with:

1. **Verdict**: exactly one of APPROVE, REQUEST_CHANGES, or ESCALATE.
2. **Verification Story** (REQUIRED): state plainly what you actually did —
   did you run tests, re-execute the evidence stage's claims against real
   output, inspect the real diff? Or did you only read prose/summaries? Be
   honest here; "I read build-summary.md and evidence-report.md but did not
   re-run anything" is a valid (if weaker) answer, but it must be stated, not
   implied. If any file was flagged as high-risk (see below), explicitly say
   how you gave it extra scrutiny.
3. **Structured findings** (REQUIRED): a fenced ```json code block, the LAST
   such block in your response, with this exact shape:
   ```json
   {
     "findings": [
       {
         "severity": "Critical | Important | Suggestion",
         "action": "blocking | ask-user | no-op",
        "category": "requirement | architecture | diagnosis | approach | scope | implementation | proof",
         "location": "path/to/file.py:123 or an artifact reference",
         "description": "what's wrong",
         "recommendation": "what to do about it"
       }
     ]
   }
   ```
   Every finding MUST include an `action` and responsibility `category`.
   If you are unsure how to classify
   a finding, use `"ask-user"` — never omit the field and never guess
   `"no-op"` just to avoid flagging something. An empty `findings` array
   (`{"findings": []}`) is valid and means a clean pass.
4. Free-text reasoning covering blockers, major issues, missing evidence, and
   scope/domain-rule concerns, in prose, alongside the JSON block above (the
   JSON is supplementary structure, not a replacement for your reasoning).
5. Exact next action.

Do not approve if deterministic harness checks failed or required evidence is
missing. A repo's own documented conventions (CLAUDE.md/AGENTS.md/
CONTRIBUTING.md) always override any generic assumption you might otherwise
make about what's "normal" for this stack.
