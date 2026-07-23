#!/usr/bin/env bash
# Run this ON THE VM (after SSHing in via IAP tunnel), not from your laptop.
# Clones the target project, builds the gantry image, fetches secrets, starts
# both containers. Safe to re-run: recreates containers; pulls if the target
# clone already exists.
set -euo pipefail

# Prefer TARGET_REPO_URL; accept legacy EDUPAID_REPO_URL as an alias.
if [[ -z "${TARGET_REPO_URL:-}" && -n "${EDUPAID_REPO_URL:-}" ]]; then
  TARGET_REPO_URL="$EDUPAID_REPO_URL"
fi
TARGET_REPO_URL="${TARGET_REPO_URL:?set TARGET_REPO_URL (git HTTPS URL of the project to build)}"
GANTRY_REPO_URL="${GANTRY_REPO_URL:-https://github.com/<org>/gantry.git}"
TARGET_NAME="${TARGET_NAME:-$(basename "${TARGET_REPO_URL}" .git)}"
TARGET_DIR="${TARGET_DIR:-/opt/${TARGET_NAME}}"
BASE_BRANCH="${BASE_BRANCH:-staging}"
GANTRY_SRC_DIR="${GANTRY_SRC_DIR:-/opt/gantry-src}"
LINEAR_PORT="${LINEAR_PORT:-8080}"

echo "This will clone/pull $TARGET_REPO_URL into $TARGET_DIR (branch $BASE_BRANCH) and build gantry:latest."
if [[ -t 0 && "${GANTRY_DEPLOY_YES:-}" != "1" ]]; then
  read -p "Proceed? [y/N] " confirm
  [[ "$confirm" == "y" ]] || exit 1
else
  echo "Non-interactive (or GANTRY_DEPLOY_YES=1) — proceeding."
fi

# --- fetch secrets from Secret Manager into a 0600 env file ---
# Optional secrets (gateway URL / auth token) are included only when present
# so a plain Anthropic ANTHROPIC_API_KEY deploy still works.
#
# Use `versions access` directly (not `secrets describe`): the VM SA is
# typically granted roles/secretmanager.secretAccessor, which can read
# versions but cannot describe secrets. describe-first treated every secret
# as missing even when access would succeed.
SECRETS_FILE="/run/gantry-secrets.env"
: > "$SECRETS_FILE"
chmod 600 "$SECRETS_FILE"

PROJECT_ID="${PROJECT_ID:-$(curl -sf -H "Metadata-Flavor: Google" \
  http://metadata.google.internal/computeMetadata/v1/project/project-id 2>/dev/null || true)}"
GCLOUD_PROJ_ARGS=()
if [[ -n "${PROJECT_ID:-}" ]]; then
  GCLOUD_PROJ_ARGS=(--project="$PROJECT_ID")
  gcloud config set project "$PROJECT_ID" >/dev/null 2>&1 || true
fi

append_secret() {
  local env_name="$1" secret_name="$2" required="${3:-0}"
  local value
  if value="$(gcloud secrets versions access latest --secret="$secret_name" \
      "${GCLOUD_PROJ_ARGS[@]}" 2>/dev/null)" && [[ -n "$value" ]]; then
    # Avoid echoing secrets into the deploy log; only write the env file.
    printf '%s=%s\n' "$env_name" "$value" >> "$SECRETS_FILE"
  elif [[ "$required" == "1" ]]; then
    echo "missing required secret: $secret_name (project=${PROJECT_ID:-unset})" >&2
    echo "hint: grant the VM SA roles/secretmanager.secretAccessor on this secret" >&2
    exit 1
  fi
}

append_secret GH_TOKEN gantry-github-token 1
append_secret ANTHROPIC_API_KEY gantry-anthropic-api-key 0
append_secret ANTHROPIC_BASE_URL gantry-anthropic-base-url 0
append_secret ANTHROPIC_AUTH_TOKEN gantry-anthropic-auth-token 0
append_secret OPENAI_API_KEY gantry-openai-api-key 0
append_secret GANTRY_LINEAR_API_KEY gantry-linear-api-key 1
append_secret GANTRY_LINEAR_TEAM_ID gantry-linear-team-id 1
append_secret GANTRY_LINEAR_WEBHOOK_SECRET gantry-linear-webhook-secret 1
append_secret CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS gantry-claude-code-disable-experimental-betas 0

GH_TOKEN="$(grep ^GH_TOKEN= "$SECRETS_FILE" | cut -d= -f2-)"

# This script runs under sudo (root), but the previous run's chown (below)
# leaves the clone owned by uid 1001 — git's dubious-ownership check then
# refuses to touch it as root on every subsequent redeploy. Idempotent.
git config --global --add safe.directory "$TARGET_DIR"

# --- persistent target clone ---
if [[ -d "$TARGET_DIR/.git" ]]; then
  git -C "$TARGET_DIR" fetch origin
  git -C "$TARGET_DIR" checkout "$BASE_BRANCH"
  git -C "$TARGET_DIR" pull origin "$BASE_BRANCH"
else
  AUTH_URL="$(echo "$TARGET_REPO_URL" | sed "s#https://#https://x-access-token:${GH_TOKEN}@#")"
  git clone "$AUTH_URL" "$TARGET_DIR"
  git -C "$TARGET_DIR" checkout "$BASE_BRANCH"
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
# Prune dangling image layers from prior rebuilds before building — small
# boot disks fill up across repeated deploys otherwise.
docker container prune -f --filter "until=1h" 2>&1 || true
docker image prune -a -f --filter "until=1h" 2>&1 || true

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
echo "Target: $TARGET_DIR (from $TARGET_REPO_URL @ $BASE_BRANCH)"
echo "Logs: docker logs -f gantry-advance   /   docker logs -f gantry-linear"
