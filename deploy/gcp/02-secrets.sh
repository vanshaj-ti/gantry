#!/usr/bin/env bash
# Create Secret Manager secrets for gantry. Prompts for each real value —
# nothing is hardcoded or logged. Re-running adds a new version to an
# existing secret rather than failing.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-aristotle-436708}"
gcloud config set project "$PROJECT_ID"

set_secret() {
  local name="$1" prompt="$2"
  echo -n "$prompt: "
  read -rs value
  echo
  if ! gcloud secrets describe "$name" &>/dev/null; then
    gcloud secrets create "$name" --replication-policy=automatic
  fi
  printf '%s' "$value" | gcloud secrets versions add "$name" --data-file=-
}

set_secret gantry-github-token \
  "GitHub PAT (fine-grained, scoped to edupaid repo, contents+PRs read/write)"
set_secret gantry-anthropic-api-key \
  "Anthropic API key"
set_secret gantry-linear-api-key \
  "Linear personal API key or OAuth token"
set_secret gantry-linear-team-id \
  "Linear team id (edupaid's team)"
set_secret gantry-linear-webhook-secret \
  "Linear webhook signing secret (pick a value now, reuse it in step 4 when registering the webhook)"

echo "Done. Secrets stored in Secret Manager under project $PROJECT_ID."
