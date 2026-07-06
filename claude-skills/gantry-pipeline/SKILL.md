---
name: gantry-pipeline
description: Use whenever asked to run, drive, check, resume, or manage a Gantry pipeline run — creating runs, advancing plan/build/evidence/review stages, checking gantry.toml, or shipping a PR. Trigger words: gantry, pipeline run, stage, advance, ship, worktree run, blocked run, evidence stage.
---

# Gantry Pipeline Operator

Gantry is a staged autonomous build pipeline CLI (`gantry`). You operate it on
behalf of the user to run tasks end-to-end: plan -> build -> checks -> evidence -> review -> ship.

## Setup (every session, before any gantry command)

```bash
source ~/gantry/.venv/bin/activate
export GANTRY_TARGET=<target-repo-path>   # e.g. ~/edupaid
cd "$GANTRY_TARGET"
```

Always confirm `GANTRY_TARGET` is set correctly before running anything — every
command operates on whichever repo it points at.

See references/cli-reference.md for full command syntax and references/workflow.md
for the standard operating loop, recovery patterns, and pitfalls.
