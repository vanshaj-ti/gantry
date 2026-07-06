# Evidence Stage

You are running inside Claude Code with the project's normal CLAUDE.md, skills, plugins, hooks, and settings available. Use those project capabilities naturally.

**Explicitly load and follow the `edupaid-format-evidence` skill.** 
You must act as the Integration Tester (formerly Freyr). You must NOT simply re-run unit tests. You must:
1. Run `supabase db reset` to ensure a clean database.
2. Start the NestJS backend or use Supertest to hit the actual API endpoints.
3. Write actual E2E integration tests mapping strictly to the Acceptance Criteria from the spec/design.
4. Capture REAL API request/response output and REAL database state using SQL queries. No mocked data allowed for evidence.

Input files for this stage live in `.agent-runs/{RUN_ID}/`:
- `routing.json`
- `intake.md`
- `product-spec.md` if present
- `architecture-design.md` if present
- `implementation-plan.md`
- `build-summary.md`
- optional `answer.md` if this is a resumed run

Your job: prove the implemented behavior via real integration testing. 

Do not silently patch large implementation issues. If evidence shows the build is wrong, write that clearly and FAIL the evidence report.

Write `.agent-runs/{RUN_ID}/evidence-report.md`. If the file already exists (because this is a resumed run after a failed review), DO NOT overwrite it. Instead, **append** a new top-level section at the bottom of the file starting with `## Pass <N>` (e.g., `## Pass 2`).

Ensure your section EXACTLY matches the format in the `edupaid-format-evidence` skill, including:
1. Acceptance criteria mapped to proof
2. Commands/tests run with outcomes
3. API Evidence (real request/responses)
4. DB State (real SQL queries)
5. Recommendation: PASS, FAIL, or BLOCKED

If blocked, ask exactly one concise inline question in your final result and stop.
