"""MCP server registration for the active agent runner.

Gantry doesn't run MCP servers itself — it ensures the agent runner (Claude Code /
Cursor / Codex) has them registered, so the agent can call their tools during a stage.

Three registration paths:
  - claude-code: `claude mcp add <name> ...` (idempotent; we check `claude mcp list` first)
  - cursor-cli : write/merge the standard mcpServers JSON into .cursor/mcp.json
  - codex-cli  : `codex mcp add <name> -- <command> <args...>` (idempotent; we check
    `codex mcp list` first)

Curated servers (opt-in via [mcp].enabled):
  - codebase-memory : structural code intelligence (architecture, call graph,
    git-diff impact + risk). Attached to plan/build/evidence/review.
  - chrome-devtools : live browser control for real E2E evidence. Attached to evidence.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .config import GantryConfig, MCPServer

if TYPE_CHECKING:
    from .profiles import AgentProfile


def _claude_registered(name: str) -> bool:
    try:
        proc = subprocess.run(["claude", "mcp", "list"], capture_output=True, text=True, timeout=30)
        return name in (proc.stdout + proc.stderr)
    except Exception:
        return False


def _register_claude(name: str, srv: MCPServer) -> dict[str, Any]:
    if _claude_registered(name):
        return {"server": name, "runner": "claude-code", "status": "already-registered"}
    cmd = srv.register.get("claude-code") or (
        f"claude mcp add {name} --scope user {srv.command} " + " ".join(srv.args))
    proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=120)
    return {"server": name, "runner": "claude-code", "command": cmd,
            "status": "registered" if proc.returncode == 0 else "failed",
            "output": (proc.stdout + proc.stderr)[-400:]}


def _register_cursor(name: str, srv: MCPServer, target: Path) -> dict[str, Any]:
    """Cursor reads .cursor/mcp.json in the project. Merge our entry in."""
    cfg_path = target / ".cursor" / "mcp.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    data = {}
    if cfg_path.exists():
        try:
            data = json.loads(cfg_path.read_text())
        except Exception:
            data = {}
    servers = data.setdefault("mcpServers", {})
    if name in servers:
        return {"server": name, "runner": "cursor-cli", "status": "already-registered"}
    servers[name] = {"command": srv.command, "args": srv.args}
    cfg_path.write_text(json.dumps(data, indent=2) + "\n")
    return {"server": name, "runner": "cursor-cli", "status": "registered", "path": str(cfg_path)}


def _codex_registered(name: str) -> bool:
    try:
        proc = subprocess.run(["codex", "mcp", "list"], capture_output=True, text=True, timeout=30)
        return name in (proc.stdout + proc.stderr)
    except Exception:
        return False


def _register_codex(name: str, srv: MCPServer) -> dict[str, Any]:
    if _codex_registered(name):
        return {"server": name, "runner": "codex-cli", "status": "already-registered"}
    override = srv.register.get("codex-cli")
    if override:
        proc = subprocess.run(override, shell=True, capture_output=True, text=True, timeout=120)
        cmd_display = override
    else:
        cmd = ["codex", "mcp", "add", name, "--", srv.command, *srv.args]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        cmd_display = " ".join(cmd)
    return {"server": name, "runner": "codex-cli", "command": cmd_display,
            "status": "registered" if proc.returncode == 0 else "failed",
            "output": (proc.stdout + proc.stderr)[-400:]}


def ensure_mcp_for_stage(
    cfg: GantryConfig,
    stage: str,
    runner: str,
    target: Path,
    profile: AgentProfile | None = None,
) -> list[dict[str, Any]]:
    """Register every enabled MCP server that applies to this stage, for the
    given (already-resolved, per-stage) runner name. Idempotent; safe to call
    before each stage. No-op if none enabled."""
    if profile is None:
        from .profiles import profile_for_stage
        profile = profile_for_stage(stage, cfg)
    results = []
    for name in profile.mcp:
        srv = cfg.mcp.servers.get(name)
        if srv is None:
            continue
        try:
            if runner == "claude-code":
                results.append(_register_claude(name, srv))
            elif runner == "cursor-cli":
                results.append(_register_cursor(name, srv, target))
            elif runner == "codex-cli":
                results.append(_register_codex(name, srv))
            else:
                results.append({"server": name, "runner": runner, "status": "unsupported-runner"})
        except Exception as exc:
            results.append({"server": name, "runner": runner, "status": "error", "error": str(exc)})
    return results
