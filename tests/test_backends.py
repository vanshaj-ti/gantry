"""Shared backend contract suite — capabilities, invoke bridge, registry."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from gantry.backends import (
    BackendCapabilities,
    InvocationSpec,
    SessionRef,
    capabilities_for,
    get_backend,
    list_backends,
)
from gantry.backends.cli import CliAgentBackend, invocation_from_runner_kwargs, wrap_runner
from gantry.backends.protocol import InvocationResult
from gantry.backends.registry import DEFAULT_FALLBACK_ORDER
from gantry.runners import (
    ClaudeCodeRunner,
    CodexRunner,
    CursorCliRunner,
    RunnerResult,
    get_runner,
)


class TestBackendRegistry(unittest.TestCase):
    def test_lists_sdk_and_legacy_cli_backends(self):
        names = list_backends()
        self.assertEqual(names, ["claude-code", "codex-cli", "cursor-cli", "cursor-sdk"])

    def test_get_backend_wraps_matching_runner(self):
        for name in ("claude-code", "cursor-cli", "codex-cli"):
            backend = get_backend(name)
            self.assertEqual(backend.name, name)
            self.assertIsInstance(backend, CliAgentBackend)
            self.assertEqual(backend.runner.name, get_runner(name).name)

    def test_unknown_backend_raises(self):
        with self.assertRaises(ValueError):
            get_backend("not-a-backend")


class TestBackendCapabilities(unittest.TestCase):
    def test_claude_capabilities(self):
        caps = capabilities_for("claude-code")
        self.assertTrue(caps.resume)
        self.assertTrue(caps.max_turns)
        self.assertTrue(caps.proxy_support)
        self.assertTrue(caps.interactive)
        self.assertFalse(caps.cancellation)
        self.assertFalse(caps.streaming)

    def test_cursor_cli_capabilities(self):
        caps = capabilities_for("cursor-cli")
        self.assertTrue(caps.plan_mode)
        self.assertFalse(caps.max_turns)
        self.assertFalse(caps.proxy_support)

    def test_codex_capabilities(self):
        caps = capabilities_for("codex-cli")
        self.assertTrue(caps.streaming)
        self.assertTrue(caps.proxy_support)
        self.assertFalse(caps.monetary_cost)

    def test_orchestration_branches_on_capabilities_not_names(self):
        # Example policy: only use plan_mode flag when advertised.
        for name in list_backends():
            caps = capabilities_for(name)
            self.assertIsInstance(caps, BackendCapabilities)
            want_plan = caps.plan_mode
            # cursor-cli is the only current CLI with a plan flag.
            if name == "cursor-cli":
                self.assertTrue(want_plan)
            else:
                self.assertFalse(want_plan)


class TestCliBackendInvokeBridge(unittest.TestCase):
    def test_invoke_delegates_to_runner_run(self):
        captured = {}

        class FakeRunner(ClaudeCodeRunner):
            def run(self, **kwargs):
                captured.update(kwargs)
                return RunnerResult(
                    ok=True, session_id="s1", raw={"result": "ok"},
                    stdout="ok", stderr="", exit_code=0,
                    usage={"cost_usd": 0.1, "input_tokens": 1, "output_tokens": 2, "duration_ms": 3},
                )

        backend = wrap_runner(FakeRunner())
        with tempfile.TemporaryDirectory() as tmp:
            spec = InvocationSpec(
                cwd=Path(tmp),
                prompt="do it",
                model="opus",
                session=SessionRef(session_id="prior", backend="claude-code"),
                plan_mode=False,
                max_turns=12,
                timeout=30,
            )
            result = backend.invoke(spec)

        self.assertEqual(captured["session_id"], "prior")
        self.assertEqual(captured["model"], "opus")
        self.assertEqual(captured["max_turns"], 12)
        self.assertIsInstance(result, InvocationResult)
        self.assertEqual(result.session_id, "s1")
        self.assertEqual(result.backend, "claude-code")
        self.assertTrue(result.ok)

    def test_to_runner_result_roundtrip(self):
        ir = InvocationResult(
            ok=True, session_id="x", raw={}, stdout="", stderr="", exit_code=0,
            usage={"cost_usd": None, "input_tokens": 1, "output_tokens": 2, "duration_ms": None},
        )
        rr = ir.to_runner_result()
        self.assertEqual(rr.session_id, "x")
        self.assertEqual(rr.usage["input_tokens"], 1)

    def test_cancel_returns_false_for_cli(self):
        self.assertFalse(get_backend("claude-code").cancel())
        self.assertFalse(get_backend("cursor-cli").cancel(SessionRef(session_id="x")))

    def test_interactive_command_matches_runner(self):
        for name in ("claude-code", "cursor-cli", "codex-cli"):
            self.assertEqual(
                get_backend(name).interactive_command(skip_permissions=True),
                get_runner(name).interactive_command(skip_permissions=True),
            )

    def test_invocation_from_runner_kwargs(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = invocation_from_runner_kwargs(
                cwd=Path(tmp), prompt="p", model="m", session_id="sid", backend="claude-code",
            )
        self.assertEqual(spec.session.session_id, "sid")
        self.assertEqual(spec.model, "m")


class TestFallbackOrder(unittest.TestCase):
    def test_pre_start_fallback_order_prefers_sdk(self):
        self.assertEqual(
            DEFAULT_FALLBACK_ORDER,
            ("cursor-sdk", "cursor-cli", "claude-code", "codex-cli"),
        )


class TestArgvParityViaBackend(unittest.TestCase):
    """Backend wrappers must not alter argv construction."""

    def test_claude_argv_unchanged(self):
        runner = ClaudeCodeRunner()
        backend = wrap_runner(runner)
        cmd_direct = runner.build_command(
            prompt="p", model="m", session_id="s", plan_mode=False,
            skip_permissions=True, output_format="json", session_name="n", max_turns=5,
        )
        cmd_via = backend.runner.build_command(
            prompt="p", model="m", session_id="s", plan_mode=False,
            skip_permissions=True, output_format="json", session_name="n", max_turns=5,
        )
        self.assertEqual(cmd_direct, cmd_via)

    def test_cursor_and_codex_argv_unchanged(self):
        for Runner in (CursorCliRunner, CodexRunner):
            runner = Runner()
            backend = wrap_runner(runner)
            kwargs = dict(
                prompt="p", model="m", session_id=None, plan_mode=True,
                skip_permissions=True, output_format="json", session_name="n", max_turns=5,
            )
            self.assertEqual(runner.build_command(**kwargs), backend.runner.build_command(**kwargs))


if __name__ == "__main__":
    unittest.main()
