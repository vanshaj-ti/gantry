import tempfile
import unittest
from pathlib import Path

from gantry.config import GantryConfig, StageModel, load_config


class TestBareRepoDefaults(unittest.TestCase):
    def test_missing_gantry_toml_returns_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(Path(tmp))
        self.assertIsInstance(cfg, GantryConfig)
        self.assertEqual(cfg.agent.runner, "claude-code")
        self.assertEqual(cfg.stages, ["plan", "build", "evidence", "review"])


class TestRunnerFor(unittest.TestCase):
    def test_falls_back_to_agent_runner_when_no_override(self):
        cfg = GantryConfig()
        cfg.agent.runner = "cursor-cli"
        self.assertEqual(cfg.runner_for("plan"), "cursor-cli")

    def test_per_stage_override_wins(self):
        cfg = GantryConfig()
        cfg.agent.runner = "claude-code"
        cfg.models["review"] = StageModel(model="", runner="codex-cli")
        self.assertEqual(cfg.runner_for("review"), "codex-cli")
        # unrelated stage still falls back
        self.assertEqual(cfg.runner_for("build"), "claude-code")

    def test_empty_runner_string_falls_back(self):
        cfg = GantryConfig()
        cfg.agent.runner = "claude-code"
        cfg.models["build"] = StageModel(model="", runner="")
        self.assertEqual(cfg.runner_for("build"), "claude-code")


class TestLoadConfigFromToml(unittest.TestCase):
    def test_per_stage_runner_override_from_toml(self):
        toml_text = """
[agent]
runner = "claude-code"

[models.review]
model = ""
runner = "codex-cli"
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gantry.toml"
            path.write_text(toml_text)
            cfg = load_config(Path(tmp))
        self.assertEqual(cfg.runner_for("review"), "codex-cli")
        self.assertEqual(cfg.runner_for("plan"), "claude-code")


if __name__ == "__main__":
    unittest.main()
