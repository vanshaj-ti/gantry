"""Docker env pass-through is project-agnostic (no org-specific key names)."""
from __future__ import annotations

import os
import unittest
from unittest import mock

from gantry.docker import _DEFAULT_DOCKER_PASS_ENV, _pass_env_args, _pass_env_names


class TestDockerPassEnv(unittest.TestCase):
    def test_default_names_are_generic_vendor_keys(self):
        names = set(_DEFAULT_DOCKER_PASS_ENV)
        self.assertIn("GH_TOKEN", names)
        self.assertIn("ANTHROPIC_API_KEY", names)
        self.assertIn("ANTHROPIC_AUTH_TOKEN", names)
        self.assertIn("ANTHROPIC_BASE_URL", names)
        self.assertIn("OPENAI_API_KEY", names)
        self.assertIn("CURSOR_API_KEY", names)
        # No org-specific hardcoding.
        self.assertNotIn("TFY_API_KEY", names)
        self.assertFalse(any("edupaid" in n.lower() for n in names))

    def test_pass_env_args_only_forwards_set_vars(self):
        with mock.patch.dict(os.environ, {
            "GH_TOKEN": "gh-secret",
            "OPENAI_API_KEY": "oai-secret",
            "ANTHROPIC_API_KEY": "",  # empty = skip
        }, clear=False):
            # Isolate to only these names for a stable assertion.
            with mock.patch.dict(os.environ, {"GANTRY_DOCKER_PASS_ENV": "GH_TOKEN,OPENAI_API_KEY,ANTHROPIC_API_KEY"}):
                args = _pass_env_args()
        # Flatten pairs: -e KEY=val
        pairs = dict(args[i + 1].split("=", 1) for i in range(0, len(args), 2) if args[i] == "-e")
        self.assertEqual(pairs.get("GH_TOKEN"), "gh-secret")
        self.assertEqual(pairs.get("OPENAI_API_KEY"), "oai-secret")
        self.assertNotIn("ANTHROPIC_API_KEY", pairs)

    def test_gantry_docker_pass_env_override(self):
        with mock.patch.dict(os.environ, {"GANTRY_DOCKER_PASS_ENV": "FOO, BAR ,"}):
            self.assertEqual(_pass_env_names(), ["FOO", "BAR"])

    def test_empty_override_forwards_nothing(self):
        with mock.patch.dict(os.environ, {"GANTRY_DOCKER_PASS_ENV": "", "GH_TOKEN": "x" * 20}):
            self.assertEqual(_pass_env_names(), [])
            self.assertEqual(_pass_env_args(), [])


if __name__ == "__main__":
    unittest.main()
