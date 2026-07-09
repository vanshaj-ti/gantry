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
from pathlib import Path
from typing import Any

from .checks import run_all_checks
from .config import AGENT_STAGES, DOC_STAGES, REVIEW_STAGE, GantryConfig
from .git import ensure_worktree
from .runners import get_runner
from .state import RunStore


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
            pass  # herdr reporting is best-effort; never break the pipeline

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
        ans = self.store.read_artifact(run_id, f"answers/{stage}.md")
        return f"\n\n# Human answer for this resumed stage\n{ans}" if ans else ""

    # --- run lifecycle ---
    def create_run(self, title: str, request: str, run_id: str | None = None) -> str:
        first = self.cfg.stages[0] if self.cfg.stages else "plan"
        rid = self.store.new_run_id(title, run_id)
        self.store.create(rid, title)
        self.store.artifact_path(rid, "intake.md").write_text(f"# Intake\n\n{request.strip() or title}\n")
        self.store.update_state(rid, status=f"awaiting_{first}", current_stage=first, title=title)
        return rid

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

        self._set_status(run_id, f"{stage}_running", current_stage=stage, resumed=resume)
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
        )
        suffix = ".resume" if resume else ""
        self.store.write_log(run_id, f"{stage}{suffix}.stdout", result.stdout)
        self.store.write_log(run_id, f"{stage}{suffix}.stderr", result.stderr)
        self.store.write_result(run_id, f"{stage}-result.json", result.raw)
        self.store.save_session(run_id, stage, session_id=result.session_id,
                                model=sm.model, runner=runner.name)
        status = f"{stage}_complete" if result.ok else f"{stage}_failed"
        self._set_status(run_id, status)
        return {"stage": stage, "ok": result.ok, "session_id": result.session_id}

    def run_checks(self, run_id: str) -> dict[str, Any]:
        return run_all_checks(self.store, run_id, self.cfg.scope, self.cfg.checks,
                              self.work_dir(run_id), self.cfg.git.base_branch)

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
