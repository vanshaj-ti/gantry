import unittest
from dataclasses import FrozenInstanceError

from gantry.config import GantryConfig, MCPServer, StageModel
from gantry.profiles import (
    PROFILE_VERSION,
    AgentProfile,
    profile_for,
    profile_for_stage,
    role_for_stage,
    snapshot_profile,
)


class TestAgentProfileDefaults(unittest.TestCase):
    def test_all_specialist_roles_resolve(self):
        cfg = GantryConfig()
        roles = (
            "spec", "design", "investigator", "researcher", "planner-builder",
            "resolver", "evidence", "review-spec", "review-standards",
            "classifier", "ship-metadata",
        )
        self.assertEqual([profile_for(role, cfg).role for role in roles], list(roles))

    def test_profile_is_immutable(self):
        profile = profile_for("spec", GantryConfig())
        with self.assertRaises(FrozenInstanceError):
            profile.backend = "codex-cli"  # type: ignore[misc]

    def test_unknown_role_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unknown agent profile role"):
            profile_for("unknown", GantryConfig())

    def test_bare_config_uses_cursor_sdk_and_project_settings_only(self):
        profile = profile_for_stage("plan", GantryConfig())
        self.assertEqual(profile.backend, "cursor-sdk")
        self.assertEqual(profile.setting_sources, ("project",))
        self.assertEqual(profile.version, PROFILE_VERSION)


class TestLegacyConfigCompilation(unittest.TestCase):
    def test_stage_runner_model_and_budget_compile_into_profile(self):
        cfg = GantryConfig()
        cfg.agent.runner = "claude-code"
        cfg.models["plan"] = StageModel(
            model="planner-model", runner="codex-cli", max_turns=17, timeout=123,
            plan_mode=True,
        )
        profile = profile_for_stage("plan", cfg)
        self.assertEqual(profile.role, "planner-builder")
        self.assertEqual(profile.backend, "codex-cli")
        self.assertEqual(profile.model, "planner-model")
        self.assertEqual(profile.turn_budget, 17)
        self.assertEqual(profile.timeout, 123)

    def test_plan_and_build_keep_distinct_legacy_models(self):
        cfg = GantryConfig()
        cfg.models["plan"] = StageModel(model="plan-model")
        cfg.models["build"] = StageModel(model="build-model")
        self.assertEqual(profile_for_stage("plan", cfg).model, "plan-model")
        self.assertEqual(profile_for_stage("build", cfg).model, "build-model")

    def test_review_axes_compile_review_config(self):
        cfg = GantryConfig()
        cfg.review.runner = "codex-cli"
        cfg.review.model = "reviewer"
        cfg.review.max_turns = 8
        cfg.review.timeout = 321
        for stage in ("review_spec", "review_standards"):
            profile = profile_for_stage(stage, cfg)
            self.assertEqual(profile.backend, "codex-cli")
            self.assertEqual(profile.model, "reviewer")
            self.assertEqual(profile.turn_budget, 8)
            self.assertEqual(profile.timeout, 321)

    def test_resolver_preserves_build_fallback_and_doubled_budget(self):
        cfg = GantryConfig()
        cfg.models["build"] = StageModel(model="builder", max_turns=21, timeout=456)
        profile = profile_for("resolver", cfg)
        self.assertEqual(profile.model, "builder")
        self.assertEqual(profile.turn_budget, 42)
        self.assertEqual(profile.timeout, 456)

    def test_legacy_skills_and_mcp_merge_with_stage_requirements_stably(self):
        cfg = GantryConfig()
        cfg.skills.enabled = ["superpowers", "gantry-stage-build"]
        cfg.mcp.enabled = ["memory", "browser"]
        cfg.mcp.servers = {
            "memory": MCPServer(stages=["build"]),
            "browser": MCPServer(stages=["evidence"]),
        }
        profile = profile_for_stage("build", cfg)
        self.assertEqual(
            profile.skills,
            ("gantry-stage-build", "superpowers"),
        )
        self.assertEqual(profile.mcp, ("memory",))


class TestExplicitProfileOverrides(unittest.TestCase):
    def test_override_is_additive_and_does_not_mutate_legacy_config(self):
        cfg = GantryConfig()
        cfg.agent.runner = "claude-code"
        cfg.models["build"] = StageModel(model="legacy", max_turns=40)
        cfg.profiles["planner-builder"] = {
            "model": "specialist",
            "prompt_preamble": "Build carefully.",
            "skills": ["custom", "gantry-stage-build"],
            "mcp": ["browser"],
            "setting_sources": ["project", "team"],
            "permissions": "prompt",
            "sandbox": "workspace-write",
            "timeout": 321,
            "turn_budget": 12,
        }

        profile = profile_for_stage("build", cfg)

        self.assertEqual(profile.backend, "claude-code")
        self.assertEqual(profile.model, "specialist")
        self.assertEqual(profile.prompt_preamble, "Build carefully.")
        self.assertEqual(
            profile.skills,
            ("gantry-stage-build", "custom"),
        )
        self.assertEqual(profile.mcp, ("browser",))
        self.assertEqual(profile.setting_sources, ("project", "team"))
        self.assertEqual(profile.permissions, "prompt")
        self.assertEqual(profile.sandbox, "workspace-write")
        self.assertEqual(profile.timeout, 321)
        self.assertEqual(profile.turn_budget, 12)
        self.assertEqual(cfg.models["build"].model, "legacy")

    def test_role_mapping(self):
        expected = {
            "spec": "spec",
            "design": "design",
            "investigation": "investigator",
            "research": "researcher",
            "plan": "planner-builder",
            "build": "planner-builder",
            "resolve": "resolver",
            "evidence": "evidence",
            "review_spec": "review-spec",
            "review_standards": "review-standards",
            "classifier": "classifier",
            "ship_metadata": "ship-metadata",
        }
        self.assertEqual({stage: role_for_stage(stage) for stage in expected}, expected)

    def test_snapshot_is_deterministic_and_json_ready(self):
        profile = AgentProfile(
            role="spec", backend="cursor-sdk", model="x", skills=("b", "a"),
            mcp=("z",), setting_sources=("project",), turn_budget=9,
        )
        first = snapshot_profile(profile)
        second = snapshot_profile(profile)
        self.assertEqual(first, second)
        self.assertEqual(first["skills"], ["b", "a"])
        self.assertEqual(first["mcp"], ["z"])


if __name__ == "__main__":
    unittest.main()
