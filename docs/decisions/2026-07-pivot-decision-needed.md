# Decision: standalone CLI, not a plugin pivot

**Status:** resolved — 2026-07-21.

**Supersedes:** the "pivot decision needed" framing this file previously
held, and the recommendation in
`docs/research/2026-07-06-tool-ecosystem-triage.md`.

## Decision

Gantry stays a standalone CLI. No pivot to a Claude Code plugin, short or
long term. The engine keeps expanding its own command surface (`gantry
setup`, spec/design stage execution, autonomy toggles) rather than
re-delivering its stage-gate/checks/independent-review model through
agent-skills.

## Why

Gantry's core value — deterministic stage gates, repo-owned checks as the
source of truth, an independent LLM as final reviewer, pluggable runners
(claude-code, cursor-cli, codex-cli) — is orchestration logic that outlives
any single agent tool's plugin surface. Continued CLI investment since
2026-07-06 (Docker isolation, multi-target daemon, per-project containers)
already assumes and depends on the standalone-CLI shape; re-platforming onto
a plugin model would discard that work for a distribution bet, not a
technical one.

## Re-check

No fixed re-check date — revisit only if plugin distribution becomes a
concrete blocker (e.g. installs stall on the CLI path specifically).
