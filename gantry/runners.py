"""Agent runner adapters.

An AgentRunner turns a stage invocation (prompt, model, session, plan-mode,
skip-permissions) into a concrete CLI command for a specific agent tool, runs
it, and parses the JSON result into a normalized RunnerResult.

Three runners ship: claude-code, cursor-cli, and codex-cli. claude-code and
cursor-cli emit a single JSON blob on stdout and share the base `parse()`.
codex-cli emits JSONL (one event per line) and overrides `run()` with its own
parser. Add a runner by subclassing AgentRunner and registering it in _RUNNERS.
"""
from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import ProxyConfig

logger = logging.getLogger(__name__)

# Stable model_provider id used for codex's per-invocation `-c` proxy
# overrides — must match between the model_providers.<id>.* overrides and
# the `--config model_provider=<id>` / provider-selection override so codex
# actually routes through the overridden provider entry.
CODEX_PROXY_PROVIDER_ID = "gantry-proxy"


@dataclass
class RunnerResult:
    ok: bool
    session_id: str | None
    raw: dict[str, Any]
    stdout: str
    stderr: str
    exit_code: int
    usage: dict[str, Any] = field(default_factory=lambda: {
        "cost_usd": None, "input_tokens": None, "output_tokens": None, "duration_ms": None,
    })


class AgentRunner:
    """Base interface. Subclasses map the normalized args to their CLI flags."""

    name: str = "base"
    # argv[0] of build_command / interactive_command — used by doctor/availability.
    binary: str = "base"

    def build_command(
        self,
        *,
        prompt: str,
        model: str,
        session_id: str | None,
        plan_mode: bool,
        skip_permissions: bool,
        output_format: str,
        session_name: str,
        max_turns: int,
        proxy: ProxyConfig | None = None,
    ) -> list[str]:
        raise NotImplementedError

    def interactive_command(self, *, skip_permissions: bool = True) -> list[str]:
        """argv for a live TUI session (cockpit / herdr assistant pane)."""
        raise NotImplementedError

    def parse(self, stdout: str, stderr: str, exit_code: int) -> RunnerResult:
        """Shared JSON result parsing. Both runners emit `--output-format json`."""
        try:
            data = json.loads(stdout)
        except Exception:
            data = {
                "type": "result",
                "subtype": "invalid_json",
                "is_error": True,
                "result": stdout[:4000],
                "stderr": stderr[:4000],
                "exit_code": exit_code,
            }
        is_error = bool(data.get("is_error")) or exit_code != 0
        from .cost import extract_usage
        return RunnerResult(
            ok=not is_error,
            session_id=data.get("session_id"),
            raw=data,
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            usage=extract_usage(data),
        )

    def run(
        self,
        *,
        cwd: Path,
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
        proxy: ProxyConfig | None = None,
    ) -> RunnerResult:
        cmd = self.build_command(
            prompt=prompt,
            model=model,
            session_id=session_id,
            plan_mode=plan_mode,
            skip_permissions=skip_permissions,
            output_format=output_format,
            session_name=session_name,
            max_turns=max_turns,
            proxy=proxy,
        )
        run_kwargs: dict[str, Any] = dict(cwd=str(cwd), capture_output=True, text=True, timeout=timeout)
        if env is not None:
            run_kwargs["env"] = env
        proc = subprocess.run(cmd, **run_kwargs)
        return self.parse(proc.stdout, proc.stderr, proc.returncode)


class ClaudeCodeRunner(AgentRunner):
    name = "claude-code"
    binary = "claude"

    def build_command(self, *, prompt, model, session_id, plan_mode, skip_permissions,
                       output_format, session_name, max_turns, proxy: ProxyConfig | None = None) -> list[str]:
        cmd = [self.binary, "-p", prompt, "--name", session_name]
        if model:
            cmd += ["--model", model]
        cmd += ["--output-format", output_format, "--max-turns", str(max_turns)]
        # Claude Code expresses plan mode via a plan agent; we keep it simple and
        # let the prompt drive planning. skip_permissions is the headless unlock.
        if skip_permissions:
            cmd += ["--dangerously-skip-permissions"]
        if session_id:
            cmd += ["--resume", session_id]
        return cmd

    def interactive_command(self, *, skip_permissions: bool = True) -> list[str]:
        cmd = [self.binary]
        if skip_permissions:
            cmd += ["--dangerously-skip-permissions"]
        return cmd


class CursorCliRunner(AgentRunner):
    name = "cursor-cli"
    binary = "cursor-agent"

    def build_command(self, *, prompt, model, session_id, plan_mode, skip_permissions,
                       output_format, session_name, max_turns, proxy: ProxyConfig | None = None) -> list[str]:
        # cursor-agent -p <prompt> --output-format json --model <m> [--plan] [-f] [--resume <id>]
        # proxy is intentionally unused here — cursor-cli has no verified
        # base-url/headers override mechanism (see config.ProxyConfig, _coerce_proxy).
        cmd = [self.binary, "-p", prompt]
        if model:
            cmd += ["--model", model]
        cmd += ["--output-format", output_format]
        if plan_mode:
            cmd += ["--plan"]
        if skip_permissions:
            cmd += ["-f"]  # force-allow (equivalent to claude's skip-permissions)
        if session_id:
            cmd += ["--resume", session_id]
        # cursor-agent has no --max-turns / --name; those are no-ops here.
        return cmd

    def interactive_command(self, *, skip_permissions: bool = True) -> list[str]:
        cmd = [self.binary]
        if skip_permissions:
            cmd += ["-f"]
        return cmd


class CodexRunner(AgentRunner):
    """codex CLI (OpenAI, ChatGPT-auth). Unlike the other two runners, `codex
    exec --json` streams JSONL events rather than a single JSON blob, so this
    overrides `run()` with its own parser instead of reusing the base one."""
    name = "codex-cli"
    binary = "codex"

    def build_command(self, *, prompt, model, session_id, plan_mode, skip_permissions,
                       output_format, session_name, max_turns, proxy: ProxyConfig | None = None) -> list[str]:
        # codex exec [resume <id>] --json -m <model> [--dangerously-bypass-approvals-and-sandbox] <prompt>
        if session_id:
            cmd = [self.binary, "exec", "resume", session_id]
        else:
            cmd = [self.binary, "exec"]
        cmd += ["--json", "--skip-git-repo-check"]
        if model:
            cmd += ["-m", model]
        if skip_permissions:
            cmd += ["--dangerously-bypass-approvals-and-sandbox"]
        cmd += self._proxy_config_args(proxy)
        # codex has no dedicated plan-mode flag or --max-turns/--name; let the
        # prompt drive planning, same approach ClaudeCodeRunner uses.
        cmd += [prompt]
        return cmd

    def interactive_command(self, *, skip_permissions: bool = True) -> list[str]:
        # Interactive TUI is bare `codex` (not `codex exec`). Sandbox bypass is
        # the same flag the headless path uses when skip_permissions is on.
        cmd = [self.binary]
        if skip_permissions:
            cmd += ["--dangerously-bypass-approvals-and-sandbox"]
        return cmd

    def _proxy_config_args(self, proxy: ProxyConfig | None) -> list[str]:
        """Per-invocation `-c` overrides for a proxy/gateway — project-local
        .codex/config.toml ignores [model_providers]/[model_provider] keys,
        so these can't be set via a config file; the dotted -c CLI flag is
        the only mechanism that reaches codex per-invocation. All overrides
        use CODEX_PROXY_PROVIDER_ID consistently so the `model_provider`
        selector below actually points at the provider entry these -c flags
        just defined."""
        if proxy is None or not (proxy.base_url or proxy.api_key_env or proxy.headers):
            return []
        pid = CODEX_PROXY_PROVIDER_ID
        args: list[str] = []
        if proxy.base_url:
            args += ["-c", f"model_providers.{pid}.base_url={proxy.base_url}"]
        if proxy.api_key_env:
            args += ["-c", f"model_providers.{pid}.env_key={proxy.api_key_env}"]
        for key, value in proxy.headers.items():
            args += ["-c", f"model_providers.{pid}.http_headers.{key}={value}"]
        if proxy.base_url or proxy.api_key_env:
            args += ["-c", f"model_provider={pid}"]
        return args

    def run(
        self,
        *,
        cwd: Path,
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
        proxy: ProxyConfig | None = None,
    ) -> RunnerResult:
        cmd = self.build_command(
            prompt=prompt, model=model, session_id=session_id, plan_mode=plan_mode,
            skip_permissions=skip_permissions, output_format=output_format,
            session_name=session_name, max_turns=max_turns, proxy=proxy,
        )
        run_kwargs: dict[str, Any] = dict(cwd=str(cwd), capture_output=True, text=True, timeout=timeout)
        if env is not None:
            run_kwargs["env"] = env
        proc = subprocess.run(cmd, **run_kwargs)
        return self._parse_jsonl(proc.stdout, proc.stderr, proc.returncode)

    def _parse_jsonl(self, stdout: str, stderr: str, exit_code: int) -> RunnerResult:
        thread_id: str | None = None
        last_message: str | None = None
        saw_turn_completed = False
        # codex exec --json emits usage on turn.completed as
        # {"type":"turn.completed","usage":{"input_tokens":...,"output_tokens":...}}
        # per its own event schema — no cost-in-USD field exists in the stream
        # (codex is ChatGPT-auth, not billed per-token via this CLI), so
        # cost_usd stays None here even when token counts are present.
        input_tokens: int | None = None
        output_tokens: int | None = None
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except Exception:
                logger.debug("skipping non-JSON codex-cli output line: %r", line)
                continue
            etype = event.get("type")
            if etype == "thread.started":
                thread_id = event.get("thread_id")
            elif etype == "item.completed":
                item = event.get("item") or {}
                if item.get("type") == "agent_message":
                    last_message = item.get("text")
            elif etype == "turn.completed":
                saw_turn_completed = True
                usage = event.get("usage") or {}
                input_tokens = usage.get("input_tokens", input_tokens)
                output_tokens = usage.get("output_tokens", output_tokens)

        is_error = exit_code != 0 or not saw_turn_completed or last_message is None
        raw = {
            "type": "result",
            "is_error": is_error,
            "result": last_message if last_message is not None else stdout[:4000],
            "session_id": thread_id,
            "exit_code": exit_code,
        }
        return RunnerResult(
            ok=not is_error,
            session_id=thread_id,
            raw=raw,
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            usage={"cost_usd": None, "input_tokens": input_tokens,
                   "output_tokens": output_tokens, "duration_ms": None},
        )


_RUNNERS: dict[str, type[AgentRunner]] = {
    ClaudeCodeRunner.name: ClaudeCodeRunner,
    CursorCliRunner.name: CursorCliRunner,
    CodexRunner.name: CodexRunner,
}


def get_runner(name: str) -> AgentRunner:
    """Compatibility facade — prefer gantry.backends.get_backend for new code.

    Returns the same AgentRunner instances (exact argv / parse / proxy behavior)
    that CliAgentBackend wraps. Names and registration stay in lockstep with
    gantry.backends.registry.
    """
    if name not in _RUNNERS:
        raise ValueError(f"Unknown agent runner: {name!r}. Available: {sorted(_RUNNERS)}")
    return _RUNNERS[name]()


def runner_binary(name: str) -> str:
    """PATH binary name for a registered runner (e.g. 'claude', 'codex')."""
    if name not in _RUNNERS:
        raise ValueError(f"Unknown agent runner: {name!r}. Available: {sorted(_RUNNERS)}")
    return _RUNNERS[name].binary


def interactive_command(name: str, *, skip_permissions: bool = True) -> list[str]:
    """argv for a live assistant session for the named runner."""
    return get_runner(name).interactive_command(skip_permissions=skip_permissions)


def resolve_proxy_env(runner_name: str, proxy: ProxyConfig | None) -> dict | None:
    """Build the subprocess env for a runner given its ProxyConfig (or None
    if unconfigured). Returns None when there's nothing to override, so
    callers can pass it straight through to AgentRunner.run(env=...) and get
    today's exact behavior (ambient env, untouched) when no [proxy.<runner>]
    section exists.

    - claude-code: sets ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN. `headers`
      has no verified passthrough for this CLI — configuring it only logs a
      warning, nothing is set for it.
    - codex-cli: sets no env var for base_url/headers (those go through
      CodexRunner._proxy_config_args' per-invocation `-c` flags instead).
      Codex reads whatever env var name model_providers.<id>.env_key points
      at; the `-c` override already sets that to proxy.api_key_env, so the
      ambient environment (which must contain that var) is enough — nothing
      further is added here.
    """
    import os
    if proxy is None or not (proxy.base_url or proxy.api_key_env or proxy.headers):
        return None
    env = os.environ.copy()
    if runner_name == "claude-code":
        if proxy.base_url:
            env["ANTHROPIC_BASE_URL"] = proxy.base_url
        if proxy.api_key_env:
            token = os.environ.get(proxy.api_key_env)
            if token:
                env["ANTHROPIC_AUTH_TOKEN"] = token
            else:
                logger.warning("proxy.claude-code.api_key_env=%r is set but that env var is "
                               "not present in the current environment — ANTHROPIC_AUTH_TOKEN "
                               "will not be overridden.", proxy.api_key_env)
        if proxy.headers:
            logger.warning("proxy.claude-code.headers is configured but Claude Code's CLI has "
                           "no verified custom-headers mechanism — headers will NOT be sent.")
    elif runner_name == "codex-cli":
        # base_url/api_key_env/headers are all applied via CodexRunner's own
        # `-c model_providers.<id>.*` command-line overrides (see
        # _proxy_config_args) rather than env vars — nothing to add here.
        pass
    return env
