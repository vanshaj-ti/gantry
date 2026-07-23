# One-time: wire GitHub Actions to auto-deploy via Workload Identity Federation

Fill in your GCP project and GitHub repo, then run once. Values below are
placeholders — nothing project-specific is baked into the workflow.

```bash
PROJECT_ID=<your-gcp-project-id>
PROJECT_NUMBER=<your-gcp-project-number>
REPO=<github-org-or-user>/gantry   # the gantry source repo, not the target app
ZONE=${ZONE:-us-central1-a}
VM_NAME=${VM_NAME:-gantry-vm}

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

# gcloud compute ssh requires the caller to act-as the target VM's own
# attached service account — gantry-vm was created with a custom SA
# (gantry-runner@...), NOT the project's default compute SA, so this binds
# to that specific one. Confirm the actual attached SA first if this ever
# needs redoing: gcloud compute instances describe $VM_NAME --zone=$ZONE \
#   --format="value(serviceAccounts[].email)"
gcloud iam service-accounts add-iam-policy-binding \
  gantry-runner@$PROJECT_ID.iam.gserviceaccount.com \
  --project=$PROJECT_ID \
  --member="serviceAccount:gantry-ci-deployer@$PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/iam.serviceAccountUser"

# Let this specific GitHub repo's OIDC identity impersonate the deployer SA.
gcloud iam service-accounts add-iam-policy-binding \
  gantry-ci-deployer@$PROJECT_ID.iam.gserviceaccount.com \
  --project=$PROJECT_ID \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/gantry-ci-pool/attribute.repository/$REPO"

# gcloud compute ssh (used by the deploy workflow) needs OS Login enabled
# on the target VM.
gcloud compute instances add-metadata $VM_NAME --zone=$ZONE \
  --project=$PROJECT_ID --metadata enable-oslogin=TRUE
```

## GitHub repo configuration (one-time, via repo Settings → Secrets and variables → Actions → Variables)

Not secrets — these are non-sensitive identifiers, safe as plain repo
variables (`vars.*`), not `secrets.*`:

- `GCP_WIF_PROVIDER` = `projects/<PROJECT_NUMBER>/locations/global/workloadIdentityPools/gantry-ci-pool/providers/gantry-ci-github`
- `GCP_CI_SERVICE_ACCOUNT` = `gantry-ci-deployer@<PROJECT_ID>.iam.gserviceaccount.com`
- `GANTRY_TARGET_REPO_URL` = HTTPS git URL of the **app** repo gantry builds
- `GANTRY_REPO_URL` = HTTPS git URL of this gantry source repo
- `GANTRY_BASE_BRANCH` (optional, default `staging`) = branch checked out on the VM
- `GCP_ZONE` / `GCP_VM_NAME` (optional) = override deploy SSH target

No JSON key, no long-lived credential stored anywhere in GitHub — WIF
exchanges GitHub's own OIDC token for short-lived GCP credentials at
workflow-run time.

## What this buys

Every green `main` push re-runs `03-deploy.sh` on the VM with the target and
gantry URLs from repo variables — no project name lives in the workflow YAML.
