# Register the Linear webhook

Prerequisite: `gantry-linear` container is reachable over public HTTPS. The
container itself serves plain HTTP on the port from `03-deploy.sh` — you
need a TLS-terminating layer in front (Linear requires HTTPS, non-localhost).
Cheapest option: a GCP HTTPS Load Balancer with a managed cert pointed at the
VM's instance group / IP, forwarding to the container's port. Set that up
before this step; the URL you get from it is what goes into the mutation
below.

Once you have that URL:

```graphql
mutation {
  webhookCreate(
    input: {
      url: "https://<your-public-endpoint>/webhook"
      teamId: "<edupaid's Linear team id>"
      resourceTypes: ["Issue", "Comment"]
    }
  ) {
    success
    webhook { id enabled }
  }
}
```

Run it via curl:
```
curl -X POST https://api.linear.app/graphql \
  -H "Content-Type: application/json" \
  -H "Authorization: <your Linear API key>" \
  --data '{"query": "mutation { webhookCreate(input: {url: \"https://<endpoint>/webhook\", teamId: \"<team-id>\", resourceTypes: [\"Issue\", \"Comment\"]}) { success webhook { id enabled } } }"}'
```

`resourceTypes` includes `Comment` now — the Linear-comment reply path
(so a human answering an investigation-stage question in a Linear comment
resumes the run) listens for `Comment` create events too, not just `Issue`
create.

Linear does not return the signing secret in this response — find it on the
webhook's detail page in Linear's settings UI after creation, and confirm it
matches whatever you put into `gantry-linear-webhook-secret` in step 2. If
you set the value in step 2 *before* creating the webhook, you must instead
copy Linear's generated secret and update the GCP secret to match:
```
printf '%s' '<secret from Linear UI>' | gcloud secrets versions add gantry-linear-webhook-secret --data-file=-
```
then restart the container so it picks up the new value:
```
docker exec gantry-linear sh -c 'kill 1'  # or: re-run 03-deploy.sh
```
