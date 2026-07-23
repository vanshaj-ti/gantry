# Verify the deployment end-to-end

1. `docker ps` — both `gantry-advance` and `gantry-linear` show `Up`, not
   restarting in a loop.
2. `docker logs gantry-linear` — no startup errors (missing env vars would
   show here immediately).
3. Create a real test issue in the target project's Linear team, title
   obviously bug-shaped (e.g. "Login button does nothing on iOS Safari").
4. Within ~1 tick interval, confirm:
   - A `bug` label appears on the issue.
   - A comment appears naming the created gantry run id.
5. SSH in (`gcloud compute ssh gantry-vm --zone=<zone> --tunnel-through-iap`)
   and check status:
   ```
   docker exec gantry-advance gantry status --run <id>
   ```
   Expect `awaiting_investigation` (or the first stage of your bug queue).
6. If the investigation agent asks a clarifying question, it now posts as a
   Linear comment on the issue (via the reply path — see
   `gantry/linear.py`'s `handle_comment_created`). Reply directly on the
   Linear issue; confirm the run resumes and reaches
   `investigation_complete`.
7. If `auto_approve_docs` is not set in the target's `gantry.toml`, approve
   via a Linear comment reply (e.g. "approve") on the ticket, or fall back
   to:
   ```
   docker exec gantry-advance gantry approve --run <id> --stage investigation
   ```
8. Confirm the run proceeds through plan/build/checks/evidence/review to
   `review_approved` -> `shipped`, and a real PR appears against the
   target's base branch (see `[git].base_branch` / deploy `BASE_BRANCH`).
9. Manually merge that PR on GitHub if `auto_merge = false` — confirms the
   human-merge gate holds as designed.
