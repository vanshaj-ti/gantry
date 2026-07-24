"""The Gantry engine: stage orchestration and the run state machine.

Pipeline: spec -> design -> plan -> build -> evidence -> review

- Doc stages (spec, design): a markdown artifact is authored, then the run pauses
  at a human-review gate. Advance with `gantry approve` / send back with `gantry revise`.
- Agent stages (plan, build, evidence): invoke the configured agent runner with the
  rendered stage prompt. On resume (e.g. after review feedback), the stored session
  is reused.
- Review stage: independent LLM review of the diff + artifacts (see review.py).

The engine names no project, model, or tool directly — all of that comes from
GantryConfig and the runner/notifier adapters.
"""
from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

from .backends.registry import get_execution_runner as get_runner
from .checks import run_all_checks
from .config import (
    AGENT_STAGES,
    DOC_STAGES,
    REVIEW_STAGE,
    GantryConfig,
    stages_for_pipeline,
)
from .git import ensure_worktree
from .invocation import InvocationRequest, invoke
from .pipeline import definition_from_snapshot, snapshot_definition
from .redact import proxy_secrets, redact_secrets
from .state import RunStore
from .status import Status
from .triage import decide, reassess_after_plan

logger = logging.getLogger(__name__)

# How often a running agent stage's heartbeat_at gets refreshed in state.json.
# Lets `gantry watch` and advance.py's stale-run repair tell "still working"
# apart from "process died mid-stage" without waiting out the full stage
# timeout — the heartbeat thread dies the instant the gantry process itself
# does, whereas a wedged-but-alive agent subprocess keeps the heartbeat going.
HEARTBEAT_INTERVAL = 20


class Engine:
    def __init__(self, target_workspace: Path, config: GantryConfig):
        self.target = target_workspace.resolve()
        self.cfg = config
        self.store = RunStore(self.target)

    def _redact(self, text: str) -> str:
        """Redact known-sensitive values (auth env vars, this config's
        proxy api_key_env/headers values) before any subprocess output gets
        persisted to a log file — see redact.py's module docstring for the
        leak vector this closes. Applied before writing, never after."""
        return redact_secrets(text, extra_secrets=proxy_secrets(self.cfg))

    def work_dir(self, run_id: str) -> Path:
        """The isolated worktree a run's agent stages/checks/review execute in.
        Created lazily on first use, reused afterward. .agent-runs/ state stays
        in self.target regardless — only the working copy of the repo moves."""
        return ensure_worktree(self.target, run_id, self.cfg.git.base_branch)


    def _set_status(self, run_id: str, status: str, **extra: Any) -> None:
        """Update run state and mirror the semantic status to herdr's sidebar
        when running inside a herdr pane (no-op otherwise)."""
        st = self.store.update_state(run_id, status=status, **extra)
        try:
            from . import herdr as _herdr
            _herdr.report_state(run_id, status, title=st.get("title", ""),
                                enabled=self.cfg.herdr.enabled and self.cfg.herdr.report_state)
        except Exception:
            logger.debug("herdr report_state failed for run %s (%s)", run_id, status, exc_info=True)

    # --- prompt rendering ---
    def _prompts_dir(self) -> Path:
        p = Path(self.cfg.prompts_dir)
        return p if p.is_absolute() else (self.target / p)

    def render_prompt(self, stage: str, run_id: str, profile=None) -> str:
        profile = profile or self.cfg.profile_for(stage)
        template_path = self._prompt_template_path(stage)
        if not template_path.exists():
            # fall back to a minimal generic instruction so a bare repo still runs
            artifact = self.cfg.artifact_for(stage)
            base = (f"# Stage: {stage}\n\nRun: {run_id}\n\n"
                    f"Read the artifacts in .agent-runs/{run_id}/. Perform the {stage} stage "
                    f"and write your output to .agent-runs/{run_id}/{artifact}.\n")
        else:
            base = template_path.read_text().replace("{RUN_ID}", run_id)
        preamble = f"{profile.prompt_preamble}\n\n" if profile.prompt_preamble else ""
        return (preamble + self._plan_context_directive(stage) + base + self._stage_skill_directive(stage)
                + self._skills_directive(stage, profile=profile)
                + self._evidence_output_directive(stage))

    def _prompt_template_path(self, stage: str) -> Path:
        """Which prompt template file to use for this stage.

        Only the plan stage has a depth variant today: [plan].depth = "brief"
        selects prompts/plan-brief.md INSTEAD OF prompts/plan.md, but only if
        that brief variant actually exists in this project's prompts_dir —
        falls back to the single existing template otherwise, so a project
        that sets depth="brief" without ever adding the variant file sees no
        behavior change (same as never setting it)."""
        if stage == "plan" and self.cfg.plan.depth == "brief":
            brief_path = self._prompts_dir() / "plan-brief.md"
            if brief_path.exists():
                return brief_path
        return self._prompts_dir() / f"{stage}.md"

    def _plan_context_directive(self, stage: str) -> str:
        """Prepended to the plan stage's prompt: recent git history and/or
        the contents of explicitly configured context files, when
        [plan].include_git_log / context_files are set. Empty (today's
        behavior) when neither is configured — the plan agent only ever saw
        intake.md and whatever it found itself via its own tools."""
        if stage != "plan":
            return ""
        sections = []
        if self.cfg.plan.include_git_log:
            log = self._recent_git_log(self.cfg.plan.git_log_lines)
            if log:
                sections.append(f"## Recent history\n```\n{log}\n```\n")
        for rel_path in self.cfg.plan.context_files:
            path = self.target / rel_path
            if path.is_file():
                sections.append(f"## Referenced file: {rel_path}\n```\n{path.read_text()}\n```\n")
            else:
                logger.warning("plan.context_files entry not found: %s", rel_path)
        if not sections:
            return ""
        return "# Context\n\n" + "\n".join(sections) + "\n---\n\n"

    def _recent_git_log(self, n: int) -> str:
        import subprocess as _subprocess
        try:
            proc = _subprocess.run(["git", "log", "--oneline", f"-{n}"], cwd=str(self.target),
                                   capture_output=True, text=True, timeout=30)
            return proc.stdout.strip() if proc.returncode == 0 else ""
        except Exception:
            logger.debug("git log failed for plan context directive", exc_info=True)
            return ""

    def _evidence_output_directive(self, stage: str) -> str:
        """Appended to the evidence stage's prompt only when
        [evidence].output_format = "structured" — asks for a trailing fenced
        JSON block review.py can parse deterministically instead of
        re-deriving pass/fail/coverage from free prose. No-op for every
        other stage and for the default "prose" format (today's behavior,
        unchanged)."""
        if stage != "evidence" or self.cfg.evidence.output_format != "structured":
            return ""
        return (
            "\n\n---\n## Structured summary (required)\n"
            "After writing evidence-report.md's normal prose sections, append a final "
            "fenced ```json block (own section, after everything else) with exactly these "
            "keys: `pass_count` (int, acceptance criteria proven), `fail_count` (int, "
            "acceptance criteria that failed or couldn't be proven), `coverage_pct` "
            "(number 0-100, or null if not meaningfully measurable for this change), "
            "`scope_summary` (one-sentence string). This is read by the review stage "
            "as a pre-digested summary — keep it accurate to the prose above it, don't "
            "pad or guess a number you didn't actually verify.\n"
        )

    # Every stage gantry ships its own authored SKILL.md for (baked into the
    # Docker image at ~/.claude/skills/ and ~/.codex/skills/ as
    # gantry-stage-<stage>/ — see Dockerfile and gantry/skills/<stage>/SKILL.md).
    # Distinct from _skills_directive below: this is gantry's OWN stage
    # discipline, not a generic third-party methodology library, so it applies
    # to every stage — not just build/evidence.
    _STAGE_SKILLS = {"spec", "design", "investigation", "research", "plan", "build", "evidence"}

    def _stage_skill_directive(self, stage: str) -> str:
        if stage not in self._STAGE_SKILLS:
            return ""
        return (
            f"\n\n---\nInvoke the `gantry-stage-{stage}` skill now for this stage's "
            f"required discipline and output format before doing any other work.\n"
        )

    def _skills_directive(self, stage: str, profile=None) -> str:
        """Scoped skill mandate. Only for build/evidence (execution stages) — NOT
        spec/design/plan, where a methodology library would fight Gantry's own
        stages. Tells the agent a plan already exists: execute, don't re-plan.

        build and evidence need different framing even when they share the
        same `enabled` skills list: build is doing EXECUTION (TDD, systematic
        debugging), evidence is doing VERIFICATION (confirm the plan actually
        landed, don't re-implement anything). Using build's directive
        verbatim for evidence risks the agent treating verification as
        another round of implementation work. [skills].evidence_directive
        lets a project override evidence's text entirely; unset falls back to
        a verification-focused default distinct from build's."""
        profile = profile or self.cfg.profile_for(stage)
        stage_skill = f"gantry-stage-{stage}"
        enabled = tuple(skill for skill in profile.skills if skill != stage_skill)
        if not enabled:
            return ""
        skills = ", ".join(f"`{s}`" for s in enabled)
        if stage == "evidence":
            framing = self.cfg.skills.evidence_directive or (
                "IMPORTANT: the implementation is already complete — your job is to VERIFY "
                "it, not redo it. Use these skills for verification rigor (confirm tests "
                "actually pass, confirm the plan's acceptance criteria are met) — do NOT "
                "re-implement, refactor, or restart any part of the build.\n"
            )
        elif stage == "build":
            framing = (
                "IMPORTANT: an approved implementation plan already exists for this run. Use "
                "these skills for EXECUTION discipline (TDD, systematic debugging, review rigor) "
                "— do NOT restart spec/design/planning. Execute the existing plan.\n"
            )
        else:
            framing = (
                "Use these profile-requested skills for this specialist role while "
                "following the stage instructions above.\n"
            )
        return (
            f"\n\n---\n## Mandated skills for this stage\n"
            f"Load and actively use: {skills}. Invoke the Skill tool — do not leave them "
            f"passively in context.\n\n{framing}"
        )

    def _answer_context(self, run_id: str, stage: str) -> str:
        from .feedback import feedback_artifacts_for_stage

        parts = []
        for artifact, heading in feedback_artifacts_for_stage(stage):
            content = self.store.read_artifact(run_id, artifact)
            if content:
                parts.append(f"# {heading}\n{content}")
        return "\n\n" + "\n\n".join(parts) if parts else ""

    # --- run lifecycle ---
    def create_run(self, title: str, request: str, run_id: str | None = None,
                    depends_on: list[str] | None = None, tag: str | None = None) -> str:
        """Create a run. If `depends_on` names other run_ids, this run is
        queued (status "queued", not "awaiting_{first_stage}") until every
        listed run is actually merged, not merely review_approved (see
        `_prereqs_met`'s docstring for why review_approved alone isn't
        enough) — see advance.py's advance_run. This lets independent runs be
        queued up front and left for the poller/advance loop to sequence
        correctly, instead of requiring a human (or a script) to watch run N
        and manually create run N+1 only once N finishes.

        `tag` is purely a filtering label (e.g. a feature/ticket/component
        name) for `gantry watch --tag`/`gantry advance --all --tag` — it has
        no effect on the run's own execution UNLESS `[queues.<tag>]` in
        gantry.toml overrides the stage list for that tag (see
        GantryConfig.stages_for) — in which case it also picks this run's
        pipeline (e.g. tag="bug" -> investigation/plan/build/evidence/review
        instead of the project's default stages)."""
        # Keep the legacy stage resolver authoritative: existing queue
        # mappings and project overrides must remain byte-for-byte compatible.
        # Triage supplies additive policy/version metadata around that pinned
        # stage list.
        stages = stages_for_pipeline(
            self.cfg.stages_for(tag), self.cfg.pipeline.version,
        )
        pipeline = replace(
            decide(title, request, tag, None, self.cfg),
            stages=tuple(stages),
            version=self.cfg.pipeline.version,
        )
        first = stages[0] if stages else "plan"
        rid = self.store.new_run_id(title, run_id)
        self.store.create(rid, title)
        self.store.artifact_path(rid, "intake.md").write_text(f"# Intake\n\n{request.strip() or title}\n")
        deps = list(depends_on) if depends_on else []
        for dep in deps:
            if not self.store.exists(dep):
                raise ValueError(f"depends_on references unknown run: {dep}")
        extra = {"tag": tag} if tag else {}
        extra["stages"] = stages
        extra.update({
            "pipeline_name": pipeline.name,
            "pipeline_version": pipeline.version,
            "definition_policy": pipeline.definition_policy,
            "pipeline_mutations": [],
            "pipeline_definition": snapshot_definition(pipeline),
        })
        if deps:
            self.store.update_state(rid, status=Status.QUEUED, current_stage=first,
                                    title=title, depends_on=deps, **extra)
        else:
            self.store.update_state(rid, status=f"awaiting_{first}", current_stage=first,
                                    title=title, **extra)
        return rid

    def reassess_risk_after_plan(self, run_id: str, *, risk: str, reason: str) -> dict[str, Any]:
        """Persist an append-only pipeline escalation after plan scope is known.

        The original pinned stage history is retained. ``pipeline_route_to`` is
        an explicit orchestration signal when the new policy requires routing
        backward through a definition gate.
        """
        state = self.store.state(run_id)
        snapshot = state.get("pipeline_definition")
        if snapshot:
            current = definition_from_snapshot(snapshot)
        else:
            current = replace(
                decide(state.get("title", ""), "", state.get("tag"), None, self.cfg),
                stages=tuple(state.get("stages") or self.cfg.stages),
                version=int(state.get("pipeline_version", 1)),
            )
        completed = tuple(state.get("completed_stages") or ("plan",))
        evolved = reassess_after_plan(
            current,
            risk=risk,
            reason=reason,
            completed_stages=completed,
        )
        if evolved is current:
            return state
        mutations = [vars(item) for item in evolved.mutations]
        route_to = evolved.mutations[-1].route_to
        updated = self.store.update_state(
            run_id,
            pipeline_name=evolved.name,
            pipeline_version=evolved.version,
            definition_policy=evolved.definition_policy,
            pipeline_definition=snapshot_definition(evolved),
            pipeline_mutations=mutations,
            pipeline_route_to=route_to,
        )
        self.store.write_result(run_id, "pipeline-mutations.json", mutations)
        return updated

    def _prereqs_met(self, run_id: str) -> bool:
        """True if every run this run depends on has actually landed.

        `review_approved` only means the LLM reviewer signed off — the PR
        hasn't even been opened yet at that point, let alone merged. A
        dependent run started the moment its prereq hit review_approved would
        be building against code that doesn't exist on base_branch yet (ship
        might still fail, or the PR might sit unmerged for days). So the only
        states that count as "landed" are `shipped`/`shipped_manually` AND
        (when [git].auto_merge is off, or shipped_manually — i.e. whenever
        Gantry can't have merged it itself) the run's own `merged` flag is
        explicitly True. auto_merge=True + status=shipped always implies
        merged is already True or False on that same state (ship_run sets it
        in the same call) — never absent — so no separate check is needed
        there. For projects with no ship/review stage in cfg.stages at all,
        {last_stage}_complete is still the correct terminal condition."""
        deps = self.store.state(run_id).get("depends_on") or []
        if not deps:
            return True
        for dep in deps:
            dep_state = self.store.state(dep)
            dep_status = dep_state.get("status", "")
            if dep_status in ("shipped", "shipped_manually") and dep_state.get("merged") is True:
                continue
            dep_stages = self.stages_for_run(dep)
            dep_last_stage = dep_stages[-1] if dep_stages else None
            terminal_incomplete_ok = f"{dep_last_stage}_complete" if dep_last_stage else None
            if not self.cfg.review.enabled and dep_status == terminal_incomplete_ok:
                continue
            return False
        return True

    def _run_build_pre_hook(self, run_id: str, work_dir: Path) -> None:
        """Run [build].pre_hook once in the worktree before the build stage's
        first (non-resumed) agent invocation for a run — e.g. `npm ci &&
        make seed-db`, setup the build agent shouldn't have to do or wait on
        itself. No-op if pre_hook is empty (the default).

        Non-fatal by default (pre_hook_required=False), same best-effort
        philosophy as git._install_deps_if_npm_project: a failing setup step
        is logged, not silently swallowed, but doesn't block build from
        starting — the failure surfaces clearly later in whichever check
        actually needed what the hook was supposed to set up. Set
        pre_hook_required=True to fail the build stage immediately instead."""
        pre_hook = self.cfg.build.pre_hook
        if not pre_hook:
            return
        import subprocess as _subprocess
        try:
            proc = _subprocess.run(pre_hook, shell=True, cwd=str(work_dir),
                                   capture_output=True, text=True, timeout=900)
            self.store.write_log(run_id, "build-pre-hook.log",
                                self._redact(f"$ {pre_hook}\n(exit {proc.returncode})\n\n"
                                             f"{proc.stdout}{proc.stderr}"))
            if proc.returncode != 0:
                logger.warning("build pre_hook failed for run %s (exit %s): %s",
                               run_id, proc.returncode, pre_hook)
                if self.cfg.build.pre_hook_required:
                    raise RuntimeError(f"build pre_hook failed (exit {proc.returncode}): {pre_hook}")
        except _subprocess.TimeoutExpired:
            logger.warning("build pre_hook timed out for run %s: %s", run_id, pre_hook)
            if self.cfg.build.pre_hook_required:
                raise

    def run_agent_stage(self, run_id: str, stage: str, resume: bool = False) -> dict[str, Any]:
        if not self.store.exists(run_id):
            raise ValueError(f"Run not found: {run_id}")
        sm = self.cfg.model_for(stage)

        # Clear any question.md from a PRIOR invocation before this one runs
        # — the deterministic question-signal check below must reflect only
        # what THIS agent call wrote, never a stale leftover from an earlier
        # round (e.g. this same stage asked Q1, got answered, resumed, and
        # this new call either asks Q2 or completes cleanly — either way
        # Q1's file must not still be sitting there being misread as live).
        question_path = self.store.artifact_path(run_id, "question.md")
        if question_path.exists():
            question_path.unlink()

        work_dir = self.work_dir(run_id)

        if stage == "build" and not resume:
            self._run_build_pre_hook(run_id, work_dir)

        outcome = invoke(InvocationRequest(
            cfg=self.cfg,
            store=self.store,
            run_id=run_id,
            stage=stage,
            cwd=work_dir,
            prompt="",
            prompt_factory=lambda profile: (
                self.render_prompt(stage, run_id, profile=profile)
                + (self._answer_context(run_id, stage) if resume else "")
            ),
            resume=resume,
            plan_mode=sm.plan_mode,
            session_name=f"{run_id}-{stage}",
            prompt_name=f"{stage}-prompt{'-resume' if resume else ''}.md",
            start_status=f"{stage}_running",
            failure_status=f"{stage}_failed",
            current_stage=stage,
            heartbeat_interval=HEARTBEAT_INTERVAL,
            backend_resolver=get_runner,
        ))
        result = outcome.result
        if outcome.timed_out:
            return {"stage": stage, "ok": False, "session_id": None, "error": "timeout"}
        if outcome.cancelled:
            return {"stage": stage, "ok": False, "session_id": result.session_id,
                    "error": "cancelled"}
        ok = result.ok
        # Deterministic "the agent has a blocking question" signal — a
        # question.md file, per every stage prompt's instruction, checked
        # BEFORE the artifact gate. Not a prose/regex guess at the agent's
        # result text (a "?" heuristic misses questions phrased without one,
        # and false-positives on any report that happens to contain a "?"):
        # this is a file the agent either wrote or didn't. When present, the
        # stage is blocked-on-question, not failed — a genuine clarifying
        # question asked mid-investigation/spec/etc is expected pipeline
        # behavior, distinct from the agent actually erroring or silently
        # skipping its required artifact.
        question_path = self.store.artifact_path(run_id, "question.md")
        has_question = question_path.exists() and question_path.read_text().strip()
        if has_question:
            status = f"{stage}_question"
        else:
            if stage in DOC_STAGES and ok:
                # Deterministic structural gate — no LLM call — that this doc
                # stage's required artifact actually exists with real content
                # before letting the run reach {stage}_complete. Seen for real:
                # an agent call reporting success (result.ok True) while never
                # writing its report at all — burned turns/cost, empty result,
                # and would otherwise have sailed through to a human-review gate
                # with nothing to review.
                from .checks import check_doc_artifact_written
                artifact_name = self.cfg.artifact_for(stage)
                gate = check_doc_artifact_written(self.store, run_id, artifact_name)
                if stage == "spec":
                    # spec additionally validates its structured
                    # acceptance-criteria.json companion, not just file presence.
                    from .checks import check_spec_artifacts
                    spec_gate = check_spec_artifacts(self.store, run_id)
                    if gate["pass"] and not spec_gate["pass"]:
                        gate = spec_gate
                self.store.write_result(run_id, f"{stage}-gate.json", gate)
                if not gate["pass"]:
                    ok = False
                    self.store.write_log(
                        run_id, f"{stage}-gate.stderr",
                        self._redact(f"{stage} stage structural gate failed: {gate['reason']}"))
            status = f"{stage}_complete" if ok else f"{stage}_failed"
        if self.store.state(run_id).get("status") != Status.CANCELLED:
            self._set_status(run_id, status)
        return {"stage": stage, "ok": ok, "session_id": result.session_id, "question": has_question}

    def _resolver_checks_prompt(self, run_id: str, wt: Path) -> str:
        """Prompt-building for the ORIGINAL resolver purpose: checks_escalated
        after the normal build/checks auto-retry loop exhausted itself.
        Unchanged behavior from before `run_resolver_stage` gained a
        `failure_kind` parameter — see `run_resolver_stage`'s docstring."""
        from .failure_detail import _checks_failure_detail
        detail = _checks_failure_detail(self.store, run_id)
        import subprocess as _subprocess
        diff_stat = _subprocess.run(["git", "diff", "--stat", "HEAD"], cwd=str(wt),
                                    capture_output=True, text=True, timeout=30).stdout
        status = _subprocess.run(["git", "status", "--porcelain"], cwd=str(wt),
                                 capture_output=True, text=True, timeout=30).stdout
        commands_list = "\n".join(f"  {c}" for c in self.cfg.checks.commands)
        return (
            f"# Resolver stage — checks were escalated after exhausting normal auto-retry\n\n"
            f"Run: {run_id}\n\n"
            f"This run's build/checks/rebuild loop failed repeatedly on the SAME issue and "
            f"auto-retry gave up. You are being invoked specifically to diagnose and fix the "
            f"actual root cause — not to repeat whatever the previous build attempts already "
            f"tried and failed at.\n\n"
            f"## Current failure detail\n{detail}\n\n"
            f"## Current git status (uncommitted changes, if any)\n```\n{status or '(clean)'}\n```\n\n"
            f"## Current diff stat vs HEAD\n```\n{diff_stat or '(no diff)'}\n```\n\n"
            f"## Repo check commands (these are what must pass — run them yourself before "
            f"declaring anything fixed)\n{commands_list}\n\n"
            f"## Requirements\n"
            f"1. Actually investigate — read the failing file(s), understand what's really "
            f"wrong (e.g. check for leftover merge-conflict markers, actual logic bugs, "
            f"missing declarations — don't guess from the error text alone).\n"
            f"2. Fix the root cause. Commit the fix.\n"
            f"3. Run the repo's own check commands YOURSELF (the exact commands listed above) "
            f"and confirm they pass with your own eyes before finishing. Do not report success "
            f"without having actually run them and seen them pass in this session.\n"
            f"4. If you cannot find or fix the actual problem, say so explicitly and describe "
            f"what you found — do not claim a fix that isn't real. A false claim of success "
            f"here wastes the retry budget and is worse than an honest 'I could not fix this.'\n"
        )

    def _resolver_conflict_prompt(self, run_id: str, wt: Path, conflict_output: str) -> str:
        """Prompt-building for the NEW conflict-shaped-ship-failure purpose
        (ship.py's push/create_pr hit a real conflict/diverged-branches
        condition — see git.is_conflict_shaped_failure). Framed around
        resolving BY INTENT, not blind ours/theirs: feeds the run's own
        planning artifacts (what this run was actually trying to achieve) so
        the resolver can trace each conflicting side back to its rationale,
        same principle as mattpocock-skills:resolving-merge-conflicts —
        preserve both intents where compatible, pick the side matching this
        run's stated goal where genuinely incompatible (and say so
        explicitly), NEVER blind ours/theirs, NEVER leave the merge/rebase
        aborted or half-finished."""
        import subprocess as _subprocess
        diff_stat = _subprocess.run(["git", "diff", "--stat", "HEAD"], cwd=str(wt),
                                    capture_output=True, text=True, timeout=30).stdout
        status = _subprocess.run(["git", "status", "--porcelain"], cwd=str(wt),
                                 capture_output=True, text=True, timeout=30).stdout
        intake = self.store.read_artifact(run_id, "intake.md") or "<missing>"
        product_spec = self.store.read_artifact(run_id, "product-spec.md") or "<missing>"
        build_summary = self.store.read_artifact(run_id, "build-summary.md") or "<missing>"
        return (
            f"# Resolver stage — ship-time push/PR failure looked conflict-shaped\n\n"
            f"Run: {run_id}\n\n"
            f"This run's changes are already built, tested, and independently reviewed "
            f"(APPROVE). At ship time, pushing/opening the PR failed with output that looks "
            f"like a real content conflict or diverged-branches condition — most likely "
            f"another run's branch shipped and moved the base branch out from under this "
            f"one's own branch. You are being invoked to resolve this BY INTENT, not by "
            f"blindly discarding either side.\n\n"
            f"## Captured push/create_pr output (the actual conflict signal)\n"
            f"```\n{conflict_output[:8000]}\n```\n\n"
            f"## Current git status (uncommitted changes, if any)\n```\n{status or '(clean)'}\n```\n\n"
            f"## Current diff stat vs HEAD\n```\n{diff_stat or '(no diff)'}\n```\n\n"
            f"## This run's own intent (what THIS side of the conflict was trying to achieve)\n"
            f"### intake.md\n{intake[:4000]}\n\n"
            f"### product-spec.md\n{product_spec[:4000]}\n\n"
            f"### build-summary.md\n{build_summary[:4000]}\n\n"
            f"## Requirements — resolve by intent, never blind ours/theirs\n"
            f"1. If this repo is mid-merge/mid-rebase (check `git status` above for "
            f"conflict markers / an in-progress merge or rebase), first understand what "
            f"each side of every conflicting hunk was actually trying to do — read the "
            f"actual conflicting hunks, not just this run's own artifacts above; the OTHER "
            f"side's intent has to be inferred from its own commit(s)/diff, since you don't "
            f"have its planning artifacts here.\n"
            f"2. Preserve both intents where they are genuinely compatible (e.g. two "
            f"additions to different parts of the same function, or complementary changes "
            f"to the same file).\n"
            f"3. Where the two sides are genuinely incompatible, prefer the side that "
            f"matches THIS run's stated goal above (that's what this ship is actually "
            f"trying to land) — but say so EXPLICITLY in your summary: state which side you "
            f"kept, which you dropped, and why, so a human reviewing this later can see the "
            f"tradeoff was a deliberate decision, not an accident.\n"
            f"4. NEVER use `git checkout --ours`/`--theirs` (or the merge/rebase equivalent) "
            f"as a blanket resolution — that's exactly the blind-discard failure mode this "
            f"stage exists to avoid. Resolve conflict markers by hand, hunk by hunk, with "
            f"the intent above in mind.\n"
            f"5. NEVER leave the merge/rebase aborted or half-finished — finish it: resolve "
            f"every conflict, then commit (or continue the rebase) so the worktree ends in a "
            f"clean, mergeable state ready to push again.\n"
            f"6. If you cannot determine a safe resolution, say so explicitly and describe "
            f"exactly what remains unresolved — do not claim success without an actually "
            f"clean git state.\n"
        )

    def run_resolver_stage(self, run_id: str, failure_kind: str = "checks",
                           conflict_output: str = "") -> dict[str, Any]:
        """Spawn a dedicated fix-it agent for one of two failure contexts,
        selected by `failure_kind`:

          - "checks" (default, original/only behavior before this parameter
            existed): the normal build/checks retry loop has been exhausted
            (checks_escalated) — gated behind cfg.checks.auto_resolve. After
            the agent finishes, re-runs real checks (self.run_checks) and
            sets this run's status itself: build_complete on a real pass,
            checks_escalated again on a real (still-)failure. Callers
            (advance.py) read that status back; this is the ONLY failure_kind
            that mutates run status here.

          - "conflict": ship.py's push/create_pr hit a conflict-shaped
            failure (see git.is_conflict_shaped_failure) — resolve by intent
            (see `_resolver_conflict_prompt`), never blind ours/theirs, never
            leave the merge/rebase aborted. Does NOT run checks or set any
            run status itself — ship.py owns re-attempting push/create_pr
            and deciding the outcome from THAT (same "never trust the
            resolver's own claim, re-verify for real" discipline as the
            checks path, just applied to a different verification action).

        Deliberately does NOT trust the resolver agent's own claim of success
        in either mode — that's exactly the failure mode that motivated this
        stage originally: a resumed build agent reported build_complete while
        a real, unresolved git merge-conflict marker was still committed in
        the file, and the auto-retry loop kept re-running the identical
        broken state three times because nothing re-verified the actual
        result.

        Builds its own prompt (not render_prompt/a stage template) because
        the resolver's context is fundamentally different from build/plan/
        evidence: it needs the concrete, current failure detail (not a spec
        to implement), explicit instructions to actually verify the fix
        itself before finishing, and a hard requirement to never claim
        success without that verification.
        """
        if failure_kind not in ("checks", "conflict"):
            raise ValueError(f"run_resolver_stage: unknown failure_kind {failure_kind!r}")
        wt = self.work_dir(run_id)
        if failure_kind == "conflict":
            prompt = self._resolver_conflict_prompt(run_id, wt, conflict_output)
        else:
            prompt = self._resolver_checks_prompt(run_id, wt)
        # Uses [models.resolve] if the project configures it, else falls back
        # to [models.build]'s model/runner (model_for's own default behavior
        # for an unconfigured stage name). Worth configuring resolve to a
        # stronger model than build where build uses a fast/cheap one — this
        # stage exists specifically because build already failed 3 times on
        # the same issue, so re-running the same model that produced (or
        # missed) the bug is a weaker bet than escalating to a more capable
        # one for the fix-it attempt.
        outcome = invoke(InvocationRequest(
            cfg=self.cfg,
            store=self.store,
            run_id=run_id,
            stage="resolve",
            cwd=wt,
            prompt=prompt,
            prepend_profile_preamble=True,
            session_name=f"{run_id}-resolve",
            start_status=Status.RESOLVE_RUNNING,
            failure_status=Status.RESOLVE_ESCALATED,
            current_stage="build",
            heartbeat_interval=HEARTBEAT_INTERVAL,
            backend_resolver=get_runner,
        ))
        result = outcome.result
        if outcome.cancelled:
            return {"agent_ok": False, "cancelled": True}

        if failure_kind == "conflict":
            # ship.py owns re-verification here (re-attempting the actual
            # push/create_pr call) and any status transition — this stage
            # only ran the fix-it agent and reports whether it claims to have
            # finished; ship.py never trusts agent_ok alone.
            return {"agent_ok": result.ok}

        # Never trust the agent's own report — re-run real checks ourselves.
        verify = self.run_checks(run_id)
        if verify["pass"]:
            self._set_status(run_id, Status.BUILD_COMPLETE, blocked_on=None, checks="pass")
        else:
            self._set_status(run_id, Status.CHECKS_ESCALATED, blocked_on=verify.get("scope") and
                             ("scope" if not verify["scope"]["pass"] else "checks"))
        return {"agent_ok": result.ok, "verified_pass": verify["pass"], "checks": verify}


    def run_checks(self, run_id: str) -> dict[str, Any]:
        # Catch up this run's branch with base_branch BEFORE the scope guard
        # computes its merge-base diff — otherwise a base_branch that moved
        # since this run's worktree was created (e.g. an earlier queued run
        # shipped mid-way through this one's build) makes already-shipped
        # files look like "unexpected new files" on this run's diff. See
        # merge_base_into_worktree's docstring for the full incident this
        # fixes. A genuine merge conflict here is surfaced via merge_result
        # (not silently discarded) — build/resume or a human needs to see it.
        from .git import merge_base_into_worktree
        merge_result = merge_base_into_worktree(self.target, run_id, self.cfg.git.base_branch)
        out = run_all_checks(self.store, run_id, self.cfg.scope, self.cfg.checks,
                             self.work_dir(run_id), self.cfg.git.base_branch)
        out["base_branch_merge"] = merge_result
        return out

    def stages_for_run(self, run_id: str) -> list[str]:
        """This run's own stage list — pinned at create_run time (per its tag's
        [queues.<tag>] override, if any) — falling back to cfg.stages for runs
        created before this field existed."""
        return self.store.state(run_id).get("stages") or self.cfg.stages

    # --- gates ---
    def approve(self, run_id: str, stage: str) -> str:
        """Pass a human-review gate: mark stage approved and advance to the next."""
        nxt = self._next_stage(run_id, stage)
        status = f"awaiting_{nxt}" if nxt else f"{stage}_approved"
        self.store.update_state(run_id, status=status, current_stage=nxt or stage,
                                last_approved_stage=stage)
        return nxt or "done"

    def revise(self, run_id: str, stage: str, comments: str) -> None:
        """Send a stage back with reviewer comments."""
        self.store.artifact_path(run_id, "review-comments.md").write_text(
            f"# Revision requested: {stage}\n\n{comments}\n")
        self.store.update_state(run_id, status=f"{stage}_changes_requested", current_stage=stage)

    def _next_stage(self, run_id: str, stage: str) -> str | None:
        stages = self.stages_for_run(run_id)
        if stage not in stages:
            return None
        i = stages.index(stage)
        return stages[i + 1] if i + 1 < len(stages) else None

    @staticmethod
    def stage_kind(stage: str) -> str:
        if stage in DOC_STAGES:
            return "doc"
        if stage in AGENT_STAGES:
            return "agent"
        if stage == REVIEW_STAGE:
            return "review"
        return "unknown"
