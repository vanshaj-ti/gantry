#!/usr/bin/env bash
# Create the GCE VM, firewall rule, and service account for gantry.
# Idempotent: re-running skips resources that already exist.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-aristotle-436708}"
ZONE="${ZONE:-us-central1-a}"
REGION="${REGION:-us-central1}"
VM_NAME="${VM_NAME:-gantry-vm}"
SA_NAME="${SA_NAME:-gantry-runner}"
LINEAR_PORT="${LINEAR_PORT:-8080}"
VPC_NETWORK="${VPC_NETWORK:-default}"  # override to edupaid's existing VPC if it has one

echo "Project:  $PROJECT_ID"
echo "Zone:     $ZONE"
echo "VM name:  $VM_NAME"
echo "Network:  $VPC_NETWORK"
read -p "Proceed? [y/N] " confirm
[[ "$confirm" == "y" ]] || exit 1

gcloud config set project "$PROJECT_ID"

# Service account: least privilege needed is Secret Manager accessor.
if ! gcloud iam service-accounts describe "${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com" &>/dev/null; then
  gcloud iam service-accounts create "$SA_NAME" \
    --display-name="gantry runner (VM identity for Secret Manager access)"
fi
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor" \
  --condition=None

# Firewall: only the linear-serve port needs public inbound, and only from
# wherever you put the TLS-terminating proxy (a load balancer IP, or your
# own IP if testing directly) — NOT 0.0.0.0/0. Edit --source-ranges below
# before running; a placeholder is intentionally restrictive (deny-all) so
# this script can't silently open the VM to the internet.
if ! gcloud compute firewall-rules describe gantry-allow-linear-webhook &>/dev/null; then
  gcloud compute firewall-rules create gantry-allow-linear-webhook \
    --network="$VPC_NETWORK" \
    --direction=INGRESS \
    --action=ALLOW \
    --rules="tcp:${LINEAR_PORT}" \
    --source-ranges="127.0.0.1/32" \
    --target-tags=gantry
  echo "NOTE: firewall rule created with source-ranges=127.0.0.1/32 (effectively closed)."
  echo "Edit it once you have your TLS-terminating proxy's real IP:"
  echo "  gcloud compute firewall-rules update gantry-allow-linear-webhook --source-ranges=<real-ip>/32"
fi

# VM: Debian + docker.io, no public IP beyond what's needed for the webhook
# port (SSH access is via IAP tunnel only — no external SSH firewall rule
# exists here on purpose).
if ! gcloud compute instances describe "$VM_NAME" --zone="$ZONE" &>/dev/null; then
  gcloud compute instances create "$VM_NAME" \
    --zone="$ZONE" \
    --machine-type=e2-standard-4 \
    --image-family=debian-12 \
    --image-project=debian-cloud \
    --tags=gantry \
    --service-account="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com" \
    --scopes=cloud-platform \
    --metadata=startup-script='#!/bin/bash
      apt-get update
      apt-get install -y docker.io git
      systemctl enable --now docker'
fi

echo "VM created. SSH in via: gcloud compute ssh $VM_NAME --zone=$ZONE --tunnel-through-iap"
