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
from .redact import proxy_secrets, redact_secrets
from .runners import get_runner, resolve_proxy_env
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

    def _redact(self, text: str) -> str:
        """Redact known-sensitive values (GH_TOKEN/TFY_API_KEY, this config's
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

    def render_prompt(self, stage: str, run_id: str) -> str:
        template_path = self._prompt_template_path(stage)
        if not template_path.exists():
            # fall back to a minimal generic instruction so a bare repo still runs
            artifact = self.cfg.artifact_for(stage)
            base = (f"# Stage: {stage}\n\nRun: {run_id}\n\n"
                    f"Read the artifacts in .agent-runs/{run_id}/. Perform the {stage} stage "
                    f"and write your output to .agent-runs/{run_id}/{artifact}.\n")
        else:
            base = template_path.read_text().replace("{RUN_ID}", run_id)
        return (self._plan_context_directive(stage) + base + self._skills_directive(stage)
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

    def _skills_directive(self, stage: str) -> str:
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
        if stage not in ("build", "evidence") or not self.cfg.skills.enabled:
            return ""
        skills = ", ".join(f"`{s}`" for s in self.cfg.skills.enabled)
        if stage == "evidence":
            framing = self.cfg.skills.evidence_directive or (
                "IMPORTANT: the implementation is already complete — your job is to VERIFY "
                "it, not redo it. Use these skills for verification rigor (confirm tests "
                "actually pass, confirm the plan's acceptance criteria are met) — do NOT "
                "re-implement, refactor, or restart any part of the build.\n"
            )
        else:
            framing = (
                "IMPORTANT: an approved implementation plan already exists for this run. Use "
                "these skills for EXECUTION discipline (TDD, systematic debugging, review rigor) "
                "— do NOT restart spec/design/planning. Execute the existing plan.\n"
            )
        return (
            f"\n\n---\n## Mandated skills for this stage\n"
            f"Load and actively use: {skills}. Invoke the Skill tool — do not leave them "
            f"passively in context.\n\n{framing}"
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
        no effect on the run's own execution, only on which runs a filtered
        command touches."""
        first = self.cfg.stages[0] if self.cfg.stages else "plan"
        rid = self.store.new_run_id(title, run_id)
        self.store.create(rid, title)
        self.store.artifact_path(rid, "intake.md").write_text(f"# Intake\n\n{request.strip() or title}\n")
        deps = list(depends_on) if depends_on else []
        for dep in deps:
            if not self.store.exists(dep):
                raise ValueError(f"depends_on references unknown run: {dep}")
        extra = {"tag": tag} if tag else {}
        if deps:
            self.store.update_state(rid, status="queued", current_stage=first,
                                    title=title, depends_on=deps, **extra)
        else:
            self.store.update_state(rid, status=f"awaiting_{first}", current_stage=first,
                                    title=title, **extra)
        return rid

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
        last_stage = self.cfg.stages[-1] if self.cfg.stages else None
        terminal_incomplete_ok = f"{last_stage}_complete" if last_stage else None
        for dep in deps:
            dep_state = self.store.state(dep)
            dep_status = dep_state.get("status", "")
            if dep_status in ("shipped", "shipped_manually") and dep_state.get("merged") is True:
                continue
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

        if stage == "build" and not resume:
            self._run_build_pre_hook(run_id, work_dir)

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
                proxy = self.cfg.proxy.get(runner.name)
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
                    env=resolve_proxy_env(runner.name, proxy),
                    proxy=proxy,
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
                                     self._redact(f"Agent subprocess timed out after {sm.timeout}s"))
                self._set_status(run_id, f"{stage}_failed")
                return {"stage": stage, "ok": False, "session_id": None, "error": "timeout"}
        finally:
            _stop_heartbeat(stop_hb, hb_thread)
        suffix = ".resume" if resume else ""
        self.store.write_log(run_id, f"{stage}{suffix}.stdout", self._redact(result.stdout))
        self.store.write_log(run_id, f"{stage}{suffix}.stderr", self._redact(result.stderr))
        self.store.write_result(run_id, f"{stage}-result.json", result.raw)
        self.store.save_session(run_id, stage, session_id=result.session_id,
                                model=sm.model, runner=runner.name)
        from .cost import accumulate as _accumulate_cost
        _accumulate_cost(self.store, run_id, stage, result.usage,
                         runner=runner.name, session_id=result.session_id)
        ok = result.ok
        if stage == "spec" and ok:
            # Deterministic structural gate — no LLM call — that the spec
            # stage's own acceptance-criteria.json companion artifact
            # actually exists and is well-formed, before letting the run
            # reach spec_complete (a human-review gate downstream trusts that
            # status to mean "the spec stage really finished its job").
            # Scoped to spec only; design/plan/build/evidence have no
            # equivalent structural gate here.
            from .checks import check_spec_artifacts
            gate = check_spec_artifacts(self.store, run_id)
            self.store.write_result(run_id, "spec-gate.json", gate)
            if not gate["pass"]:
                ok = False
                self.store.write_log(
                    run_id, "spec-gate.stderr",
                    self._redact(f"Spec stage structural gate failed: {gate['reason']}"))
        status = f"{stage}_complete" if ok else f"{stage}_failed"
        self._set_status(run_id, status)
        return {"stage": stage, "ok": ok, "session_id": result.session_id}

    def _resolver_checks_prompt(self, run_id: str, wt: Path) -> str:
        """Prompt-building for the ORIGINAL resolver purpose: checks_escalated
        after the normal build/checks auto-retry loop exhausted itself.
        Unchanged behavior from before `run_resolver_stage` gained a
        `failure_kind` parameter — see `run_resolver_stage`'s docstring."""
        from .advance import _checks_failure_detail
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
            proxy = self.cfg.proxy.get(runner.name)
            result = runner.run(
                cwd=wt, prompt=prompt, model=sm.model,
                session_id=None, plan_mode=False, skip_permissions=self.cfg.agent.skip_permissions,
                output_format=self.cfg.agent.output_format, session_name=f"{run_id}-resolve",
                max_turns=sm.max_turns * 2, timeout=sm.timeout,
                env=resolve_proxy_env(runner.name, proxy), proxy=proxy,
            )
        finally:
            _stop_heartbeat(stop_hb, hb_thread)
        self.store.write_log(run_id, "resolve.stdout", self._redact(result.stdout))
        self.store.write_log(run_id, "resolve.stderr", self._redact(result.stderr))
        self.store.write_result(run_id, "resolve-result.json", result.raw)
        from .cost import accumulate as _accumulate_cost
        _accumulate_cost(self.store, run_id, "resolve", result.usage,
                         runner=runner.name, session_id=result.session_id)

        if failure_kind == "conflict":
            # ship.py owns re-verification here (re-attempting the actual
            # push/create_pr call) and any status transition — this stage
            # only ran the fix-it agent and reports whether it claims to have
            # finished; ship.py never trusts agent_ok alone.
            return {"agent_ok": result.ok}

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
