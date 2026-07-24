from __future__ import annotations

import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from gantry.backends.protocol import BackendCapabilities
from gantry.cli.run_commands import cmd_cancel
from gantry.config import GantryConfig
from gantry.invocation import InvocationRequest, invoke
from gantry.runners import RunnerResult
from gantry.state import RunStore


def _result(
    *,
    ok: bool = True,
    session_id: str | None = "session-1",
    usage: dict | None = None,
    stderr: str = "",
) -> RunnerResult:
    return RunnerResult(
        ok=ok,
        session_id=session_id,
        raw={"result": "done" if ok else "failed"},
        stdout="done" if ok else "",
        stderr=stderr,
        exit_code=0 if ok else 1,
        usage=usage
        or {
            "cost_usd": None,
            "input_tokens": None,
            "output_tokens": None,
            "duration_ms": None,
        },
    )


class _Runner:
    name = "fake"
    capabilities = BackendCapabilities()

    def __init__(self, result: RunnerResult | None = None, exc: BaseException | None = None):
        self.result = result or _result()
        self.exc = exc
        self.calls: list[dict] = []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc:
            raise self.exc
        return self.result

    def cancel(self, session=None) -> bool:
        return False


class TestInvocation(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = RunStore(self.root)
        self.run_id = self.store.new_run_id("invocation")
        self.store.create(self.run_id, "invocation")
        self.cfg = GantryConfig()

    def tearDown(self):
        self._tmp.cleanup()

    def request(self, runner: _Runner, **overrides) -> InvocationRequest:
        values = {
            "cfg": self.cfg,
            "store": self.store,
            "run_id": self.run_id,
            "stage": "investigation",
            "cwd": self.root,
            "prompt": "investigate",
            "backend_resolver": lambda _name: runner,
            "start_status": "investigation_running",
            "failure_status": "investigation_failed",
        }
        values.update(overrides)
        return InvocationRequest(**values)

    def test_success_persists_logs_result_session_and_usage(self):
        runner = _Runner(
            _result(
                usage={
                    "cost_usd": 0.25,
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "duration_ms": 100,
                }
            )
        )

        outcome = invoke(self.request(runner))

        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.session_id, "session-1")
        self.assertEqual(self.store.get_session_id(self.run_id, "investigation"), "session-1")
        self.assertEqual(
            self.store.read_result(self.run_id, "investigation-result.json")["result"], "done"
        )
        self.assertEqual(
            self.store.read_result(self.run_id, "cost.json")["by_stage"]["investigation"]["cost_usd"],
            0.25,
        )

    def test_terminal_error_is_persisted(self):
        outcome = invoke(self.request(_Runner(_result(ok=False, stderr="terminal failure"))))

        self.assertFalse(outcome.ok)
        self.assertEqual(self.store.state(self.run_id)["status"], "investigation_failed")
        self.assertEqual(
            self.store.get_session(self.run_id, "investigation")["terminal_status"], "error"
        )
        self.assertIn(
            "terminal failure",
            self.store.read_artifact(self.run_id, "logs/investigation.stderr"),
        )

    def test_timeout_returns_failure_and_stops_heartbeat(self):
        runner = _Runner(exc=subprocess.TimeoutExpired(cmd="agent", timeout=3))

        outcome = invoke(self.request(runner))

        self.assertFalse(outcome.ok)
        self.assertTrue(outcome.timed_out)
        self.assertEqual(outcome.error, "timeout")
        self.assertEqual(self.store.state(self.run_id)["status"], "investigation_failed")
        self.assertIn(
            "timed out",
            self.store.read_artifact(self.run_id, "logs/investigation.stderr"),
        )

    def test_cancellation_race_cooperatively_cancels_supported_backend(self):
        started = threading.Event()
        released = threading.Event()

        class CancellableRunner(_Runner):
            capabilities = BackendCapabilities(cancellation=True)

            def run(self, **kwargs):
                self.calls.append(kwargs)
                started.set()
                released.wait(2)
                return _result(ok=True)

            def cancel(self, session=None) -> bool:
                released.set()
                return True

        runner = CancellableRunner()
        holder = {}
        with patch("gantry.invocation.CANCEL_POLL_INTERVAL", 0.01):
            thread = threading.Thread(
                target=lambda: holder.setdefault("outcome", invoke(self.request(runner)))
            )
            thread.start()
            self.assertTrue(started.wait(1))
            with patch("gantry.cli.run_commands._target", return_value=self.root):
                cmd_cancel(SimpleNamespace(run=self.run_id, force=False, cleanup=False))
            thread.join(2)

        self.assertFalse(thread.is_alive())
        self.assertTrue(holder["outcome"].cancelled)
        self.assertFalse(holder["outcome"].ok)
        self.assertEqual(self.store.state(self.run_id)["status"], "cancelled")

    def test_missing_usage_does_not_invent_zero_cost(self):
        outcome = invoke(self.request(_Runner(_result())))

        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.usage["cost_usd"], None)
        self.assertEqual(self.store.read_result(self.run_id, "cost.json"), {})

    def test_resume_after_restart_reuses_durable_session_id(self):
        first = _Runner(_result(session_id="durable-session"))
        invoke(self.request(first))

        restarted_store = RunStore(self.root)
        second = _Runner(_result(session_id="durable-session"))
        request = self.request(second, store=restarted_store, resume=True)
        outcome = invoke(request)

        self.assertTrue(outcome.ok)
        self.assertEqual(second.calls[0]["session_id"], "durable-session")


if __name__ == "__main__":
    unittest.main()
