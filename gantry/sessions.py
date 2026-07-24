"""Task-scoped durable sessions with approved lineage topology.

Additive schema over legacy sessions.json records. Existing stage entries with
only ``session_id`` / ``model`` / ``runner`` remain readable; new fields are
optional and filled on write.
"""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from gantry.state import RunStore, now_iso

SCHEMA_VERSION = 2

# Approved topology (plan § Session topology):
# - Isolated: each revision resumes only its own session.
# - Shared implementation: plan → build → resolve share one lineage.
# - Fresh isolated: evidence and each review axis always get new identities.
LineageKind = Literal["isolated", "shared_implementation", "fresh"]

STAGE_LINEAGE: dict[str, LineageKind] = {
    "spec": "isolated",
    "design": "isolated",
    "definition": "isolated",
    "investigation": "isolated",
    "research": "isolated",
    "plan": "shared_implementation",
    "build": "shared_implementation",
    "resolve": "shared_implementation",
    "evidence": "fresh",
    "review": "fresh",
    "review_spec": "fresh",
    "review_standards": "fresh",
}

IMPLEMENTATION_LINEAGE_STAGES = ("plan", "build", "resolve")
IMPLEMENTATION_LINEAGE_ID = "implementation"


@dataclass
class SessionPolicy:
    """Whether a stage may resume a prior native backend session."""

    lineage: LineageKind
    allow_native_resume: bool
    shared_lineage_id: str | None = None
    reason: str = ""


@dataclass
class SessionRecord:
    """Additive session record persisted under sessions.json[<stage>]."""

    session_id: str | None = None
    model: str = ""
    runner: str = ""
    # Additive fields (schema v2+)
    schema_version: int = SCHEMA_VERSION
    gantry_session_id: str = ""
    backend_agent_id: str | None = None
    backend_run_id: str | None = None
    profile: str = ""
    profile_version: str = ""
    lineage: LineageKind | str = "isolated"
    lineage_id: str | None = None
    worktree_id: str = ""
    created_at: str = ""
    updated_at: str = ""
    terminal_status: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        extra = data.pop("extra", {}) or {}
        # Drop Nones for compact on-disk shape; keep empty strings for stable keys
        # that callers already expect (model/runner).
        out = {k: v for k, v in data.items() if v is not None and v != {}}
        out.update({k: v for k, v in extra.items() if v is not None})
        return out

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> SessionRecord:
        raw = dict(raw or {})
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        # Legacy records lack schema_version — treat as v1.
        if "schema_version" not in raw and (raw.get("session_id") or raw.get("runner") or raw.get("model")):
            raw.setdefault("schema_version", 1)
        fields = {k: raw[k] for k in list(raw) if k in known}
        extra = {k: v for k, v in raw.items() if k not in known}
        rec = cls(**fields)
        rec.extra = extra
        return rec


def lineage_for(stage: str) -> LineageKind:
    return STAGE_LINEAGE.get(stage, "isolated")


def policy_for(stage: str) -> SessionPolicy:
    kind = lineage_for(stage)
    if kind == "fresh":
        return SessionPolicy(
            lineage=kind,
            allow_native_resume=False,
            reason="evidence/review axes always start fresh",
        )
    if kind == "shared_implementation":
        return SessionPolicy(
            lineage=kind,
            allow_native_resume=True,
            shared_lineage_id=IMPLEMENTATION_LINEAGE_ID,
            reason="plan/build/resolve share implementation lineage",
        )
    return SessionPolicy(
        lineage=kind,
        allow_native_resume=True,
        reason="doc/investigation/research stages resume only their own revisions",
    )


def new_gantry_session_id() -> str:
    return f"gsess-{uuid.uuid4().hex[:16]}"


def migrate_record(raw: dict[str, Any] | None, *, stage: str) -> SessionRecord:
    """Upgrade a legacy or partial record to schema v2 in memory (additive)."""
    raw = dict(raw or {})
    had_explicit_lineage = "lineage" in raw
    rec = SessionRecord.from_dict(raw)
    pol = policy_for(stage)
    if not rec.gantry_session_id:
        rec.gantry_session_id = new_gantry_session_id()
    if not had_explicit_lineage:
        rec.lineage = pol.lineage
    if pol.shared_lineage_id and not rec.lineage_id:
        rec.lineage_id = pol.shared_lineage_id
    if rec.schema_version < SCHEMA_VERSION:
        rec.schema_version = SCHEMA_VERSION
    if not rec.backend_agent_id and rec.session_id:
        rec.backend_agent_id = rec.session_id
    return rec


@dataclass
class ResumeDecision:
    allowed: bool
    session_id: str | None
    reason: str
    fallback_to_artifacts: bool = False


def can_native_resume(
    *,
    stage: str,
    stored: SessionRecord | dict[str, Any] | None,
    backend: str,
    profile: str = "",
    model: str = "",
    worktree_id: str = "",
) -> ResumeDecision:
    """Reject native resume when identity/compatibility differs.

    Fall back to artifact-based continuation only via an explicit recorded
    transition (caller must log it) — never silently.
    """
    pol = policy_for(stage)
    if not pol.allow_native_resume:
        return ResumeDecision(False, None, pol.reason, fallback_to_artifacts=True)

    rec = stored if isinstance(stored, SessionRecord) else SessionRecord.from_dict(stored)
    if not rec.session_id:
        return ResumeDecision(False, None, "no prior session_id",
                              fallback_to_artifacts=(pol.lineage != "isolated"))

    stored_backend = rec.runner or ""
    if stored_backend and backend and stored_backend != backend:
        return ResumeDecision(
            False, None,
            f"backend mismatch: stored={stored_backend!r} requested={backend!r}",
            fallback_to_artifacts=True,
        )
    if rec.profile and profile and rec.profile != profile:
        return ResumeDecision(
            False, None,
            f"profile mismatch: stored={rec.profile!r} requested={profile!r}",
            fallback_to_artifacts=True,
        )
    if rec.worktree_id and worktree_id and rec.worktree_id != worktree_id:
        return ResumeDecision(
            False, None,
            f"worktree mismatch: stored={rec.worktree_id!r} requested={worktree_id!r}",
            fallback_to_artifacts=True,
        )
    # Model compatibility: exact match when both set; empty means "default" and is compatible.
    if rec.model and model and rec.model != model:
        return ResumeDecision(
            False, None,
            f"model mismatch: stored={rec.model!r} requested={model!r}",
            fallback_to_artifacts=True,
        )

    # Shared implementation lineage: may resume session from plan/build/resolve peers.
    if pol.lineage == "shared_implementation":
        return ResumeDecision(True, rec.session_id, "shared implementation lineage")

    return ResumeDecision(True, rec.session_id, "same-stage isolated resume")


def resolve_resume_session_id(
    store: RunStore,
    run_id: str,
    stage: str,
    *,
    backend: str,
    profile: str = "",
    model: str = "",
    worktree_id: str = "",
) -> ResumeDecision:
    """Pick the session_id to resume for this stage invocation, if any."""
    pol = policy_for(stage)
    if not pol.allow_native_resume:
        return can_native_resume(
            stage=stage, stored=None, backend=backend, profile=profile,
            model=model, worktree_id=worktree_id,
        )

    if pol.lineage == "shared_implementation":
        # Prefer this stage's own session; else walk plan → build → resolve.
        for key in (stage, *IMPLEMENTATION_LINEAGE_STAGES):
            raw = store.get_session(run_id, key)
            if not raw.get("session_id"):
                continue
            decision = can_native_resume(
                stage=stage, stored=raw, backend=backend, profile=profile,
                model=model, worktree_id=worktree_id,
            )
            if decision.allowed:
                return decision
        return ResumeDecision(False, None, "no implementation lineage session",
                              fallback_to_artifacts=True)

    raw = store.get_session(run_id, stage)
    return can_native_resume(
        stage=stage, stored=raw, backend=backend, profile=profile,
        model=model, worktree_id=worktree_id,
    )


def save_session_record(
    store: RunStore,
    run_id: str,
    stage: str,
    *,
    session_id: str | None = None,
    model: str | None = None,
    runner: str | None = None,
    profile: str | None = None,
    profile_version: str | None = None,
    worktree_id: str | None = None,
    backend_agent_id: str | None = None,
    backend_run_id: str | None = None,
    terminal_status: str | None = None,
    **extra: Any,
) -> SessionRecord:
    """Merge-write an additive session record for ``stage``."""
    existing = store.get_session(run_id, stage)
    rec = migrate_record(existing, stage=stage)
    now = now_iso()
    if not rec.created_at:
        rec.created_at = now
    rec.updated_at = now
    if session_id is not None:
        rec.session_id = session_id
        if not rec.backend_agent_id:
            rec.backend_agent_id = session_id
    if model is not None:
        rec.model = model
    if runner is not None:
        rec.runner = runner
    if profile is not None:
        rec.profile = profile
    if profile_version is not None:
        rec.profile_version = profile_version
    if worktree_id is not None:
        rec.worktree_id = worktree_id
    if backend_agent_id is not None:
        rec.backend_agent_id = backend_agent_id
    if backend_run_id is not None:
        rec.backend_run_id = backend_run_id
    if terminal_status is not None:
        rec.terminal_status = terminal_status
    for k, v in extra.items():
        if v is not None:
            rec.extra[k] = v
    store.save_session(run_id, stage, **rec.to_dict())
    return rec
