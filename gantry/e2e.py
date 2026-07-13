"""Deterministic e2e test runner — no agent, no LLM.

Runs between checks and the evidence stage: for each configured app whose e2e
spec surface was actually touched by this run's diff, runs that app's e2e
command directly and captures pass/fail + failure artifact paths into a JSON
report. The evidence-stage prompt reads this report instead of invoking
Playwright itself, so a slow or hanging test suite can no longer burn (or
kill) an expensive LLM turn — this step is cheap to re-run on its own.
"""
from __future__ import annotations

import fnmatch
import subprocess
from pathlib import Path
from typing import Any

from .checks import _changed_files, _merge_base
from .config import E2eConfig
from .state import RunStore


def _touched_apps(files: list[str], apps: dict[str, str]) -> list[str]:
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


def run_e2e_tests(store: RunStore, run_id: str, cfg: E2eConfig, cwd: Path, base: str) -> dict[str, Any]:
    """No-op result (pass=True, apps=[]) when e2e isn't configured, or when no
    configured app was touched — evidence stage's own fallback path (running
    Playwright itself) only kicks in via the prompt template, not here."""
    if not cfg.enabled or not cfg.apps:
        out = {"enabled": False, "pass": True, "apps": []}
        store.write_result(run_id, "e2e-report.json", out)
        return out

    fixed_base = _merge_base(cwd, base)
    files = _changed_files(cwd, base)
    candidates = _touched_apps(files, cfg.apps)

    results = []
    all_pass = True
    for app in candidates:
        if not _has_specs(cwd, app, cfg.spec_glob):
            results.append({"app": app, "skipped": True, "reason": "no e2e specs found"})
            continue
        command = cfg.apps[app]
        try:
            proc = subprocess.run(command, shell=True, cwd=str(cwd),
                                  capture_output=True, text=True, timeout=cfg.timeout)
        except subprocess.TimeoutExpired as exc:
            all_pass = False
            partial_out = (exc.stdout or b"")
            partial_out = partial_out.decode() if isinstance(partial_out, bytes) else (partial_out or "")
            results.append({
                "app": app,
                "command": command,
                "exit_code": None,
                "pass": False,
                "stdout_tail": partial_out[-4000:],
                "stderr_tail": f"Timed out after {cfg.timeout}s",
                "failure_artifacts": [],
            })
            continue
        ok = proc.returncode == 0
        all_pass = all_pass and ok
        test_results_dir = cwd / "apps" / app / "test-results"
        artifacts = [str(p.relative_to(cwd)) for p in test_results_dir.glob("**/*")
                     if p.is_file()] if test_results_dir.is_dir() else []
        results.append({
            "app": app,
            "command": command,
            "exit_code": proc.returncode,
            "pass": ok,
            "stdout_tail": proc.stdout[-4000:],
            "stderr_tail": proc.stderr[-2000:],
            "failure_artifacts": artifacts[:50],
        })

    out = {
        "enabled": True,
        "base": fixed_base,
        "touched_apps": candidates,
        "apps": results,
        "pass": all_pass,
    }
    store.write_result(run_id, "e2e-report.json", out)
    return out
