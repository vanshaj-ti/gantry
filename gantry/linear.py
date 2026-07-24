"""Linear ticket intake: webhook receiver + classifier + gantry-run trigger.

Flow: Linear issue created -> webhook POST here -> verify HMAC signature ->
classifier agent reads title/description, picks one tag (feature/bug/hotfix/
research/chore) -> tag applied back to the issue as a Linear label -> a
gantry run is created using that tag's stage list (GantryConfig.stages_for)
-> gantry's own advance/notify loop takes over from there.

Linear is also the ONLY human-input channel for this target (no Telegram):
a human replying on the Linear issue (a Comment webhook event) is how a
human-gated stage gets answered/approved. handle_comment_created resolves
the issue's comment back to its run (via RunStore.run_for_linear_issue) and
posts the reply through gantry/cli/watch.py's existing _handle_reply/
_notify status-transition logic — same gating rules Telegram replies use,
just with a LinearNotifier as the reply channel instead of Telegram.

This module owns Linear-specific I/O only (webhook verify, GraphQL calls).
Everything downstream is plain gantry (Engine.create_run + advance +
cli.watch._handle_reply).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
import urllib.request
from pathlib import Path
from typing import Any

from .config import DOC_STAGES, load_config
from .engine import Engine
from .feedback import FeedbackRoute, reply_prompt, route_for_state, route_feedback
from .notify import LinearNotifier

logger = logging.getLogger(__name__)

LINEAR_API_URL = "https://api.linear.app/graphql"

# One tag per queue (see gantry-pipeline skill / docs/architecture). Each
# tag's stage list comes from gantry.toml's [queues.<tag>] section (falls
# back to the project's default `stages` if absent) — see
# GantryConfig.stages_for.
QUEUE_TAGS = ("feature", "bug", "hotfix", "research", "chore")

# Replay-attack window per Linear's webhook docs: reject anything older than this.
_WEBHOOK_MAX_AGE_SECONDS = 60


class LinearError(RuntimeError):
    pass


def verify_webhook_signature(raw_body: bytes, header_signature: str | None, secret: str) -> bool:
    """HMAC-SHA256 of the raw body, hex-compared against Linear-Signature.
    Must run on the raw bytes, not a re-serialized JSON body — see
    https://linear.app/developers/webhooks#securing-webhooks."""
    if not header_signature:
        return False
    computed = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(computed, header_signature)


def verify_webhook_timestamp(webhook_timestamp_ms: int, now_ms: int | None = None) -> bool:
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    return abs(now_ms - webhook_timestamp_ms) <= _WEBHOOK_MAX_AGE_SECONDS * 1000


def _graphql(query: str, variables: dict[str, Any], api_key: str) -> dict[str, Any]:
    payload = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        LINEAR_API_URL, data=payload,
        headers={"Content-Type": "application/json", "Authorization": api_key},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode())
    if body.get("errors"):
        raise LinearError(str(body["errors"]))
    return body["data"]


def get_or_create_label(team_id: str, name: str, api_key: str) -> str:
    """Return the label id for `name` on this team, creating it if absent.

    Case-insensitive match: Linear enforces case-insensitive uniqueness on
    label names, so an exact-case comparison here can miss an existing
    label (e.g. team default "Bug") and then fail to create a
    differently-cased one ("bug") with a "duplicate label name" error."""
    data = _graphql(
        "query($teamId: String!) { team(id: $teamId) { labels { nodes { id name } } } }",
        {"teamId": team_id}, api_key,
    )
    for lbl in data["team"]["labels"]["nodes"]:
        if lbl["name"].lower() == name.lower():
            return lbl["id"]
    data = _graphql(
        "mutation($teamId: String!, $name: String!) { "
        "issueLabelCreate(input: {teamId: $teamId, name: $name}) { "
        "success issueLabel { id } } }",
        {"teamId": team_id, "name": name}, api_key,
    )
    return data["issueLabelCreate"]["issueLabel"]["id"]


def get_issue_labels(issue_id: str, api_key: str) -> list[dict[str, str]]:
    data = _graphql(
        "query($issueId: String!) { issue(id: $issueId) { labels { nodes { id name } } } }",
        {"issueId": issue_id}, api_key,
    )
    return data["issue"]["labels"]["nodes"]


def tag_issue(issue_id: str, label_id: str, api_key: str) -> None:
    """Add the queue-tag label, preserving whatever labels the issue already
    has (e.g. a human-added priority label before gantry classified it) —
    an unconditional overwrite here would silently drop them."""
    current_ids = [lbl["id"] for lbl in get_issue_labels(issue_id, api_key)]
    _graphql(
        "mutation($issueId: String!, $labelIds: [String!]!) { "
        "issueUpdate(id: $issueId, input: {labelIds: $labelIds}) { success } }",
        {"issueId": issue_id, "labelIds": list(set(current_ids + [label_id]))}, api_key,
    )


_STAGE_LABEL_PREFIX = "stage:"


def set_stage_label(issue_id: str, stage: str, team_id: str, api_key: str) -> None:
    """Swap the issue's stage:<x> label to stage:<stage> — every other
    label (the queue tag, any human-added labels) is preserved; only a
    prior stage:* label is dropped, per the "swap, don't accumulate"
    design (only the current stage should be visible on the issue)."""
    current = get_issue_labels(issue_id, api_key)
    keep_ids = [lbl["id"] for lbl in current if not lbl["name"].startswith(_STAGE_LABEL_PREFIX)]
    new_label_id = get_or_create_label(team_id, f"{_STAGE_LABEL_PREFIX}{stage}", api_key)
    _graphql(
        "mutation($issueId: String!, $labelIds: [String!]!) { "
        "issueUpdate(id: $issueId, input: {labelIds: $labelIds}) { success } }",
        {"issueId": issue_id, "labelIds": keep_ids + [new_label_id]}, api_key,
    )


# Every comment gantry itself posts carries this prefix. Without it, a
# real, catastrophic incident: gantry posts a status comment -> Linear
# delivers that as a Comment webhook event right back to gantry ->
# handle_comment_created treats it as a human reply and resumes the stage
# -> which posts another comment -> infinite loop, dozens of comments per
# second, real API cost, until the container is killed by hand. Confirmed
# live against a real Linear team (see incident notes in linear.py's git
# history) — this is not a hypothetical. A plain readable prefix (not a
# hidden HTML-comment marker) so it also reads naturally as "this is the
# bot talking" to a human scanning the thread.
_GANTRY_COMMENT_PREFIX = "Gantry: "


def feedback_reply_prompt(route: FeedbackRoute) -> str:
    """Linear presentation of the shared route's reply choices."""
    return reply_prompt(route, channel="linear")


def post_comment(issue_id: str, body: str, api_key: str) -> None:
    _graphql(
        "mutation($issueId: String!, $body: String!) { "
        "commentCreate(input: {issueId: $issueId, body: $body}) { success } }",
        {"issueId": issue_id, "body": f"{_GANTRY_COMMENT_PREFIX}{body}"}, api_key,
    )


def upload_file_to_linear(content: bytes, filename: str, content_type: str, api_key: str) -> str:
    """3-step upload per https://linear.app/developers/how-to-upload-a-file-to-linear:
    request a pre-signed URL via fileUpload, PUT the bytes there, return the
    resulting assetUrl for use in an attachmentCreate/commentCreate."""
    data = _graphql(
        "mutation($contentType: String!, $filename: String!, $size: Int!) { "
        "fileUpload(contentType: $contentType, filename: $filename, size: $size) { "
        "success uploadFile { uploadUrl assetUrl headers { key value } } } }",
        {"contentType": content_type, "filename": filename, "size": len(content)}, api_key,
    )
    upload = data["fileUpload"]["uploadFile"]
    headers = {"Content-Type": content_type}
    for h in upload["headers"]:
        headers[h["key"]] = h["value"]
    req = urllib.request.Request(upload["uploadUrl"], data=content, headers=headers, method="PUT")
    with urllib.request.urlopen(req, timeout=60):
        pass
    return upload["assetUrl"]


def post_stage_doc(issue_id: str, stage: str, artifact_path: Path, api_key: str) -> None:
    """Upload a completed doc stage's artifact (investigation-report.md,
    product-spec.md, etc) to Linear's storage and post it as a comment link
    — the human reviewing a *_complete gate should see the actual content
    there, not just a status change.

    NOT attachmentCreate: that mutation is for external-resource link cards
    (GitHub PRs, exception trackers) and renders identically as a generic
    "linked" card regardless of whether the URL is Linear's own storage or
    someone else's — confirmed live, it does not give an inline file
    preview. Embedding the assetUrl in a comment's markdown is what Linear
    actually treats specially for uploaded content (per
    https://linear.app/developers/how-to-upload-a-file-to-linear)."""
    content = artifact_path.read_bytes()
    asset_url = upload_file_to_linear(content, artifact_path.name, "text/markdown", api_key)
    post_comment(
        issue_id,
        f"{stage.capitalize()} stage complete — [{artifact_path.name}]({asset_url})\n\n"
        f"{feedback_reply_prompt(route_feedback(f'{stage}_complete'))}",
        api_key,
    )


# Which gantry run status maps to which Linear workflow category. Checked in
# order against status via prefix/exact match — first match wins.
# review_approved is deliberately NOT "done" — the PR isn't even open yet at
# that point, let alone merged (same reasoning Engine._prereqs_met already
# uses for run dependencies). It stays "in_progress" (still being shipped);
# "done" only fires once status is actually shipped/shipped_manually.
# "blocked" covers every escalation/failure state — review REQUEST_CHANGES/
# Human-gate statuses that should move Linear to Blocked. Retry-pending
# failures (checks_failed / e2e_failed / ordinary *_failed) stay In Progress
# while Gantry auto-retries — only escalations and true HITL pauses block.
_STATUS_TO_CATEGORY: list[tuple[str, str]] = [
    ("shipped", "done"), ("cancelled", "done"),
    ("review_escalated", "blocked"),
    ("checks_escalated", "blocked"), ("checks_high_risk_escalated", "blocked"),
    ("e2e_escalated", "blocked"),
    ("resolve_escalated", "blocked"),
    ("ship_checks_failed", "blocked"),
    ("blocked", "blocked"), ("held", "blocked"),
    *((f"{stage}_complete", "blocked") for stage in DOC_STAGES),
]

# Preferred Linear state names for each category — matched case-insensitively
# as a substring against the team's actual states first (so a team with a
# custom-named state uses it exactly). Falls back to Linear's built-in state
# `type` for teams with no dedicated "Blocked" state of their own.
_CATEGORY_NAME_HINTS = {
    "in_progress": ["in progress"],
    "blocked": ["blocked"],
    "done": ["done"],
}
_CATEGORY_TYPE_FALLBACK = {
    "in_progress": "started", "blocked": "started", "done": "completed",
}

# Statuses that are still self-healing — stay In Progress on Linear.
_AUTO_HEALING_STATUSES = frozenset({
    "checks_failed", "e2e_failed", "review_changes_requested", "ship_failed",
})


def status_to_category(status: str, state: dict | None = None) -> str | None:
    """Map a gantry run status to a Linear workflow category, or None if this
    status shouldn't move the issue (e.g. mid-doc-stage awaiting_* — the
    issue is already "In Progress" from run creation, no need to churn it on
    every intermediate awaiting_/*_running tick).

    Optional ``state`` lets retry-budget exhaustion flip ordinary ``*_failed``
    into Blocked once auto-retries are spent.
    """
    for prefix, category in _STATUS_TO_CATEGORY:
        if status == prefix or status.startswith(prefix):
            return category
    # Agent/pipeline *_complete (plan/build/evidence/…) — not human doc gates.
    # Doc-stage *_complete is already mapped to Blocked above. Without this,
    # build_complete returns None and Linear stays stuck on a prior Blocked
    # (seen live: evidence_running in state.json while Linear still Blocked + stage:build).
    if status.endswith("_complete"):
        return "in_progress"
    if status.startswith("awaiting_") or status.endswith("_running") or status == "review_approved":
        return "in_progress"
    if status in _AUTO_HEALING_STATUSES:
        return "in_progress"
    if status.endswith("_question"):
        # Agent legitimately paused with question.md — needs a human answer.
        return "blocked"
    if status.endswith("_failed"):
        stage = status.removesuffix("_failed")
        if state is not None and _agent_stage_retries_exhausted(state, stage):
            return "blocked"
        # Still within auto-retry budget (or budget unknown) — keep moving.
        return "in_progress"
    return None


def _agent_stage_retries_exhausted(state: dict, stage: str) -> bool:
    """True when ordinary agent-stage auto-retries are spent for this stage."""
    attempts = int(state.get("stage_retry_attempts") or state.get(f"{stage}_retry_count") or 0)
    # stage_retry_max is stamped on the run when config is applied; fall back
    # to treating "has a high retry count" via the count alone when missing.
    max_attempts = state.get("stage_retry_max")
    if max_attempts is None:
        return False
    return attempts >= int(max_attempts)


def get_team_states(team_id: str, api_key: str) -> list[dict[str, str]]:
    data = _graphql(
        "query($teamId: String!) { team(id: $teamId) { states { nodes { id name type } } } }",
        {"teamId": team_id}, api_key,
    )
    return data["team"]["states"]["nodes"]


def resolve_state_id(team_id: str, category: str, api_key: str) -> str | None:
    states = get_team_states(team_id, api_key)
    for hint in _CATEGORY_NAME_HINTS.get(category, []):
        for st in states:
            if hint in st["name"].lower():
                return st["id"]
    fallback_type = _CATEGORY_TYPE_FALLBACK.get(category)
    for st in states:
        if st["type"] == fallback_type:
            return st["id"]
    return None


def set_issue_state(issue_id: str, state_id: str, api_key: str) -> None:
    _graphql(
        "mutation($issueId: String!, $stateId: String!) { "
        "issueUpdate(id: $issueId, input: {stateId: $stateId}) { success } }",
        {"issueId": issue_id, "stateId": state_id}, api_key,
    )


def _maybe_post_stage_doc(run_id: str, store: Any, issue_id: str, status: str, api_key: str) -> None:
    """Attach a completed doc stage's artifact to the Linear issue — once
    per DISTINCT version of that artifact, not once ever per stage name.

    A doc stage can complete more than once for the same run: human sends
    feedback -> resume -> investigation_complete fires again with a
    rewritten report. Deduping on stage name alone (the original design)
    means that second, genuinely different report never gets posted —
    confirmed live: the issue moved to Blocked with no new comment, the
    human had nothing to review the second time around. Dedup key is now
    a content hash, keyed by stage, in `linear_docs_posted` (dict, not
    list) — re-posts whenever the file's actual content changed since the
    last post, not just whenever the stage name repeats."""
    if not (status.endswith("_complete") and status.removesuffix("_complete") in DOC_STAGES):
        return
    stage = status.removesuffix("_complete")
    from .config import STAGE_ARTIFACTS
    artifact_name = STAGE_ARTIFACTS.get(stage)
    if not artifact_name:
        return
    artifact_path = store.artifact_path(run_id, artifact_name)
    if not artifact_path.exists():
        return
    content_hash = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    posted = store.state(run_id).get("linear_docs_posted") or {}
    if posted.get(stage) == content_hash:
        return
    post_stage_doc(issue_id, stage, artifact_path, api_key)
    store.update_state(run_id, linear_docs_posted={**posted, stage: content_hash})


def _simple_failure_detail(run_id: str, store: Any, status: str) -> str:
    """Plain-language failure summary for Linear (no internal path dumps)."""
    from .failure_detail import (
        _checks_failure_detail,
        _e2e_failure_detail,
        _review_findings_detail,
        format_checks_failure_detail,
    )

    if status in ("checks_failed", "checks_escalated", "blocked") and (
        store.state(run_id).get("blocked_on") in (None, "checks", "scope")
        or status.startswith("checks_")
    ):
        checks = store.read_result(run_id, "checks.json")
        if checks:
            detail = format_checks_failure_detail(
                checks, normalize_optional_sections=True,
            )
            # Append a short stderr hint from the first failing command.
            results = ((checks.get("checks") or {}).get("results") or [])
            for row in results:
                if row.get("pass"):
                    continue
                tail = (row.get("stderr_tail") or row.get("stdout_tail") or "").strip()
                if not tail:
                    continue
                # One readable last line / error sentence.
                lines = [ln.strip() for ln in tail.splitlines() if ln.strip()]
                hint = next(
                    (ln for ln in reversed(lines) if "Error" in ln or "error" in ln
                     or "ENOENT" in ln or "failed" in ln.lower()),
                    lines[-1] if lines else "",
                )
                if hint:
                    if len(hint) > 200:
                        hint = hint[:197] + "..."
                    detail = f"{detail}\n\nDetail: {hint}"
                break
            return detail
        return _checks_failure_detail(store, run_id)
    if status in ("e2e_failed", "e2e_escalated"):
        return _e2e_failure_detail(store, run_id)
    if status == "review_escalated":
        review_result = store.read_result(run_id, "review-result.json")
        from .review import is_review_runner_failure
        if is_review_runner_failure(review_result):
            return (
                "Review agent failed to run (transport/runner error), not a "
                "content judgment. Gantry exhausted automatic retries."
            )
        return _review_findings_detail(review_result)
    if status.endswith("_failed") or status.endswith("_question"):
        stage = status.rsplit("_", 1)[0]
        if status.endswith("_question"):
            return (store.read_artifact(run_id, "question.md") or "(empty question)").strip()
        gate = store.read_result(run_id, f"{stage}-gate.json")
        reason = (gate or {}).get("reason", "") if isinstance(gate, dict) else ""
        stderr = (store.read_artifact(run_id, f"logs/{stage}.stderr") or "").strip()
        if reason:
            return reason
        if stderr:
            first = next((ln for ln in stderr.splitlines() if ln.strip()), stderr[:300])
            return first.strip()
        return f"{stage} failed — see gantry logs."
    if status.endswith("_escalated"):
        stage = status.removesuffix("_escalated")
        if stage == "checks_high_risk":
            checks = store.read_result(run_id, "checks.json") or {}
            high_risk = (checks.get("scope") or {}).get("high_risk_files") or []
            if high_risk:
                return "High-risk path(s) touched:\n" + "\n".join(
                    f"  • `{f}`" for f in high_risk[:8]
                )
            return "High-risk path(s) touched — needs human sign-off."
        return f"{stage.replace('_', ' ')} needs a human decision."
    return "See gantry state/logs for detail."


def _is_human_gate_status(status: str, state: dict | None = None) -> bool:
    """True when Linear should ask for human input (Blocked category)."""
    return status_to_category(status, state) == "blocked"


def _maybe_post_stage_failure(run_id: str, store: Any, issue_id: str, status: str, api_key: str) -> None:
    """Post a plain-language stop reason. Auto-healing failures get a short
    progress note; only true human gates ask for input with keyword replies.
    """
    if not (
        status.endswith("_question")
        or status.endswith("_failed")
        or status.endswith("_escalated")
        or status == "blocked"
    ):
        return

    state = store.state(run_id)
    human_gate = _is_human_gate_status(status, state)
    detail = _simple_failure_detail(run_id, store, status)

    if status.endswith("_question"):
        stage = status.removesuffix("_question")
        dedup_field = "linear_questions_posted"
        body = (
            f"Needs your input\n\n"
            f"Question from **{stage}**:\n{detail}\n\n"
            f"Need from you: reply on this issue with your answer."
        )
    elif human_gate:
        stage = (
            status.removesuffix("_failed")
            if status.endswith("_failed")
            else status.removesuffix("_escalated")
            if status.endswith("_escalated")
            else str(state.get("blocked_on") or "run")
        )
        dedup_field = "linear_failures_posted"
        tried = []
        if state.get("checks_retry_count"):
            tried.append(f"{state['checks_retry_count']} check retry(ies)")
        if state.get("resolve_attempt_count"):
            tried.append(f"{state['resolve_attempt_count']} resolve attempt(s)")
        stage_retries = state.get(f"{stage}_retry_count")
        if stage_retries:
            tried.append(f"{stage_retries} {stage} retry(ies)")
        tried_line = f"Tried: {', '.join(tried)}\n" if tried else ""
        review_result = (
            store.read_result(run_id, "review-result.json")
            if status == "review_escalated"
            else None
        )
        route = route_for_state(state, review_result=review_result)
        body = (
            f"Needs your input\n\n"
            f"Failed: {detail}\n"
            f"{tried_line}"
            f"Need from you: choose an action below (or write guidance).\n\n"
            f"{feedback_reply_prompt(route)}"
        )
    else:
        stage = (
            status.removesuffix("_failed")
            if status.endswith("_failed")
            else status.removesuffix("_escalated")
            if status.endswith("_escalated")
            else "run"
        )
        dedup_field = "linear_failures_posted"
        if status in ("checks_failed", "e2e_failed"):
            attempt = int(state.get("checks_retry_count") or 0) + 1
            framing = f"Retrying automatically (attempt {attempt})"
        elif status.endswith("_failed"):
            attempt = int(state.get(f"{stage}_retry_count") or 0) + 1
            framing = f"Retrying **{stage}** automatically (attempt {attempt})"
        elif status == "ship_failed":
            framing = "Ship failed — retrying automatically"
        else:
            framing = f"**{str(stage).replace('_', ' ').capitalize()}** — continuing"
        body = (
            f"{framing}\n\n"
            f"Failed: {detail}\n"
            f"Next: Gantry will retry / resolve without waiting on you."
        )

    kind = "human" if human_gate or status.endswith("_question") else "auto"
    dedup_key = hashlib.sha256(f"{stage}:{detail}:{kind}".encode()).hexdigest()
    posted = state.get(dedup_field) or {}
    if posted.get(stage) == dedup_key:
        return
    post_comment(issue_id, body, api_key)
    store.update_state(run_id, **{dedup_field: {**posted, stage: dedup_key}})


def _maybe_post_run_announcement(run_id: str, store: Any, issue_id: str,
                                  status: str, current_stage: str | None,
                                  api_key: str) -> None:
    """Guarantee the issue thread names the gantry run at least once.

    Intake posts a create comment, but if that GraphQL call fails after the
    run is already recorded, webhook retries are idempotent and never re-post.
    First successful sync then announces the run so humans aren't left with
    only a stage: label and no run id.
    """
    if store.state(run_id).get("linear_run_announced"):
        return
    stage_bit = f" (stage `{current_stage}`)" if current_stage else ""
    post_comment(
        issue_id,
        f"Tracking run `{run_id}` — status `{status}`{stage_bit}.",
        api_key,
    )
    store.update_state(run_id, linear_run_announced=True)


def _stage_progress_message(status: str, run_id: str) -> str | None:
    """Human-readable Linear update for notable status transitions, or None
    to stay quiet (mid-flight awaiting_* ticks, unrecognized statuses)."""
    from .config import DOC_STAGES

    if status.endswith("_running"):
        stage = status.removesuffix("_running")
        return f"Starting **{stage}** stage for run `{run_id}`."
    if status.endswith("_complete"):
        stage = status.removesuffix("_complete")
        # Doc stages already get a richer artifact comment via
        # _maybe_post_stage_doc — don't double-post a bare "complete".
        if stage in DOC_STAGES:
            return None
        return f"**{stage.capitalize()}** stage complete for run `{run_id}`."
    if status == "review_approved":
        return f"**Review** approved for run `{run_id}` — proceeding to ship."
    if status == "review_changes_requested":
        return f"**Review** requested changes for run `{run_id}` — returning to build."
    if status in {"shipped", "shipped_manually"}:
        return f"**Shipped** run `{run_id}`."
    if status == "checks_running":
        return f"Starting **checks** for run `{run_id}`."
    if status == "e2e_running":
        return f"Starting **e2e** for run `{run_id}`."
    return None


def _maybe_post_stage_progress(run_id: str, store: Any, issue_id: str,
                               status: str, api_key: str) -> None:
    """Post a Linear comment on each notable status *transition*.

    Dedup is transition-based (`linear_last_progress_status`): the same
    status on every advance tick must not spam the thread, but re-entering
    e.g. `build_running` after review_changes_requested must post again.
    """
    message = _stage_progress_message(status, run_id)
    if not message:
        return
    last = store.state(run_id).get("linear_last_progress_status")
    if last == status:
        return
    post_comment(issue_id, message, api_key)
    store.update_state(run_id, linear_last_progress_status=status)


def sync_issue_status_if_configured(store: Any, run_id: str) -> dict[str, Any] | None:
    """No-op sync when Linear env is unset; otherwise sync_issue_status.

    Safe to call mid-stage (right after ``*_running`` is stamped) so Linear
    leaves Blocked / advances ``stage:`` labels while a long agent invoke
    is still in flight — not only after ``advance_run`` returns.
    """
    api_key = os.environ.get("GANTRY_LINEAR_API_KEY")
    team_id = os.environ.get("GANTRY_LINEAR_TEAM_ID")
    if not api_key or not team_id:
        return None
    return sync_issue_status(run_id, store, team_id, api_key)


def sync_issue_status(run_id: str, store: Any, team_id: str, api_key: str) -> dict[str, Any]:
    """Keep a run's tracked Linear issue in sync with its gantry state:

    1. Workflow state — In Progress from creation through review_approved
       (still being shipped, PR may not even be open yet), Blocked on any
       escalation/failure, Done once actually shipped.
    2. stage:<stage> label — swapped to match current_stage on every call
       (independent of (1): moving investigation -> plan is still
       "in_progress" category-wise, but the visible stage label must still
       advance so the issue shows where the run actually is right now).
    3. Run announcement + per-stage progress comments — create-run id on
       first sync if missing, then a comment on each start/complete
       transition (see _maybe_post_stage_progress).
    4. Doc-stage artifact attachment — the completed doc gets attached as a
       file the first time its *_complete status is seen (see
       _maybe_post_stage_doc).

    No-op if this run has no tracked Linear issue (e.g. a run created
    outside the Linear intake path)."""
    issue_id = store.linear_issue_for_run(run_id)
    if not issue_id:
        return {"synced": False, "reason": "no linear issue tracked for this run"}

    run_state = store.state(run_id)
    current_stage = run_state.get("current_stage")
    if current_stage:
        set_stage_label(issue_id, current_stage, team_id, api_key)

    status = run_state.get("status", "")
    _maybe_post_run_announcement(run_id, store, issue_id, status, current_stage, api_key)
    _maybe_post_stage_progress(run_id, store, issue_id, status, api_key)
    _maybe_post_stage_doc(run_id, store, issue_id, status, api_key)
    _maybe_post_stage_failure(run_id, store, issue_id, status, api_key)

    category = status_to_category(status, run_state)
    # review_escalated from a dead invoke is not a human gate — advance will
    # auto-retry. Keep Linear In Progress so the ticket doesn't look blocked
    # while Sonnet/transport flaps.
    if status == "review_escalated":
        from .review import is_review_runner_failure
        if is_review_runner_failure(store.read_result(run_id, "review-result.json")):
            category = "in_progress"
    if not category:
        return {"synced": True, "issue_id": issue_id, "stage": current_stage,
                "state_reason": f"status {status!r} has no mapped category"}
    state_id = resolve_state_id(team_id, category, api_key)
    if not state_id:
        return {"synced": True, "issue_id": issue_id, "stage": current_stage,
                "state_reason": f"no Linear state found for category {category!r}"}
    set_issue_state(issue_id, state_id, api_key)
    return {"synced": True, "issue_id": issue_id, "stage": current_stage, "category": category}


def classify_ticket(title: str, description: str, *,
                    runner: str | None = None, model: str = "",
                    project_root: Path | None = None) -> str:
    """Classifier agent: one call, forced to pick exactly one queue tag.

    Deliberately a single cheap agent turn, not a full gantry stage — this
    runs before any run exists, so there's nothing to resume/gate here.

    Uses [agent].runner (and its default model) when project_root/runner are
    given, so a codex-only install doesn't hard-depend on `claude` being on
    PATH. model="" lets the runner pick its own default."""
    from .backends.registry import get_execution_runner
    from .config import GantryConfig
    from .invocation import InvocationRequest, invoke
    from .runners import get_runner

    cfg = load_config(project_root) if project_root is not None else GantryConfig()
    if project_root is None and not runner:
        cfg.profiles["classifier"] = {
            **cfg.profiles.get("classifier", {}),
            "backend": "claude-code",
        }
    if runner or model:
        override = dict(cfg.profiles.get("classifier", {}))
        if runner:
            override["backend"] = runner
        if model:
            override["model"] = model
        cfg.profiles["classifier"] = override
    prompt = f"""Classify this Linear ticket into exactly one tag: feature, bug, hotfix, research, chore.

- feature: new capability or behavior that doesn't exist yet
- bug: existing behavior is wrong, needs root-cause investigation
- hotfix: known, urgent fix — no investigation needed, speed matters
- research: produces a doc/analysis, not code
- chore: mechanical maintenance (deps, config, cleanup) — no product/design decision needed

Title: {title}
Description: {description}

Respond with exactly one word: the tag."""
    def resolve(name: str):
        try:
            return get_runner(name)
        except ValueError:
            return get_execution_runner(name)

    outcome = invoke(InvocationRequest(
        cfg=cfg,
        stage="classifier",
        role="classifier",
        cwd=project_root or Path.cwd(),
        prompt=prompt,
        prepend_profile_preamble=True,
        output_format="json",
        session_name="linear-classifier",
        backend_resolver=resolve,
    ))
    result = outcome.result
    text = (result.raw.get("result") or "").strip().lower() if result.raw else ""
    for tag in QUEUE_TAGS:
        if tag in text:
            return tag
    raise LinearError(f"classifier returned unrecognized tag: {text!r}")


def handle_issue_created(payload: dict[str, Any], team_id: str, linear_api_key: str,
                          project_root: Path) -> dict[str, Any]:
    """Full intake: classify -> tag in Linear -> create the matching gantry run.

    Idempotent per issue_id: Linear can and does redeliver the same webhook
    event (retries, or a genuine duplicate delivery) — confirmed live, two
    `Issue create` events ~40s apart for the same issue produced two
    separate runs before this check existed. RunStore.run_for_linear_issue
    is checked first; a second delivery for an already-handled issue is a
    no-op, not a second classify+run+comment."""
    from .state import RunStore

    issue = payload["data"]
    issue_id = issue["id"]
    title = issue.get("title", "")
    description = issue.get("description") or ""

    existing_run_id = RunStore(project_root).run_for_linear_issue(issue_id)
    if existing_run_id:
        return {"tag": None, "run_id": existing_run_id, "issue_id": issue_id,
                "duplicate": True}

    # Classify with this project's configured agent runner so a codex-only
    # (or cursor-only) install doesn't require `claude` on PATH.
    tag = classify_ticket(title, description, project_root=project_root)
    label_id = get_or_create_label(team_id, tag, linear_api_key)
    tag_issue(issue_id, label_id, linear_api_key)

    # A single gantry.toml drives every queue: cfg.stages_for(tag) resolves
    # this tag's stage list from [queues.<tag>] if present, else the
    # project's default `stages` — see GantryConfig.stages_for.
    cfg = load_config(project_root)
    engine = Engine(project_root, cfg)
    run_id = engine.create_run(
        title=f"[{tag}] {title}", request=description or title, tag=tag,
    )
    engine.store.record_linear_issue(issue_id, run_id)
    post_comment(issue_id, f"Classified as `{tag}`. Gantry run `{run_id}` created.", linear_api_key)
    engine.store.update_state(run_id, linear_run_announced=True)
    return {"tag": tag, "run_id": run_id, "issue_id": issue_id}


def handle_comment_created(payload: dict[str, Any], linear_api_key: str,
                           project_root: Path) -> dict[str, Any]:
    """A human replied on a Linear issue's comment thread. Resolve which run
    this issue belongs to (recorded at create-run time above), and dispatch
    the reply through the exact same deterministic status-driven gating
    logic Telegram replies already use — cli.watch._handle_reply reads the
    run's current status and decides plainly (approve / rewrite / retry /
    answer-a-question) based on string matching, no agent call involved in
    the routing itself; only the stage work an approval triggers (e.g.
    resuming the investigation stage) invokes an agent, same as today.

    Idempotent per comment id: Linear can and does redeliver the same
    Comment webhook event (confirmed live — a single "retry" reply produced
    two "Resuming..." notifications, both hitting run_agent_stage(resume=
    True) on a stage with no stored session, since a timed-out stage never
    got a session_id saved). A redelivered comment id is a no-op, same
    pattern as handle_issue_created's issue_id dedup above."""
    from .cli.watch import _handle_reply
    from .state import RunStore

    comment = payload["data"]
    comment_id = comment.get("id")
    issue_id = comment.get("issueId") or (comment.get("issue") or {}).get("id")
    body = comment.get("body", "")
    if not issue_id:
        raise LinearError("comment payload missing issueId")
    if body.startswith(_GANTRY_COMMENT_PREFIX):
        # gantry's own status comment, delivered back as a webhook event —
        # NOT a human reply. Processing this is what causes the infinite
        # comment loop (see _GANTRY_COMMENT_PREFIX's docstring). Must be
        # checked before anything else.
        return {"handled": False, "reason": "comment authored by gantry itself, ignoring"}

    cfg = load_config(project_root)
    store = RunStore(project_root)
    run_id = store.run_for_linear_issue(issue_id)
    if not run_id:
        # Not a reply to a run gantry created (e.g. a comment on an
        # unrelated issue, or one predating this deployment) — no-op, not
        # an error; every Comment event on the team hits this webhook.
        return {"handled": False, "reason": "no run tracked for this issue"}

    if comment_id:
        processed = store.state(run_id).get("linear_comments_processed") or []
        if comment_id in processed:
            return {"handled": False, "reason": "duplicate comment delivery, already processed"}
        store.update_state(run_id, linear_comments_processed=processed + [comment_id])

    notifier = LinearNotifier(issue_id, linear_api_key)
    _handle_reply(store, cfg, notifier, run_id, body)
    return {"handled": True, "run_id": run_id, "issue_id": issue_id}
