"""Backend registry — capability-based lookup with legacy runner-name aliases."""
from __future__ import annotations

from gantry.backends.cli import CliAgentBackend, wrap_runner
from gantry.backends.protocol import AgentBackend, BackendCapabilities
from gantry.runners import (
    ClaudeCodeRunner,
    CodexRunner,
    CursorCliRunner,
    get_runner as get_legacy_runner,
)

# Registered backend factories keyed by canonical name.
_BACKENDS: dict[str, type] = {
    ClaudeCodeRunner.name: ClaudeCodeRunner,
    CursorCliRunner.name: CursorCliRunner,
    CodexRunner.name: CodexRunner,
}

# Ordered pre-start fallback candidates (Phase 2 adds cursor-sdk first).
DEFAULT_FALLBACK_ORDER: tuple[str, ...] = (
    "cursor-cli",
    "claude-code",
    "codex-cli",
)


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


def get_backend_or_runner(name: str) -> AgentBackend:
    """Preferred lookup: backend protocol. Falls back to wrapping get_runner."""
    try:
        return get_backend(name)
    except ValueError:
        return wrap_runner(get_legacy_runner(name))
