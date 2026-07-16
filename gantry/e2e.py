"""Deterministic e2e test runner — no agent, no LLM.

Runs between checks and the evidence stage: for each configured app whose e2e
spec surface was actually touched by this run's diff, runs that app's e2e
command directly and captures pass/fail + failure artifact paths into a JSON
report. The evidence-stage prompt reads this report instead of invoking
Playwright itself, so a slow or hanging test suite can no longer burn (or
kill) an expensive LLM turn — this step is cheap to re-run on its own.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from .checks import _changed_files, _merge_base
from .config import E2eConfig, _coerce_e2e_app
from .state import RunStore


def _touched_apps(files: list[str], apps: dict[str, Any]) -> list[str]:
    """Which configured apps does this run's diff actually touch (anywhere
    under apps/<name>/), not just its e2e specs — a change to app source with
    no spec changes still needs its existing e2e suite run for regression
    coverage; only apps with zero touched files skip entirely."""
    touched = []
    for name in apps:
        prefix = f"apps/{name}/"
        if any(f.startswith(prefix) for f in files):
            touched.append(name)
    return touched


def _has_specs(cwd: Path, app: str, spec_glob: str) -> bool:
    app_dir = cwd / "apps" / app
    if not app_dir.is_dir():
        return False
    pattern = str(app_dir / spec_glob)
    import glob as _glob
    return bool(_glob.glob(pattern, recursive=True))


def _run_one_app(cwd: Path, app: str, command: str, timeout: int) -> dict[str, Any]:
    """One e2e attempt for one app. Caller (run_e2e_tests) handles retry —
    kept separate so a retry re-runs the exact same subprocess+artifact-
    collection logic, not a copy of it."""
    try:
        proc = subprocess.run(command, shell=True, cwd=str(cwd),
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        partial_out = exc.stdout or b""
        partial_out = partial_out.decode() if isinstance(partial_out, bytes) else (partial_out or "")
        return {
            "app": app, "command": command, "exit_code": None, "pass": False,
            "stdout_tail": partial_out[-4000:],
            "stderr_tail": f"Timed out after {timeout}s",
            "failure_artifacts": [],
        }
    test_results_dir = cwd / "apps" / app / "test-results"
    artifacts = [str(p.relative_to(cwd)) for p in test_results_dir.glob("**/*")
                 if p.is_file()] if test_results_dir.is_dir() else []
    return {
        "app": app, "command": command, "exit_code": proc.returncode,
        "pass": proc.returncode == 0,
        "stdout_tail": proc.stdout[-4000:],
        "stderr_tail": proc.stderr[-2000:],
        "failure_artifacts": artifacts[:50],
    }


def run_e2e_tests(store: RunStore, run_id: str, cfg: E2eConfig, cwd: Path, base: str) -> dict[str, Any]:
    """No-op result (pass=True, apps=[]) when e2e isn't configured, or when no
    configured app was touched — evidence stage's own fallback path (running
    Playwright itself) only kicks in via the prompt template, not here.

    A failing app retries up to its own E2eAppConfig.retry count before being
    included in the failure report as failed — scoped per-app since e2e
    flakiness is usually app-specific (a Playwright suite hitting a real
    external service is flakier than a pure-frontend suite), unlike
    checks.retry_checks which retries the whole build on any check failure."""
    if not cfg.enabled or not cfg.apps:
        out = {"enabled": False, "pass": True, "apps": []}
        store.write_result(run_id, "e2e-report.json", out)
        return out

    apps = {name: _coerce_e2e_app(spec) for name, spec in cfg.apps.items()}
    fixed_base = _merge_base(cwd, base)
    files = _changed_files(cwd, base)
    candidates = _touched_apps(files, cfg.apps)

    results = []
    all_pass = True
    for app in candidates:
        app_cfg = apps[app]
        spec_glob = app_cfg.spec_glob or cfg.spec_glob
        if not _has_specs(cwd, app, spec_glob):
            results.append({"app": app, "skipped": True, "reason": "no e2e specs found"})
            continue
        attempts = []
        result = _run_one_app(cwd, app, app_cfg.command, cfg.timeout)
        attempts.append(result)
        retries_used = 0
        while not result["pass"] and retries_used < app_cfg.retry:
            retries_used += 1
            result = _run_one_app(cwd, app, app_cfg.command, cfg.timeout)
            attempts.append(result)
        if retries_used:
            result = {**result, "retries_used": retries_used, "retry_cap": app_cfg.retry}
        all_pass = all_pass and result["pass"]
        results.append(result)

    out = {
        "enabled": True,
        "base": fixed_base,
        "touched_apps": candidates,
        "apps": results,
        "pass": all_pass,
    }
    store.write_result(run_id, "e2e-report.json", out)
    return out
