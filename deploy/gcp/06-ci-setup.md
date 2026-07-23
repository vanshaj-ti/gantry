# One-time: wire GitHub Actions to auto-deploy via Workload Identity Federation

Already run for this repo (project `aristotle-436708`) — documented here so
it's reproducible, not because it needs running again.

```bash
PROJECT_ID=aristotle-436708
PROJECT_NUMBER=1065787674750
REPO=vanshaj-ti/gantry

gcloud services enable iamcredentials.googleapis.com sts.googleapis.com \
  --project=$PROJECT_ID

gcloud iam workload-identity-pools create gantry-ci-pool \
  --project=$PROJECT_ID --location=global \
  --display-name="Gantry CI (GitHub Actions)"

gcloud iam workload-identity-pools providers create-oidc gantry-ci-github \
  --project=$PROJECT_ID --location=global \
  --workload-identity-pool=gantry-ci-pool \
  --display-name="GitHub Actions" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref" \
  --attribute-condition="assertion.repository=='$REPO'" \
  --issuer-uri="https://token.actions.githubusercontent.com"

gcloud iam service-accounts create gantry-ci-deployer \
  --project=$PROJECT_ID --display-name="Gantry CI deployer (GitHub Actions)"

# Just enough to open an IAP tunnel and SSH in as this identity — no
# Secret Manager access needed here, the VM's own gantry-runner service
# account (already bound) fetches secrets locally inside 03-deploy.sh.
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:gantry-ci-deployer@$PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/iap.tunnelResourceAccessor" --condition=None
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:gantry-ci-deployer@$PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/compute.viewer" --condition=None
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:gantry-ci-deployer@$PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/compute.osAdminLogin" --condition=None

# Let this specific GitHub repo's OIDC identity impersonate the deployer SA.
gcloud iam service-accounts add-iam-policy-binding \
  gantry-ci-deployer@$PROJECT_ID.iam.gserviceaccount.com \
  --project=$PROJECT_ID \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/gantry-ci-pool/attribute.repository/$REPO"

# gcloud compute ssh (used by the deploy workflow) needs OS Login enabled
# on the target VM.
gcloud compute instances add-metadata gantry-vm --zone=us-central1-a \
  --project=$PROJECT_ID --metadata enable-oslogin=TRUE
```

## GitHub repo configuration (one-time, via repo Settings → Secrets and variables → Actions → Variables)

Not secrets — these are non-sensitive identifiers, safe as plain repo
variables (`vars.*`), not `secrets.*`:

- `GCP_WIF_PROVIDER` = `projects/1065787674750/locations/global/workloadIdentityPools/gantry-ci-pool/providers/gantry-ci-github`
- `GCP_CI_SERVICE_ACCOUNT` = `gantry-ci-deployer@aristotle-436708.iam.gserviceaccount.com`

No JSON key, no long-lived credential stored anywhere in GitHub — WIF
exchanges GitHub's own OIDC token for short-lived GCP credentials at
workflow-run time.

## What this buys

Every push to `main` that passes `ci.yml` (tests + lint) auto-triggers
`.github/workflows/deploy.yml`, which SSHes into `gantry-vm` via IAP tunnel
and re-runs `deploy/gcp/03-deploy.sh` (the same script already cloned onto
the VM at `/opt/gantry-src/deploy/gcp/03-deploy.sh` by that script's own
prior run) — auto-prunes stale Docker layers, rebuilds the image, recreates
both containers. Manual `03-deploy.sh` invocation (per `README.md`'s
existing instructions) remains available as a fallback/debugging path.
