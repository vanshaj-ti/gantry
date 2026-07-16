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

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

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


def sync_local_base_branch(target: Path, base_branch: str) -> dict:
    """Fast-forward the LOCAL base_branch ref (e.g. "main") to match
    origin/<base_branch>, if it's behind. `git fetch origin` alone updates
    origin/main but never touches local main — a run's worktree branches off
    local main (git worktree add -b <run-branch> <wt> <base_branch> resolves
    against the local ref), so if local main has drifted behind origin (e.g.
    because auto_merge just squash-merged a prior run's PR on GitHub, which
    updates origin/main but nothing pulls that back locally), every
    subsequently-created worktree branches off a STALE main. Its later scope-
    guard diff then falsely flags the prior run's already-shipped files as
    "unexpected" (they look brand-new relative to that stale merge-base) —
    checks fail, auto-retry exhausts, and the run escalates to build_failed
    for a run that has no real problem, just a stale base ref. Runs this
    after every fetch in ensure_worktree so newly-created worktrees always
    branch from a base that's actually current. No-op (and never raises) if
    base_branch is a remote ref already (e.g. "origin/main") or has no
    upstream, or if the local branch has diverged (ambiguous — leave it for a
    human rather than silently force-updating local history).

    Fetches origin itself rather than trusting the caller already did — the
    origin/<base_branch> ref this compares against must itself be current, or
    "already_current"/"fast_forwarded" would be judged against stale data."""
    if base_branch.startswith("origin/") or "/" in base_branch:
        return {"ok": True, "action": "skipped_remote_ref"}
    _run(["git", "fetch", "--quiet", "origin", base_branch], target, timeout=120)
    local = _run(["git", "rev-parse", "--verify", "--quiet", base_branch], target)
    remote = _run(["git", "rev-parse", "--verify", "--quiet", f"origin/{base_branch}"], target)
    if local.returncode != 0 or remote.returncode != 0:
        return {"ok": True, "action": "skipped_no_upstream"}
    if local.stdout.strip() == remote.stdout.strip():
        return {"ok": True, "action": "already_current"}
    is_ancestor = _run(["git", "merge-base", "--is-ancestor", base_branch, f"origin/{base_branch}"], target)
    if is_ancestor.returncode != 0:
        # Local base_branch has commits origin doesn't (diverged, not just
        # behind) — fast-forwarding here could silently discard local work.
        # Leave it alone; this is a human decision, not one to automate.
        return {"ok": True, "action": "skipped_diverged"}
    current = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], target)
    if current.stdout.strip() == base_branch:
        proc = _run(["git", "merge", "--ff-only", f"origin/{base_branch}"], target, timeout=60)
    else:
        proc = _run(["git", "fetch", ".", f"origin/{base_branch}:{base_branch}"], target, timeout=60)
    return {"ok": proc.returncode == 0, "action": "fast_forwarded" if proc.returncode == 0 else "ff_failed",
            "output": (proc.stdout + proc.stderr)[-500:]}


def merge_base_into_worktree(target: Path, run_id: str, base_branch: str) -> dict:
    """Merge the current base_branch into this run's own branch, inside its
    worktree, before the scope guard computes a merge-base diff.

    sync_local_base_branch (above) only fixes drift at worktree-CREATION time
    — it keeps a newly-created worktree from branching off a stale base. It
    does nothing for a worktree that already exists: once a run's branch is
    cut, base_branch can keep moving (e.g. run N ships mid-way through run
    N+1's build), and the scope guard's `git diff <merge-base> --` then
    diffs against an increasingly stale merge-base, making run N's
    already-shipped files look like "unexpected new files" on run N+1's
    branch. Real incident: run 3 shipped while run 4's build was already in
    flight — run 4's branch still forked from before run 3's merge, and
    checks kept failing on scope even though sync_local_base_branch had
    already done its job at creation time.

    Runs `git merge base_branch` inside the worktree itself (not the target
    repo) so the run's OWN branch — not just the shared base_branch ref —
    catches up. Safe by construction: a real content conflict here means the
    run's own changes and base_branch's changes touch the same lines, which
    is a genuine merge conflict a human or the build agent needs to resolve,
    not something to auto-resolve or discard — so a conflict is reported,
    never force-resolved, and the worktree is left in its conflicted state
    for the next build/resume to handle (or a human to intervene on)."""
    proc = _run(["git", "fetch", "--quiet", "origin"], target, timeout=120)
    sync_local_base_branch(target, base_branch)
    wt = worktree_path(target, run_id)
    if not wt.exists():
        return {"ok": True, "action": "no_worktree"}
    is_ancestor = _run(["git", "merge-base", "--is-ancestor", base_branch, "HEAD"], wt)
    if is_ancestor.returncode == 0:
        return {"ok": True, "action": "already_current"}
    proc = _run(["git", "merge", "--no-edit", base_branch], wt, timeout=60)
    return {"ok": proc.returncode == 0,
            "action": "merged" if proc.returncode == 0 else "merge_conflict",
            "output": (proc.stdout + proc.stderr)[-1000:]}


def ensure_worktree(target: Path, run_id: str, base_branch: str) -> Path:
    """Idempotent: create the run's worktree+branch if missing, else reuse it.
    Returns the worktree path. Raises RuntimeError with git's stderr on failure.
    """
    wt = worktree_path(target, run_id)
    if wt.exists():
        return wt

    wt.parent.mkdir(parents=True, exist_ok=True)
    branch = branch_name(run_id)

    # Make sure base_branch is resolvable (fetch if it's a remote ref not yet local),
    # then fast-forward the LOCAL base_branch ref itself — see
    # sync_local_base_branch's docstring for why this second step is required.
    _run(["git", "fetch", "--quiet", "origin"], target, timeout=120)
    sync_local_base_branch(target, base_branch)

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
                logger.warning("failed to copy untracked file %s into worktree", src, exc_info=True)


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
        logger.warning("best-effort npm install failed for worktree %s", wt, exc_info=True)


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
    # Check STAGED changes, not raw `git status --porcelain` — porcelain also
    # reports untracked-but-intentionally-excluded paths (e.g. .agent-runs
    # itself, just unstaged above) as "?? .agent-runs/", which is non-empty
    # even when nothing is actually staged to commit. Gating on porcelain
    # made `git commit` run with nothing staged whenever a run's only "diff"
    # was .agent-runs noise — commit fails with "nothing added to commit but
    # untracked files present", which ship.py then reports as ship_failed for
    # a run that has no real work left to commit. `git diff --cached
    # --name-only` reflects only what will actually land in the commit.
    staged = _run(["git", "diff", "--cached", "--name-only"], worktree)
    if not staged.stdout.strip():
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


def merge_pr(worktree: Path, remote_branch: str) -> dict:
    """Uses `gh pr merge --squash --delete-branch` on the branch just opened by
    create_pr. Opt-in via [git].auto_merge — meant for solo/local projects with
    no external review gate, where the independent LLM review stage already
    served as the approval step and a human PR click-through would be pure
    friction. NOT appropriate for team repos where a real human should look at
    the diff before it lands on the base branch; that's why this is a separate
    opt-in from auto_ship rather than bundled into it unconditionally."""
    proc = _run(
        ["gh", "pr", "merge", remote_branch, "--squash", "--delete-branch"],
        worktree, timeout=60,
    )
    out = (proc.stdout + proc.stderr).strip()
    return {"ok": proc.returncode == 0, "output": out[-1000:]}
