# Implementation Plan Stage

You are running inside Claude Code with the project's normal CLAUDE.md, skills, plugins, hooks, and settings available. Use those project capabilities naturally.

Input files for this stage live in `.agent-runs/{RUN_ID}/`:
- `routing.json`
- `intake.md`
- `product-spec.md` if present
- `architecture-design.md` if present
- optional `answer.md` if this is a resumed run

Your job: produce an implementation plan only. Do not modify application/source files.

Use existing planning/implementation skills when applicable. The plan must be precise enough for a cheaper build agent to execute without making product or architecture decisions.

If you need clarification, ask exactly one concise inline question in your final result and stop. Do not guess.

Write the final plan to `.agent-runs/{RUN_ID}/implementation-plan.md` and also summarize it in your final result.

Required plan sections:
1. Goal
2. Scope and risk level
3. Allowed files
4. Forbidden files / non-goals
5. Ordered implementation steps
6. Test plan and exact commands
7. Evidence requirements
8. Rollback / safety notes
9. Open questions, if any
