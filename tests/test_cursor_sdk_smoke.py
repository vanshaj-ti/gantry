"""Cursor SDK contract + credential-gated live smoke tests.

Mocked contract tests always run in CI. Live smoke runs only when both
GANTRY_CURSOR_SDK_LIVE=1 and CURSOR_API_KEY are set.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Live gate — ordinary CI must skip these.
_LIVE = (
    os.environ.get("GANTRY_CURSOR_SDK_LIVE") == "1"
    and bool(os.environ.get("CURSOR_API_KEY"))
)


class TestCursorSdkDocumentedContract(unittest.TestCase):
    """Freeze the documented API surface Gantry will wrap (mocked)."""

    def test_compatibility_doc_exists(self):
        doc = Path(__file__).resolve().parents[1] / "docs" / "cursor-sdk-compatibility.md"
        self.assertTrue(doc.is_file(), f"missing {doc}")
        text = doc.read_text()
        for needle in (
            "cursor-sdk",
            "CURSOR_API_KEY",
            "LocalAgentOptions",
            "cost_usd",
            "GANTRY_CURSOR_SDK_LIVE",
            "cursor-cli",
            "claude-code",
            "codex-cli",
        ):
            self.assertIn(needle, text)

    def test_mocked_local_invoke_uses_explicit_cwd_and_model(self):
        """Simulate the Phase 2 backend contract without importing cursor_sdk."""
        cwd = Path(tempfile.mkdtemp())
        create = MagicMock()
        agent = MagicMock()
        agent.agent_id = "agent-mock-001"
        agent.model = MagicMock(id="composer-2.5")
        run = MagicMock()
        run.wait = MagicMock(return_value=None)
        run.text = MagicMock(return_value="done")
        run.status = "finished"
        run.usage = MagicMock(input_tokens=11, output_tokens=7)
        # No monetary cost field on documented usage.
        if hasattr(run.usage, "cost_usd"):
            delattr(run.usage, "cost_usd")
        agent.send.return_value = run
        create.return_value.__enter__ = MagicMock(return_value=agent)
        create.return_value.__exit__ = MagicMock(return_value=False)

        with patch.dict("sys.modules", {"cursor_sdk": MagicMock(Agent=MagicMock(create=create))}):
            # Mimic what cursor_sdk backend will do:
            from types import SimpleNamespace
            LocalAgentOptions = lambda **kw: SimpleNamespace(**kw)  # noqa: E731
            opts = LocalAgentOptions(cwd=str(cwd))
            handle = create(
                model="composer-2.5",
                api_key="test-key",
                local=opts,
            ).__enter__()
            result_run = handle.send("hello")
            result_run.wait()

        create.assert_called()
        kwargs = create.call_args.kwargs
        self.assertEqual(kwargs["model"], "composer-2.5")
        self.assertEqual(kwargs["local"].cwd, str(cwd))
        self.assertEqual(handle.agent_id, "agent-mock-001")
        self.assertEqual(result_run.usage.input_tokens, 11)
        self.assertFalse(hasattr(result_run.usage, "cost_usd"))

    def test_mocked_cancel_and_resume_hooks(self):
        agent = MagicMock()
        agent.agent_id = "agent-resume-1"
        run = MagicMock()
        run.cancel = MagicMock()
        agent.send.return_value = run
        resume = MagicMock(return_value=agent)

        resumed = resume(agent_id="agent-resume-1")
        self.assertEqual(resumed.agent_id, "agent-resume-1")
        active = resumed.send("continue")
        active.cancel()
        run.cancel.assert_called_once()

    def test_fallback_order_documented(self):
        doc = Path(__file__).resolve().parents[1] / "docs" / "cursor-sdk-compatibility.md"
        text = doc.read_text()
        sdk_i = text.index("cursor-sdk")
        cli_i = text.index("`cursor-cli`")
        claude_i = text.index("`claude-code`")
        codex_i = text.index("`codex-cli`")
        self.assertLess(sdk_i, cli_i)
        self.assertLess(cli_i, claude_i)
        self.assertLess(claude_i, codex_i)


@unittest.skipUnless(_LIVE, "set GANTRY_CURSOR_SDK_LIVE=1 and CURSOR_API_KEY for live SDK smoke")
class TestCursorSdkLiveSmoke(unittest.TestCase):
    """Opt-in live acceptance — not part of ordinary CI."""

    def test_local_create_send_dispose(self):
        from cursor_sdk import Agent, LocalAgentOptions

        with tempfile.TemporaryDirectory() as tmp:
            with Agent.create(
                model=os.environ.get("GANTRY_CURSOR_SDK_MODEL", "composer-2.5"),
                local=LocalAgentOptions(cwd=tmp),
            ) as agent:
                self.assertTrue(agent.agent_id)
                run = agent.send("Reply with exactly: pong")
                # Prefer wait()/text() if present; otherwise iterate stream.
                if hasattr(run, "wait"):
                    run.wait()
                text = run.text() if callable(getattr(run, "text", None)) else str(run)
                self.assertTrue(text)


if __name__ == "__main__":
    unittest.main()
