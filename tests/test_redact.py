import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gantry.config import GantryConfig, ProxyConfig
from gantry.redact import known_secrets, proxy_secrets, redact_secrets


class TestRedactSecrets(unittest.TestCase):
    def test_known_env_secret_replaced(self):
        with mock.patch.dict(os.environ, {"GH_TOKEN": "ghp_supersecrettoken123"}):
            out = redact_secrets("push failed: authentication with ghp_supersecrettoken123 failed")
        self.assertNotIn("ghp_supersecrettoken123", out)
        self.assertIn("***REDACTED***", out)

    def test_multiple_known_secrets_all_replaced(self):
        with mock.patch.dict(os.environ, {
            "GH_TOKEN": "tok-gh-abcdef",
            "ANTHROPIC_API_KEY": "tok-ant-ghijkl",
            "OPENAI_API_KEY": "tok-oai-mnopqr",
        }):
            out = redact_secrets("gh=tok-gh-abcdef ant=tok-ant-ghijkl oai=tok-oai-mnopqr")
        self.assertNotIn("tok-gh-abcdef", out)
        self.assertNotIn("tok-ant-ghijkl", out)
        self.assertNotIn("tok-oai-mnopqr", out)

    def test_project_gateway_key_via_extra_env_or_proxy(self):
        # Org-specific gateway keys are not hardcoded — pass via known_secrets
        # extra names (docker pass-env) or [proxy].api_key_env.
        with mock.patch.dict(os.environ, {"MY_GATEWAY_TOKEN": "tok-gw-stuvwxyz"}):
            secrets = known_secrets(["MY_GATEWAY_TOKEN"])
            out = redact_secrets("auth tok-gw-stuvwxyz", extra_secrets=secrets)
        self.assertNotIn("tok-gw-stuvwxyz", out)

    def test_extra_secrets_replaced(self):
        out = redact_secrets("Bearer sk-live-abcdef123456", extra_secrets=["sk-live-abcdef123456"])
        self.assertNotIn("sk-live-abcdef123456", out)

    def test_short_values_never_treated_as_secrets(self):
        with mock.patch.dict(os.environ, {"GH_TOKEN": "a"}):
            out = redact_secrets("a plain sentence containing the letter a")
        self.assertEqual(out, "a plain sentence containing the letter a")

    def test_empty_text_returns_empty(self):
        self.assertEqual(redact_secrets(""), "")
        self.assertIsNone(redact_secrets(None))

    def test_no_secrets_present_leaves_text_unchanged(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            out = redact_secrets("nothing sensitive here")
        self.assertEqual(out, "nothing sensitive here")

    def test_known_secrets_skips_unset_vars(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(known_secrets(), [])

    def test_proxy_secrets_collects_api_key_env_value_and_headers(self):
        cfg = GantryConfig()
        cfg.proxy["claude-code"] = ProxyConfig(
            base_url="https://gateway.example.invalid",
            api_key_env="MY_PROXY_TOKEN",
            headers={"Authorization": "Bearer proxy-header-secret-1"},
        )
        with mock.patch.dict(os.environ, {"MY_PROXY_TOKEN": "proxy-env-secret-value"}):
            secrets = proxy_secrets(cfg)
        self.assertIn("proxy-env-secret-value", secrets)
        self.assertIn("Bearer proxy-header-secret-1", secrets)


class TestSecretDoesNotSurviveIntoWrittenLogFile(unittest.TestCase):
    """End-to-end proof: a known fake secret value injected into a fake
    subprocess's stderr must not appear in the .stderr log file
    engine.run_agent_stage writes to disk."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.target = Path(self._tmp.name)
        subprocess.run(["git", "init", "-q"], cwd=str(self.target), check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"],
                       cwd=str(self.target), check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=str(self.target), check=True)
        subprocess.run(["git", "commit", "--allow-empty", "-m", "init", "-q"],
                       cwd=str(self.target), check=True)
        subprocess.run(["git", "branch", "-M", "main"], cwd=str(self.target), check=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_fake_secret_in_agent_stderr_is_redacted_before_writing(self):
        from gantry.engine import Engine
        from gantry.runners import RunnerResult

        cfg = GantryConfig()
        eng = Engine(self.target, cfg)
        run_id = eng.create_run("t", "do the thing")

        fake_secret = "ghp_thisisaveryfakesecretvalue999"

        class _FakeRunner:
            name = "claude-code"

            def run(self, **kwargs):
                return RunnerResult(
                    ok=True, session_id="sess-1", exit_code=0, raw={"result": "done"},
                    stdout="all good", stderr=f"warning: leaked token {fake_secret} in logs")

        with mock.patch.dict(os.environ, {"GH_TOKEN": fake_secret}), \
             mock.patch("gantry.engine.get_runner", return_value=_FakeRunner()):
            eng.run_agent_stage(run_id, "plan")

        stderr_log = eng.store.run_dir(run_id) / "logs" / "plan.stderr"
        self.assertTrue(stderr_log.exists(), f"expected log file at {stderr_log}")
        content = stderr_log.read_text()
        self.assertNotIn(fake_secret, content)
        self.assertIn("***REDACTED***", content)


if __name__ == "__main__":
    unittest.main()
