"""Cockpit agent-pane resolution: must honor [agent].runner (claude/codex/cursor)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gantry.cockpit import _agent_pane_cmd


class TestAgentPaneCmd(unittest.TestCase):
    def test_codex_runner_from_toml(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "gantry.toml").write_text(
                '[agent]\nrunner = "codex-cli"\nskip_permissions = true\n'
            )
            cmd, runner = _agent_pane_cmd(root)
        self.assertEqual(runner, "codex-cli")
        self.assertTrue(cmd.startswith("codex"))
        self.assertNotIn("exec", cmd.split())
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", cmd)

    def test_sdk_default_uses_cursor_cli_for_interactive_pane(self):
        with tempfile.TemporaryDirectory() as tmp:
            cmd, runner = _agent_pane_cmd(Path(tmp))
        self.assertEqual(runner, "cursor-cli")
        self.assertEqual(cmd, "cursor-agent -f")

    def test_skip_permissions_false_omits_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "gantry.toml").write_text(
                '[agent]\nrunner = "codex-cli"\nskip_permissions = false\n'
            )
            cmd, runner = _agent_pane_cmd(root)
        self.assertEqual(runner, "codex-cli")
        self.assertEqual(cmd, "codex")


class TestBuildCockpitUsesRunner(unittest.TestCase):
    def test_build_cockpit_returns_runner_and_launches_agent_cmd(self):
        from gantry import cockpit as cockpit_mod

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "gantry.toml").write_text('[agent]\nrunner = "codex-cli"\n')

            sent = []

            def fake_tmux(*args, **kwargs):
                class P:
                    returncode = 0
                    stdout = ""
                    stderr = ""
                # list-panes geometry: status top, docs left, agent right
                if args and args[0] == "list-panes":
                    P.stdout = "%0 0 0\n%1 15 0\n%2 15 40\n"
                if args and args[0] == "send-keys":
                    sent.append(args)
                if args and args[0] == "has-session":
                    P.returncode = 1  # does not exist
                return P()

            with patch.object(cockpit_mod, "_tmux", side_effect=fake_tmux), \
                 patch.object(cockpit_mod, "session_exists", return_value=False):
                result = cockpit_mod.build_cockpit(root)

        self.assertTrue(result["ok"])
        self.assertEqual(result.get("runner"), "codex-cli")
        agent_sends = [a for a in sent if any("codex" in str(x) for x in a)]
        self.assertTrue(agent_sends, f"expected codex launch in send-keys, got {sent}")


if __name__ == "__main__":
    unittest.main()
