"""Independent LLM review stage.

Runs after evidence, as a genuine agentic investigation — not a single prompt
stuffed with truncated artifact snippets. The reviewing agent runs inside the
run's own worktree (same `cwd` build/evidence used) with its normal file/shell
tools, and is pointed at the full, untruncated artifacts (which live in the
target repo's .agent-runs/<run_id>/, not the worktree) plus instructed to run
its own `git diff` and cross-reference the plan/acceptance-criteria against
what was actually implemented. Ideally a different model family than the
builder. Verdicts:

  APPROVE          -> review_approved
  REQUEST_CHANGES  -> review_changes_requested (+ review-comments.md; resume build)
  ESCALATE         -> review_escalated (human decision)
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from . import herdr as _herdr
from .config import GantryConfig
from .runners import get_runner
from .state import RunStore

logger = logging.getLogger(__name__)

REVIEW_ARTIFACTS = [
    "intake.md", "product-spec.md", "architecture-design.md",
    "implementation-plan.md", "build-summary.md", "evidence-report.md",
    "scope.json", "checks.json",
]


def _build_prompt(store: RunStore, run_id: str, cwd: Path, base: str, template: str) -> str:
    run_dir = store.run_dir(run_id)
    artifact_lines = []
    for name in REVIEW_ARTIFACTS:
        path = store.artifact_path(run_id, name)
        artifact_lines.append(f"- {path} {'(exists)' if path.exists() else '(missing)'}")
    parts = [
        template.replace("{RUN_ID}", run_id),
        "\n\n# Investigation instructions\n",
        f"You are running inside the implementation worktree at {cwd}. The run's "
        f"planning/evidence artifacts live in a separate directory, {run_dir} "
        f"(NOT inside this worktree) — read them directly with your file tools:\n",
        "\n".join(artifact_lines),
        f"\n\nTo see the actual code changes, run `git diff {base} --` yourself in "
        f"this worktree — do not rely on any diff text pasted into this prompt, "
        f"there isn't one; read the real files and the real diff.\n"
        f"\nCross-reference: does the diff satisfy every acceptance criterion in "
        f"product-spec.md? Does it match the scope and approach in "
        f"implementation-plan.md? Do the claims in evidence-report.md hold up "
        f"against what you can independently verify (read the actual test files, "
        f"re-run tests/checks if useful)? Investigate as deeply as needed before "
        f"deciding.\n",
    ]
    return "".join(parts)


def _parse_verdict(text: str, cfg: GantryConfig) -> str:
    upper = (text or "").upper()
    if any(k.upper() in upper for k in cfg.review.request_changes_keywords):
        return "REQUEST_CHANGES"
    if any(k.upper() in upper for k in cfg.review.escalate_keywords):
        return "ESCALATE"
    if any(k.upper() in upper for k in cfg.review.approve_keywords):
        return "APPROVE"
    return "ESCALATE"


def _report_herdr(cfg: GantryConfig, run_id: str, status: str) -> None:
    """Best-effort herdr sidebar report; never raises, mirrors Engine._set_status."""
    try:
        _herdr.report_state(run_id, status, enabled=cfg.herdr.enabled and cfg.herdr.report_state)
    except Exception:
        logger.debug("herdr report_state failed for run %s (%s)", run_id, status, exc_info=True)


def run_review(store: RunStore, run_id: str, cfg: GantryConfig, cwd: Path) -> dict[str, Any]:
    base = cfg.git.base_branch
    prompts_dir = Path(cfg.prompts_dir)
    prompts_dir = prompts_dir if prompts_dir.is_absolute() else (cwd / prompts_dir)
    tmpl_path = prompts_dir / "review.md"
    template = tmpl_path.read_text() if tmpl_path.exists() else (
        "# Independent review\n\nInvestigate the run's artifacts and this worktree's actual "
        "diff (see instructions below). Reply with exactly one of APPROVE, REQUEST_CHANGES, "
        "or ESCALATE, followed by your reasoning.\n")

    prompt = _build_prompt(store, run_id, cwd, base, template)
    store.write_log(run_id, "review-prompt.md", prompt)

    _report_herdr(cfg, run_id, "review_running")
    runner = get_runner(cfg.review.runner)

    # Register any enabled MCP servers for review (e.g. codebase-memory), same
    # as engine.run_agent_stage does for plan/build/evidence.
    from .mcp import ensure_mcp_for_stage
    mcp_results = ensure_mcp_for_stage(cfg, "review", runner.name, cwd)
    if mcp_results:
        store.write_log(run_id, "review-mcp.json", json.dumps(mcp_results, indent=2))

    session_id = store.get_session_id(run_id, "review")
    # Save runner/model BEFORE invoking (mirrors engine.run_agent_stage) so
    # `gantry watch`'s AGENT/MODEL columns aren't blank for the whole duration
    # of review_running — session_id isn't known until the agent returns, but
    # which runner/model is driving it right now is, and that's what the
    # live-status columns are for.
    store.save_session(run_id, "review", model=cfg.review.model, runner=runner.name)
    # This is now an agentic investigation (the reviewer reads files, runs git
    # diff, re-checks tests itself), so it needs the same headless auto-approve
    # the other stages get, and more turns than a single-shot prompt needed.
    result = runner.run(
        cwd=cwd, prompt=prompt, model=cfg.review.model,
        session_id=session_id, plan_mode=False, skip_permissions=cfg.agent.skip_permissions,
        output_format="json", session_name=f"{run_id}-review", max_turns=80, timeout=900,
    )
    store.save_session(run_id, "review", session_id=result.session_id,
                       model=cfg.review.model, runner=runner.name)
    from .cost import accumulate as _accumulate_cost
    _accumulate_cost(store, run_id, "review", result.usage)

    text = result.raw.get("result", "") if isinstance(result.raw, dict) else result.stdout
    verdict = _parse_verdict(str(text) or result.stdout, cfg) if result.ok else "ESCALATE"

    out = {"verdict": verdict, "ok": result.ok, "model": cfg.review.model,
           "session_id": result.session_id, "result": str(text)[:8000]}
    store.write_result(run_id, "review-result.json", out)

    status = {"APPROVE": "review_approved", "REQUEST_CHANGES": "review_changes_requested",
              "ESCALATE": "review_escalated"}[verdict]
    store.update_state(run_id, status=status, review_verdict=verdict)
    _report_herdr(cfg, run_id, status)
    if verdict == "REQUEST_CHANGES":
        store.artifact_path(run_id, "review-comments.md").write_text(
            f"# Review: changes requested\n\n{text}\n")
    return out
