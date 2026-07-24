# Migration guide: Cursor SDK primary + adaptive pipeline

This guide covers rolling forward to Gantry's Cursor-SDK-primary agent
architecture and (optionally) v2 verification / adaptive pipelines — without
invalidating existing `gantry.toml`, in-flight runs, or on-disk artifacts.

## What stays compatible

- Status strings in `state.json` (byte-identical legacy values).
- Legacy `sessions.json` records (`session_id` / `model` / `runner`).
- Explicit `[agent].runner = "claude-code"|"cursor-cli"|"codex-cli"` choices.
- Existing queue mappings (`feature` / `bug` / `hotfix` / `research` / `chore`).
- In-flight runs without `pipeline_version` keep the legacy embedded
  checks/e2e path inside `build_complete` advance.

## Default changes (new scaffolds only)

| Setting | Previous default | New default |
|---------|------------------|-------------|
| `[agent].runner` | `claude-code` | `cursor-sdk` |
| `[review].runner` | `claude-code` | inherits agent runner (`cursor-sdk`) |

Existing projects that already write `runner = "claude-code"` are unchanged.

## Cursor SDK setup

1. Install Gantry (`pip install -e .` pulls `cursor-sdk>=1.0.24,<2`).
2. Export `CURSOR_API_KEY` (user or service-account key).
3. Run `gantry doctor` — inspect the additive `cursor_sdk` diagnosis block:
   package availability, API key presence, and CLI fallback readiness.
4. Optional live smoke (not CI):

```bash
export GANTRY_CURSOR_SDK_LIVE=1
export CURSOR_API_KEY=...
python -m unittest tests.test_cursor_sdk_smoke.TestCursorSdkLiveSmoke -v
```

See [cursor-sdk-compatibility.md](./cursor-sdk-compatibility.md) for documented
SDK assumptions and explicit non-assumptions.

## Rollback to a legacy CLI backend

Set in `gantry.toml`:

```toml
[agent]
runner = "claude-code"   # or "cursor-cli" / "codex-cli"
```

No run history rewrite is required. Sessions recorded under `cursor-sdk` will
not native-resume on another backend (backend mismatch → artifact continuation).

## Pre-start fallbacks

If `cursor-sdk` is preferred but the package or API key is missing, Gantry may
fall back **before an invocation starts** along:

`cursor-sdk` → `cursor-cli` → `claude-code` → `codex-cli`

Never after a mutating invocation has begun.

Interactive cockpit/herdr panes still use `cursor-cli` (or another TUI runner)
because the SDK path is non-TUI.

## Session lineage (approved topology)

| Stages | Lineage |
|--------|---------|
| spec, design, investigation, research | Isolated (resume own revisions only) |
| plan → build → resolve | Shared implementation lineage |
| evidence, review_spec, review_standards | Always fresh / mutually isolated |

Investigation never shares the implementation lineage.

## Pipeline versioning

- `pipeline_version: 1` (default): legacy embedded verification after build.
- `pipeline_version: 2` (opt-in): explicit checks/e2e stages when configured.

Pin happens at `create_run`. Changing `gantry.toml` later does not rewrite
completed history.

## Staged rollout checklist

1. Backend available but opt-in (`runner = "cursor-sdk"` per project).
2. Cursor SDK default for newly scaffolded projects (current template).
3. v2 adaptive pipeline default only after legacy replay + operator UX sign-off.

## Operator visibility

`gantry doctor`, `gantry status`, `gantry watch`, and cost output surface
backend, model, profile, session lineage, pipeline version, and routed
blockers as they become available for a run.
