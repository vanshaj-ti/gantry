# Build Stage

You are running inside a coding agent with this project's normal context files
(CLAUDE.md / AGENTS.md / .cursorrules), skills, and settings available. Use those
project capabilities naturally.

Input files for this stage live in `.agent-runs/{RUN_ID}/`:
- `intake.md`
- `product-spec.md` if present
- `architecture-design.md` if present
- `implementation-plan.md`
- optional `answers/build.md` if this is a resumed run

Your job: execute the implementation plan exactly. Do NOT redesign. Do NOT expand scope.

Rules:
- Touch only files listed in the plan's "Allowed files" section.
- If implementation reveals you genuinely need to create or touch a file the
  plan never mentioned (e.g. a new test fixture, a config file an unexpected
  dependency requires), you may do so WITHOUT stopping — but you MUST declare
  it: add a `## Scope additions` section to build-summary.md listing each
  such path in backticks with a one-line reason, e.g.
  `` `src/fixtures/mock-data.json` — needed by the new parser test``.
  Undeclared files outside the plan's scope will fail the automated scope
  guard. Reserve stopping to ask a question for cases where you're genuinely
  unsure whether the new file is in scope at all, not for routine discoveries.
- Do not read or write `.env` or credential files.
- Do not run `git push`, `git commit`, or destructive git commands.
- Run only the verification commands necessary for the change.
- Follow this repo's own conventions and package manager as declared in its
  context files — do not introduce a different toolchain.

Write `.agent-runs/{RUN_ID}/build-summary.md`. If the file already exists (because
this is a resumed run fixing review feedback), DO NOT overwrite it. Instead,
**append** a new top-level section at the bottom starting with `## Pass <N>`
(e.g. `## Pass 2`) so iteration history is preserved.

In your summary (or appended section), include:
1. Files changed in this pass
2. Plan steps completed or review feedback addressed
3. Tests/commands run with outcomes
4. Deviations from plan
5. Remaining risks or questions

If blocked, ask exactly one concise inline question in your final result and stop.

<!-- Customize per repo: add project-specific skill/plugin directives here.
     Example: "Load the `superpowers:using-superpowers` skill and invoke the Skill
     tool for relevant execution/testing-discipline skills." -->
