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
# ccusage — codex-cli reports token counts but no cost_usd field (ChatGPT-auth
# isn't billed per-token via this CLI); ccusage computes real $ cost from
# local ~/.codex/sessions rollout files + LiteLLM/gateway pricing, see
# gantry/cost.py's _codex_cost_from_ccusage.
RUN npm install -g @openai/codex ccusage@20

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

# gantry's own per-stage discipline, authored in this repo (gantry/skills/)
# — every deployment gets these with no post-install step, matching the
# manual `ln -s ~/gantry/claude-skills/... ~/.claude/skills/...` pattern
# README.md documents for gantry-pipeline itself, just baked at build time.
# Same skill trees are installed for both claude-code (~/.claude/skills) and
# codex-cli (~/.codex/skills) so [agent].runner = "codex-cli" gets the same
# gantry-stage-* discipline the Dockerfile always gave Claude.
COPY --chown=gantry:gantry gantry/skills/ /home/gantry/.claude/skills/
COPY --chown=gantry:gantry gantry/skills/ /home/gantry/.codex/skills/
COPY --chown=gantry:gantry claude-skills/gantry-pipeline/ /home/gantry/.claude/skills/gantry-pipeline/
COPY --chown=gantry:gantry claude-skills/gantry-pipeline/ /home/gantry/.codex/skills/gantry-pipeline/

# Marketplace skills this org's Claude Code sessions already use locally —
# baked in so a headless VM agent gets the same toolset, not a bare CLI.
# Exact slugs/sources confirmed from a live installed_plugins.json /
# known_marketplaces.json, not guessed. Codex picks up third-party skill
# libraries via [skills].installers at `gantry init --with-skills` time
# (npx skills add ... -a codex), not baked here — those are project-opt-in.
RUN claude plugin marketplace add JuliusBrussee/caveman \
    && claude plugin marketplace add mksglu/context-mode \
    && claude plugin marketplace add DietrichGebert/ponytail \
    && claude plugin marketplace add anthropics/claude-plugins-official \
    && claude plugin install caveman@caveman \
    && claude plugin install context-mode@context-mode \
    && claude plugin install ponytail@ponytail \
    && claude plugin install playwright@claude-plugins-official

ENTRYPOINT ["/opt/gantry/docker-entrypoint.sh"]
