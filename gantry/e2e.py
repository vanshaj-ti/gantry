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
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .checks import _changed_files, _merge_base
from .config import E2eConfig, _coerce_e2e_app
from .state import RunStore, now_iso


@dataclass(frozen=True)
class E2eOutcome:
    """Typed, transition-free result of the E2E verification stage."""

    status: str
    passed: bool
    enabled: bool
    apps: list[dict[str, Any]]
    reason: str | None = None
    base: str | None = None
    touched_apps: list[str] = field(default_factory=list)
    started_at: str = field(default_factory=now_iso)
    completed_at: str = field(default_factory=now_iso)
    duration_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "status": self.status,
            "enabled": self.enabled,
            "pass": self.passed,
            "apps": self.apps,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_seconds": self.duration_seconds,
        }
        if self.reason:
            out["reason"] = self.reason
        if self.base is not None:
            out["base"] = self.base
            out["touched_apps"] = self.touched_apps
        return out

    def to_legacy_dict(self) -> dict[str, Any]:
        if not self.enabled:
            return {"enabled": False, "pass": True, "apps": []}
        return {
            "enabled": True,
            "base": self.base,
            "touched_apps": self.touched_apps,
            "apps": self.apps,
            "pass": self.passed,
        }


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


def evaluate_e2e(store: RunStore, run_id: str, cfg: E2eConfig,
                 cwd: Path, base: str) -> E2eOutcome:
    """Execute E2E and return a typed outcome without changing run state."""
    del store, run_id
    started_at = now_iso()
    started = time.monotonic()
    if not cfg.enabled:
        return E2eOutcome(
            status="skipped", passed=True, enabled=False, apps=[],
            reason="disabled", started_at=started_at, completed_at=now_iso(),
            duration_seconds=round(time.monotonic() - started, 6),
        )
    if not cfg.apps:
        return E2eOutcome(
            status="skipped", passed=True, enabled=False, apps=[],
            reason="no_apps_configured", started_at=started_at, completed_at=now_iso(),
            duration_seconds=round(time.monotonic() - started, 6),
        )

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

    status = "failed" if not all_pass else ("skipped" if not candidates else "passed")
    reason = "no_touched_apps" if not candidates else None
    return E2eOutcome(
        status=status,
        passed=all_pass,
        enabled=True,
        base=fixed_base,
        touched_apps=candidates,
        apps=results,
        reason=reason,
        started_at=started_at,
        completed_at=now_iso(),
        duration_seconds=round(time.monotonic() - started, 6),
    )


def run_e2e_tests(store: RunStore, run_id: str, cfg: E2eConfig, cwd: Path, base: str) -> dict[str, Any]:
    """No-op result (pass=True, apps=[]) when e2e isn't configured, or when no
    configured app was touched — evidence stage's own fallback path (running
    Playwright itself) only kicks in via the prompt template, not here.

    A failing app retries up to its own E2eAppConfig.retry count before being
    included in the failure report as failed — scoped per-app since e2e
    flakiness is usually app-specific (a Playwright suite hitting a real
    external service is flakier than a pure-frontend suite), unlike
    checks.retry_checks which retries the whole build on any check failure."""
    outcome = evaluate_e2e(store, run_id, cfg, cwd, base)
    out = outcome.to_legacy_dict()
    store.write_result(run_id, "e2e-report.json", out)
    return out
