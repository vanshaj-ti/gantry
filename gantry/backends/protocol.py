"""Capability-based agent backend protocol.

Backends advertise what they support; orchestration branches on capabilities
rather than backend names. CLI runners from gantry.runners remain the
compatibility facades for existing call sites.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from gantry.config import ProxyConfig


@dataclass(frozen=True)
class BackendCapabilities:
    """Advertised features for a backend implementation."""

    resume: bool = False
    streaming: bool = False
    cancellation: bool = False
    plan_mode: bool = False
    max_turns: bool = False
    mcp_mode: bool = False
    proxy_support: bool = False
    usage_reporting: bool = False
    interactive: bool = False
    monetary_cost: bool = False


@dataclass
class SessionRef:
    """Opaque backend session / agent identity for resume."""

    session_id: str | None = None
    backend: str = ""
    agent_id: str | None = None
    run_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class InvocationSpec:
    """Normalized request to invoke an agent backend."""

    cwd: Path
    prompt: str
    model: str = ""
    session: SessionRef | None = None
    plan_mode: bool = False
    skip_permissions: bool = True
    output_format: str = "json"
    session_name: str = "gantry"
    max_turns: int = 60
    timeout: int = 900
    env: dict[str, str] | None = None
    proxy: ProxyConfig | None = None
    # Opaque extras for SDK backends (skills, MCP subset, sandbox, etc.)
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class InvocationResult:
    """Normalized terminal result shared across CLI and SDK backends."""

    ok: bool
    session_id: str | None
    raw: dict[str, Any]
    stdout: str
    stderr: str
    exit_code: int
    usage: dict[str, Any] = field(default_factory=lambda: {
        "cost_usd": None, "input_tokens": None, "output_tokens": None, "duration_ms": None,
    })
    cancelled: bool = False
    events_path: str | None = None
    backend: str = ""
    agent_id: str | None = None

    def to_runner_result(self):
        """Bridge to legacy RunnerResult for call sites not yet migrated."""
        from gantry.runners import RunnerResult
        return RunnerResult(
            ok=self.ok,
            session_id=self.session_id,
            raw=self.raw,
            stdout=self.stdout,
            stderr=self.stderr,
            exit_code=self.exit_code,
            usage=self.usage,
        )


@runtime_checkable
class AgentBackend(Protocol):
    """Protocol every agent backend must satisfy."""

    name: str

    @property
    def capabilities(self) -> BackendCapabilities: ...

    def invoke(self, spec: InvocationSpec) -> InvocationResult: ...

    def cancel(self, session: SessionRef | None = None) -> bool:
        """Cooperative cancel when supported. Returns True if cancel was signaled."""
        ...

    def interactive_command(self, *, skip_permissions: bool = True) -> list[str]:
        """argv for a live TUI session, if capabilities.interactive."""
        ...
