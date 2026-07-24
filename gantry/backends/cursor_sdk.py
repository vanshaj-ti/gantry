"""Local Cursor Python SDK backend."""
from __future__ import annotations

import dataclasses
import importlib
import importlib.util
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any

from gantry.backends.protocol import (
    BackendCapabilities,
    InvocationResult,
    InvocationSpec,
    SessionRef,
)


def is_cursor_sdk_available() -> bool:
    """Return whether the optional Cursor SDK package can be imported."""
    try:
        return importlib.util.find_spec("cursor_sdk") is not None
    except (ImportError, ValueError):
        return "cursor_sdk" in sys.modules


def diagnose_cursor_sdk() -> dict[str, Any]:
    """Report package and authentication readiness without importing the SDK."""
    diagnosis: dict[str, Any] = {
        "package_available": is_cursor_sdk_available(),
        "api_key_present": bool(os.environ.get("CURSOR_API_KEY")),
    }
    if not diagnosis["package_available"]:
        diagnosis["import_error"] = (
            "cursor-sdk is not installed; install gantry with cursor-sdk>=1.0.24,<2"
        )
    return diagnosis


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "__dict__"):
        return {key: val for key, val in vars(value).items() if not key.startswith("_")}
    return {"repr": repr(value)}


def _value(source: Any, name: str, default: Any = None) -> Any:
    if isinstance(source, dict):
        return source.get(name, default)
    return getattr(source, name, default)


class CursorSdkBackend:
    name = "cursor-sdk"

    def __init__(self) -> None:
        self._active_run: Any = None
        self._lock = threading.Lock()

    @property
    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            resume=True,
            streaming=True,
            cancellation=True,
            plan_mode=False,
            max_turns=False,
            mcp_mode=True,
            proxy_support=False,
            usage_reporting=True,
            interactive=False,
            monetary_cost=False,
        )

    def invoke(self, spec: InvocationSpec) -> InvocationResult:
        try:
            sdk = importlib.import_module("cursor_sdk")
        except (ImportError, ModuleNotFoundError) as exc:
            raise ImportError(
                "Cursor SDK backend requires the 'cursor-sdk' package "
                "(cursor-sdk>=1.0.24,<2)."
            ) from exc

        api_key = spec.extras.get("api_key") or os.environ.get("CURSOR_API_KEY")
        # Local SDK requires an explicit model; empty gantry.toml model="" used to
        # mean "runner default" for CLIs but Agent.create rejects a blank model.
        model = (spec.model or "").strip() or os.environ.get(
            "GANTRY_CURSOR_SDK_MODEL", "composer-2.5",
        )
        agent = None
        run = None
        events_path = spec.extras.get("events_path")
        try:
            resume_id = None
            if spec.session is not None:
                resume_id = spec.session.agent_id or spec.session.session_id
            local_opts = sdk.LocalAgentOptions(cwd=str(spec.cwd))
            if resume_id:
                options = sdk.AgentOptions(
                    api_key=api_key,
                    model=model,
                    local=local_opts,
                )
                agent = sdk.Agent.resume(agent_id=resume_id, options=options)
            else:
                agent = sdk.Agent.create(
                    model=model,
                    api_key=api_key,
                    local=local_opts,
                )

            run = agent.send(spec.prompt)
            with self._lock:
                self._active_run = run

            holder: dict[str, Any] = {}

            def finish_run() -> None:
                try:
                    if events_path and callable(getattr(run, "events", None)):
                        path = Path(events_path)
                        path.parent.mkdir(parents=True, exist_ok=True)
                        with path.open("a", encoding="utf-8") as stream:
                            for event in run.events():
                                stream.write(json.dumps(_jsonable(event), default=str) + "\n")
                    holder["terminal"] = run.wait()
                except BaseException as exc:  # propagated on the invoking thread
                    holder["error"] = exc

            worker = threading.Thread(target=finish_run, daemon=True)
            worker.start()
            worker.join(max(0, spec.timeout))
            if worker.is_alive():
                self.cancel(spec.session)
                worker.join(1)
                return self._cancelled_result(agent, events_path, "Cursor SDK run timed out")
            if "error" in holder:
                raise holder["error"]

            terminal = holder.get("terminal") or run
            status = str(_value(terminal, "status", _value(run, "status", ""))).lower()
            cancelled = status in {"cancelled", "canceled"}
            text = _value(terminal, "result")
            if text is None:
                text_fn = getattr(run, "text", None)
                text = text_fn() if callable(text_fn) else ""
            text = str(text or "")
            usage_obj = _value(terminal, "usage", _value(run, "usage"))
            usage = {
                "cost_usd": None,
                "input_tokens": _value(usage_obj, "input_tokens"),
                "output_tokens": _value(usage_obj, "output_tokens"),
                "duration_ms": _value(usage_obj, "duration_ms"),
            }
            agent_id = getattr(agent, "agent_id", None)
            ok = not cancelled and status not in {"failed", "error"}
            raw = {
                "type": "result",
                "is_error": not ok,
                "result": text,
                "status": status,
                "session_id": agent_id,
            }
            return InvocationResult(
                ok=ok,
                session_id=agent_id,
                raw=raw,
                stdout=text,
                stderr="",
                exit_code=0 if ok else 1,
                usage=usage,
                cancelled=cancelled,
                events_path=str(events_path) if events_path else None,
                backend=self.name,
                agent_id=agent_id,
            )
        finally:
            with self._lock:
                if self._active_run is run:
                    self._active_run = None
            if agent is not None:
                close = getattr(agent, "close", None)
                if callable(close):
                    close()

    def _cancelled_result(
        self, agent: Any, events_path: str | None, message: str
    ) -> InvocationResult:
        agent_id = getattr(agent, "agent_id", None)
        return InvocationResult(
            ok=False,
            session_id=agent_id,
            raw={"type": "result", "is_error": True, "result": "", "status": "cancelled"},
            stdout="",
            stderr=message,
            exit_code=1,
            cancelled=True,
            events_path=str(events_path) if events_path else None,
            backend=self.name,
            agent_id=agent_id,
        )

    def cancel(self, session: SessionRef | None = None) -> bool:
        with self._lock:
            run = self._active_run
        cancel = getattr(run, "cancel", None)
        if not callable(cancel):
            return False
        cancel()
        return True

    def interactive_command(self, *, skip_permissions: bool = True) -> list[str]:
        raise NotImplementedError("cursor-sdk is non-interactive; use cursor-cli for cockpit")
