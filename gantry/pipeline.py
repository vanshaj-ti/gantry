"""Versioned, immutable pipeline definitions."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Literal

DefinitionPolicy = Literal["separate", "combined", "skip"]


@dataclass(frozen=True)
class PipelineMutation:
    """One append-only change to a pipeline selected for a run."""

    from_version: int
    to_version: int
    reason: str
    previous_name: str
    new_name: str
    route_to: str | None = None


@dataclass(frozen=True)
class PipelineDefinition:
    """Execution shape and policy pinned to a run.

    ``version`` changes whenever the selected shape changes. ``mutations`` and
    ``completed_stages`` are append-only so an escalation never rewrites work
    that has already happened.
    """

    name: str
    version: int
    stages: tuple[str, ...]
    definition_policy: DefinitionPolicy = "skip"
    requires_investigation: bool = False
    human_gates: tuple[str, ...] = ()
    checks_required: bool = True
    e2e_optional: bool = True
    evidence_policy: str = "standard"
    review_policy: str = "independent"
    ship_policy: str = "standard"
    plan_depth: str = "detailed"
    allows_build_side_effects: bool = True
    completed_stages: tuple[str, ...] = ()
    mutations: tuple[PipelineMutation, ...] = field(default_factory=tuple)

    def evolve(
        self,
        target: PipelineDefinition,
        *,
        reason: str,
        completed_stages: tuple[str, ...] = (),
        route_to: str | None = None,
    ) -> PipelineDefinition:
        """Create the next version while retaining immutable history."""
        next_version = self.version + 1
        mutation = PipelineMutation(
            from_version=self.version,
            to_version=next_version,
            reason=reason,
            previous_name=self.name,
            new_name=target.name,
            route_to=route_to,
        )
        completed = tuple(dict.fromkeys((*self.completed_stages, *completed_stages)))
        return replace(
            target,
            version=next_version,
            completed_stages=completed,
            mutations=(*self.mutations, mutation),
        )


def snapshot_definition(definition: PipelineDefinition) -> dict[str, object]:
    """Return a stable JSON-ready definition, including mutation history."""
    return asdict(definition)


def definition_from_snapshot(data: dict[str, object]) -> PipelineDefinition:
    """Restore a definition persisted in run state."""
    values = dict(data)
    for key in ("stages", "human_gates", "completed_stages"):
        values[key] = tuple(values.get(key, ()))
    values["mutations"] = tuple(
        PipelineMutation(**mutation)
        for mutation in values.get("mutations", ())
    )
    return PipelineDefinition(**values)


def materialize_stages(
    stages: list[str] | tuple[str, ...],
    definition_policy: DefinitionPolicy,
) -> list[str]:
    """Apply definition_policy to a queue/stage list.

    - ``skip``: drop spec/design/definition stages
    - ``combined``: replace contiguous spec+design (or either alone) with
      a single ``definition`` stage
    - ``separate``: keep spec/design as-is; drop any explicit definition
    """
    out: list[str] = []
    i = 0
    seq = list(stages)
    while i < len(seq):
        stage = seq[i]
        if definition_policy == "skip" and stage in ("spec", "design", "definition"):
            i += 1
            continue
        if definition_policy == "separate" and stage == "definition":
            i += 1
            continue
        if definition_policy == "combined":
            if stage == "definition":
                if "definition" not in out:
                    out.append("definition")
                i += 1
                continue
            if stage == "spec":
                if i + 1 < len(seq) and seq[i + 1] == "design":
                    i += 2
                else:
                    i += 1
                if "definition" not in out:
                    out.append("definition")
                continue
            if stage == "design":
                if "definition" not in out:
                    out.append("definition")
                i += 1
                continue
        out.append(stage)
        i += 1
    return out
