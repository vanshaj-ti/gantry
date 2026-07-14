# Pivot decision needed: standalone CLI vs. agent-skills integration

**Status:** undecided — flagged here, not resolved.

## Background

`docs/research/2026-07-06-tool-ecosystem-triage.md` is a self-authored
competitive analysis concluding that the "agent-skills" ecosystem has won
distribution, and that Gantry's core insights — deterministic stage gates,
repo-owned lint/build checks as the source of truth, an independent LLM as
final reviewer — should survive but be re-delivered differently:

- **Short term:** ship as a Claude Code plugin that augments agent-skills,
  rather than compete as a standalone CLI.
- **Medium term:** a standalone skill usable by other agent runners
  (Cursor, Codex), not just Claude Code.
- **Long term:** narrow the CLI itself into a multi-repo / multi-agent
  orchestration layer, rather than a single-agent stage driver.

The doc frames this as urgent ("the window to pivot is now").

## Why this doc exists

No tracking issue or ADR exists for this recommendation anywhere else in the
repo (checked: no reference in code, commit messages, or other docs). Every
`cli.py` feature landed since 2026-07-06 has continued building on the
standalone-CLI assumption the triage doc argues against. That's a real risk:
each new command/feature surface added to `gantry/cli/` is more to migrate
or discard if the pivot recommendation is later accepted.

This doc does not make the call — that's a product decision for the
maintainer, not something to resolve silently via code changes. It exists so
the open question is visible instead of buried in `docs/research/`.

## Next step

Once decided, replace this doc with a dated decision record stating the
actual choice (accept and pivot / reject and continue as standalone CLI /
explicitly defer with a re-check date), and link it from here.
