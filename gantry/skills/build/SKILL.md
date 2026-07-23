---
name: gantry-stage-build
description: Use when running a gantry pipeline's build stage — executing an approved implementation plan exactly, in verified incremental commits. Not a design or planning stage.
---

# Build Execution Discipline

Execute the implementation plan exactly. Do NOT redesign. Do NOT expand
scope.

## Verified slices, committed incrementally

Execute the plan's ordered implementation steps in sequence, one at a
time, with this discipline for EACH step:
1. Implement just that step.
2. Run that step's own declared verification (the command/test/check the
   plan named for this specific step).
3. Once that verification passes, commit ONLY the files that step touched
   (`git add <specific files>`, never `git add -A`/`git add .`), with a
   commit message naming the step (e.g. `step 3: add validate_email`).
4. If a step's verification fails and you can't fix it after a reasonable
   attempt, STOP — do not push into later steps that may depend on the
   broken one. Report exactly which step failed and why.

This leaves clean, verified, committed partial progress if you run out of
turns or crash mid-build, instead of one uncommitted all-or-nothing blob.

Rules:
- Touch only files listed in the plan's "Allowed files" section. If
  implementation genuinely reveals a file the plan never mentioned, you
  may touch it WITHOUT stopping — but you MUST declare it (path + one-line
  reason) as a scope addition. Undeclared files outside the plan's scope
  will fail the automated scope guard.
- Do not read or write `.env` or credential files.
- Do not run `git push` or destructive git commands. `git commit` per
  verified step (above) is required, not prohibited.
- Run only the verification commands necessary for the change.
- Follow this repo's own conventions and package manager as declared in
  its context files — do not introduce a different toolchain.

If blocked, write your question to `.agent-runs/{RUN_ID}/question.md` and
stop — do NOT put the question only in your final result text (gantry
checks question.md's existence deterministically, not your prose).
