"""Backend registry — capability-based lookup with legacy runner-name aliases."""
from __future__ import annotations

import shutil
from dataclasses import dataclass

from gantry.backends.cli import CliAgentBackend, wrap_runner
from gantry.backends.cursor_sdk import CursorSdkBackend, diagnose_cursor_sdk
from gantry.backends.protocol import AgentBackend, BackendCapabilities
from gantry.runners import (
    ClaudeCodeRunner,
    CodexRunner,
    CursorCliRunner,
    get_runner as get_legacy_runner,
    runner_binary,
)

# Registered backend factories keyed by canonical name.
_BACKENDS: dict[str, type] = {
    CursorSdkBackend.name: CursorSdkBackend,
    ClaudeCodeRunner.name: ClaudeCodeRunner,
    CursorCliRunner.name: CursorCliRunner,
    CodexRunner.name: CodexRunner,
}

DEFAULT_FALLBACK_ORDER: tuple[str, ...] = (
    "cursor-sdk",
    "cursor-cli",
    "claude-code",
    "codex-cli",
)


@dataclass(frozen=True)
class ResolvedBackend:
    backend: AgentBackend
    resolved_name: str
    fallback_reason: str | None = None


class BackendRunnerAdapter:
    """Runner-shaped bridge for legacy orchestration call sites."""

    def __init__(self, resolved: ResolvedBackend):
        self._backend = resolved.backend
        self.name = resolved.resolved_name
        self.fallback_reason = resolved.fallback_reason

    def run(self, **kwargs):
        from gantry.backends.cli import invocation_from_runner_kwargs

        spec = invocation_from_runner_kwargs(backend=self.name, **kwargs)
        return self._backend.invoke(spec).to_runner_result()

    @property
    def capabilities(self) -> BackendCapabilities:
        return self._backend.capabilities

    def invoke(self, spec):
        return self._backend.invoke(spec)

    def cancel(self, session=None) -> bool:
        return self._backend.cancel(session)


def list_backends() -> list[str]:
    return sorted(_BACKENDS)


def register_backend(name: str, runner_cls: type) -> None:
    """Register or replace a backend factory (used by tests / future SDK)."""
    _BACKENDS[name] = runner_cls


def get_backend(name: str) -> AgentBackend:
    if name not in _BACKENDS:
        raise ValueError(f"Unknown agent backend: {name!r}. Available: {sorted(_BACKENDS)}")
    cls = _BACKENDS[name]
    # CLI runner classes are instantiated then wrapped; SDK backends may
    # register themselves as already satisfying AgentBackend.
    instance = cls()
    if isinstance(instance, CliAgentBackend):
        return instance
    if hasattr(instance, "invoke") and hasattr(instance, "capabilities"):
        return instance  # type: ignore[return-value]
    return wrap_runner(instance)


def capabilities_for(name: str) -> BackendCapabilities:
    return get_backend(name).capabilities


def resolve_backend(preferred: str, *, allow_fallback: bool = True) -> ResolvedBackend:
    """Resolve a ready backend before invocation; never switches after start."""
    if preferred != "cursor-sdk":
        return ResolvedBackend(get_backend(preferred), preferred)

    diagnosis = diagnose_cursor_sdk()
    if diagnosis["package_available"] and diagnosis["api_key_present"]:
        return ResolvedBackend(get_backend(preferred), preferred)

    reasons = []
    if not diagnosis["package_available"]:
        reasons.append(str(diagnosis.get("import_error") or "cursor-sdk package unavailable"))
    if not diagnosis["api_key_present"]:
        reasons.append("CURSOR_API_KEY is not set")
    reason = "; ".join(reasons)
    if not allow_fallback:
        raise RuntimeError(f"cursor-sdk is unavailable: {reason}")

    for name in DEFAULT_FALLBACK_ORDER:
        if name == "cursor-sdk":
            continue
        if shutil.which(runner_binary(name)):
            return ResolvedBackend(get_backend(name), name, reason)
    raise RuntimeError(f"cursor-sdk is unavailable and no fallback runner is installed: {reason}")


def get_execution_runner(name: str, *, allow_fallback: bool = True) -> BackendRunnerAdapter:
    """Resolve a backend and expose the legacy ``run(**kwargs)`` facade."""
    return BackendRunnerAdapter(resolve_backend(name, allow_fallback=allow_fallback))


def get_backend_or_runner(name: str) -> AgentBackend:
    """Preferred lookup: backend protocol. Falls back to wrapping get_runner."""
    try:
        return get_backend(name)
    except ValueError:
        return wrap_runner(get_legacy_runner(name))
