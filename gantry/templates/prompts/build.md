# Build Stage

You are running inside Claude Code with the project's normal CLAUDE.md, skills, plugins, hooks, and settings available. Use those project capabilities naturally.

**Explicitly use the `superpowers` plugin and `caveman` skill for this task:**
- Load `superpowers:using-superpowers` at the start and follow its skill-selection guidance for implementation work (e.g. relevant `superpowers:*` execution/testing-discipline skills) — do not just let it sit in context unused.
- Load the `caveman` skill for compressed internal reasoning/logging where appropriate.
- Actually invoke the `Skill` tool for these — do not only read the injected hook content passively.

Input files for this stage live in `.agent-runs/{RUN_ID}/`:
- `routing.json`
- `intake.md`
- `product-spec.md` if present
- `architecture-design.md` if present
- `implementation-plan.md`
- optional `answer.md` if this is a resumed run

Your job: execute the implementation plan exactly. Do not redesign. Do not expand scope.

Rules:
- Touch only files listed in the plan's allowed files.
- If another file is required, ask one concise inline question and stop.
- Do not read or write `.env` or credential files.
- Do not create Supabase edge functions.
- Do not run `git push`, `git commit`, or destructive git commands.
- Run only verification commands necessary for the change.
- Use npm, not pnpm, for all install/lint/test/build commands. Do not modify `pnpm-lock.yaml`.

Write `.agent-runs/{RUN_ID}/build-summary.md`. If the file already exists (because this is a resumed run fixing review feedback), DO NOT overwrite it. Instead, **append** a new top-level section at the bottom of the file starting with `## Pass <N>` (e.g., `## Pass 2`).

In your summary (or appended section), include:
1. Files changed in this pass
2. Plan steps completed or review feedback addressed
3. Tests/commands run with outcomes
4. Deviations from plan
5. Remaining risks or questions
6. Confirmation that `superpowers` and `caveman` skills were loaded and used (name which specific skills were invoked)

If blocked, ask exactly one concise inline question in your final result and stop.
