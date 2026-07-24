# Cursor SDK Compatibility Notes

Gantry's primary agent driver is the official `cursor-sdk` Python package
(local runtime). This document records the **documented** SDK surface Gantry
may depend on. Undocumented sandbox / skill-loading behavior is **not** assumed
until credential-gated smoke tests pass.

## Package

| Item | Value |
|------|-------|
| PyPI package | `cursor-sdk` |
| Import | `cursor_sdk` |
| Compatible range (target) | `>=1.0.24,<2` |
| Python floor | 3.10+ (Gantry keeps `>=3.11`) |
| Auth | `CURSOR_API_KEY` or `api_key=` on create |
| Docs | https://cursor.com/docs/sdk/python |

## Documented assumptions Gantry relies on

1. **Local cwd** — `Agent.create(..., local=LocalAgentOptions(cwd=...))` runs
   against an explicit working directory (Gantry worktree path).
2. **Model selection** — `model=` accepts a model id string (e.g. `composer-2.5`);
   `agent.model.id` is readable after create.
3. **Agent identity** — local agents expose `agent.agent_id` (typically
   `agent-...`); used as the durable session/resume key.
4. **Resume** — `Agent.resume(...)` (or client equivalent) restores conversation
   state after process restart when the store/workspace identity matches.
5. **Send + wait** — `agent.send(prompt)` returns a run handle; callers wait for
   terminal status and read assistant text / stream events.
6. **Cancellation** — runs expose cooperative cancel; Gantry maps this to
   `gantry cancel` when the backend advertises cancellation.
7. **Token usage** — SDK documents per-run token counts; **no monetary
   `cost_usd` field** is documented. Gantry must leave monetary cost unknown
   (never invent zero or model-claimed dollars).
8. **Stream events** — typed stream messages (`SDKMessage`) are iterated during
   a run; unstable tool payloads are persisted as opaque NDJSON envelopes.
9. **Resources** — agent/client handles are context-managed; Gantry must dispose
   them even on timeout/cancel paths.
10. **Cloud optional** — `cloud=CloudAgentOptions(...)` is a later optional
    backend; local is the default for Gantry-managed worktrees.

## Explicit non-assumptions (until smoke suite passes)

- Undocumented Python sandbox construction details.
- Automatic project/user/team/plugin settings merge unless a profile requests it.
- Skill/plugin discovery paths beyond what the smoke suite verifies.
- Silent cross-backend resume (never replay a partially mutating run on another
  backend).

## Live smoke gate

Live tests in `tests/test_cursor_sdk_smoke.py` run only when:

```bash
export GANTRY_CURSOR_SDK_LIVE=1
export CURSOR_API_KEY=...
# optional model override:
# export GANTRY_CURSOR_SDK_MODEL=composer-2.5
python -m unittest tests.test_cursor_sdk_smoke.TestCursorSdkLiveSmoke -v
```

Or via doctor:

```bash
gantry doctor --live-sdk-smoke
```

Ordinary CI runs mocked contract tests only. A GitHub Actions
`workflow_dispatch` job (`sdk-smoke`) can run the live suite when
`CURSOR_API_KEY` is configured as a repository secret.

Live coverage includes: create/send/dispose, resume round-trip, cancel
(without inventing `cost_usd`), and `CursorSdkBackend.invoke` against a
temp directory.

## Fallback order (pre-start only)

1. `cursor-sdk` (primary)
2. `cursor-cli`
3. `claude-code`
4. `codex-cli`

Fallback is allowed **only before** an invocation starts. Never auto-fallback
after a mutating invocation has begun.
