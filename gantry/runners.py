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

logger = logging.getLogger(__name__)


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
    ) -> list[str]:
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
        )
        proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout)
        return self.parse(proc.stdout, proc.stderr, proc.returncode)


class ClaudeCodeRunner(AgentRunner):
    name = "claude-code"

    def build_command(self, *, prompt, model, session_id, plan_mode, skip_permissions,
                       output_format, session_name, max_turns) -> list[str]:
        cmd = ["claude", "-p", prompt, "--name", session_name]
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


class CursorCliRunner(AgentRunner):
    name = "cursor-cli"

    def build_command(self, *, prompt, model, session_id, plan_mode, skip_permissions,
                       output_format, session_name, max_turns) -> list[str]:
        # cursor-agent -p <prompt> --output-format json --model <m> [--plan] [-f] [--resume <id>]
        cmd = ["cursor-agent", "-p", prompt]
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


class CodexRunner(AgentRunner):
    """codex CLI (OpenAI, ChatGPT-auth). Unlike the other two runners, `codex
    exec --json` streams JSONL events rather than a single JSON blob, so this
    overrides `run()` with its own parser instead of reusing the base one."""
    name = "codex-cli"

    def build_command(self, *, prompt, model, session_id, plan_mode, skip_permissions,
                       output_format, session_name, max_turns) -> list[str]:
        # codex exec [resume <id>] --json -m <model> [--dangerously-bypass-approvals-and-sandbox] <prompt>
        if session_id:
            cmd = ["codex", "exec", "resume", session_id]
        else:
            cmd = ["codex", "exec"]
        cmd += ["--json", "--skip-git-repo-check"]
        if model:
            cmd += ["-m", model]
        if skip_permissions:
            cmd += ["--dangerously-bypass-approvals-and-sandbox"]
        # codex has no dedicated plan-mode flag or --max-turns/--name; let the
        # prompt drive planning, same approach ClaudeCodeRunner uses.
        cmd += [prompt]
        return cmd

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
    ) -> RunnerResult:
        cmd = self.build_command(
            prompt=prompt, model=model, session_id=session_id, plan_mode=plan_mode,
            skip_permissions=skip_permissions, output_format=output_format,
            session_name=session_name, max_turns=max_turns,
        )
        proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout)
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
    if name not in _RUNNERS:
        raise ValueError(f"Unknown agent runner: {name!r}. Available: {sorted(_RUNNERS)}")
    return _RUNNERS[name]()
