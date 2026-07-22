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
import json
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .config import CheckCommand, ChecksConfig, ScopeConfig, _coerce_check_command
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


def _strip_fenced_code_blocks(text: str) -> str:
    """Remove ```-fenced code blocks before scanning for single-backtick paths.
    Without this, a stray single backtick inside a fenced snippet (e.g. an
    apostrophe-adjacent backtick in embedded TS/SQL) pairs across the fence
    boundary with an unrelated backtick elsewhere in the doc, silently
    swallowing real single-line `path/to/file.ts` mentions into one bogus
    multi-line "path" and dropping them from the allowlist.

    Split on the fence delimiter itself rather than a non-greedy `.*?` regex:
    a naive `re.sub(r"```.*?```", "", text, flags=re.DOTALL)` pairs the FIRST
    ``` in the document with the NEXT ``` regardless of which fenced block
    each one actually opens/closes — two independent fenced blocks elsewhere
    in a long plan (e.g. one at char 4934 closing, an unrelated one opening
    at char 7328) get treated as one bogus pair spanning everything between
    them, silently deleting real prose (and the backtick-quoted file paths in
    it) that was never inside a code fence at all. Splitting on the literal
    ``` marker and dropping every odd-indexed segment (the content strictly
    between an opening and its matching closing fence) has no such
    cross-pairing failure mode, since split() naturally alternates
    outside/inside/outside/inside per fence in document order."""
    parts = text.split("```")
    # parts[0], parts[2], parts[4], ... are outside fences (keep).
    # parts[1], parts[3], parts[5], ... are inside fences (drop).
    return "".join(part for i, part in enumerate(parts) if i % 2 == 0)


_BACKTICK_RUN_RE = re.compile(r"`+")


def _extract_code_spans(text: str) -> list[str]:
    """Extract inline code-span contents using CommonMark backtick-run
    matching, not naive first-backtick/next-backtick pairing.

    Per spec, a code span opens with a run of N backticks and closes ONLY
    with the next run of exactly N backticks — a run of a different length
    is not a closer and is skipped over. `re.findall(r"`([^`]+)`", ...)`
    ignores run length entirely: it pairs every backtick with the very next
    one, in order, for the whole document. That desyncs the moment a run of
    length != 1 appears earlier than expected — e.g. a JS template literal
    nested inside a single-backtick markdown span (`` `url = `${a}${b}` ` ``)
    produces backtick runs of length 1, 1, 2 in sequence. The naive regex
    pairs run 1 with run 2 (fine) but then run 3 (length 2, meant to close
    nothing on its own) with whatever single backtick comes next in the
    document, merging everything between them — including real
    `path/to/file.ts` mentions — into one bogus multi-paragraph "path" and
    dropping it from the scope-guard allowlist.

    Matching by run length keeps every span independent: an opening run
    with no same-length closer anywhere later is literal text (per spec),
    and scanning resumes right after it — so it can never desync pairing
    for spans elsewhere in the document.
    """
    runs = [(m.start(), m.end()) for m in _BACKTICK_RUN_RE.finditer(text)]
    spans: list[str] = []
    i = 0
    n_runs = len(runs)
    while i < n_runs:
        start, end = runs[i]
        length = end - start
        j = i + 1
        closer = None
        while j < n_runs:
            c_start, c_end = runs[j]
            if c_end - c_start == length:
                closer = (c_start, c_end)
                break
            j += 1
        if closer is None:
            i += 1
            continue
        spans.append(text[end:closer[0]])
        i = j + 1
    return spans


_SCOPE_ADDITIONS_HEADER_RE = re.compile(r"^#{1,6}\s*scope additions\s*$", re.IGNORECASE | re.MULTILINE)


def _paths_from_text(text: str) -> list[str]:
    """Extract backtick-quoted, plausible file paths out of arbitrary markdown
    prose. Shared by both the plan and the build-summary "Scope additions"
    section, so both sources of declared scope use identical path validation.

    Must accept root-level filenames with no "/" (e.g. `eslint.config.js`,
    `package-lock.json`), dotfile/dot-prefixed paths (e.g.
    `.cursor/rules/foo.mdc`, `.env.local.example`), and bare top-level
    dotfiles with no extension after the leading dot (e.g. `.gitignore`,
    `.env`) — a prior version of this filter dropped all three cases, which
    produced false-positive scope-guard failures for plans that legitimately
    touch repo-root config files, dotdirs, or bare dotfiles."""
    text = _strip_fenced_code_blocks(text)
    paths = _extract_code_spans(text)
    return [p for p in paths
            if "\n" not in p
            and (re.match(r"^[.\w/-][\w./-]*\.[A-Za-z0-9]+$", p)
                 or re.match(r"^\.[A-Za-z0-9_-]+$", p))]


def _scope_additions_section(build_summary: str) -> str:
    """Extract just the "## Scope additions" section's body (up to the next
    heading of the same or higher level, or end of document). Returns "" if no
    such section exists — old-style build summaries with no additions section
    are unaffected, same as before this feature existed."""
    match = _SCOPE_ADDITIONS_HEADER_RE.search(build_summary)
    if not match:
        return ""
    rest = build_summary[match.end():]
    next_heading = re.search(r"^#{1,6}\s+\S", rest, re.MULTILINE)
    return rest[:next_heading.start()] if next_heading else rest


def _base_allowed_paths(store: RunStore, run_id: str) -> list[str]:
    """The plan stage's declared scope, BEFORE unioning build-declared
    additions: `allowed-files.json` (structured, written by the plan prompt)
    if it exists and parses with a non-empty `allowed_globs` list, else the
    same backtick-quoted-paths scrape of `implementation-plan.md` prose used
    before this file existed.

    Fully backward compatible by construction: a project whose plan.md
    template/agent doesn't yet write allowed-files.json never has the file at
    all, so `read_result` returns `{}`, `allowed_globs` is missing, and this
    falls straight through to the exact prose-scrape call that ran before
    this function existed. A malformed file (not a dict, empty list, wrong
    type) is treated the same as an absent one — fail open to the
    known-good prose path rather than either blocking everything (empty
    allowlist) or silently allowing everything (empty allowlist is also
    falsy, which run_scope_guard already treats as "scope check off" via its
    `if allowed:` guard) with no scope enforcement at all."""
    try:
        result = store.read_result(run_id, "allowed-files.json")
    except (ValueError, OSError):
        # store.read_result/_load does a bare json.loads with no error
        # handling of its own — a malformed allowed-files.json (invalid
        # JSON) must fall back to the prose path here, not propagate and
        # blow up plan-stage scope checking for the whole run.
        result = None
    if isinstance(result, dict):
        globs = result.get("allowed_globs")
        if isinstance(globs, list) and globs:
            return [g for g in globs if isinstance(g, str)]
    plan = store.read_artifact(run_id, "implementation-plan.md")
    return _paths_from_text(plan) if plan else []


def _allowed_paths(store: RunStore, run_id: str) -> list[str]:
    """Declared scope: `allowed-files.json` if present and valid, else
    backtick-quoted paths from the implementation plan prose (see
    `_base_allowed_paths`) — UNION any paths the build stage declared under a
    "## Scope additions" section in build-summary.md. Project-agnostic: no
    hardcoded allowlist.

    The plan is written once before build starts, so it can never anticipate
    a file the build agent only discovers it needs mid-implementation (e.g. a
    new test fixture, a config file an unexpected dependency requires). The
    build prompt template asks the agent to declare any such file under this
    section with a one-line reason; unioning it into the allowlist here means
    a build that's honest about scope drift doesn't get penalized for it —
    only genuinely undeclared/unexplained new files still trip the guard."""
    allowed = _base_allowed_paths(store, run_id)
    build_summary = store.read_artifact(run_id, "build-summary.md")
    if build_summary:
        additions = _scope_additions_section(build_summary)
        if additions:
            allowed.extend(_paths_from_text(additions))
    return allowed


def run_scope_guard(store: RunStore, run_id: str, cfg: ScopeConfig, cwd: Path, base: str) -> dict[str, Any]:
    files = _changed_files(cwd, base)
    forbidden = [f for f in files if _matches_any(f, cfg.forbid_paths)]
    # High-risk is a separate signal from forbidden/unexpected — it does NOT
    # affect `pass` below. It is surfaced here for advance.py to escalate to a
    # human-gated status (checks_high_risk_escalated) regardless of autonomy
    # flags; a scope-guard failure/pass is orthogonal to that decision.
    high_risk = [f for f in files if _matches_any(f, cfg.high_risk_paths)]

    unexpected: list[str] = []
    warnings: list[str] = []
    if cfg.mode != "off":
        allowed = _allowed_paths(store, run_id)
        if allowed:
            drifted = [f for f in files
                       if not _matches_any(f, allowed)
                       and not any(f.startswith(a.rstrip("/") + "/") for a in allowed)]
            # require_declared_additions=True (default) is already satisfied by
            # _allowed_paths unioning in build-declared files above — anything
            # still in `drifted` here is genuinely undeclared. When False, an
            # undeclared new file is treated as a warning instead of a scope
            # violation regardless of "block" vs "warn" mode.
            if cfg.mode == "block" and cfg.require_declared_additions:
                unexpected = drifted
            else:
                warnings = [f"undeclared file outside plan scope: {f}" for f in drifted]

    out = {
        "base": base,
        "changed_files": files,
        "forbidden_files": forbidden,
        "unexpected_files": unexpected,
        "high_risk_files": high_risk,
        "warnings": warnings,
        "pass": not forbidden and not unexpected,
    }
    store.write_result(run_id, "scope.json", out)
    return out


def _run_one_check(cmd: CheckCommand, default_timeout: int, cwd: Path) -> dict[str, Any]:
    proc = subprocess.run(cmd.command, shell=True, cwd=str(cwd), capture_output=True,
                          text=True, timeout=cmd.timeout or default_timeout)
    return {
        "command": cmd.command,
        "exit_code": proc.returncode,
        "pass": proc.returncode == 0,
        "stdout_tail": proc.stdout[-2000:],
        "stderr_tail": proc.stderr[-2000:],
    }


def _run_one_check_with_flaky_retry(cmd: CheckCommand, default_timeout: int, cwd: Path,
                                    flaky_retry_attempts: int, store: "RunStore | None",
                                    run_id: str | None) -> dict[str, Any]:
    """Wraps `_run_one_check`: if the first attempt fails and
    `flaky_retry_attempts > 0`, re-run the SAME command (bare, no code
    changes, no agent call) up to `flaky_retry_attempts` additional times,
    respecting the command's own configured/default timeout each time. If any
    re-run passes, the returned result is overwritten to report a pass (with
    a `flaky` marker + attempt count) and a flake-occurrence record is
    appended to the target repo's flake log via `store.record_flake` (when a
    store/run_id were supplied). If every re-run also fails, the ORIGINAL
    failing result is returned unchanged — identical behavior to today, so
    this function is a no-op passthrough when `flaky_retry_attempts == 0`
    (the default) or the first attempt already passes."""
    result = _run_one_check(cmd, default_timeout, cwd)
    if result["pass"] or flaky_retry_attempts <= 0:
        return result
    for attempt in range(1, flaky_retry_attempts + 1):
        retry_result = _run_one_check(cmd, default_timeout, cwd)
        if retry_result["pass"]:
            retry_result["flaky"] = True
            retry_result["attempts_before_pass"] = attempt
            if store is not None and run_id is not None:
                store.record_flake(cmd.command, run_id, attempt)
            return retry_result
    # every re-run failed too — treat as a genuine failure, same as today
    return result


def run_repo_checks(cfg: ChecksConfig, cwd: Path, store: "RunStore | None" = None,
                    run_id: str | None = None) -> dict[str, Any]:
    """Runs cfg.commands in the order the caller listed them, but a
    `parallel=true` command is bounded-concurrent with the OTHER
    parallel=true commands via a thread pool (subprocess.run releases the
    GIL while blocked on the child, so this is real wall-clock parallelism,
    not just concurrency theater). Commands without parallel=true still run
    serially, in list order, same as before this feature existed — so a
    project with no [checks] commands using {parallel=true} sees byte-identical
    behavior. Output shape ({"pass": bool, "results": [...]}) is unchanged
    regardless of how commands were split, so run_all_checks/advance.py need
    no changes.

    `cfg.flaky_retry_attempts` (default 0 = disabled) is threaded through to
    each command via `_run_one_check_with_flaky_retry` — see that function's
    docstring. `store`/`run_id` are only needed when flaky_retry_attempts > 0
    (to record flake occurrences); omit them for the flag-off path or in
    tests exercising `run_repo_checks` directly with no RunStore at hand.

    SECURITY: `cfg` here is always the ChecksConfig resolved from the TARGET
    repo's gantry.toml (via Engine.__init__ -> config.load_config(self.target)),
    never re-read from the worktree `cwd` these commands actually execute in.
    `cwd` is only ever used as the subprocess working directory — an
    agent-produced branch editing its own worktree's gantry.toml cannot change
    which commands get shelled out here, since this function is never handed a
    ChecksConfig loaded from that worktree. See config.load_config's docstring
    for the full invariant this depends on."""
    commands = [_coerce_check_command(c) for c in cfg.commands]
    # Index-keyed, not command-string-keyed: two entries can legitimately
    # share the same command string (e.g. the same lint command listed twice
    # with different timeouts is unusual but not invalid), and a string key
    # would silently collapse them to one result.
    results: list[dict[str, Any] | None] = [None] * len(commands)
    parallel_indices = [i for i, c in enumerate(commands) if c.parallel]
    serial_indices = [i for i, c in enumerate(commands) if not c.parallel]

    if parallel_indices:
        with ThreadPoolExecutor(max_workers=max(1, cfg.max_parallel)) as pool:
            futures = {pool.submit(_run_one_check_with_flaky_retry, commands[i], cfg.timeout, cwd,
                                   cfg.flaky_retry_attempts, store, run_id): i
                      for i in parallel_indices}
            for future in futures:
                results[futures[future]] = future.result()
    for i in serial_indices:
        results[i] = _run_one_check_with_flaky_retry(commands[i], cfg.timeout, cwd,
                                                      cfg.flaky_retry_attempts, store, run_id)

    all_pass = all(r["pass"] for r in results)
    return {"pass": all_pass, "results": results}


def run_all_checks(store: RunStore, run_id: str, scope_cfg: ScopeConfig,
                   checks_cfg: ChecksConfig, cwd: Path, base: str) -> dict[str, Any]:
    scope = run_scope_guard(store, run_id, scope_cfg, cwd, base)
    checks = run_repo_checks(checks_cfg, cwd, store=store, run_id=run_id)
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


def check_spec_artifacts(store: RunStore, run_id: str) -> dict[str, Any]:
    """Structural (deterministic, no LLM call) gate check for the spec stage
    only: does `.agent-runs/{run_id}/acceptance-criteria.json` exist, parse as
    valid JSON, and have `criteria` as a non-empty list?

    This is intentionally narrow — it does not judge whether the criteria are
    GOOD, just that the spec stage actually produced the structured artifact
    the spec.md prompt template now asks for, so a spec that silently skips
    the JSON companion (or writes it malformed) doesn't sail through to
    `spec_complete` unnoticed. Scoped to the spec stage only; design/plan/
    build/evidence have no equivalent structural gate here."""
    raw = store.read_artifact(run_id, "acceptance-criteria.json")
    if raw is None:
        return {"pass": False, "reason": "acceptance-criteria.json is missing"}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {"pass": False, "reason": f"acceptance-criteria.json is not valid JSON: {exc}"}
    if not isinstance(data, dict):
        return {"pass": False, "reason": "acceptance-criteria.json must be a JSON object"}
    criteria = data.get("criteria")
    if not isinstance(criteria, list) or not criteria:
        return {"pass": False, "reason": "acceptance-criteria.json's \"criteria\" must be a non-empty list"}
    return {"pass": True, "criteria_count": len(criteria)}
