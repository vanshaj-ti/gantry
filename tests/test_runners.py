import os
import unittest

from gantry.config import ProxyConfig
from gantry.runners import ClaudeCodeRunner, CursorCliRunner, CodexRunner, get_runner, resolve_proxy_env


def _base_kwargs(**overrides):
    kwargs = dict(
        prompt="do the thing",
        model="",
        session_id=None,
        plan_mode=False,
        skip_permissions=True,
        output_format="json",
        session_name="gantry",
        max_turns=60,
    )
    kwargs.update(overrides)
    return kwargs


class TestClaudeCodeRunner(unittest.TestCase):
    def test_fresh_run_command(self):
        cmd = ClaudeCodeRunner().build_command(**_base_kwargs(model="opus"))
        self.assertEqual(cmd[0], "claude")
        self.assertIn("--model", cmd)
        self.assertIn("opus", cmd)
        self.assertIn("--dangerously-skip-permissions", cmd)
        self.assertNotIn("--resume", cmd)

    def test_resume_appends_session_id(self):
        cmd = ClaudeCodeRunner().build_command(**_base_kwargs(session_id="sess-1"))
        self.assertIn("--resume", cmd)
        self.assertEqual(cmd[cmd.index("--resume") + 1], "sess-1")

    def test_skip_permissions_false_omits_flag(self):
        cmd = ClaudeCodeRunner().build_command(**_base_kwargs(skip_permissions=False))
        self.assertNotIn("--dangerously-skip-permissions", cmd)


class TestCursorCliRunner(unittest.TestCase):
    def test_plan_mode_adds_flag(self):
        cmd = CursorCliRunner().build_command(**_base_kwargs(plan_mode=True))
        self.assertEqual(cmd[0], "cursor-agent")
        self.assertIn("--plan", cmd)

    def test_no_max_turns_or_name_flags(self):
        cmd = CursorCliRunner().build_command(**_base_kwargs())
        self.assertNotIn("--max-turns", cmd)
        self.assertNotIn("--name", cmd)

    def test_skip_permissions_maps_to_force_flag(self):
        cmd = CursorCliRunner().build_command(**_base_kwargs(skip_permissions=True))
        self.assertIn("-f", cmd)


class TestCodexRunner(unittest.TestCase):
    def test_fresh_run_uses_exec(self):
        cmd = CodexRunner().build_command(**_base_kwargs(model="gpt-5.5"))
        self.assertEqual(cmd[0], "codex")
        self.assertEqual(cmd[1], "exec")
        self.assertIn("--json", cmd)
        self.assertIn("-m", cmd)
        self.assertIn("gpt-5.5", cmd)
        self.assertNotIn("resume", cmd)

    def test_resume_uses_exec_resume_with_session_id(self):
        cmd = CodexRunner().build_command(**_base_kwargs(session_id="thread-abc"))
        self.assertEqual(cmd[:4], ["codex", "exec", "resume", "thread-abc"])

    def test_skip_permissions_maps_to_bypass_flag(self):
        cmd = CodexRunner().build_command(**_base_kwargs(skip_permissions=True))
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", cmd)

    def test_parse_jsonl_extracts_session_and_message(self):
        jsonl = "\n".join([
            '{"type": "thread.started", "thread_id": "thread-123"}',
            '{"type": "turn.started"}',
            '{"type": "item.completed", "item": {"type": "agent_message", "text": "done doing the thing"}}',
            '{"type": "turn.completed"}',
        ])
        result = CodexRunner()._parse_jsonl(jsonl, "", 0)
        self.assertTrue(result.ok)
        self.assertEqual(result.session_id, "thread-123")
        self.assertEqual(result.raw["result"], "done doing the thing")

    def test_parse_jsonl_missing_turn_completed_is_error(self):
        jsonl = '{"type": "thread.started", "thread_id": "thread-123"}'
        result = CodexRunner()._parse_jsonl(jsonl, "", 0)
        self.assertFalse(result.ok)
        self.assertTrue(result.raw["is_error"])

    def test_parse_jsonl_nonzero_exit_is_error(self):
        jsonl = "\n".join([
            '{"type": "thread.started", "thread_id": "thread-123"}',
            '{"type": "item.completed", "item": {"type": "agent_message", "text": "partial"}}',
            '{"type": "turn.completed"}',
        ])
        result = CodexRunner()._parse_jsonl(jsonl, "boom", 1)
        self.assertFalse(result.ok)

    def test_parse_jsonl_ignores_malformed_lines(self):
        jsonl = "\n".join([
            "not json at all",
            '{"type": "thread.started", "thread_id": "thread-1"}',
            '{"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}}',
            '{"type": "turn.completed"}',
        ])
        result = CodexRunner()._parse_jsonl(jsonl, "", 0)
        self.assertTrue(result.ok)
        self.assertEqual(result.raw["result"], "ok")


class TestCodexProxyConfigArgs(unittest.TestCase):
    def test_no_proxy_adds_no_flags(self):
        cmd = CodexRunner().build_command(**_base_kwargs(model="gpt-5.5"))
        self.assertNotIn("-c", cmd)

    def test_base_url_and_api_key_env_add_c_flags(self):
        proxy = ProxyConfig(base_url="https://gw.example.com", api_key_env="MY_TOKEN")
        cmd = CodexRunner().build_command(**_base_kwargs(model="gpt-5.5", proxy=proxy))
        joined = " ".join(cmd)
        self.assertIn("model_providers.gantry-proxy.base_url=https://gw.example.com", joined)
        self.assertIn("model_providers.gantry-proxy.env_key=MY_TOKEN", joined)
        self.assertIn("model_provider=gantry-proxy", joined)

    def test_headers_add_http_headers_flags(self):
        proxy = ProxyConfig(headers={"X-Foo": "bar"})
        cmd = CodexRunner().build_command(**_base_kwargs(model="gpt-5.5", proxy=proxy))
        joined = " ".join(cmd)
        self.assertIn("model_providers.gantry-proxy.http_headers.X-Foo=bar", joined)

    def test_empty_proxy_config_adds_no_flags(self):
        cmd = CodexRunner().build_command(**_base_kwargs(model="gpt-5.5", proxy=ProxyConfig()))
        self.assertNotIn("-c", cmd)


class TestResolveProxyEnv(unittest.TestCase):
    def test_none_proxy_returns_none(self):
        self.assertIsNone(resolve_proxy_env("claude-code", None))

    def test_empty_proxy_config_returns_none(self):
        self.assertIsNone(resolve_proxy_env("claude-code", ProxyConfig()))

    def test_claude_code_sets_base_url_and_auth_token(self):
        os.environ["GANTRY_TEST_TOKEN"] = "secret-value"
        try:
            proxy = ProxyConfig(base_url="https://gw.example.com", api_key_env="GANTRY_TEST_TOKEN")
            env = resolve_proxy_env("claude-code", proxy)
            self.assertEqual(env["ANTHROPIC_BASE_URL"], "https://gw.example.com")
            self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "secret-value")
        finally:
            del os.environ["GANTRY_TEST_TOKEN"]

    def test_claude_code_missing_api_key_env_var_skips_token(self):
        proxy = ProxyConfig(base_url="https://gw.example.com", api_key_env="GANTRY_TEST_TOKEN_MISSING")
        env = resolve_proxy_env("claude-code", proxy)
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)

    def test_codex_cli_env_untouched_by_base_url(self):
        proxy = ProxyConfig(base_url="https://gw.example.com", api_key_env="SOME_TOKEN")
        env = resolve_proxy_env("codex-cli", proxy)
        # codex's overrides go through -c flags, not env vars
        self.assertNotIn("OPENAI_BASE_URL", env or {})


class TestGetRunner(unittest.TestCase):
    def test_known_runners_resolve(self):
        for name in ("claude-code", "cursor-cli", "codex-cli"):
            runner = get_runner(name)
            self.assertEqual(runner.name, name)

    def test_unknown_runner_raises(self):
        with self.assertRaises(ValueError):
            get_runner("not-a-real-runner")


if __name__ == "__main__":
    unittest.main()
