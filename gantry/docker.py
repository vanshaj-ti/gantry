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
import tempfile
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
    # Read-only auth mount for gh — identity is static enough to bind-mount
    # safely. ~/.gitconfig is NOT bind-mounted here (see _write_container_gitconfig):
    # it needs host-path sanitization first, so it's copied in instead.
    # claude's (~/.claude) and codex's (~/.codex) own dirs are each a named,
    # writable volume instead (see _claude_auth_volume/_codex_auth_volume) —
    # both write their own session/cache state at runtime, and the host's own
    # ~/.codex or ~/.claude shouldn't be shared live with a container's
    # independent process anyway.
    host_path = home / ".config" / "gh"
    if host_path.exists():
        mounts += ["-v", f"{host_path}:/home/gantry/.config/gh:ro"]
    return mounts


_GH_HELPER_LINE_RE = re.compile(r"^(\s*helper\s*=\s*!).*/gh(\s+auth\s+git-credential.*)$")


def _write_container_gitconfig(name: str) -> None:
    """Copy host ~/.gitconfig into the container, rewritten for container use.

    Bind-mounting ~/.gitconfig directly (the old approach) broke `gh` auth for
    git push/pull when the host config points the github.com credential helper
    at an absolute host path (e.g. `!/opt/homebrew/bin/gh auth git-credential`),
    which doesn't exist in the container (gh lives at /usr/bin/gh there,
    installed by the Dockerfile) — every push failed with
    "gh: not found" / "could not read Username for 'https://github.com'".
    Rewriting to a bare `!gh auth git-credential` resolves via $PATH in
    either environment. Also drops `credential.helper = osxkeychain`
    (macOS-only, meaningless inside a Linux container) and any
    `includeIf "gitdir:...` pointing at a host-only path.
    """
    host_gitconfig = Path.home() / ".gitconfig"
    if not host_gitconfig.exists():
        return
    lines = []
    skip_next_helper_blank = False
    for line in host_gitconfig.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("[includeIf"):
            skip_next_helper_blank = True
            continue
        if skip_next_helper_blank:
            if stripped.startswith("path") or stripped == "":
                continue
            skip_next_helper_blank = False
        if stripped == "helper = osxkeychain":
            continue
        m = _GH_HELPER_LINE_RE.match(line)
        if m:
            line = f"{m.group(1)}gh{m.group(2)}"
        lines.append(line)
    sanitized = "\n".join(lines) + "\n"
    tmp = Path(tempfile.gettempdir()) / f"{name}-gitconfig"
    tmp.write_text(sanitized)
    subprocess.run(["docker", "cp", str(tmp), f"{name}:/home/gantry/.gitconfig"],
                   capture_output=True)
    subprocess.run(["docker", "exec", "-u", "root", name, "chown", "gantry:gantry",
                   "/home/gantry/.gitconfig"], capture_output=True)
    tmp.unlink(missing_ok=True)


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
    env var passed through _pass_env_args)."""
    return f"gantry-codex-auth-{_slug_for(target)}"


# Host env vars forwarded into the container when set. Covers common auth for
# gh, Claude Code, Codex/OpenAI, and optional org gateways. Override the full
# list with GANTRY_DOCKER_PASS_ENV=comma,separated,NAMES (empty entries ignored).
# Codex OAuth (localhost callback) does not work inside containers — pass the
# API key / gateway env your ~/.codex/config.toml's model_provider.env_key names.
_DEFAULT_DOCKER_PASS_ENV = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "OPENAI_API_KEY",
    "CODEX_API_KEY",
    "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS",
)


def _pass_env_names() -> list[str]:
    import os
    override = os.environ.get("GANTRY_DOCKER_PASS_ENV")
    if override is not None:
        return [n.strip() for n in override.split(",") if n.strip()]
    return list(_DEFAULT_DOCKER_PASS_ENV)


def _pass_env_args() -> list[str]:
    """Forward selected host env vars into the container (only if set)."""
    import os
    args: list[str] = []
    for name in _pass_env_names():
        val = os.environ.get(name)
        if val:
            args += ["-e", f"{name}={val}"]
    return args


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
        *_pass_env_args(),
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

    # Snapshot host runner auth/config into the container's named volumes
    # (not bind-mounted — host files are live/rewritten and can corrupt
    # mid-read). ~/.claude.json = claude-code login state; settings.json =
    # optional gateway/env overrides; ~/.codex/config.toml = codex
    # model_provider/base_url/env_key. Env vars from _pass_env_args cover
    # token auth when present; these files cover interactive-login state
    # and static provider config the CLIs also read from disk.
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

    _write_container_gitconfig(name)

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
