"""CLI adapters that wrap existing gantry.runners without behavior changes."""
from __future__ import annotations

from gantry.backends.protocol import (
    AgentBackend,
    BackendCapabilities,
    InvocationResult,
    InvocationSpec,
    SessionRef,
)
from gantry.runners import (
    ClaudeCodeRunner,
    CodexRunner,
    CursorCliRunner,
    AgentRunner,
)


def _caps_for(runner: AgentRunner) -> BackendCapabilities:
    if isinstance(runner, ClaudeCodeRunner):
        return BackendCapabilities(
            resume=True,
            streaming=False,
            cancellation=False,
            plan_mode=False,  # prompt-driven only
            max_turns=True,
            mcp_mode=True,
            proxy_support=True,
            usage_reporting=True,
            interactive=True,
            monetary_cost=True,  # Claude JSON often includes total_cost_usd
        )
    if isinstance(runner, CursorCliRunner):
        return BackendCapabilities(
            resume=True,
            streaming=False,
            cancellation=False,
            plan_mode=True,
            max_turns=False,
            mcp_mode=True,
            proxy_support=False,
            usage_reporting=True,
            interactive=True,
            monetary_cost=False,
        )
    if isinstance(runner, CodexRunner):
        return BackendCapabilities(
            resume=True,
            streaming=True,  # JSONL event stream
            cancellation=False,
            plan_mode=False,
            max_turns=False,
            mcp_mode=True,
            proxy_support=True,
            usage_reporting=True,
            interactive=True,
            monetary_cost=False,
        )
    return BackendCapabilities()


class CliAgentBackend:
    """Thin AgentBackend over an AgentRunner — exact argv/parse/proxy behavior."""

    def __init__(self, runner: AgentRunner):
        self._runner = runner
        self.name = runner.name
        self._capabilities = _caps_for(runner)

    @property
    def capabilities(self) -> BackendCapabilities:
        return self._capabilities

    @property
    def runner(self) -> AgentRunner:
        return self._runner

    def invoke(self, spec: InvocationSpec) -> InvocationResult:
        session_id = None
        if spec.session is not None:
            session_id = spec.session.session_id
        result = self._runner.run(
            cwd=spec.cwd,
            prompt=spec.prompt,
            model=spec.model,
            session_id=session_id,
            plan_mode=spec.plan_mode,
            skip_permissions=spec.skip_permissions,
            output_format=spec.output_format,
            session_name=spec.session_name,
            max_turns=spec.max_turns,
            timeout=spec.timeout,
            env=spec.env,
            proxy=spec.proxy,
        )
        return InvocationResult(
            ok=result.ok,
            session_id=result.session_id,
            raw=result.raw,
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.exit_code,
            usage=result.usage,
            backend=self.name,
            agent_id=result.session_id,
        )

    def cancel(self, session: SessionRef | None = None) -> bool:
        # CLI runners have no cooperative cancel API today.
        return False

    def interactive_command(self, *, skip_permissions: bool = True) -> list[str]:
        return self._runner.interactive_command(skip_permissions=skip_permissions)


def wrap_runner(runner: AgentRunner) -> AgentBackend:
    return CliAgentBackend(runner)


def invocation_from_runner_kwargs(
    *,
    cwd,
    prompt: str,
    model: str,
    session_id: str | None = None,
    plan_mode: bool = False,
    skip_permissions: bool = True,
    output_format: str = "json",
    session_name: str = "gantry",
    max_turns: int = 60,
    timeout: int = 900,
    env: dict | None = None,
    proxy=None,
    backend: str = "",
) -> InvocationSpec:
    """Build an InvocationSpec from legacy AgentRunner.run kwargs."""
    session = SessionRef(session_id=session_id, backend=backend) if session_id else None
    return InvocationSpec(
        cwd=cwd,
        prompt=prompt,
        model=model,
        session=session,
        plan_mode=plan_mode,
        skip_permissions=skip_permissions,
        output_format=output_format,
        session_name=session_name,
        max_turns=max_turns,
        timeout=timeout,
        env=env,
        proxy=proxy,
    )
