"""Run gantry for a target project inside its own Docker container, isolated
from the host machine's process tree and any other active session (see the
Dockerfile's header comment for why this exists — subprocess-death
symptoms observed running gantry directly on the host alongside an
interactive Claude Code session).

One container per target, named `gantry-<slug>` so multiple projects don't
collide. Docker's own --restart policy replaces the host daemon's launchd/
systemd layer; the container's entrypoint (docker-entrypoint.sh) loops
`gantry advance --all` against the bind-mounted target forever.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

IMAGE_NAME = "gantry:latest"
GANTRY_REPO_ROOT = Path(__file__).resolve().parent.parent


def _slug_for(target: Path) -> str:
    return re.sub(r"[^a-z0-9]+", "-", target.resolve().name.lower()).strip("-") or "target"


def _container_name(target: Path) -> str:
    return f"gantry-{_slug_for(target)}"


def build_image() -> dict:
    proc = subprocess.run(
        ["docker", "build", "-t", IMAGE_NAME, str(GANTRY_REPO_ROOT)],
        capture_output=True, text=True,
    )
    return {"ok": proc.returncode == 0, "output": (proc.stdout + proc.stderr)[-4000:]}


def _mount_args() -> list[str]:
    home = Path.home()
    mounts = []
    # Read-only auth/config mounts for gh/git — identity is static enough to
    # bind-mount safely. claude's (~/.claude) and codex's (~/.codex) own
    # dirs are each a named, writable volume instead (see
    # _claude_auth_volume/_codex_auth_volume) — both write their own
    # session/cache state at runtime, and the host's own ~/.codex or
    # ~/.claude shouldn't be shared live with a container's independent
    # process anyway.
    for host_path, container_path in [
        (home / ".config" / "gh", "/home/gantry/.config/gh"),
        (home / ".gitconfig", "/home/gantry/.gitconfig"),
    ]:
        if host_path.exists():
            mounts += ["-v", f"{host_path}:{container_path}:ro"]
    return mounts


def _claude_auth_volume(target: Path) -> str:
    """A named volume (not a bind mount) for /home/gantry/.claude — survives
    `docker rm`+recreate (e.g. a fresh `gantry docker up` after an image
    rebuild), so an interactive `docker exec -it <name> claude` -> /login
    only has to be done once per target, not after every container
    recreation. Login state lives here instead of the ephemeral container
    filesystem."""
    return f"gantry-claude-auth-{_slug_for(target)}"


def _codex_auth_volume(target: Path) -> str:
    """Named volume for codex's ~/.codex — needs to be writable (codex
    writes cache/session files there even when auth itself comes from an
    env var, see _codex_env_args)."""
    return f"gantry-codex-auth-{_slug_for(target)}"


def _codex_env_args() -> list[str]:
    """This org's ~/.codex/config.toml sets `env_key = "TFY_API_KEY"` under
    its custom model_provider — codex reads that env var directly as its
    auth, bypassing the normal OAuth `codex login` flow entirely. Good,
    because that flow doesn't work from a container anyway: it runs a
    localhost:1455 callback server that (a) binds loopback-only inside the
    container regardless of published ports, and (b) this org's OpenAI
    workspace has --device-auth (the no-callback alternative) disabled.
    Passing the env var through is simpler and actually works, unlike
    fighting the OAuth callback."""
    import os
    val = os.environ.get("TFY_API_KEY")
    return ["-e", f"TFY_API_KEY={val}"] if val else []


def up(target: Path, interval_seconds: int = 60) -> dict:
    target = target.resolve()
    name = _container_name(target)
    # Idempotent: remove any existing container with this name first (covers
    # both "already running" and "stopped/crashed, needs a fresh start").
    # The named auth volume is deliberately NOT removed here — recreating
    # the container shouldn't force re-login.
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    cmd = [
        "docker", "run", "-d", "--name", name, "--restart", "unless-stopped",
        "-v", f"{_claude_auth_volume(target)}:/home/gantry/.claude",
        "-v", f"{_codex_auth_volume(target)}:/home/gantry/.codex",
        *_codex_env_args(),
        # Mounted at the SAME absolute path as on the host, not a fixed
        # /workspace — pre-existing worktrees under target/.worktrees/gantry/
        # have `.git` gitlink files pointing at an absolute host path
        # (git worktree metadata isn't remapped through a bind mount), so a
        # container-side path mismatch breaks every worktree gantry already
        # created (observed: "fatal: not a git repository"). New worktrees
        # created from inside the container would face the same problem in
        # reverse if mounted elsewhere, so same-path is the only mount shape
        # that works both directions.
        "-v", f"{target}:{target}",
        *_mount_args(),
        "-e", f"GANTRY_TARGET={target}",
        "-e", f"GANTRY_TICK_INTERVAL={interval_seconds}",
        IMAGE_NAME,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        return {"ok": False, "error": (proc.stdout + proc.stderr).strip(), "name": name}
    container_id = proc.stdout.strip()

    # Docker creates a brand-new named volume owned by root — the gantry
    # user can't write into it until chowned. A pre-existing volume (i.e.
    # every `up()` after the first for this target) is already gantry-owned,
    # so this is a harmless no-op then.
    subprocess.run(["docker", "exec", "-u", "root", name, "chown", "-R", "gantry:gantry",
                   "/home/gantry/.claude", "/home/gantry/.codex"], capture_output=True)

    # ~/.claude.json carries claude-code's own login state (oauthAccount,
    # userID) and ~/.claude/settings.json carries this org's custom gateway
    # config (ANTHROPIC_BASE_URL + bearer token) — env vars alone weren't
    # sufficient ("Not logged in" even with the gateway ANTHROPIC_* vars
    # set). Both are copied in as one-time snapshots rather than bind-
    # mounted: ~/.claude.json is live/frequently-rewritten (a bind mount
    # risks the container reading it mid-write off the host, observed as
    # "Unterminated string" JSON corruption), and settings.json needs to
    # land inside the same volume-backed ~/.claude dir as the persisted
    # login state (see _auth_volume) rather than as a separate bind mount
    # nested under it. Skipped entirely if the auth volume already has a
    # completed interactive `/login` from a prior `docker exec -it ...
    # claude` session — copying over it would just overwrite it with the
    # same (or stale) host snapshot harmlessly, but there's no need to.
    # codex's config.toml (model_provider, base_url, env_key mapping) is
    # the same static-config story as claude's settings.json above — copy
    # once rather than bind-mount, so it lands inside the same volume-backed
    # ~/.codex dir as codex's own runtime state.
    for host_rel, container_path in [
        (Path.home() / ".claude.json", "/home/gantry/.claude.json"),
        (Path.home() / ".claude" / "settings.json", "/home/gantry/.claude/settings.json"),
        (Path.home() / ".codex" / "config.toml", "/home/gantry/.codex/config.toml"),
    ]:
        if host_rel.exists():
            subprocess.run(["docker", "cp", str(host_rel), f"{name}:{container_path}"],
                           capture_output=True)
            subprocess.run(["docker", "exec", "-u", "root", name, "chown", "gantry:gantry",
                           container_path], capture_output=True)

    return {"ok": True, "name": name, "container_id": container_id}


def down(target: Path) -> dict:
    name = _container_name(target.resolve())
    proc = subprocess.run(["docker", "rm", "-f", name], capture_output=True, text=True)
    if proc.returncode != 0:
        return {"ok": False, "error": (proc.stdout + proc.stderr).strip(), "name": name}
    return {"ok": True, "removed": name}


def status(target: Path | None = None) -> dict:
    proc = subprocess.run(
        ["docker", "ps", "-a", "--filter", "name=gantry-", "--format",
         "{{.Names}}\t{{.Status}}\t{{.ID}}"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return {"ok": False, "error": (proc.stdout + proc.stderr).strip()}
    containers = []
    for line in proc.stdout.strip().splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            containers.append({"name": parts[0], "status": parts[1], "id": parts[2]})
    if target is not None:
        name = _container_name(target.resolve())
        containers = [c for c in containers if c["name"] == name]
    return {"ok": True, "containers": containers}
