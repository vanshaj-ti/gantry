"""Agent runner adapters.

An AgentRunner turns a stage invocation (prompt, model, session, plan-mode,
skip-permissions) into a concrete CLI command for a specific agent tool, runs
it, and parses the JSON result into a normalized RunnerResult.

Two runners ship in v1: claude-code and cursor-cli. Their command surfaces are
nearly identical; the adapter is essentially a flag-mapping table plus shared
JSON parsing. Add a runner by subclassing AgentRunner and registering it.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class RunnerResult:
    ok: bool
    session_id: str | None
    raw: dict[str, Any]
    stdout: str
    stderr: str
    exit_code: int


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
        return RunnerResult(
            ok=not is_error,
            session_id=data.get("session_id"),
            raw=data,
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
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


_RUNNERS: dict[str, type[AgentRunner]] = {
    ClaudeCodeRunner.name: ClaudeCodeRunner,
    CursorCliRunner.name: CursorCliRunner,
}


def get_runner(name: str) -> AgentRunner:
    if name not in _RUNNERS:
        raise ValueError(f"Unknown agent runner: {name!r}. Available: {sorted(_RUNNERS)}")
    return _RUNNERS[name]()
