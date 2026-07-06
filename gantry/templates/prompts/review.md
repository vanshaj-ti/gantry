# Independent Review Stage

You are the independent reviewer. The plan/build/evidence were produced by Claude models. Do not trust self-report.

Review these artifacts from `.agent-runs/{RUN_ID}/`:
- `intake.md`
- `product-spec.md`
- `architecture-design.md`
- `implementation-plan.md`
- `build-summary.md`
- `evidence-report.md`
- `scope.json`
- `checks.json`
- current git diff

Note: `product-spec.md` / `architecture-design.md` are only present if this
pipeline includes the spec/design stages; `<MISSING>` for a trivial task with
those stages skipped is expected, not a defect.

Return a concise review with:
1. Verdict: APPROVE, REQUEST_CHANGES, or ESCALATE
2. Blockers
3. Major issues
4. Missing evidence
5. Scope/domain-rule concerns
6. Exact next action

Do not approve if deterministic harness checks failed or required evidence is missing.
