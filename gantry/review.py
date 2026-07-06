"""Independent LLM review stage.

Runs after evidence. Feeds the diff + run artifacts to a reviewing agent
(ideally a different model family than the builder) and parses a verdict from
config-driven keywords. Verdicts:

  APPROVE          -> review_approved
  REQUEST_CHANGES  -> review_changes_requested (+ review-comments.md; resume build)
  ESCALATE         -> review_escalated (human decision)
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from .config import GantryConfig
from .runners import get_runner
from .state import RunStore
from . import herdr as _herdr

REVIEW_ARTIFACTS = [
    "intake.md", "product-spec.md", "architecture-design.md",
    "implementation-plan.md", "build-summary.md", "evidence-report.md",
    "scope.json", "checks.json",
]


def _git_diff(cwd: Path, base: str) -> str:
    proc = subprocess.run(["git", "diff", base, "--"], cwd=str(cwd),
                          capture_output=True, text=True, timeout=120)
    return proc.stdout[:50000]


def _build_prompt(store: RunStore, run_id: str, cwd: Path, base: str, template: str) -> str:
    parts = [template.replace("{RUN_ID}", run_id), "\n\n# Artifacts\n"]
    for name in REVIEW_ARTIFACTS:
        content = store.read_artifact(run_id, name)
        parts.append(f"\n--- {name} ---\n{(content or '<MISSING>')[:12000]}")
    parts.append(f"\n\n# Git diff vs {base}\n{_git_diff(cwd, base)}")
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
        pass


def run_review(store: RunStore, run_id: str, cfg: GantryConfig, cwd: Path) -> dict[str, Any]:
    base = cfg.git.base_branch
    prompts_dir = Path(cfg.prompts_dir)
    prompts_dir = prompts_dir if prompts_dir.is_absolute() else (cwd / prompts_dir)
    tmpl_path = prompts_dir / "review.md"
    template = tmpl_path.read_text() if tmpl_path.exists() else (
        "# Independent review\n\nReview the diff and artifacts below. Reply with exactly one of "
        "APPROVE, REQUEST_CHANGES, or ESCALATE, followed by your reasoning.\n")

    prompt = _build_prompt(store, run_id, cwd, base, template)
    store.write_log(run_id, "review-prompt.md", prompt)

    _report_herdr(cfg, run_id, "review_running")
    runner = get_runner(cfg.review.runner)
    session_id = store.get_session_id(run_id, "review")
    result = runner.run(
        cwd=cwd, prompt=prompt, model=cfg.review.model,
        session_id=session_id, plan_mode=False, skip_permissions=False,
        output_format="json", session_name=f"{run_id}-review", max_turns=40,
    )
    store.save_session(run_id, "review", session_id=result.session_id, model=cfg.review.model)

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
