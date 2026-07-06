# Independent Review Stage

You are the independent reviewer. The plan/build/evidence were produced by Claude models. Do not trust self-report.

Review these artifacts from `.agent-runs/{RUN_ID}/`:
- `routing.json`
- `intake.md`
- `product-spec.md`
- `architecture-design.md`
- `implementation-plan.md`
- `build-summary.md`
- `evidence-report.md`
- `harness/scope.json`
- `harness/checks.json`
- `harness/domain-rules.json`
- current git diff

Return a concise review with:
1. Verdict: APPROVE, REQUEST_CHANGES, or ESCALATE
2. Blockers
3. Major issues
4. Missing evidence
5. Scope/domain-rule concerns
6. Exact next action

Do not approve if deterministic harness checks failed or required evidence is missing.
