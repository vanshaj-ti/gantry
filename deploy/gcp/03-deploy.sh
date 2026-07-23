#!/usr/bin/env bash
# Run this ON THE VM (after SSHing in via IAP tunnel), not from your laptop.
# Clones edupaid, builds the gantry image, fetches secrets, starts both
# containers. Safe to re-run: recreates containers, does not re-clone if
# /opt/edupaid already exists (pulls instead).
set -euo pipefail

EDUPAID_REPO_URL="${EDUPAID_REPO_URL:?set EDUPAID_REPO_URL, e.g. https://github.com/<org>/edupaid.git}"
GANTRY_REPO_URL="${GANTRY_REPO_URL:-https://github.com/<org>/gantry.git}"
TARGET_DIR="/opt/edupaid"
GANTRY_SRC_DIR="/opt/gantry-src"
LINEAR_PORT="${LINEAR_PORT:-8080}"

echo "This will clone/pull $EDUPAID_REPO_URL into $TARGET_DIR and build gantry:latest."
read -p "Proceed? [y/N] " confirm
[[ "$confirm" == "y" ]] || exit 1

# --- fetch secrets from Secret Manager into a 0600 env file ---
SECRETS_FILE="/run/gantry-secrets.env"
{
  echo "GH_TOKEN=$(gcloud secrets versions access latest --secret=gantry-github-token)"
  # This org routes Claude Code through TrueFoundry's gateway (see Maat) —
  # ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN, not a raw ANTHROPIC_API_KEY.
  echo "ANTHROPIC_BASE_URL=$(gcloud secrets versions access latest --secret=gantry-anthropic-base-url)"
  echo "ANTHROPIC_AUTH_TOKEN=$(gcloud secrets versions access latest --secret=gantry-anthropic-auth-token)"
  echo "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1"
  echo "GANTRY_LINEAR_API_KEY=$(gcloud secrets versions access latest --secret=gantry-linear-api-key)"
  echo "GANTRY_LINEAR_TEAM_ID=$(gcloud secrets versions access latest --secret=gantry-linear-team-id)"
  echo "GANTRY_LINEAR_WEBHOOK_SECRET=$(gcloud secrets versions access latest --secret=gantry-linear-webhook-secret)"
} > "$SECRETS_FILE"
chmod 600 "$SECRETS_FILE"

GH_TOKEN="$(grep ^GH_TOKEN= "$SECRETS_FILE" | cut -d= -f2-)"

# --- persistent edupaid clone ---
if [[ -d "$TARGET_DIR/.git" ]]; then
  git -C "$TARGET_DIR" fetch origin
  git -C "$TARGET_DIR" checkout staging
  git -C "$TARGET_DIR" pull origin staging
else
  AUTH_URL="$(echo "$EDUPAID_REPO_URL" | sed "s#https://#https://x-access-token:${GH_TOKEN}@#")"
  git clone "$AUTH_URL" "$TARGET_DIR"
  git -C "$TARGET_DIR" checkout staging
fi
# The container runs as its own unprivileged "gantry" user (uid 1001, see
# Dockerfile) — a clone/checkout done here (as root, since this script runs
# under sudo) leaves the worktree root-owned, and the container then can't
# write .agent-runs/ into the bind-mounted target. Match ownership so
# create_run's mkdir succeeds.
chown -R 1001:1001 "$TARGET_DIR"

# --- gantry source + image ---
if [[ -d "$GANTRY_SRC_DIR/.git" ]]; then
  git -C "$GANTRY_SRC_DIR" pull
else
  git clone "$GANTRY_REPO_URL" "$GANTRY_SRC_DIR"
fi
docker build -t gantry:latest "$GANTRY_SRC_DIR"

# --- start both containers ---
docker rm -f gantry-advance gantry-linear 2>/dev/null || true

docker run -d --name gantry-advance --restart unless-stopped \
  -v "${TARGET_DIR}:${TARGET_DIR}" \
  --env-file "$SECRETS_FILE" \
  -e GANTRY_TARGET="$TARGET_DIR" \
  -e GANTRY_TICK_INTERVAL=60 \
  gantry:latest

docker run -d --name gantry-linear --restart unless-stopped \
  -v "${TARGET_DIR}:${TARGET_DIR}" \
  -p "${LINEAR_PORT}:${LINEAR_PORT}" \
  --env-file "$SECRETS_FILE" \
  -e GANTRY_TARGET="$TARGET_DIR" \
  --entrypoint gantry gantry:latest linear-serve --port "$LINEAR_PORT"

echo "Both containers started. Check: docker ps"
echo "Logs: docker logs -f gantry-advance   /   docker logs -f gantry-linear"
