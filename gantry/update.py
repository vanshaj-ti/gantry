"""`gantry update`: pull the latest gantry source and reinstall.

Gantry is installed via `pip install -e .` (editable) from a git clone — there
is no PyPI package yet. Updating today means manually `cd`-ing into that
clone, `git pull`, and re-running `pip install -e .`. This wraps those two
steps into one command runnable from any directory.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def gantry_repo_root() -> Path | None:
    """Locate the gantry source checkout this install actually runs from,
    via the package's own file location — works regardless of where the
    user's shell cwd is when they run `gantry update`."""
    import gantry
    pkg_dir = Path(gantry.__file__).resolve().parent
    root = pkg_dir.parent
    if (root / "pyproject.toml").exists() and (root / "gantry").is_dir():
        return root
    return None


def _run(cmd: list[str], cwd: Path) -> dict:
    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=300)
    return {"ok": proc.returncode == 0, "cmd": " ".join(cmd),
            "output": (proc.stdout + proc.stderr).strip()}


def update_gantry() -> dict:
    root = gantry_repo_root()
    if root is None:
        return {"ok": False, "error": "could not locate the gantry git checkout for this "
                "install — was it installed editable via `pip install -e .` from a clone?"}

    git_ok = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"],
                            cwd=str(root), capture_output=True, text=True).returncode == 0
    if not git_ok:
        return {"ok": False, "error": f"{root} is not a git checkout — can't `git pull`"}

    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=str(root),
                           capture_output=True, text=True).stdout.strip()
    if dirty:
        return {"ok": False, "error": f"{root} has uncommitted local changes — "
                "commit, stash, or discard them before updating.",
                "dirty_files": dirty.splitlines()}

    before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(root),
                            capture_output=True, text=True).stdout.strip()

    pull = _run(["git", "pull", "--ff-only"], root)
    if not pull["ok"]:
        return {"ok": False, "stage": "git pull", **pull}

    after = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(root),
                           capture_output=True, text=True).stdout.strip()

    if before == after:
        return {"ok": True, "repo": str(root), "updated": False,
                "commit": after, "note": "already up to date"}

    install = _run([sys.executable, "-m", "pip", "install", "-e", "."], root)
    if not install["ok"]:
        return {"ok": False, "stage": "pip install -e .", "repo": str(root),
                "from_commit": before, "to_commit": after, **install}

    return {"ok": True, "repo": str(root), "updated": True,
            "from_commit": before, "to_commit": after}
