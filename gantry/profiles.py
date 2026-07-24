"""Versioned specialist agent profiles and legacy-config compilation."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any, Iterable

if TYPE_CHECKING:
    from .config import GantryConfig, StageModel

PROFILE_VERSION = 1

ROLES = (
    "spec",
    "design",
    "investigator",
    "researcher",
    "planner-builder",
    "resolver",
    "evidence",
    "review-spec",
    "review-standards",
    "classifier",
    "ship-metadata",
)

_STAGE_ROLES = {
    "spec": "spec",
    "design": "design",
    "investigation": "investigator",
    "investigator": "investigator",
    "research": "researcher",
    "researcher": "researcher",
    "plan": "planner-builder",
    "build": "planner-builder",
    "planner-builder": "planner-builder",
    "resolve": "resolver",
    "resolver": "resolver",
    "evidence": "evidence",
    "review": "review-spec",
    "review_spec": "review-spec",
    "review-spec": "review-spec",
    "review_standards": "review-standards",
    "review-standards": "review-standards",
    "classifier": "classifier",
    "ship": "ship-metadata",
    "ship_metadata": "ship-metadata",
    "ship-metadata": "ship-metadata",
}

_DEFAULT_STAGE = {
    "spec": "spec",
    "design": "design",
    "investigator": "investigation",
    "researcher": "research",
    "planner-builder": "plan",
    "resolver": "resolve",
    "evidence": "evidence",
    "review-spec": "review_spec",
    "review-standards": "review_standards",
    "classifier": "classifier",
    "ship-metadata": "ship_metadata",
}

@dataclass(frozen=True)
class AgentProfile:
    """Complete, immutable policy for one specialist invocation."""

    role: str
    version: int = PROFILE_VERSION
    backend: str = "cursor-sdk"
    model: str = ""
    prompt_preamble: str = ""
    skills: tuple[str, ...] = ()
    mcp: tuple[str, ...] = ()
    setting_sources: tuple[str, ...] = ("project",)
    permissions: str = "allow"
    sandbox: str = "workspace-write"
    timeout: int = 900
    turn_budget: int = 60


def _ordered_union(*groups: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    merged: list[str] = []
    for group in groups:
        for item in group:
            if item not in seen:
                seen.add(item)
                merged.append(item)
    return tuple(merged)


def role_for_stage(stage: str) -> str:
    """Map a pipeline/invocation stage name to its specialist role."""
    try:
        return _STAGE_ROLES[stage]
    except KeyError as exc:
        raise ValueError(f"Unknown agent stage: {stage!r}") from exc


def _legacy_model(cfg: GantryConfig, role: str, stage: str) -> StageModel:
    if role == "resolver":
        return cfg.models.get("resolve") or cfg.model_for("build")
    return cfg.model_for(stage)


def _legacy_execution(cfg: GantryConfig, role: str, stage: str) -> tuple[str, str, int, int]:
    if role in ("review-spec", "review-standards"):
        return cfg.review.runner, cfg.review.model, cfg.review.max_turns, cfg.review.timeout
    if role == "ship-metadata":
        return cfg.review.runner, cfg.review.model, 10, 900
    if role == "classifier":
        model = cfg.models.get("classifier")
        return (
            (model.runner if model and model.runner else cfg.agent.runner),
            model.model if model else "",
            model.max_turns if model else 1,
            model.timeout if model else 900,
        )
    model = _legacy_model(cfg, role, stage)
    turns = model.max_turns * 2 if role == "resolver" else model.max_turns
    return model.runner or cfg.agent.runner, model.model, turns, model.timeout


def _stage_skills(cfg: GantryConfig, stage: str) -> tuple[str, ...]:
    required = (f"gantry-stage-{stage}",) if stage in {
        "spec", "design", "investigation", "research", "plan", "build", "evidence",
    } else ()
    legacy = cfg.skills.enabled if stage in ("build", "evidence") else ()
    return _ordered_union(required, legacy)


def _stage_mcp(cfg: GantryConfig, stage: str) -> tuple[str, ...]:
    mcp_stage = "review" if stage in ("review_spec", "review_standards") else stage
    return tuple(cfg.mcp.for_stage(mcp_stage))


def profile_for(
    role: str,
    cfg: GantryConfig | None = None,
    *,
    stage: str | None = None,
) -> AgentProfile:
    """Resolve a specialist profile from defaults, legacy config, then overrides."""
    if role not in ROLES:
        raise ValueError(f"Unknown agent profile role: {role!r}")
    if cfg is None:
        from .config import GantryConfig
        cfg = GantryConfig()

    stage = stage or _DEFAULT_STAGE[role]
    backend, model, turn_budget, timeout = _legacy_execution(cfg, role, stage)
    permissions = (
        "prompt"
        if role == "ship-metadata"
        else ("allow" if cfg.agent.skip_permissions else "prompt")
    )
    sandbox = "read-only" if role in {
        "evidence", "review-spec", "review-standards", "classifier", "ship-metadata",
    } else "workspace-write"
    implicit_skills = _stage_skills(cfg, stage)
    implicit_mcp = _stage_mcp(cfg, stage)
    override: dict[str, Any] = dict(cfg.profiles.get(role, {}))

    return AgentProfile(
        role=role,
        version=int(override.get("version", PROFILE_VERSION)),
        backend=override.get("backend", override.get("runner", backend)),
        model=override.get("model", model),
        # Empty is the compatibility preamble: legacy prompts remain byte-for-byte
        # unchanged until a project opts into a role-specific override.
        prompt_preamble=override.get("prompt_preamble", ""),
        skills=_ordered_union(implicit_skills, override.get("skills", ())),
        mcp=_ordered_union(implicit_mcp, override.get("mcp", override.get("mcp_servers", ()))),
        setting_sources=tuple(override.get("setting_sources", ("project",))),
        permissions=override.get("permissions", permissions),
        sandbox=override.get("sandbox", sandbox),
        timeout=int(override.get("timeout", timeout)),
        turn_budget=int(override.get("turn_budget", override.get("max_turns", turn_budget))),
    )


def profile_for_stage(stage: str, cfg: GantryConfig | None = None) -> AgentProfile:
    """Resolve the role and legacy per-stage settings for an invocation stage."""
    return profile_for(role_for_stage(stage), cfg, stage=stage)


def snapshot_profile(profile: AgentProfile) -> dict[str, Any]:
    """Return a stable, JSON-ready profile snapshot for invocation logs."""
    data = asdict(profile)
    for key in ("skills", "mcp", "setting_sources"):
        data[key] = list(data[key])
    return data
