"""Per-run git isolation via worktrees.

Gantry used to run agent stages directly in the target repo's checked-out
working tree — no branch, no isolation. Two runs on the same repo would
collide, and nothing stopped a run from executing against main/staging.

This gives each run its own worktree at `<target>/.worktrees/gantry/<run_id>`
on a fresh branch `gantry/<run_id>` off `cfg.git.base_branch`. Convention
matches a `.worktrees/` layout that's easy to prune with a periodic script
(or `git worktree list` + a merged-branch check) — merged/deleted branches
get their worktrees reaped the same way, no gantry-specific cleanup infra
needed.

`.agent-runs/` (run state/artifacts) stays in the MAIN repo, not the worktree,
so `gantry status`/`gantry watch` see every run without needing to know which
worktree it lives in.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

WORKTREES_SUBDIR = Path(".worktrees") / "gantry"


def branch_name(run_id: str) -> str:
    return f"gantry/{run_id}"


def worktree_path(target: Path, run_id: str) -> Path:
    return target / WORKTREES_SUBDIR / run_id


def _run(cmd: list[str], cwd: Path, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout)


def _branch_exists(target: Path, branch: str) -> bool:
    proc = _run(["git", "rev-parse", "--verify", "--quiet", branch], target)
    return proc.returncode == 0


def ensure_worktree(target: Path, run_id: str, base_branch: str) -> Path:
    """Idempotent: create the run's worktree+branch if missing, else reuse it.
    Returns the worktree path. Raises RuntimeError with git's stderr on failure.
    """
    wt = worktree_path(target, run_id)
    if wt.exists():
        return wt

    wt.parent.mkdir(parents=True, exist_ok=True)
    branch = branch_name(run_id)

    # Make sure base_branch is resolvable (fetch if it's a remote ref not yet local).
    _run(["git", "fetch", "--quiet", "origin"], target, timeout=120)

    if _branch_exists(target, branch):
        # Branch already exists (e.g. resumed run after a crash) — attach worktree to it.
        proc = _run(["git", "worktree", "add", str(wt), branch], target, timeout=120)
    else:
        proc = _run(["git", "worktree", "add", "-b", branch, str(wt), base_branch], target, timeout=120)

    if proc.returncode != 0:
        raise RuntimeError(f"git worktree add failed for {run_id}: {proc.stderr or proc.stdout}")

    # Stage prompts reference `.agent-runs/<run_id>/...` relative to the agent's
    # cwd. State/artifacts live in the main repo (RunStore), so symlink the whole
    # directory into the worktree the agent actually runs in. git ignores it
    # there too (matches the main repo's .gitignore entry).
    runs_link = wt / ".agent-runs"
    runs_target = target / ".agent-runs"
    if not runs_link.exists():
        runs_target.mkdir(parents=True, exist_ok=True)
        runs_link.symlink_to(runs_target, target_is_directory=True)

    _copy_env_files_if_present(target, wt)
    _install_deps_if_npm_project(wt)

    return wt


def _copy_env_files_if_present(target: Path, wt: Path) -> None:
    """Best-effort copy of gitignored .env files from the main repo into the
    fresh worktree.

    `.env` files are (correctly) gitignored, so a bare `git worktree add`
    never carries them — every agent stage/check that needs one (e.g. a
    Next.js app reading `NEXT_PUBLIC_*` at build time) fails with an error
    that looks identical to a real code regression, burning retries on an
    infra problem no code change can fix. Discovers .env files dynamically
    (root + one level under any top-level directory) rather than a hardcoded
    per-project list, so it works on any repo shape without gantry.toml
    needing to enumerate them.
    """
    root_env = target / ".env"
    if root_env.exists():
        (wt / ".env").write_text(root_env.read_text())
    # Covers both one-level (foo/.env) and two-level (apps/core/.env,
    # supabase/functions/.env) project layouts.
    for pattern in ("*/.env", "*/*/.env"):
        for src in target.glob(pattern):
            if ".worktrees" in src.parts or "node_modules" in src.parts:
                continue
            rel = src.relative_to(target)
            dst = wt / rel
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_text(src.read_text())
            except OSError:
                pass


def _install_deps_if_npm_project(wt: Path) -> None:
    """Best-effort npm dependency install for a freshly created worktree.

    A fresh `git worktree add` only checks out git-tracked files — node_modules
    is untracked, so a new worktree starts with none. Without this, build/checks
    stages fail on missing or stale packages that have nothing to do with the
    run's actual diff (e.g. a package.json dependency added on main after the
    worktree's base branch was cut). Non-fatal: a failed install here should not
    block worktree creation — the failure will surface clearly later if it
    actually matters, in whichever check needed the missing package.
    """
    if not (wt / "package.json").exists():
        return
    cmd = ["npm", "ci"] if (wt / "package-lock.json").exists() else ["npm", "install"]
    try:
        subprocess.run(cmd, cwd=str(wt), capture_output=True, text=True, timeout=600)
    except Exception:
        pass


def remove_worktree(target: Path, run_id: str) -> dict:
    """Prune a finished run's worktree + local branch. Never raises — mirrors
    ensure_worktree's {ok, ...} contract. No-ops if the worktree is already
    gone (idempotent, safe to call from a cleanup sweep more than once)."""
    wt = worktree_path(target, run_id)
    if not wt.exists():
        return {"ok": True, "removed": False, "reason": "worktree not present"}

    proc = _run(["git", "worktree", "remove", "--force", str(wt)], target, timeout=60)
    if proc.returncode != 0:
        return {"ok": False, "removed": False, "error": proc.stderr.strip() or proc.stdout.strip()}

    # Best-effort: the branch may already be gone, merged-and-deleted by the
    # PR flow, or checked out elsewhere — none of that should fail a cleanup.
    _run(["git", "branch", "-D", branch_name(run_id)], target, timeout=30)
    return {"ok": True, "removed": True}


def commit_all(worktree: Path, message: str) -> dict:
    """Stage and commit everything in the worktree. No-op (ok=True, committed=False)
    if there's nothing to commit.

    `ensure_worktree` symlinks `.agent-runs` into every worktree (see its comment)
    so stage prompts can reference run artifacts relative to their cwd. A repo's
    `.gitignore` entry for `.agent-runs/` (directory-only pattern) does NOT match
    that path when it's a symlink rather than a real directory, so `git add -A`
    happily stages the symlink itself as a new tracked file — pointing at an
    absolute path outside the repo, and pure noise in the PR diff. Explicitly
    unstage it every time regardless of whether the target repo's .gitignore
    happens to also cover the symlink form — this is a gantry-created artifact,
    gantry should be the one keeping it out of every commit.
    """
    _run(["git", "add", "-A"], worktree)
    _run(["git", "reset", "--", ".agent-runs"], worktree)
    status = _run(["git", "status", "--porcelain"], worktree)
    if not status.stdout.strip():
        return {"ok": True, "committed": False, "reason": "no changes"}
    proc = _run(["git", "commit", "--quiet", "-m", message], worktree, timeout=60)
    return {"ok": proc.returncode == 0, "committed": proc.returncode == 0,
            "output": (proc.stdout + proc.stderr)[-1000:]}


def push(worktree: Path, branch: str, remote_branch: str | None = None) -> dict:
    """Push `branch` (the worktree's local branch, always `gantry/<run_id>`) to
    `remote_branch` on origin if given, else to a same-named remote branch.
    Lets the pushed/PR branch read as normal engineering work (e.g.
    `chore/remove-dead-webhook`) while the local/worktree branch keeps its
    run_id-keyed name for Gantry's own bookkeeping."""
    remote_branch = remote_branch or branch
    refspec = f"{branch}:refs/heads/{remote_branch}"
    proc = _run(["git", "push", "--quiet", "-u", "origin", refspec], worktree, timeout=120)
    return {"ok": proc.returncode == 0, "output": (proc.stdout + proc.stderr)[-1000:],
            "remote_branch": remote_branch}


def create_pr(worktree: Path, remote_branch: str, base_branch: str, title: str, body: str) -> dict:
    """Uses `gh pr create`. Requires gh to be authenticated in the environment
    (GH_TOKEN or `gh auth login`). base_branch is normalized (strips 'origin/'
    since gh wants a plain branch name for --base)."""
    base = base_branch.removeprefix("origin/")
    proc = _run(
        ["gh", "pr", "create", "--base", base, "--head", remote_branch,
         "--title", title, "--body", body],
        worktree, timeout=60,
    )
    out = (proc.stdout + proc.stderr).strip()
    return {"ok": proc.returncode == 0, "url": proc.stdout.strip() if proc.returncode == 0 else None,
            "output": out[-1000:]}
