"""Guardrails that run after build, before review.

Two deterministic layers (Decision B — no regex rule engine, no LLM here):

  1. Scope guard  (Gantry-owned) — forbidden path globs + optional plan-scope
     enforcement. Catches an agent that touched files it shouldn't have.
  2. Repo checks  (delegated)     — run the repo's own commands (lint/build/tsc)
     and gate on exit code. The repo owns its house rules; Gantry just runs them.

Semantic/architectural judgment is NOT here — that's the LLM review stage.
"""
from __future__ import annotations

import fnmatch
import re
import subprocess
from pathlib import Path
from typing import Any

from .config import ChecksConfig, ScopeConfig
from .state import RunStore


def _merge_base(cwd: Path, base: str) -> str:
    """Resolve the fixed commit the run's branch actually forked from.

    `base` (cfg.git.base_branch, e.g. "origin/staging") is a moving ref — if
    the base branch advances after the worktree's branch was cut, diffing
    against it directly picks up every unrelated commit merged upstream since,
    not just this run's own changes. Diff against the merge-base instead, a
    fixed point in history, so the scope guard only ever sees what this run
    actually touched.
    """
    proc = subprocess.run(["git", "merge-base", "HEAD", base],
                          cwd=str(cwd), capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        # Fall back to the moving ref if merge-base can't be resolved (e.g.
        # base was force-pushed past history) rather than hard-failing checks.
        return base
    return proc.stdout.strip() or base


def _changed_files(cwd: Path, base: str) -> list[str]:
    fixed_base = _merge_base(cwd, base)
    proc = subprocess.run(["git", "diff", "--name-only", fixed_base, "--"],
                          cwd=str(cwd), capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout)
    return [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]


def _matches_any(path: str, patterns: list[str]) -> bool:
    for pat in patterns:
        # support both prefix ("supabase/functions/") and glob ("**/*.pem")
        if path == pat or path.startswith(pat.rstrip("/") + "/"):
            return True
        if fnmatch.fnmatch(path, pat):
            return True
    return False


def _allowed_from_plan(store: RunStore, run_id: str) -> list[str]:
    """Extract backtick-quoted paths from the implementation plan as the
    declared scope. Project-agnostic: no hardcoded file allowlist."""
    plan = store.read_artifact(run_id, "implementation-plan.md")
    if not plan:
        return []
    paths = re.findall(r"`([^`]+)`", plan)
    return [p for p in paths if "/" in p and not p.startswith(".")]


def run_scope_guard(store: RunStore, run_id: str, cfg: ScopeConfig, cwd: Path, base: str) -> dict[str, Any]:
    files = _changed_files(cwd, base)
    forbidden = [f for f in files if _matches_any(f, cfg.forbid_paths)]

    unexpected: list[str] = []
    if cfg.enforce_plan_scope:
        allowed = _allowed_from_plan(store, run_id)
        if allowed:
            unexpected = [f for f in files
                          if not _matches_any(f, allowed)
                          and not any(f.startswith(a.rstrip("/") + "/") for a in allowed)]

    out = {
        "base": base,
        "changed_files": files,
        "forbidden_files": forbidden,
        "unexpected_files": unexpected,
        "pass": not forbidden and not unexpected,
    }
    store.write_result(run_id, "scope.json", out)
    return out


def run_repo_checks(cfg: ChecksConfig, cwd: Path) -> dict[str, Any]:
    results = []
    all_pass = True
    for command in cfg.commands:
        proc = subprocess.run(command, shell=True, cwd=str(cwd),
                              capture_output=True, text=True, timeout=cfg.timeout)
        ok = proc.returncode == 0
        all_pass = all_pass and ok
        results.append({
            "command": command,
            "exit_code": proc.returncode,
            "pass": ok,
            "stdout_tail": proc.stdout[-2000:],
            "stderr_tail": proc.stderr[-2000:],
        })
    return {"pass": all_pass, "results": results}


def run_all_checks(store: RunStore, run_id: str, scope_cfg: ScopeConfig,
                   checks_cfg: ChecksConfig, cwd: Path, base: str) -> dict[str, Any]:
    scope = run_scope_guard(store, run_id, scope_cfg, cwd, base)
    checks = run_repo_checks(checks_cfg, cwd)
    out = {"pass": scope["pass"] and checks["pass"], "scope": scope, "checks": checks}
    store.write_result(run_id, "checks.json", out)
    if out["pass"]:
        # Clear any prior block so advance_all (which only fires on
        # AUTO_TRANSITIONS states) can pick this run back up. Restoring
        # status to build_complete re-enters the normal build->evidence
        # transition — this is what makes `gantry checks --run ID` a real
        # recovery path after fixing a scope/lint/build failure, instead of
        # leaving the run permanently stuck at status=blocked.
        store.update_state(run_id, status="build_complete", blocked_on=None, checks="pass")
    else:
        blocked = "scope" if not scope["pass"] else "checks"
        store.update_state(run_id, status="blocked", blocked_on=blocked, checks="fail")
    return out
