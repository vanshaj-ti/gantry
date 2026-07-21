# gantry container image — one instance per target project (see gantry/docker.py).
# Runs gantry's daemon-tick loop against a bind-mounted project directory,
# isolated from whatever else is running on the host (no shared claude-code
# daemon, no shared machine resources with an interactive session).
FROM node:22-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv git curl ca-certificates gnupg \
    && rm -rf /var/lib/apt/lists/*

# gh CLI (official apt repo)
RUN mkdir -p -m 755 /etc/apt/keyrings \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# codex CLI — npm global (matches host's @openai/codex install)
RUN npm install -g @openai/codex

# gantry itself
COPY . /opt/gantry
RUN pip3 install --break-system-packages --no-cache-dir /opt/gantry

# claude refuses --dangerously-skip-permissions as root ("cannot be used with
# root/sudo privileges for security reasons") — every build/evidence/resolve
# invocation was hitting this and exiting immediately. Run as a real
# unprivileged user instead; give it access to the bind-mounted workspace
# and read-only auth mounts (root:root owned, world-readable is fine since
# they're ro).
RUN useradd -m -s /bin/sh gantry
USER gantry
ENV HOME=/home/gantry
RUN curl -fsSL https://claude.ai/install.sh | bash
ENV PATH="/home/gantry/.local/bin:${PATH}"

ENTRYPOINT ["/opt/gantry/docker-entrypoint.sh"]
