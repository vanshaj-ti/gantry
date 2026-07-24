from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

from gantry.backends.cursor_sdk import CursorSdkBackend
from gantry.backends.protocol import InvocationSpec, SessionRef
from gantry.backends.registry import resolve_backend


def _sdk_module(*, agent=None, create=None, resume=None) -> ModuleType:
    module = ModuleType("cursor_sdk")
    module.AgentOptions = lambda **kwargs: SimpleNamespace(**kwargs)
    module.LocalAgentOptions = lambda **kwargs: SimpleNamespace(**kwargs)
    if agent is None:
        agent = MagicMock()
        agent.agent_id = "agent-123"
    module.Agent = SimpleNamespace(
        create=create or MagicMock(return_value=agent),
        resume=resume or MagicMock(return_value=agent),
    )
    return module


def _finished_run(text: str = "done", input_tokens: int = 11, output_tokens: int = 7):
    terminal = SimpleNamespace(
        status="finished",
        result=text,
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )
    run = MagicMock()
    run.wait.return_value = terminal
    run.status = "finished"
    run.usage = terminal.usage
    return run


class TestCursorSdkBackend(unittest.TestCase):
    def test_invoke_creates_local_agent_with_explicit_cwd_and_model(self):
        agent = MagicMock(agent_id="agent-create")
        agent.agent_id = "agent-create"
        agent.send.return_value = _finished_run()
        create = MagicMock(return_value=agent)
        sdk = _sdk_module(agent=agent, create=create)

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            sys.modules, {"cursor_sdk": sdk}
        ), patch.dict("os.environ", {"CURSOR_API_KEY": "key"}, clear=False):
            result = CursorSdkBackend().invoke(
                InvocationSpec(cwd=Path(tmp), prompt="build it", model="composer-2.5")
            )

        kwargs = create.call_args.kwargs
        self.assertEqual(kwargs["model"], "composer-2.5")
        self.assertEqual(kwargs["api_key"], "key")
        self.assertEqual(kwargs["local"].cwd, tmp)
        agent.send.assert_called_once_with("build it")
        agent.close.assert_called_once()
        self.assertTrue(result.ok)
        self.assertEqual(result.agent_id, "agent-create")

    def test_resume_uses_existing_agent_id(self):
        agent = MagicMock(agent_id="agent-resumed")
        agent.agent_id = "agent-resumed"
        agent.send.return_value = _finished_run("continued")
        resume = MagicMock(return_value=agent)
        sdk = _sdk_module(agent=agent, resume=resume)

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            sys.modules, {"cursor_sdk": sdk}
        ), patch.dict("os.environ", {"CURSOR_API_KEY": "key"}, clear=False):
            CursorSdkBackend().invoke(
                InvocationSpec(
                    cwd=Path(tmp),
                    prompt="continue",
                    session=SessionRef(session_id="agent-old", agent_id="agent-specific"),
                )
            )

        resume.assert_called_once()
        self.assertEqual(resume.call_args.kwargs["agent_id"], "agent-specific")
        self.assertEqual(resume.call_args.kwargs["options"].api_key, "key")
        self.assertEqual(resume.call_args.kwargs["options"].local.cwd, tmp)
        sdk.Agent.create.assert_not_called()

    def test_usage_maps_tokens_and_never_reports_monetary_cost(self):
        agent = MagicMock()
        agent.agent_id = "agent-usage"
        agent.send.return_value = _finished_run(input_tokens=31, output_tokens=19)
        sdk = _sdk_module(agent=agent)

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            sys.modules, {"cursor_sdk": sdk}
        ), patch.dict("os.environ", {"CURSOR_API_KEY": "key"}, clear=False):
            result = CursorSdkBackend().invoke(
                InvocationSpec(cwd=Path(tmp), prompt="count")
            )

        self.assertEqual(result.usage["input_tokens"], 31)
        self.assertEqual(result.usage["output_tokens"], 19)
        self.assertIsNone(result.usage["cost_usd"])

    def test_events_are_persisted_as_opaque_ndjson(self):
        agent = MagicMock()
        agent.agent_id = "agent-events"
        run = _finished_run()
        run.events.return_value = iter([
            SimpleNamespace(type="status", status="running"),
            {"type": "result", "status": "finished"},
        ])
        agent.send.return_value = run
        sdk = _sdk_module(agent=agent)

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            sys.modules, {"cursor_sdk": sdk}
        ), patch.dict("os.environ", {"CURSOR_API_KEY": "key"}, clear=False):
            events_path = Path(tmp) / "events.ndjson"
            result = CursorSdkBackend().invoke(
                InvocationSpec(
                    cwd=Path(tmp),
                    prompt="stream",
                    extras={"events_path": str(events_path)},
                )
            )
            events = [json.loads(line) for line in events_path.read_text().splitlines()]

        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["type"], "status")
        self.assertEqual(result.events_path, str(events_path))

    def test_cancel_signals_active_run(self):
        entered_wait = threading.Event()
        release_wait = threading.Event()
        run = MagicMock()

        def wait():
            entered_wait.set()
            release_wait.wait(2)
            return SimpleNamespace(status="cancelled", result="", usage=None)

        run.wait.side_effect = wait
        run.cancel.side_effect = release_wait.set
        agent = MagicMock()
        agent.agent_id = "agent-cancel"
        agent.send.return_value = run
        sdk = _sdk_module(agent=agent)
        backend = CursorSdkBackend()

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            sys.modules, {"cursor_sdk": sdk}
        ), patch.dict("os.environ", {"CURSOR_API_KEY": "key"}, clear=False):
            thread = threading.Thread(
                target=backend.invoke,
                args=(InvocationSpec(cwd=Path(tmp), prompt="wait", timeout=2),),
            )
            thread.start()
            self.assertTrue(entered_wait.wait(1))
            self.assertTrue(backend.cancel())
            thread.join(2)

        run.cancel.assert_called_once()
        self.assertFalse(thread.is_alive())

    def test_timeout_cancels_run_and_returns_cancelled_result(self):
        release_wait = threading.Event()
        run = MagicMock()
        run.wait.side_effect = lambda: (
            release_wait.wait(1)
            or SimpleNamespace(status="cancelled", result="", usage=None)
        )
        run.cancel.side_effect = release_wait.set
        agent = MagicMock()
        agent.agent_id = "agent-timeout"
        agent.send.return_value = run
        sdk = _sdk_module(agent=agent)

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            sys.modules, {"cursor_sdk": sdk}
        ), patch.dict("os.environ", {"CURSOR_API_KEY": "key"}, clear=False):
            result = CursorSdkBackend().invoke(
                InvocationSpec(cwd=Path(tmp), prompt="hang", timeout=0.01)
            )

        self.assertTrue(result.cancelled)
        self.assertFalse(result.ok)
        self.assertIn("timed out", result.stderr)
        run.cancel.assert_called_once()

    def test_missing_package_raises_clear_import_error(self):
        with patch("importlib.import_module", side_effect=ModuleNotFoundError("cursor_sdk")):
            with self.assertRaisesRegex(ImportError, "cursor-sdk"):
                CursorSdkBackend().invoke(
                    InvocationSpec(cwd=Path.cwd(), prompt="hello")
                )

    def test_resolve_backend_falls_back_when_sdk_unavailable(self):
        diagnosis = {
            "package_available": False,
            "api_key_present": False,
            "import_error": "not installed",
        }
        with patch("gantry.backends.registry.diagnose_cursor_sdk", return_value=diagnosis), patch(
            "gantry.backends.registry.shutil.which",
            side_effect=lambda binary: "/bin/cursor-agent" if binary == "cursor-agent" else None,
        ):
            resolved = resolve_backend("cursor-sdk")

        self.assertEqual(resolved.resolved_name, "cursor-cli")
        self.assertEqual(resolved.backend.name, "cursor-cli")
        self.assertIn("not installed", resolved.fallback_reason)


if __name__ == "__main__":
    unittest.main()
