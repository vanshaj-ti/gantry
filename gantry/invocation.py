"""Central lifecycle for every non-interactive agent invocation."""
from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .backends.protocol import BackendCapabilities, InvocationResult, InvocationSpec, SessionRef
from .config import GantryConfig
from .cost import accumulate
from .mcp import ensure_mcp_for_stage
from .profiles import AgentProfile, profile_for, role_for_stage, snapshot_profile
from .redact import proxy_secrets, redact_secrets
from .runners import resolve_proxy_env
from .sessions import resolve_resume_session_id, save_session_record
from .state import RunStore, now_iso

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL = 20.0
CANCEL_POLL_INTERVAL = 0.25

# Review axes invoke concurrently but sessions.json and cost.json are
# read-modify-write files. Keep those short persistence sections atomic while
# leaving the backend calls themselves fully parallel.
_PERSIST_LOCK = threading.RLock()


@dataclass
class InvocationRequest:
    cfg: GantryConfig
    stage: str
    cwd: Path
    prompt: str
    store: RunStore | None = None
    run_id: str | None = None
    role: str | None = None
    session_key: str | None = None
    mcp_stage: str | None = None
    log_prefix: str | None = None
    prompt_name: str | None = None
    result_name: str | None = None
    resume: bool = False
    resume_existing: bool = False
    output_format: str | None = None
    plan_mode: bool = False
    prepend_profile_preamble: bool = False
    session_name: str | None = None
    start_status: str | None = None
    failure_status: str | None = None
    current_stage: str | None = None
    heartbeat_interval: float | None = None
    backend_resolver: Callable[[str], Any] | None = None
    prompt_factory: Callable[[AgentProfile], str] | None = None


@dataclass
class InvocationOutcome:
    result: InvocationResult
    profile: AgentProfile
    backend_name: str
    timed_out: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.result.ok

    @property
    def cancelled(self) -> bool:
        return self.result.cancelled

    @property
    def session_id(self) -> str | None:
        return self.result.session_id

    @property
    def usage(self) -> dict[str, Any]:
        return self.result.usage


def _resolve_profile(request: InvocationRequest) -> AgentProfile:
    role = request.role or role_for_stage(request.stage)
    return profile_for(role, request.cfg, stage=request.stage)


def _resolve_backend(request: InvocationRequest, profile: AgentProfile) -> Any:
    if request.backend_resolver is not None:
        return request.backend_resolver(profile.backend)
    from .backends.registry import get_execution_runner

    return get_execution_runner(profile.backend)


def _capabilities(backend: Any) -> BackendCapabilities:
    caps = getattr(backend, "capabilities", None)
    return caps if isinstance(caps, BackendCapabilities) else BackendCapabilities()


def _backend_name(backend: Any, profile: AgentProfile) -> str:
    return str(getattr(backend, "name", "") or profile.backend)


def _normalize_result(value: Any, backend_name: str) -> InvocationResult:
    if isinstance(value, InvocationResult):
        if not value.backend:
            value.backend = backend_name
        return value
    session_id = getattr(value, "session_id", None)
    if session_id is not None and not isinstance(session_id, (str, int)):
        session_id = None
    elif session_id is not None:
        session_id = str(session_id)
    usage = getattr(value, "usage", None)
    if not isinstance(usage, dict):
        usage = {}
    raw = getattr(value, "raw", None)
    exit_code = getattr(value, "exit_code", 0)
    try:
        exit_code_i = int(exit_code)
    except (TypeError, ValueError):
        exit_code_i = 1
    return InvocationResult(
        ok=bool(getattr(value, "ok", False)),
        session_id=session_id,
        raw=raw if isinstance(raw, dict) else {},
        stdout=str(getattr(value, "stdout", None) or ""),
        stderr=str(getattr(value, "stderr", None) or ""),
        exit_code=exit_code_i,
        usage={
            "cost_usd": usage.get("cost_usd"),
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "duration_ms": usage.get("duration_ms"),
        },
        backend=backend_name,
        agent_id=session_id,
    )


def _error_result(
    backend_name: str,
    message: str,
    *,
    cancelled: bool = False,
) -> InvocationResult:
    return InvocationResult(
        ok=False,
        session_id=None,
        raw={
            "type": "result",
            "is_error": True,
            "result": "",
            "status": "cancelled" if cancelled else "error",
        },
        stdout="",
        stderr=message,
        exit_code=1,
        usage={
            "cost_usd": None,
            "input_tokens": None,
            "output_tokens": None,
            "duration_ms": None,
        },
        cancelled=cancelled,
        backend=backend_name,
    )


def _invoke_backend(backend: Any, spec: InvocationSpec) -> InvocationResult:
    # Prefer invoke only for real AgentBackend instances. MagicMock runners
    # used in tests expose a callable .invoke that must not win over .run.
    caps = getattr(backend, "capabilities", None)
    if isinstance(caps, BackendCapabilities) and callable(getattr(backend, "invoke", None)):
        return _normalize_result(backend.invoke(spec), getattr(backend, "name", ""))
    result = backend.run(
        cwd=spec.cwd,
        prompt=spec.prompt,
        model=spec.model,
        session_id=spec.session.session_id if spec.session else None,
        plan_mode=spec.plan_mode,
        skip_permissions=spec.skip_permissions,
        output_format=spec.output_format,
        session_name=spec.session_name,
        max_turns=spec.max_turns,
        timeout=spec.timeout,
        env=spec.env,
        proxy=spec.proxy,
    )
    return _normalize_result(result, getattr(backend, "name", ""))


def _start_monitor(
    request: InvocationRequest,
    backend: Any,
    session: SessionRef | None,
) -> tuple[threading.Event, threading.Thread] | None:
    if request.store is None or request.run_id is None:
        return None
    stop = threading.Event()
    interval = request.heartbeat_interval or HEARTBEAT_INTERVAL
    capabilities = _capabilities(backend)

    def monitor() -> None:
        next_heartbeat = time.monotonic() + interval
        cancel_sent = False
        while not stop.wait(CANCEL_POLL_INTERVAL):
            state = request.store.state(request.run_id)
            if state.get("status") == "cancelled" and not cancel_sent:
                cancel_sent = True
                if capabilities.cancellation:
                    try:
                        backend.cancel(session)
                    except Exception:
                        logger.debug("cooperative backend cancellation failed", exc_info=True)
            if time.monotonic() >= next_heartbeat:
                # Never let a heartbeat revive or replace a cancellation; this
                # update only adds the timestamp to the latest state snapshot.
                request.store.update_state(request.run_id, heartbeat_at=now_iso())
                next_heartbeat = time.monotonic() + interval

    thread = threading.Thread(target=monitor, daemon=True)
    thread.start()
    return stop, thread


def _stop_monitor(monitor: tuple[threading.Event, threading.Thread] | None) -> None:
    if monitor is None:
        return
    stop, thread = monitor
    stop.set()
    thread.join(timeout=1)


def _has_usage(usage: dict[str, Any]) -> bool:
    return any(usage.get(key) is not None for key in (
        "cost_usd", "input_tokens", "output_tokens", "duration_ms"
    ))


def invoke(request: InvocationRequest) -> InvocationOutcome:
    """Resolve policy and execute one complete invocation lifecycle."""
    profile = _resolve_profile(request)
    prompt = request.prompt_factory(profile) if request.prompt_factory else request.prompt
    if request.prepend_profile_preamble and profile.prompt_preamble:
        prompt = f"{profile.prompt_preamble}\n\n{prompt}"
    backend = _resolve_backend(request, profile)
    backend_name = _backend_name(backend, profile)
    session_key = request.session_key or request.stage
    log_prefix = request.log_prefix or request.stage.replace("_", "-")
    result_name = request.result_name or f"{log_prefix}-result.json"
    store = request.store
    run_id = request.run_id
    worktree_id = str(request.cwd.resolve())

    if (store is None) != (run_id is None):
        raise ValueError("store and run_id must be provided together")
    if store is not None and not store.exists(run_id):
        raise ValueError(f"Run not found: {run_id}")

    if store is not None:
        store.write_log(
            run_id,
            request.prompt_name or f"{log_prefix}-prompt.md",
            prompt,
        )
        store.write_log(
            run_id,
            f"{log_prefix}-profile.json",
            json.dumps(snapshot_profile(profile), indent=2),
        )

    resume_session_id = None
    if request.resume_existing and store is not None:
        # Compatibility path for long-lived review sessions. Session lookup
        # still stays inside this lifecycle and all writes use sessions.py.
        with _PERSIST_LOCK:
            resume_session_id = store.get_session_id(run_id, session_key)
    elif request.resume and store is not None:
        with _PERSIST_LOCK:
            decision = resolve_resume_session_id(
                store,
                run_id,
                session_key,
                backend=backend_name,
                profile=profile.role,
                model=profile.model,
                worktree_id=worktree_id,
            )
        if decision.allowed:
            resume_session_id = decision.session_id
        elif not decision.fallback_to_artifacts:
            raise ValueError(f"No stored session for {run_id}/{session_key}; cannot resume")

    mcp_results = ensure_mcp_for_stage(
        request.cfg,
        request.mcp_stage or request.stage,
        backend_name,
        request.cwd,
        profile=profile,
    )
    if mcp_results and store is not None:
        store.write_log(run_id, f"{log_prefix}-mcp.json", json.dumps(mcp_results, indent=2))

    if store is not None:
        if request.start_status:
            store.update_state(
                run_id,
                status=request.start_status,
                current_stage=request.current_stage or request.stage,
                heartbeat_at=now_iso(),
                resumed=request.resume,
            )
        with _PERSIST_LOCK:
            save_session_record(
                store,
                run_id,
                session_key,
                model=profile.model,
                runner=backend_name,
                profile=profile.role,
                profile_version=str(profile.version),
                worktree_id=worktree_id,
            )

    proxy = request.cfg.proxy.get(backend_name)
    session = (
        SessionRef(session_id=resume_session_id, backend=backend_name, agent_id=resume_session_id)
        if resume_session_id
        else None
    )
    events_path = None
    if store is not None:
        events_path = str(store.run_dir(run_id) / "logs" / f"{log_prefix}.events.jsonl")
    spec = InvocationSpec(
        cwd=request.cwd,
        prompt=prompt,
        model=profile.model,
        session=session,
        plan_mode=request.plan_mode and _capabilities(backend).plan_mode,
        skip_permissions=profile.permissions == "allow",
        output_format=request.output_format or request.cfg.agent.output_format,
        session_name=request.session_name or (
            f"{run_id}-{session_key}" if run_id else session_key
        ),
        max_turns=profile.turn_budget,
        timeout=profile.timeout,
        env=resolve_proxy_env(backend_name, proxy),
        proxy=proxy,
        extras={
            "skills": list(profile.skills),
            "mcp": list(profile.mcp),
            "setting_sources": list(profile.setting_sources),
            "sandbox": profile.sandbox,
            "events_path": events_path,
        },
    )

    timed_out = False
    error = None
    monitor = _start_monitor(request, backend, session)
    try:
        try:
            result = _invoke_backend(backend, spec)
        except subprocess.TimeoutExpired:
            timed_out = True
            error = "timeout"
            result = _error_result(
                backend_name, f"Agent subprocess timed out after {profile.timeout}s"
            )
        except Exception as exc:
            error = "startup"
            result = _error_result(backend_name, f"Agent invocation failed: {exc}")
    finally:
        _stop_monitor(monitor)

    # Cancellation can race with a successful terminal response. The durable
    # run state is authoritative: once cancelled, no caller may promote it.
    if store is not None and store.state(run_id).get("status") == "cancelled":
        result.ok = False
        result.cancelled = True
        if not result.stderr:
            result.stderr = "Invocation cancelled"
        error = "cancelled"

    if store is not None:
        secrets = proxy_secrets(request.cfg)
        suffix = ".resume" if request.resume else ""
        store.write_log(
            run_id,
            f"{log_prefix}{suffix}.stdout",
            redact_secrets(result.stdout, extra_secrets=secrets),
        )
        store.write_log(
            run_id,
            f"{log_prefix}{suffix}.stderr",
            redact_secrets(result.stderr, extra_secrets=secrets),
        )
        store.write_result(run_id, result_name, result.raw)
        with _PERSIST_LOCK:
            save_session_record(
                store,
                run_id,
                session_key,
                session_id=result.session_id,
                model=profile.model,
                runner=backend_name,
                profile=profile.role,
                profile_version=str(profile.version),
                worktree_id=worktree_id,
                backend_agent_id=result.agent_id or result.session_id,
                terminal_status="cancelled" if result.cancelled else ("ok" if result.ok else "error"),
            )
            if _has_usage(result.usage):
                accumulate(
                    store,
                    run_id,
                    session_key,
                    result.usage,
                    runner=backend_name,
                    session_id=result.session_id,
                )
        if not result.ok and not result.cancelled and request.failure_status:
            store.update_state(run_id, status=request.failure_status)

    return InvocationOutcome(
        result=result,
        profile=profile,
        backend_name=backend_name,
        timed_out=timed_out,
        error=error,
    )
