#!/usr/bin/env bash
# Create Secret Manager secrets for gantry. Prompts for each real value —
# nothing is hardcoded or logged. Re-running adds a new version to an
# existing secret rather than failing.
#
# Required: GitHub PAT + Linear credentials.
# Optional: Anthropic direct key, and/or gateway base_url+auth_token, and/or
# OpenAI key — store only what your runners need.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?set PROJECT_ID to your GCP project id}"
gcloud config set project "$PROJECT_ID"

set_secret() {
  local name="$1" prompt="$2" optional="${3:-0}"
  if [[ "$optional" == "1" ]]; then
    echo -n "$prompt (Enter to skip): "
  else
    echo -n "$prompt: "
  fi
  read -rs value
  echo
  if [[ -z "$value" ]]; then
    if [[ "$optional" == "1" ]]; then
      echo "  skipped $name"
      return 0
    fi
    echo "  required value empty — aborting" >&2
    exit 1
  fi
  if ! gcloud secrets describe "$name" &>/dev/null; then
    gcloud secrets create "$name" --replication-policy=automatic
  fi
  printf '%s' "$value" | gcloud secrets versions add "$name" --data-file=-
}

set_secret gantry-github-token \
  "GitHub PAT (fine-grained, scoped to the target repo, contents+PRs read/write)"
set_secret gantry-anthropic-api-key \
  "ANTHROPIC_API_KEY (direct Anthropic; skip if you only use a gateway)" 1
set_secret gantry-anthropic-base-url \
  "ANTHROPIC_BASE_URL (optional LLM gateway URL)" 1
set_secret gantry-anthropic-auth-token \
  "ANTHROPIC_AUTH_TOKEN (optional gateway token; pair with base_url)" 1
set_secret gantry-openai-api-key \
  "OPENAI_API_KEY (optional; for codex-cli)" 1
set_secret gantry-linear-api-key \
  "Linear personal API key or OAuth token"
set_secret gantry-linear-team-id \
  "Linear team id for the target project"
set_secret gantry-linear-webhook-secret \
  "Linear webhook signing secret (pick a value now, reuse it when registering the webhook)"

echo "Done. Secrets stored in Secret Manager under project $PROJECT_ID."
