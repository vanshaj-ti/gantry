"""Deterministic adaptive task triage.

Resolution order is explicit overrides, project queue/tag policy,
deterministic task/risk rules, then an optional classifier hint.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping

from .config import DEFAULT_QUEUE_STAGES, GantryConfig
from .pipeline import PipelineDefinition

PIPELINE_VERSION = 1

BUILTIN_PIPELINES: dict[str, PipelineDefinition] = {
    "small": PipelineDefinition(
        name="small",
        version=PIPELINE_VERSION,
        stages=tuple(DEFAULT_QUEUE_STAGES["chore"]),
        definition_policy="skip",
    ),
    "medium": PipelineDefinition(
        name="medium",
        version=PIPELINE_VERSION,
        stages=tuple(DEFAULT_QUEUE_STAGES["feature"]),
        definition_policy="combined",
        human_gates=("definition",),
    ),
    "large": PipelineDefinition(
        name="large",
        version=PIPELINE_VERSION,
        stages=tuple(DEFAULT_QUEUE_STAGES["feature"]),
        definition_policy="separate",
        human_gates=("spec", "design"),
        evidence_policy="expanded",
    ),
    "bug": PipelineDefinition(
        name="bug",
        version=PIPELINE_VERSION,
        stages=tuple(DEFAULT_QUEUE_STAGES["bug"]),
        requires_investigation=True,
        definition_policy="skip",
    ),
    "hotfix": PipelineDefinition(
        name="hotfix",
        version=PIPELINE_VERSION,
        stages=tuple(DEFAULT_QUEUE_STAGES["hotfix"]),
        definition_policy="skip",
        plan_depth="brief",
        evidence_policy="focused",
        review_policy="mandatory-fast-independent",
        ship_policy="staging",
    ),
    "research": PipelineDefinition(
        name="research",
        version=PIPELINE_VERSION,
        stages=tuple(DEFAULT_QUEUE_STAGES["research"]),
        definition_policy="skip",
        human_gates=("publication",),
        checks_required=False,
        e2e_optional=False,
        evidence_policy="research",
        review_policy="none",
        ship_policy="none",
        allows_build_side_effects=False,
    ),
}

_TAG_PROFILE = {
    "feature": "medium",
    "bug": "bug",
    "hotfix": "hotfix",
    "research": "research",
    "chore": "small",
}
_HIGH_RISK_TERMS = {
    "auth", "authentication", "authorization", "billing", "payment", "security",
    "migration", "breaking", "cross-service", "production", "data loss",
}
_SMALL_TERMS = {"typo", "docs", "documentation", "chore", "rename", "copy"}
_FEATURE_TERMS = {"add", "feature", "endpoint", "support", "implement", "new"}


def _project_queue(tag: str | None, cfg: GantryConfig) -> PipelineDefinition | None:
    if not tag or tag not in cfg.queues:
        return None
    stages = tuple(cfg.queues[tag])
    built_in = DEFAULT_QUEUE_STAGES.get(tag)
    if built_in is not None and stages == tuple(built_in):
        return None
    return PipelineDefinition(
        name=f"queue:{tag}",
        version=PIPELINE_VERSION,
        stages=stages,
        definition_policy="skip",
        requires_investigation=bool(stages and stages[0] == "investigation"),
    )


def _deterministic_profile(title: str, request: str) -> str | None:
    text = f"{title} {request}".lower()
    if any(term in text for term in _HIGH_RISK_TERMS):
        return "large"
    if any(term in text for term in _SMALL_TERMS):
        return "small"
    if any(term in text for term in _FEATURE_TERMS):
        return "medium"
    return None


def _apply_field_overrides(
    definition: PipelineDefinition,
    overrides: Mapping[str, Any],
) -> PipelineDefinition:
    allowed = {
        "definition_policy", "requires_investigation", "human_gates",
        "checks_required", "e2e_optional", "evidence_policy", "review_policy",
        "ship_policy", "plan_depth", "allows_build_side_effects", "stages",
    }
    values = {key: value for key, value in overrides.items() if key in allowed}
    for key in ("stages", "human_gates"):
        if key in values:
            values[key] = tuple(values[key])
    return replace(definition, **values) if values else definition


def decide(
    title: str,
    request: str,
    tag: str | None,
    overrides: Mapping[str, Any] | None,
    cfg: GantryConfig,
) -> PipelineDefinition:
    """Select a pipeline without invoking a non-deterministic classifier."""
    overrides = overrides or {}
    explicit = overrides.get("definition")
    if isinstance(explicit, PipelineDefinition):
        return explicit
    pipeline_name = (
        overrides.get("pipeline")
        or overrides.get("profile")
        or overrides.get("task_profile")
    )
    if pipeline_name:
        if pipeline_name not in BUILTIN_PIPELINES:
            raise ValueError(f"unknown pipeline override: {pipeline_name!r}")
        return _apply_field_overrides(BUILTIN_PIPELINES[pipeline_name], overrides)

    project = _project_queue(tag, cfg)
    if project is not None:
        return _apply_field_overrides(project, overrides)

    tagged_profile = _TAG_PROFILE.get(tag or "")
    if tagged_profile:
        explicit_risk = str(overrides.get("risk", "")).lower()
        if tagged_profile == "medium" and (
            explicit_risk in {"high", "critical"}
            or _deterministic_profile(title, request) == "large"
        ):
            tagged_profile = "large"
        return _apply_field_overrides(BUILTIN_PIPELINES[tagged_profile], overrides)

    deterministic = _deterministic_profile(title, request)
    if deterministic:
        return _apply_field_overrides(BUILTIN_PIPELINES[deterministic], overrides)

    classifier_enabled = bool(cfg.profiles.get("classifier", {}).get("enabled"))
    classifier_result = overrides.get("classifier_result") if classifier_enabled else None
    if classifier_result in _TAG_PROFILE:
        return BUILTIN_PIPELINES[_TAG_PROFILE[classifier_result]]
    if classifier_result in BUILTIN_PIPELINES:
        return BUILTIN_PIPELINES[classifier_result]
    return BUILTIN_PIPELINES["medium"]


def reassess_after_plan(
    definition: PipelineDefinition,
    *,
    risk: str,
    reason: str,
    completed_stages: tuple[str, ...] = (),
) -> PipelineDefinition:
    """Escalate after plan scope is known, preserving completed history."""
    if risk.lower() not in {"high", "critical"} or definition.name == "large":
        return definition
    return definition.evolve(
        BUILTIN_PIPELINES["large"],
        reason=reason,
        completed_stages=completed_stages,
        route_to="spec",
    )
