import tempfile
import unittest
from pathlib import Path

from gantry.config import DEFAULT_QUEUE_STAGES, GantryConfig
from gantry.engine import Engine
from gantry.pipeline import PipelineDefinition, PipelineMutation
from gantry.triage import BUILTIN_PIPELINES, decide, reassess_after_plan


class TestBuiltInPipelines(unittest.TestCase):
    def test_all_adaptive_profiles_are_defined(self):
        self.assertEqual(
            set(BUILTIN_PIPELINES),
            {"small", "medium", "large", "bug", "hotfix", "research"},
        )

    def test_profile_policies_match_task_shape(self):
        small = BUILTIN_PIPELINES["small"]
        self.assertEqual(small.definition_policy, "skip")
        self.assertTrue(small.checks_required)
        self.assertTrue(small.e2e_optional)
        self.assertEqual(small.evidence_policy, "standard")

        medium = BUILTIN_PIPELINES["medium"]
        self.assertEqual(medium.definition_policy, "combined")

        large = BUILTIN_PIPELINES["large"]
        self.assertEqual(large.definition_policy, "separate")
        self.assertEqual(large.human_gates, ("spec", "design"))

        bug = BUILTIN_PIPELINES["bug"]
        self.assertTrue(bug.requires_investigation)
        self.assertEqual(bug.stages[0], "investigation")

        hotfix = BUILTIN_PIPELINES["hotfix"]
        self.assertEqual(hotfix.plan_depth, "brief")
        self.assertEqual(hotfix.review_policy, "mandatory-fast-independent")
        self.assertEqual(hotfix.ship_policy, "staging")

        research = BUILTIN_PIPELINES["research"]
        self.assertEqual(research.stages, ("research",))
        self.assertFalse(research.allows_build_side_effects)
        self.assertEqual(research.human_gates, ("publication",))

    def test_legacy_queue_definitions_preserve_stage_mappings(self):
        cfg = GantryConfig()
        for tag, expected in DEFAULT_QUEUE_STAGES.items():
            self.assertEqual(list(decide(tag, tag, tag, None, cfg).stages), expected)


class TestTriagePrecedence(unittest.TestCase):
    def test_explicit_pipeline_override_wins_over_tag_and_risk(self):
        cfg = GantryConfig()
        result = decide(
            "production outage",
            "critical data loss",
            "hotfix",
            {"pipeline": "research"},
            cfg,
        )
        self.assertEqual(result.name, "research")

    def test_explicit_definition_object_wins(self):
        cfg = GantryConfig()
        explicit = PipelineDefinition("project-special", 7, ("plan",))
        self.assertIs(decide("bug", "urgent", "bug", {"definition": explicit}, cfg), explicit)

    def test_project_queue_override_is_preserved(self):
        cfg = GantryConfig()
        cfg.queues["feature"] = ["plan", "build"]
        result = decide("large migration", "high risk", "feature", None, cfg)
        self.assertEqual(result.name, "queue:feature")
        self.assertEqual(result.stages, ("plan", "build"))

    def test_deterministic_size_and_risk_rules(self):
        cfg = GantryConfig()
        self.assertEqual(decide("typo", "update docs", None, None, cfg).name, "small")
        self.assertEqual(decide("add export", "new feature endpoint", None, None, cfg).name, "medium")
        self.assertEqual(
            decide("auth migration", "cross-service breaking migration", None, None, cfg).name,
            "large",
        )

    def test_optional_classifier_hint_is_used_last(self):
        cfg = GantryConfig()
        cfg.profiles["classifier"] = {"enabled": True}
        result = decide("ambiguous request", "please assess this", None, {"classifier_result": "bug"}, cfg)
        self.assertEqual(result.name, "bug")


class TestPipelineEvolution(unittest.TestCase):
    def test_plan_risk_escalation_versions_and_appends_mutation(self):
        original = BUILTIN_PIPELINES["medium"]
        evolved = reassess_after_plan(
            original,
            risk="high",
            reason="plan touches authentication and migrations",
            completed_stages=("plan",),
        )
        self.assertEqual(evolved.version, original.version + 1)
        self.assertEqual(evolved.name, "large")
        self.assertEqual(evolved.completed_stages, ("plan",))
        self.assertEqual(len(evolved.mutations), 1)
        self.assertIsInstance(evolved.mutations[0], PipelineMutation)
        self.assertEqual(evolved.mutations[0].from_version, original.version)
        self.assertEqual(evolved.mutations[0].to_version, evolved.version)
        self.assertEqual(evolved.mutations[0].route_to, "spec")

    def test_engine_persists_definition_metadata_additively(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            engine = Engine(root, GantryConfig())
            run_id = engine.create_run("fix crash", "null pointer", tag="bug")
            state = engine.store.state(run_id)
        self.assertEqual(state["stages"], DEFAULT_QUEUE_STAGES["bug"])
        self.assertEqual(state["pipeline_name"], "bug")
        self.assertEqual(state["pipeline_version"], 1)
        self.assertEqual(state["definition_policy"], "skip")

    def test_engine_logs_escalation_without_rewriting_pinned_stages(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            engine = Engine(root, GantryConfig())
            run_id = engine.create_run("add export", "new feature endpoint")
            original_stages = engine.store.state(run_id)["stages"]
            state = engine.reassess_risk_after_plan(
                run_id,
                risk="high",
                reason="plan discovered authentication changes",
            )
            log = engine.store.read_result(run_id, "pipeline-mutations.json")
        self.assertEqual(state["stages"], original_stages)
        self.assertEqual(state["pipeline_version"], 2)
        self.assertEqual(state["pipeline_route_to"], "spec")
        self.assertEqual(log[0]["reason"], "plan discovered authentication changes")


if __name__ == "__main__":
    unittest.main()
