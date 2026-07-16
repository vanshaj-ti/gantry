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

import json
import logging
import threading
from pathlib import Path
from typing import Any

from .checks import run_all_checks
from .config import AGENT_STAGES, DOC_STAGES, REVIEW_STAGE, GantryConfig
from .git import ensure_worktree
from .runners import get_runner
from .state import RunStore, now_iso

logger = logging.getLogger(__name__)

# How often a running agent stage's heartbeat_at gets refreshed in state.json.
# Lets `gantry watch` and advance.py's stale-run repair tell "still working"
# apart from "process died mid-stage" without waiting out the full stage
# timeout — the heartbeat thread dies the instant the gantry process itself
# does, whereas a wedged-but-alive agent subprocess keeps the heartbeat going.
HEARTBEAT_INTERVAL = 20


def _start_heartbeat(store: RunStore, run_id: str, interval: float | None = None) -> tuple[threading.Event, threading.Thread]:
    stop = threading.Event()

    def _beat() -> None:
        # Read the module global at wait-time (not a bound default arg) so
        # tests can patch gantry.engine.HEARTBEAT_INTERVAL and see it take
        # effect without needing to thread a parameter through run_agent_stage.
        while not stop.wait(interval if interval is not None else HEARTBEAT_INTERVAL):
            store.update_state(run_id, heartbeat_at=now_iso())

    thread = threading.Thread(target=_beat, daemon=True)
    thread.start()
    return stop, thread


def _stop_heartbeat(stop: threading.Event, thread: threading.Thread) -> None:
    stop.set()
    thread.join(timeout=1)


class Engine:
    def __init__(self, target_workspace: Path, config: GantryConfig):
        self.target = target_workspace.resolve()
        self.cfg = config
        self.store = RunStore(self.target)

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

    def render_prompt(self, stage: str, run_id: str) -> str:
        template_path = self._prompts_dir() / f"{stage}.md"
        if not template_path.exists():
            # fall back to a minimal generic instruction so a bare repo still runs
            artifact = self.cfg.artifact_for(stage)
            base = (f"# Stage: {stage}\n\nRun: {run_id}\n\n"
                    f"Read the artifacts in .agent-runs/{run_id}/. Perform the {stage} stage "
                    f"and write your output to .agent-runs/{run_id}/{artifact}.\n")
        else:
            base = template_path.read_text().replace("{RUN_ID}", run_id)
        return base + self._skills_directive(stage)

    def _skills_directive(self, stage: str) -> str:
        """Scoped skill mandate. Only for build/evidence (execution stages) — NOT
        spec/design/plan, where a methodology library would fight Gantry's own
        stages. Tells the agent a plan already exists: execute, don't re-plan."""
        if stage not in ("build", "evidence") or not self.cfg.skills.enabled:
            return ""
        skills = ", ".join(f"`{s}`" for s in self.cfg.skills.enabled)
        return (
            f"\n\n---\n## Mandated skills for this stage\n"
            f"Load and actively use: {skills}. Invoke the Skill tool — do not leave them "
            f"passively in context.\n\n"
            f"IMPORTANT: an approved implementation plan already exists for this run. Use "
            f"these skills for EXECUTION discipline (TDD, systematic debugging, review rigor) "
            f"— do NOT restart spec/design/planning. Execute the existing plan.\n"
        )

    def _answer_context(self, run_id: str, stage: str) -> str:
        # Two independent producers feed a resumed agent stage, and a resume
        # must see whichever one actually fired or it silently repeats its
        # prior (rejected/failing) output with no new guidance:
        #   - review.py / Engine.revise(): writes review-comments.md when a
        #     reviewer requests changes.
        #   - advance.py's checks/e2e auto-retry loop: writes answers/build.md
        #     when repo checks (lint/build/test) or e2e fail, independent of
        #     any review verdict.
        # These fire at different points in the pipeline (checks failures
        # happen before evidence/review even run), so neither can be dropped
        # in favor of the other. Concatenate whichever exist, most-recent
        # last so it reads as the final word if both are somehow present.
        parts = []
        checks_answer = self.store.read_artifact(run_id, f"answers/{stage}.md")
        if checks_answer:
            parts.append(f"# Checks/e2e failure detail for this resumed stage\n{checks_answer}")
        review_comments = self.store.read_artifact(run_id, "review-comments.md")
        if review_comments:
            parts.append(f"# Revision comments for this resumed stage\n{review_comments}")
        return "\n\n" + "\n\n".join(parts) if parts else ""

    # --- run lifecycle ---
    def create_run(self, title: str, request: str, run_id: str | None = None,
                    depends_on: list[str] | None = None) -> str:
        """Create a run. If `depends_on` names other run_ids, this run is
        queued (status "queued", not "awaiting_{first_stage}") until every
        listed run reaches review_approved (or, if review is disabled in
        config, {last_stage}_complete) — see `_prereqs_met` and its use in
        advance.py's advance_run. This lets independent runs be queued up
        front and left for the poller/advance loop to sequence correctly,
        instead of requiring a human (or a script) to watch run N and
        manually create run N+1 only once N finishes."""
        first = self.cfg.stages[0] if self.cfg.stages else "plan"
        rid = self.store.new_run_id(title, run_id)
        self.store.create(rid, title)
        self.store.artifact_path(rid, "intake.md").write_text(f"# Intake\n\n{request.strip() or title}\n")
        deps = list(depends_on) if depends_on else []
        for dep in deps:
            if not self.store.exists(dep):
                raise ValueError(f"depends_on references unknown run: {dep}")
        if deps:
            self.store.update_state(rid, status="queued", current_stage=first,
                                    title=title, depends_on=deps)
        else:
            self.store.update_state(rid, status=f"awaiting_{first}", current_stage=first, title=title)
        return rid

    def _prereqs_met(self, run_id: str) -> bool:
        """True if every run this run depends on has reached a terminal
        success state. Terminal success = review_approved when review is
        enabled (the normal case), or {last_stage}_complete when review is
        disabled entirely for this project — a prereq stuck anywhere else
        (still building, blocked, escalated, changes requested) is NOT met."""
        deps = self.store.state(run_id).get("depends_on") or []
        if not deps:
            return True
        last_stage = self.cfg.stages[-1] if self.cfg.stages else None
        terminal_incomplete_ok = f"{last_stage}_complete" if last_stage else None
        for dep in deps:
            dep_status = self.store.state(dep).get("status", "")
            if dep_status == "review_approved":
                continue
            if not self.cfg.review.enabled and dep_status == terminal_incomplete_ok:
                continue
            return False
        return True

    def run_agent_stage(self, run_id: str, stage: str, resume: bool = False) -> dict[str, Any]:
        if not self.store.exists(run_id):
            raise ValueError(f"Run not found: {run_id}")
        sm = self.cfg.model_for(stage)
        runner = get_runner(self.cfg.runner_for(stage))

        prompt = self.render_prompt(stage, run_id)
        if resume:
            prompt += self._answer_context(run_id, stage)
        self.store.write_log(run_id, f"{stage}-prompt{'-resume' if resume else ''}.md", prompt)

        session_id = self.store.get_session_id(run_id, stage) if resume else None
        if resume and not session_id:
            raise ValueError(f"No stored session for {run_id}/{stage}; cannot resume")

        # Register any enabled MCP servers for this stage before invoking the agent.
        from .mcp import ensure_mcp_for_stage
        work_dir = self.work_dir(run_id)
        mcp_results = ensure_mcp_for_stage(self.cfg, stage, runner.name, work_dir)
        if mcp_results:
            self.store.write_log(run_id, f"{stage}-mcp.json", json.dumps(mcp_results, indent=2))

        self._set_status(run_id, f"{stage}_running", current_stage=stage, resumed=resume,
                         heartbeat_at=now_iso())
        # Record runner/model before invoking (not just after, in save_session
        # below) so a live *_running status shows what's actually driving it
        # right now — session_id isn't known until the agent returns, but
        # which runner/model is in flight is, and that's the useful part for
        # `gantry watch`'s detail column while a stage is still running.
        self.store.save_session(run_id, stage, model=sm.model, runner=runner.name)
        import subprocess as _subprocess
        stop_hb, hb_thread = _start_heartbeat(self.store, run_id)
        try:
            try:
                result = runner.run(
                    cwd=work_dir,
                    prompt=prompt,
                    model=sm.model,
                    session_id=session_id,
                    plan_mode=sm.plan_mode,
                    skip_permissions=self.cfg.agent.skip_permissions,
                    output_format=self.cfg.agent.output_format,
                    session_name=f"{run_id}-{stage}",
                    max_turns=sm.max_turns,
                    timeout=sm.timeout,
                )
            except _subprocess.TimeoutExpired:
                # Without this, a killed/timed-out agent subprocess leaves state.json
                # stuck at "{stage}_running" forever — `gantry watch`/status then lies
                # about a dead run still being in flight (see recovery notes in the
                # workflow skill). Mark it failed like any other unsuccessful stage so
                # the normal retry/escalate machinery (advance.py's "blocked"/
                # "checks_escalated" path) can act on it instead of a human having to
                # notice a stale lockfile and reset state by hand.
                self.store.write_log(run_id, f"{stage}.stderr",
                                     f"Agent subprocess timed out after {sm.timeout}s")
                self._set_status(run_id, f"{stage}_failed")
                return {"stage": stage, "ok": False, "session_id": None, "error": "timeout"}
        finally:
            _stop_heartbeat(stop_hb, hb_thread)
        suffix = ".resume" if resume else ""
        self.store.write_log(run_id, f"{stage}{suffix}.stdout", result.stdout)
        self.store.write_log(run_id, f"{stage}{suffix}.stderr", result.stderr)
        self.store.write_result(run_id, f"{stage}-result.json", result.raw)
        self.store.save_session(run_id, stage, session_id=result.session_id,
                                model=sm.model, runner=runner.name)
        from .cost import accumulate as _accumulate_cost
        _accumulate_cost(self.store, run_id, stage, result.usage)
        status = f"{stage}_complete" if result.ok else f"{stage}_failed"
        self._set_status(run_id, status)
        return {"stage": stage, "ok": result.ok, "session_id": result.session_id}

    def run_resolver_stage(self, run_id: str) -> dict[str, Any]:
        """Spawn a dedicated fix-it agent when the normal build/checks retry
        loop has been exhausted (checks_escalated) — instead of dead-ending
        at a human, gated behind cfg.checks.auto_resolve.

        Deliberately does NOT trust the resolver agent's own claim of
        success — that's exactly the failure mode that motivated this: a
        resumed build agent reported build_complete while a real, unresolved
        git merge-conflict marker was still committed in the file, and the
        auto-retry loop kept re-running the identical broken state three
        times because nothing re-verified the actual result. This method
        re-runs real checks itself (self.run_checks) after the resolver
        agent finishes, and the caller (advance.py) decides success based on
        THAT, never on the agent's stdout/self-report.

        Builds its own prompt (not render_prompt/a stage template) because
        the resolver's context is fundamentally different from build/plan/
        evidence: it needs the concrete, current failure detail (not a spec
        to implement), explicit instructions to actually run the repo's own
        checks commands itself before finishing, and a hard requirement to
        never claim success without that verification.
        """
        checks = self.store.read_result(run_id, "checks.json") or {}
        from .advance import _checks_failure_detail
        detail = _checks_failure_detail(self.store, run_id)
        wt = self.work_dir(run_id)
        import subprocess as _subprocess
        diff_stat = _subprocess.run(["git", "diff", "--stat", "HEAD"], cwd=str(wt),
                                    capture_output=True, text=True, timeout=30).stdout
        status = _subprocess.run(["git", "status", "--porcelain"], cwd=str(wt),
                                 capture_output=True, text=True, timeout=30).stdout
        commands_list = "\n".join(f"  {c}" for c in self.cfg.checks.commands)
        prompt = (
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
        self.store.write_log(run_id, "resolve-prompt.md", prompt)
        # Uses [models.resolve] if the project configures it, else falls back
        # to [models.build]'s model/runner (model_for's own default behavior
        # for an unconfigured stage name). Worth configuring resolve to a
        # stronger model than build where build uses a fast/cheap one — this
        # stage exists specifically because build already failed 3 times on
        # the same issue, so re-running the same model that produced (or
        # missed) the bug is a weaker bet than escalating to a more capable
        # one for the fix-it attempt.
        sm = self.cfg.model_for("resolve") if "resolve" in self.cfg.models else self.cfg.model_for("build")
        runner = get_runner(sm.runner or self.cfg.agent.runner)
        self._set_status(run_id, "resolve_running", current_stage="build", heartbeat_at=now_iso())
        stop_hb, hb_thread = _start_heartbeat(self.store, run_id)
        try:
            result = runner.run(
                cwd=wt, prompt=prompt, model=sm.model,
                session_id=None, plan_mode=False, skip_permissions=self.cfg.agent.skip_permissions,
                output_format=self.cfg.agent.output_format, session_name=f"{run_id}-resolve",
                max_turns=sm.max_turns * 2, timeout=sm.timeout,
            )
        finally:
            _stop_heartbeat(stop_hb, hb_thread)
        self.store.write_log(run_id, "resolve.stdout", result.stdout)
        self.store.write_log(run_id, "resolve.stderr", result.stderr)
        self.store.write_result(run_id, "resolve-result.json", result.raw)
        from .cost import accumulate as _accumulate_cost
        _accumulate_cost(self.store, run_id, "resolve", result.usage)

        # Never trust the agent's own report — re-run real checks ourselves.
        verify = self.run_checks(run_id)
        if verify["pass"]:
            self._set_status(run_id, "build_complete", blocked_on=None, checks="pass")
        else:
            self._set_status(run_id, "checks_escalated", blocked_on=verify.get("scope") and
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

    # --- gates ---
    def approve(self, run_id: str, stage: str) -> str:
        """Pass a human-review gate: mark stage approved and advance to the next."""
        nxt = self._next_stage(stage)
        status = f"awaiting_{nxt}" if nxt else f"{stage}_approved"
        self.store.update_state(run_id, status=status, current_stage=nxt or stage,
                                last_approved_stage=stage)
        return nxt or "done"

    def revise(self, run_id: str, stage: str, comments: str) -> None:
        """Send a stage back with reviewer comments."""
        self.store.artifact_path(run_id, "review-comments.md").write_text(
            f"# Revision requested: {stage}\n\n{comments}\n")
        self.store.update_state(run_id, status=f"{stage}_changes_requested", current_stage=stage)

    def _next_stage(self, stage: str) -> str | None:
        stages = self.cfg.stages
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
