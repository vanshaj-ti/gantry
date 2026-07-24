import tempfile
import unittest
from pathlib import Path

from gantry.config import (
    DEFAULT_MCP_SERVERS,
    DEFAULT_QUEUE_STAGES,
    DEFAULT_SKILL_INSTALLERS,
    GantryConfig,
    StageModel,
    load_config,
)


class TestBareRepoDefaults(unittest.TestCase):
    def test_missing_gantry_toml_returns_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(Path(tmp))
        self.assertIsInstance(cfg, GantryConfig)
        self.assertEqual(cfg.agent.runner, "cursor-sdk")
        self.assertEqual(cfg.review.runner, "cursor-sdk")
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
    def test_absent_review_section_inherits_explicit_agent_runner(self):
        toml_text = """
[agent]
runner = "claude-code"
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gantry.toml"
            path.write_text(toml_text)
            cfg = load_config(Path(tmp))
        self.assertEqual(cfg.review.runner, "claude-code")

    def test_missing_agent_runner_defaults_to_cursor_sdk_and_review_inherits(self):
        toml_text = """
[agent]
skip_permissions = true

[review]
enabled = true
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gantry.toml"
            path.write_text(toml_text)
            cfg = load_config(Path(tmp))
        self.assertEqual(cfg.agent.runner, "cursor-sdk")
        self.assertEqual(cfg.review.runner, "cursor-sdk")

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


class TestNewConfigSectionsDefaultOff(unittest.TestCase):
    """Every new config-gated feature must be a no-op when its section is
    absent from gantry.toml, reproducing exactly today's default behavior."""

    def test_missing_sections_yield_documented_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(Path(tmp))
        self.assertEqual(cfg.agent.max_concurrent, 0)
        self.assertEqual(cfg.review.max_turns, 10)
        self.assertEqual(cfg.review.checklist, [])
        self.assertEqual(cfg.review.keyword_mode, "anywhere")
        self.assertEqual(cfg.plan.include_git_log, False)
        self.assertEqual(cfg.plan.depth, "detailed")
        self.assertEqual(cfg.build.pre_hook, "")
        self.assertEqual(cfg.build.pre_hook_required, False)
        self.assertEqual(cfg.evidence.output_format, "prose")
        self.assertEqual(cfg.skills.evidence_directive, "")
        self.assertEqual(cfg.checks.max_parallel, 4)
        # Fix 3: ship_retry_attempts defaults to 2 — the exact value
        # advance.py used to borrow from cfg.checks.resolve_attempts, so a
        # project that never sets this explicitly sees zero behavior change.
        self.assertEqual(cfg.git.ship_retry_attempts, 2)


class TestShipRetryAttempts(unittest.TestCase):
    def test_defaults_to_two_with_no_git_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(Path(tmp))
        self.assertEqual(cfg.git.ship_retry_attempts, 2)

    def test_configurable_via_toml(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gantry.toml"
            path.write_text("""
[git]
auto_ship = true
ship_retry_attempts = 5
""")
            cfg = load_config(Path(tmp))
        self.assertEqual(cfg.git.ship_retry_attempts, 5)
        self.assertTrue(cfg.git.auto_ship)

    def test_other_git_fields_still_default_when_ship_retry_attempts_unset(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gantry.toml"
            path.write_text("""
[git]
base_branch = "staging"
""")
            cfg = load_config(Path(tmp))
        self.assertEqual(cfg.git.base_branch, "staging")
        self.assertEqual(cfg.git.ship_retry_attempts, 2)

    def test_template_gantry_toml_parses_with_new_field(self):
        from gantry.cli._shared import TEMPLATE_DIR
        tmpl = TEMPLATE_DIR / "gantry.toml"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gantry.toml"
            path.write_text(tmpl.read_text())
            cfg = load_config(Path(tmp))
        self.assertEqual(cfg.git.ship_retry_attempts, 2)


class TestQueueDefaults(unittest.TestCase):
    """The 5 fixed generic queues (feature/bug/hotfix/research/chore) ship
    built into gantry — no gantry.toml [queues.*] required to get them."""

    def test_bare_config_carries_all_5_built_in_queues(self):
        cfg = GantryConfig()
        self.assertEqual(cfg.queues, DEFAULT_QUEUE_STAGES)

    def test_missing_gantry_toml_still_carries_built_in_queues(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(Path(tmp))
        self.assertEqual(cfg.queues, DEFAULT_QUEUE_STAGES)

    def test_stages_for_bug_uses_investigation_led_pipeline(self):
        cfg = GantryConfig()
        self.assertEqual(cfg.stages_for("bug"),
                         ["investigation", "plan", "build", "evidence", "review"])

    def test_stages_for_hotfix_skips_review(self):
        cfg = GantryConfig()
        self.assertEqual(cfg.stages_for("hotfix"), ["build", "evidence"])

    def test_stages_for_none_or_unknown_tag_falls_back_to_project_stages(self):
        cfg = GantryConfig()
        cfg.stages = ["plan", "build"]
        self.assertEqual(cfg.stages_for(None), ["plan", "build"])
        self.assertEqual(cfg.stages_for("not_a_real_tag"), ["plan", "build"])

    def test_toml_queues_override_merges_not_replaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gantry.toml"
            path.write_text("""
[queues.bug]
stages = ["investigation", "plan", "build"]
""")
            cfg = load_config(Path(tmp))
        # overridden tag reflects the project's own list
        self.assertEqual(cfg.stages_for("bug"), ["investigation", "plan", "build"])
        # every other tag keeps its built-in default, untouched
        for tag in ("feature", "hotfix", "research", "chore"):
            self.assertEqual(cfg.stages_for(tag), DEFAULT_QUEUE_STAGES[tag])

    def test_template_gantry_toml_parses_with_built_in_queues_intact(self):
        from gantry.cli._shared import TEMPLATE_DIR
        tmpl = TEMPLATE_DIR / "gantry.toml"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gantry.toml"
            path.write_text(tmpl.read_text())
            cfg = load_config(Path(tmp))
        self.assertEqual(cfg.queues, DEFAULT_QUEUE_STAGES)


class TestNewConfigSectionsFromToml(unittest.TestCase):
    def _load(self, toml_text: str):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gantry.toml"
            path.write_text(toml_text)
            return load_config(Path(tmp))

    def test_plan_section(self):
        cfg = self._load("""
[plan]
include_git_log = true
git_log_lines = 5
context_files = ["README.md"]
depth = "brief"
""")
        self.assertTrue(cfg.plan.include_git_log)
        self.assertEqual(cfg.plan.git_log_lines, 5)
        self.assertEqual(cfg.plan.context_files, ["README.md"])
        self.assertEqual(cfg.plan.depth, "brief")

    def test_build_section(self):
        cfg = self._load("""
[build]
pre_hook = "npm ci"
pre_hook_required = true
""")
        self.assertEqual(cfg.build.pre_hook, "npm ci")
        self.assertTrue(cfg.build.pre_hook_required)

    def test_evidence_section(self):
        cfg = self._load("""
[evidence]
output_format = "structured"
""")
        self.assertEqual(cfg.evidence.output_format, "structured")

    def test_review_new_fields(self):
        cfg = self._load("""
[review]
max_turns = 20
checklist = ["confirm no secrets committed"]
keyword_mode = "line_start"
""")
        self.assertEqual(cfg.review.max_turns, 20)
        self.assertEqual(cfg.review.checklist, ["confirm no secrets committed"])
        self.assertEqual(cfg.review.keyword_mode, "line_start")

    def test_agent_max_concurrent(self):
        cfg = self._load("""
[agent]
max_concurrent = 3
""")
        self.assertEqual(cfg.agent.max_concurrent, 3)

    def test_skills_evidence_directive(self):
        cfg = self._load("""
[skills]
enabled = ["superpowers"]
evidence_directive = "Verify only, do not implement."
""")
        self.assertEqual(cfg.skills.evidence_directive, "Verify only, do not implement.")

    def test_default_skill_installers_cover_all_runners(self):
        sp = DEFAULT_SKILL_INSTALLERS["superpowers"]
        for runner in ("claude-code", "cursor-cli", "codex-cli"):
            self.assertIn(runner, sp, f"missing superpowers installer for {runner}")
        self.assertIn("codex", sp["codex-cli"])
        cfg = GantryConfig()
        self.assertIn("codex", cfg.skills.install_command("superpowers", "codex-cli") or "")

    def test_default_mcp_register_covers_codex(self):
        for name in ("codebase-memory", "chrome-devtools"):
            reg = DEFAULT_MCP_SERVERS[name]["register"]
            self.assertIn("codex-cli", reg)
            self.assertTrue(reg["codex-cli"].startswith("codex mcp add"))

    def test_e2e_apps_bare_string_and_table_shapes(self):
        cfg = self._load("""
[e2e]
enabled = true

[e2e.apps]
web = "npm run e2e"

[e2e.apps.api]
command = "npm run e2e:api"
retry = 2
spec_glob = "tests/e2e/api/*.spec.ts"
""")
        self.assertEqual(cfg.e2e.apps["web"], "npm run e2e")
        self.assertEqual(cfg.e2e.apps["api"]["command"], "npm run e2e:api")
        self.assertEqual(cfg.e2e.apps["api"]["retry"], 2)

    def test_checks_max_parallel(self):
        cfg = self._load("""
[checks]
commands = ["npm run lint"]
max_parallel = 8
""")
        self.assertEqual(cfg.checks.max_parallel, 8)

    def test_profiles_section_is_loaded_additively(self):
        cfg = self._load("""
[agent]
runner = "claude-code"

[models.build]
model = "legacy-builder"
max_turns = 80

[profiles.planner-builder]
model = "specialist-builder"
prompt_preamble = "Use the approved plan."
skills = ["superpowers"]
mcp = ["codebase-memory"]
setting_sources = ["project", "team"]
permissions = "prompt"
sandbox = "workspace-write"
timeout = 1200
turn_budget = 100
""")
        self.assertEqual(cfg.agent.runner, "claude-code")
        self.assertEqual(cfg.models["build"].model, "legacy-builder")
        self.assertEqual(cfg.profiles["planner-builder"]["model"], "specialist-builder")
        profile = cfg.profile_for("build")
        self.assertEqual(profile.backend, "claude-code")
        self.assertEqual(profile.model, "specialist-builder")
        self.assertEqual(profile.setting_sources, ("project", "team"))

    def test_missing_profiles_section_keeps_empty_overrides(self):
        cfg = self._load("[agent]\nrunner = \"codex-cli\"\n")
        self.assertEqual(cfg.profiles, {})
        self.assertEqual(cfg.profile_for("plan").backend, "codex-cli")


class TestCoerceHelpers(unittest.TestCase):
    def test_coerce_check_command_from_string(self):
        from gantry.config import CheckCommand, _coerce_check_command
        result = _coerce_check_command("npm run lint")
        self.assertEqual(result, CheckCommand(command="npm run lint"))

    def test_coerce_check_command_from_table(self):
        from gantry.config import _coerce_check_command
        result = _coerce_check_command({"command": "npm test", "timeout": 60, "parallel": True})
        self.assertEqual(result.command, "npm test")
        self.assertEqual(result.timeout, 60)
        self.assertTrue(result.parallel)

    def test_coerce_check_command_idempotent_on_already_coerced(self):
        from gantry.config import CheckCommand, _coerce_check_command
        original = CheckCommand(command="x", timeout=5, parallel=True)
        self.assertIs(_coerce_check_command(original), original)

    def test_coerce_e2e_app_from_string(self):
        from gantry.config import E2eAppConfig, _coerce_e2e_app
        result = _coerce_e2e_app("npm run e2e")
        self.assertEqual(result, E2eAppConfig(command="npm run e2e"))

    def test_coerce_e2e_app_from_table(self):
        from gantry.config import _coerce_e2e_app
        result = _coerce_e2e_app({"command": "npm run e2e", "retry": 2, "spec_glob": "x/*.spec.ts"})
        self.assertEqual(result.command, "npm run e2e")
        self.assertEqual(result.retry, 2)
        self.assertEqual(result.spec_glob, "x/*.spec.ts")


class TestProxyConfig(unittest.TestCase):
    def test_missing_proxy_section_yields_empty_dict(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(Path(tmp))
        self.assertEqual(cfg.proxy, {})

    def test_proxy_claude_code_and_codex_cli_parsed(self):
        toml_text = """
[proxy.claude-code]
base_url = "https://gateway.example.com"
api_key_env = "MY_ANTHROPIC_TOKEN"

[proxy.codex-cli]
base_url = "https://gateway.example.com"
api_key_env = "MY_OPENAI_TOKEN"
headers = { "X-My-Header" = "value" }
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gantry.toml"
            path.write_text(toml_text)
            cfg = load_config(Path(tmp))
        self.assertEqual(cfg.proxy["claude-code"].base_url, "https://gateway.example.com")
        self.assertEqual(cfg.proxy["claude-code"].api_key_env, "MY_ANTHROPIC_TOKEN")
        self.assertEqual(cfg.proxy["codex-cli"].headers, {"X-My-Header": "value"})

    def test_proxy_cursor_cli_ignored(self):
        toml_text = """
[proxy.cursor-cli]
base_url = "https://gateway.example.com"
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gantry.toml"
            path.write_text(toml_text)
            cfg = load_config(Path(tmp))
        self.assertNotIn("cursor-cli", cfg.proxy)

    def test_coerce_proxy_direct(self):
        from gantry.config import ProxyConfig, _coerce_proxy
        out = _coerce_proxy({
            "claude-code": {"base_url": "https://x", "api_key_env": "TOK"},
            "cursor-cli": {"base_url": "https://y"},
        })
        self.assertEqual(out, {"claude-code": ProxyConfig(base_url="https://x", api_key_env="TOK")})


if __name__ == "__main__":
    unittest.main()
