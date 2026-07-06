"""MCP server registration for the active agent runner.

Gantry doesn't run MCP servers itself — it ensures the agent runner (Claude Code /
Cursor) has them registered, so the agent can call their tools during a stage.

Two registration paths:
  - claude-code: `claude mcp add <name> ...` (idempotent; we check `claude mcp list` first)
  - cursor-cli : write/merge the standard mcpServers JSON into .cursor/mcp.json

Curated servers (opt-in via [mcp].enabled):
  - codebase-memory : structural code intelligence (architecture, call graph,
    git-diff impact + risk). Attached to plan/build/evidence/review.
  - chrome-devtools : live browser control for real E2E evidence. Attached to evidence.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from .config import GantryConfig, MCPServer


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


def ensure_mcp_for_stage(cfg: GantryConfig, stage: str, target: Path) -> list[dict[str, Any]]:
    """Register every enabled MCP server that applies to this stage, for the
    active runner. Idempotent; safe to call before each stage. No-op if none enabled."""
    runner = cfg.agent.runner
    results = []
    for name, srv in cfg.mcp.for_stage(stage).items():
        try:
            if runner == "claude-code":
                results.append(_register_claude(name, srv))
            elif runner == "cursor-cli":
                results.append(_register_cursor(name, srv, target))
            else:
                results.append({"server": name, "runner": runner, "status": "unsupported-runner"})
        except Exception as exc:
            results.append({"server": name, "runner": runner, "status": "error", "error": str(exc)})
    return results
