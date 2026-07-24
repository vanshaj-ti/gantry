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
    capabilities_for,
    get_backend,
    list_backends,
    register_backend,
)

__all__ = [
    "AgentBackend",
    "BackendCapabilities",
    "DEFAULT_FALLBACK_ORDER",
    "InvocationResult",
    "InvocationSpec",
    "SessionRef",
    "capabilities_for",
    "get_backend",
    "list_backends",
    "register_backend",
]
