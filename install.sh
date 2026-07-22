#!/usr/bin/env bash
# Gantry installer — clones (or updates) ~/gantry, sets up its venv, wires
# `gantry` onto PATH, and best-effort installs the CLI tools specific gantry
# commands use (gh, tmux, fzf, glow; docker is checked but not installed).
#
# Fully non-interactive: `curl -fsSL <url> | bash` must work with no stdin.
set -euo pipefail

GANTRY_DIR="${GANTRY_DIR:-$HOME/gantry}"
GANTRY_REPO_URL="${GANTRY_REPO_URL:-https://github.com/vanshaj-ti/gantry.git}"

log() { printf '%s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
err() { printf 'ERROR: %s\n' "$*" >&2; }

# ---------------------------------------------------------------------------
# 1. OS detection
# ---------------------------------------------------------------------------
OS="$(uname -s)"
case "$OS" in
    Darwin) PLATFORM="macos" ;;
    Linux) PLATFORM="linux" ;;
    *)
        err "Windows/other OS not yet supported — see README for manual install"
        exit 1
        ;;
esac

IS_DEBIAN=false
if [ "$PLATFORM" = "linux" ] && (command -v apt-get >/dev/null 2>&1); then
    IS_DEBIAN=true
fi

# ---------------------------------------------------------------------------
# 2. python3 >= 3.11
# ---------------------------------------------------------------------------
check_python() {
    command -v python3 >/dev/null 2>&1 && \
        python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'
}

if ! check_python; then
    err "python3 >= 3.11 is required but was not found (or is too old)."
    if [ "$PLATFORM" = "macos" ]; then
        err "Install it with: brew install python@3.12"
    elif [ "$IS_DEBIAN" = true ]; then
        err "Install it with: sudo apt-get update && sudo apt-get install -y python3.12 python3.12-venv"
    else
        err "Install a modern Python with pyenv: curl https://pyenv.run | bash && pyenv install 3.12 && pyenv global 3.12"
    fi
    exit 1
fi
log "python3 >= 3.11: OK ($(python3 --version))"

# ---------------------------------------------------------------------------
# 3. Clone or update ~/gantry
# ---------------------------------------------------------------------------
is_gantry_repo() {
    [ -f "$1/pyproject.toml" ] && grep -q '^name = "gantry-cli"' "$1/pyproject.toml" 2>/dev/null
}

if [ -d "$GANTRY_DIR" ]; then
    if [ -d "$GANTRY_DIR/.git" ] && is_gantry_repo "$GANTRY_DIR"; then
        log "Found existing gantry checkout at $GANTRY_DIR — updating."
        git -C "$GANTRY_DIR" pull --ff-only
    else
        err "$GANTRY_DIR already exists and is not a gantry git checkout — refusing to overwrite."
        err "Move it aside or set GANTRY_DIR to install elsewhere."
        exit 1
    fi
else
    log "Cloning $GANTRY_REPO_URL to $GANTRY_DIR"
    git clone "$GANTRY_REPO_URL" "$GANTRY_DIR"
fi

# ---------------------------------------------------------------------------
# 4. venv + editable install
# ---------------------------------------------------------------------------
cd "$GANTRY_DIR"
if [ ! -d ".venv" ]; then
    log "Creating venv at $GANTRY_DIR/.venv"
    python3 -m venv .venv
else
    log "venv already exists — reusing it."
fi
"$GANTRY_DIR/.venv/bin/pip" install -q --upgrade pip
"$GANTRY_DIR/.venv/bin/pip" install -q -e .
log "Installed gantry-cli into $GANTRY_DIR/.venv"

# ---------------------------------------------------------------------------
# 5. PATH export (idempotent, marker-guarded)
# ---------------------------------------------------------------------------
MARKER="# gantry PATH (added by install.sh)"
PATH_LINE="export PATH=\"$GANTRY_DIR/.venv/bin:\$PATH\""

case "${SHELL:-}" in
    */zsh) RC_FILE="$HOME/.zshrc" ;;
    */bash) RC_FILE="$HOME/.bashrc" ;;
    *) RC_FILE="$HOME/.profile" ;;
esac

if [ -f "$RC_FILE" ] && grep -qF "$MARKER" "$RC_FILE" 2>/dev/null; then
    log "PATH already configured in $RC_FILE — skipping."
else
    {
        printf '\n%s\n%s\n' "$MARKER" "$PATH_LINE"
    } >> "$RC_FILE"
    log "Added gantry to PATH in $RC_FILE (open a new shell, or run: source $RC_FILE)"
fi
export PATH="$GANTRY_DIR/.venv/bin:$PATH"

# ---------------------------------------------------------------------------
# 6. docker (non-fatal warning only)
# ---------------------------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
    warn "Docker not found — \`gantry setup\`'s containerized mode won't work until installed. See https://docs.docker.com/get-docker/"
else
    log "docker: OK"
fi

# ---------------------------------------------------------------------------
# 7. gh / tmux / fzf / glow — best-effort install
# ---------------------------------------------------------------------------
install_tool_macos() {
    if command -v brew >/dev/null 2>&1; then
        brew install "$1" || warn "brew install $1 failed — install it manually."
    else
        warn "Homebrew not found — install $1 manually: https://formulae.brew.sh/formula/$1"
    fi
}

install_glow_debian() {
    if command -v glow >/dev/null 2>&1; then
        return 0
    fi
    (
        set -e
        sudo mkdir -p /etc/apt/keyrings
        curl -fsSL https://repo.charm.sh/apt/gpg.key | sudo gpg --dearmor -o /etc/apt/keyrings/charm.gpg
        echo "deb [signed-by=/etc/apt/keyrings/charm.gpg] https://repo.charm.sh/apt/ * *" | \
            sudo tee /etc/apt/sources.list.d/charm.list >/dev/null
        sudo apt-get update -y
        sudo apt-get install -y glow
    ) || warn "Automatic glow install failed — see https://github.com/charmbracelet/glow#installation for manual steps."
}

for tool in gh tmux fzf glow; do
    if command -v "$tool" >/dev/null 2>&1; then
        log "$tool: OK"
        continue
    fi
    log "$tool not found — attempting install..."
    if [ "$PLATFORM" = "macos" ]; then
        install_tool_macos "$tool"
    elif [ "$IS_DEBIAN" = true ]; then
        if [ "$tool" = "glow" ]; then
            install_glow_debian
        else
            sudo apt-get update -y && sudo apt-get install -y "$tool" || \
                warn "apt-get install -y $tool failed — install it manually."
        fi
    else
        warn "Don't know how to auto-install $tool on this platform — install it manually."
    fi
done

# ---------------------------------------------------------------------------
# 8. Verify
# ---------------------------------------------------------------------------
log ""
log "== gantry --version =="
"$GANTRY_DIR/.venv/bin/gantry" --version || true
log ""
log "== gantry doctor =="
"$GANTRY_DIR/.venv/bin/gantry" doctor || true

log ""
log "Install complete. Open a new shell (or run: source $RC_FILE) to get \`gantry\` on PATH."
