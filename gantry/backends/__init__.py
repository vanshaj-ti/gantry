"""Agent backends — capability protocol, CLI wrappers, and registry."""

from gantry.backends.protocol import (
    AgentBackend,
    BackendCapabilities,
    InvocationResult,
    InvocationSpec,
    SessionRef,
)
from gantry.backends.registry import (
    DEFAULT_FALLBACK_ORDER,
    ResolvedBackend,
    capabilities_for,
    get_backend,
    get_execution_runner,
    list_backends,
    register_backend,
    resolve_backend,
)

__all__ = [
    "AgentBackend",
    "BackendCapabilities",
    "DEFAULT_FALLBACK_ORDER",
    "InvocationResult",
    "InvocationSpec",
    "ResolvedBackend",
    "SessionRef",
    "capabilities_for",
    "get_backend",
    "get_execution_runner",
    "list_backends",
    "register_backend",
    "resolve_backend",
]
