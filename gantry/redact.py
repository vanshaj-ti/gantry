"""Secret redaction for anything gantry writes to disk or stdout/stderr.

gantry holds several credential-shaped values with its own ambient
privileges: `GH_TOKEN`/`TFY_API_KEY` (see docker.py), and per-runner
`[proxy.<runner>]` `api_key_env`-resolved tokens plus `headers` values (see
config.ProxyConfig, runners.resolve_proxy_env). None of these are ever
*written* deliberately anywhere gantry persists state — but a subprocess
gantry shells out to (an agent runner CLI, a repo check command) can echo
its own invocation args or quote a failed auth header in its stdout/stderr,
and that text gets persisted verbatim to a `.stderr`/`.stdout` log file in
the run directory (see engine.py/review.py's `store.write_log` calls) or
surfaces in an exception message printed by cli/__init__.py's top-level
handler. This is a low-probability but real leak vector.

Deliberately simple: literal substring replacement of known secret VALUES,
not regex/URL-based credential detection (gantry doesn't store credentialled
remote URLs the way some tools do, so that heavier machinery isn't needed
here).
"""
from __future__ import annotations

import os

REDACTED = "***REDACTED***"

# Env vars whose VALUES are always secret-shaped, if present in this process's
# environment, regardless of any per-run proxy config.
_ALWAYS_SENSITIVE_ENV_VARS = ("GH_TOKEN", "TFY_API_KEY")

# Trivially short "secrets" (empty string, a single char) would redact far too
# aggressively if ever accidentally collected — never treat anything shorter
# than this as a real secret value.
_MIN_SECRET_LEN = 6


def known_secrets(extra_env_vars: list[str] | None = None) -> list[str]:
    """Collect the current process's known-sensitive env var VALUES:
    GH_TOKEN, TFY_API_KEY, plus any `extra_env_vars` names the caller wants
    resolved too (e.g. a configured `[proxy.<runner>].api_key_env` name).
    Returns literal secret values (never the var names themselves) — empty
    or unset vars are silently skipped."""
    names = list(_ALWAYS_SENSITIVE_ENV_VARS) + list(extra_env_vars or [])
    values = []
    for name in names:
        val = os.environ.get(name)
        if val and len(val) >= _MIN_SECRET_LEN:
            values.append(val)
    return values


def proxy_secrets(cfg) -> list[str]:
    """Every proxy-related secret value reachable from a GantryConfig: each
    configured runner's `api_key_env`-resolved value, plus every literal
    `headers` value (header values are typically the token itself, e.g.
    `Authorization: Bearer <token>`)."""
    secrets: list[str] = []
    for proxy in (cfg.proxy or {}).values():
        if proxy.api_key_env:
            secrets.extend(known_secrets([proxy.api_key_env]))
        secrets.extend(v for v in proxy.headers.values() if v and len(v) >= _MIN_SECRET_LEN)
    return secrets


def redact_secrets(text: str, extra_secrets: list[str] | None = None) -> str:
    """Replace every literal occurrence of a known-sensitive value in `text`
    with a placeholder. `extra_secrets` are additional literal secret VALUES
    (not env var names) the caller already resolved — e.g. from
    `proxy_secrets(cfg)`. Always also checks the always-sensitive env vars
    (GH_TOKEN, TFY_API_KEY). Safe on empty/None text."""
    if not text:
        return text
    secrets = known_secrets() + list(extra_secrets or [])
    out = text
    for secret in secrets:
        if secret:
            out = out.replace(secret, REDACTED)
    return out
