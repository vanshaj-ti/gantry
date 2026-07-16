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


def _allowed_paths(store: RunStore, run_id: str) -> list[str]:
    """Declared scope: backtick-quoted paths from the implementation plan,
    UNION any paths the build stage declared under a "## Scope additions"
    section in build-summary.md. Project-agnostic: no hardcoded allowlist.

    The plan is written once before build starts, so it can never anticipate
    a file the build agent only discovers it needs mid-implementation (e.g. a
    new test fixture, a config file an unexpected dependency requires). The
    build prompt template asks the agent to declare any such file under this
    section with a one-line reason; unioning it into the allowlist here means
    a build that's honest about scope drift doesn't get penalized for it —
    only genuinely undeclared/unexplained new files still trip the guard."""
    plan = store.read_artifact(run_id, "implementation-plan.md")
    allowed = _paths_from_text(plan) if plan else []
    build_summary = store.read_artifact(run_id, "build-summary.md")
    if build_summary:
        additions = _scope_additions_section(build_summary)
        if additions:
            allowed.extend(_paths_from_text(additions))
    return allowed


def run_scope_guard(store: RunStore, run_id: str, cfg: ScopeConfig, cwd: Path, base: str) -> dict[str, Any]:
    files = _changed_files(cwd, base)
    forbidden = [f for f in files if _matches_any(f, cfg.forbid_paths)]

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
        "warnings": warnings,
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
