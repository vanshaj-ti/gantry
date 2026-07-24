"""Responsibility-based routing for human and reviewer feedback."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .config import AGENT_STAGES, DEFAULT_QUEUE_STAGES, DOC_STAGES


FINDING_TARGETS = {
    "requirement": "spec",
    "architecture": "design",
    "diagnosis": "investigation",
    "approach": "plan",
    "scope": "plan",
    "implementation": "build",
    "proof": "evidence",
}

_OPTION_TEXT = {
    "approve": "approve and let the run continue",
    "revise": "send guidance to send it back for changes",
    "retry": "re-check now",
    "retry_stage": "retry the same stage",
    "retry_ship": "re-check and re-attempt ship",
    "hold": "leave it — you'll investigate yourself",
    "answer": "reply with your answer",
}


@dataclass(frozen=True)
class FeedbackRoute:
    """One resolved feedback destination and its human interaction contract."""

    status: str
    blocked_reason: str | None
    finding_category: str | None
    task_profile: str | None
    target_stage: str
    artifact: str
    resume_policy: str
    reply_options: tuple[str, ...]
    next_state: str


@dataclass(frozen=True)
class _RouteRule:
    statuses: tuple[str, ...]
    target_stage: str
    reply_options: tuple[str, ...]
    blocked_reasons: tuple[str, ...] = ()
    resume_policy: str = "resume"
    next_state: str | None = None
    artifact: str | None = None


_ROUTE_RULES = (
    _RouteRule(("blocked",), "build", ("retry", "revise"), ("scope", "checks", "e2e")),
    _RouteRule(("checks_failed", "e2e_failed"), "build", ("retry", "revise")),
    _RouteRule(
        ("checks_escalated", "e2e_escalated", "resolve_escalated"),
        "build", ("revise", "hold"),
    ),
    _RouteRule(
        ("checks_high_risk_escalated",), "build", ("approve", "revise"),
        ("high_risk_paths",),
    ),
    _RouteRule(("ship_checks_failed",), "build", ("retry_ship", "revise")),
    _RouteRule(
        ("ship_failed",), "ship", ("retry_ship", "hold"),
        resume_policy="retry", next_state="review_approved", artifact="review-comments.md",
    ),
    _RouteRule(
        ("review_escalated",), "build", ("approve", "revise"),
        artifact="review-comments.md",
    ),
)


def _profile_stages(task_profile: str | None) -> list[str]:
    if not task_profile:
        return []
    return list(DEFAULT_QUEUE_STAGES.get(task_profile, ()))


def _fallback_stage(preferred: str, task_profile: str | None) -> str:
    stages = _profile_stages(task_profile)
    if not stages or task_profile == "feature" or preferred in stages:
        return preferred
    responsibility_fallbacks = {
        "spec": ("plan", "build", "research"),
        "design": ("plan", "build", "research"),
        "investigation": ("plan", "build", "research"),
        "plan": ("build", "research"),
        "build": ("research",),
        "evidence": ("build", "research"),
    }
    return next(
        (stage for stage in responsibility_fallbacks.get(preferred, ()) if stage in stages),
        next((stage for stage in stages if stage != "review"), preferred),
    )


def _make_route(
    status: str,
    stage: str,
    options: Iterable[str],
    *,
    blocked_reason: str | None,
    finding_category: str | None,
    task_profile: str | None,
    resume_policy: str = "resume",
    next_state: str | None = None,
    artifact: str | None = None,
) -> FeedbackRoute:
    target = _fallback_stage(stage, task_profile)
    if target != stage:
        artifact = None
        next_state = None
    return FeedbackRoute(
        status=status,
        blocked_reason=blocked_reason,
        finding_category=finding_category,
        task_profile=task_profile,
        target_stage=target,
        artifact=artifact or f"answers/{target}.md",
        resume_policy=resume_policy,
        reply_options=tuple(options),
        next_state=next_state or f"{target}_running",
    )


def route_feedback(
    status: str,
    blocked_reason: str | None = None,
    finding_category: str | None = None,
    task_profile: str | None = None,
) -> FeedbackRoute:
    """Resolve feedback by responsibility, then by the current failure state."""
    category = (finding_category or "").strip().lower() or None
    if category in FINDING_TARGETS:
        return _make_route(
            status, FINDING_TARGETS[category], ("approve", "revise"),
            blocked_reason=blocked_reason, finding_category=category,
            task_profile=task_profile,
        )

    for rule in _ROUTE_RULES:
        if status not in rule.statuses:
            continue
        if (
            status == "blocked" and rule.blocked_reasons
            and blocked_reason and blocked_reason not in rule.blocked_reasons
        ):
            continue
        return _make_route(
            status, rule.target_stage, rule.reply_options,
            blocked_reason=blocked_reason, finding_category=None,
            task_profile=task_profile, resume_policy=rule.resume_policy,
            next_state=rule.next_state, artifact=rule.artifact,
        )

    if status.endswith("_complete") and status.removesuffix("_complete") in DOC_STAGES:
        stage = status.removesuffix("_complete")
        return _make_route(
            status, stage, ("approve", "revise"), blocked_reason=blocked_reason,
            finding_category=None, task_profile=task_profile,
        )
    if status.endswith("_question"):
        stage = status.removesuffix("_question")
        return _make_route(
            status, stage, ("answer",), blocked_reason=blocked_reason,
            finding_category=None, task_profile=task_profile,
        )
    if status.endswith("_failed"):
        stage = status.removesuffix("_failed")
        return _make_route(
            status, stage, ("retry_stage", "hold"), blocked_reason=blocked_reason,
            finding_category=None, task_profile=task_profile, resume_policy="retry",
        )

    current = status.removeprefix("awaiting_").split("_", 1)[0] or "build"
    if current not in DOC_STAGES | AGENT_STAGES:
        current = "build"
    return _make_route(
        status, current, ("answer",), blocked_reason=blocked_reason,
        finding_category=None, task_profile=task_profile,
    )


def finding_category(review_result: dict | None) -> str | None:
    """Return the first actionable structured finding category, deterministically."""
    if not isinstance(review_result, dict):
        return None
    axes = review_result.get("axes")
    sources = axes.values() if isinstance(axes, dict) else (review_result,)
    categories = []
    for axis in sources:
        if not isinstance(axis, dict):
            continue
        for finding in axis.get("findings") or ():
            if isinstance(finding, dict) and finding.get("action") in ("blocking", "ask-user"):
                category = str(finding.get("category") or "").strip().lower()
                if category in FINDING_TARGETS:
                    categories.append(category)
    priority = tuple(FINDING_TARGETS)
    return next((category for category in priority if category in categories), None)


def route_for_state(state: dict, review_result: dict | None = None) -> FeedbackRoute:
    return route_feedback(
        str(state.get("status") or ""),
        blocked_reason=state.get("blocked_on"),
        finding_category=finding_category(review_result),
        task_profile=state.get("tag"),
    )


_LINEAR_KEYWORDS = {
    "approve": "`approve`",
    "revise": "`revise` or write guidance in plain English",
    "retry": "`retry`",
    "retry_stage": "`retry`",
    "retry_ship": "`retry ship`",
    "hold": "`hold`",
    "answer": "reply with your answer",
}


def reply_prompt(route: FeedbackRoute, channel: str = "notification") -> str:
    """Render the route's choices for Telegram, CLI/watch, or Linear."""
    descriptions = [_OPTION_TEXT[option] for option in route.reply_options]
    if channel == "notification":
        return "\n".join(
            f"*Reply {index}* to {description}."
            for index, description in enumerate(descriptions, 1)
        )
    if channel == "linear":
        # Linear tickets use keyword / prose replies — numbered 1/2 is a
        # Telegram-era UX that feels wrong in an issue thread.
        lines = ["How to reply (comment on this issue):"]
        for option, description in zip(route.reply_options, descriptions):
            keyword = _LINEAR_KEYWORDS.get(option, f"`{option}`")
            lines.append(f"- {keyword} — {description}")
        return "\n".join(lines)
    if channel == "watch":
        return "\n".join(
            f"{index}. {description}" for index, description in enumerate(descriptions, 1)
        )
    raise ValueError(f"unknown feedback channel: {channel}")


def feedback_artifacts_for_stage(stage: str) -> tuple[tuple[str, str], ...]:
    """Artifacts consumed by a resumed stage, in stable precedence order."""
    heading = (
        "Checks/e2e failure detail for this resumed stage"
        if stage == "build"
        else "Routed feedback for this resumed stage"
    )
    artifacts = [(f"answers/{stage}.md", heading)]
    if stage == "build":
        artifacts.append(("review-comments.md", "Revision comments for this resumed stage"))
    return tuple(artifacts)


def _needs_input_statuses() -> set[str]:
    statuses = {status for rule in _ROUTE_RULES for status in rule.statuses}
    stages = DOC_STAGES | AGENT_STAGES | {"resolve"}
    statuses.update(f"{stage}_failed" for stage in stages)
    statuses.update(f"{stage}_question" for stage in stages)
    statuses.update(f"{stage}_complete" for stage in DOC_STAGES)
    return statuses


NEEDS_INPUT_STATUSES = frozenset(_needs_input_statuses())

