"""MCP registration paths for each runner, including codex-cli parity."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gantry.config import GantryConfig, MCPConfig, MCPServer
from gantry.mcp import ensure_mcp_for_stage


def _cfg_with_server(name: str = "codebase-memory") -> GantryConfig:
    cfg = GantryConfig()
    cfg.mcp = MCPConfig(
        enabled=[name],
        servers={
            name: MCPServer(
                command="codebase-memory-mcp",
                args=["serve"],
                stages=["plan", "build"],
                register={
                    "claude-code": "claude mcp add codebase-memory --scope user codebase-memory-mcp serve",
                    "codex-cli": "codex mcp add codebase-memory -- codebase-memory-mcp serve",
                },
            )
        },
    )
    return cfg


class TestEnsureMcpCodex(unittest.TestCase):
    def test_codex_registers_via_codex_mcp_add(self):
        cfg = _cfg_with_server()
        with tempfile.TemporaryDirectory() as tmp:
            with patch("gantry.mcp._codex_registered", return_value=False), \
                 patch("gantry.mcp.subprocess.run") as mock_run:
                mock_run.return_value.returncode = 0
                mock_run.return_value.stdout = ""
                mock_run.return_value.stderr = ""
                results = ensure_mcp_for_stage(cfg, "plan", "codex-cli", Path(tmp))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["runner"], "codex-cli")
        self.assertEqual(results[0]["status"], "registered")
        cmd = results[0]["command"]
        self.assertIn("codex mcp add", cmd)
        self.assertIn("codebase-memory", cmd)

    def test_codex_already_registered_is_noop(self):
        cfg = _cfg_with_server()
        with tempfile.TemporaryDirectory() as tmp:
            with patch("gantry.mcp._codex_registered", return_value=True), \
                 patch("gantry.mcp.subprocess.run") as mock_run:
                results = ensure_mcp_for_stage(cfg, "plan", "codex-cli", Path(tmp))
        self.assertEqual(results[0]["status"], "already-registered")
        mock_run.assert_not_called()

    def test_codex_fallback_builds_argv_when_no_override(self):
        cfg = GantryConfig()
        cfg.mcp = MCPConfig(
            enabled=["custom"],
            servers={
                "custom": MCPServer(
                    command="my-mcp",
                    args=["serve", "--flag"],
                    stages=["plan"],
                    register={},  # force fallback path
                )
            },
        )
        with tempfile.TemporaryDirectory() as tmp:
            with patch("gantry.mcp._codex_registered", return_value=False), \
                 patch("gantry.mcp.subprocess.run") as mock_run:
                mock_run.return_value.returncode = 0
                mock_run.return_value.stdout = ""
                mock_run.return_value.stderr = ""
                results = ensure_mcp_for_stage(cfg, "plan", "codex-cli", Path(tmp))
        self.assertEqual(results[0]["status"], "registered")
        # fallback uses argv list, not shell string
        argv = mock_run.call_args[0][0]
        self.assertEqual(argv[:4], ["codex", "mcp", "add", "custom"])
        self.assertEqual(argv[4:], ["--", "my-mcp", "serve", "--flag"])

    def test_default_mcp_servers_include_codex_register(self):
        # Defaults are merged when servers empty — check the module defaults
        # are loadable onto a fresh MCPConfig via DEFAULT_MCP_SERVERS shape.
        from gantry.config import DEFAULT_MCP_SERVERS
        for name in ("codebase-memory", "chrome-devtools"):
            self.assertIn("codex-cli", DEFAULT_MCP_SERVERS[name]["register"])

    def test_explicit_profile_mcp_subset_is_used(self):
        cfg = _cfg_with_server()
        cfg.mcp.enabled = []
        cfg.profiles["planner-builder"] = {"mcp": ["codebase-memory"]}
        with tempfile.TemporaryDirectory() as tmp:
            with patch("gantry.mcp._codex_registered", return_value=True):
                results = ensure_mcp_for_stage(cfg, "plan", "codex-cli", Path(tmp))
        self.assertEqual([result["server"] for result in results], ["codebase-memory"])

    def test_profile_and_legacy_mcp_are_deduplicated(self):
        cfg = _cfg_with_server()
        cfg.profiles["planner-builder"] = {"mcp": ["codebase-memory"]}
        with tempfile.TemporaryDirectory() as tmp:
            with patch("gantry.mcp._codex_registered", return_value=True):
                results = ensure_mcp_for_stage(cfg, "plan", "codex-cli", Path(tmp))
        self.assertEqual(len(results), 1)


if __name__ == "__main__":
    unittest.main()
