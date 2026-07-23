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

from .config import load_config
from .engine import Engine
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


def tag_issue(issue_id: str, label_id: str, api_key: str) -> None:
    _graphql(
        "mutation($issueId: String!, $labelIds: [String!]!) { "
        "issueUpdate(id: $issueId, input: {labelIds: $labelIds}) { success } }",
        {"issueId": issue_id, "labelIds": [label_id]}, api_key,
    )


def post_comment(issue_id: str, body: str, api_key: str) -> None:
    _graphql(
        "mutation($issueId: String!, $body: String!) { "
        "commentCreate(input: {issueId: $issueId, body: $body}) { success } }",
        {"issueId": issue_id, "body": body}, api_key,
    )


def classify_ticket(title: str, description: str) -> str:
    """Classifier agent: one call, forced to pick exactly one queue tag.

    Deliberately a single cheap agent turn, not a full gantry stage — this
    runs before any run exists, so there's nothing to resume/gate here."""
    from .runners import get_runner

    prompt = f"""Classify this Linear ticket into exactly one tag: feature, bug, hotfix, research, chore.

- feature: new capability or behavior that doesn't exist yet
- bug: existing behavior is wrong, needs root-cause investigation
- hotfix: known, urgent fix — no investigation needed, speed matters
- research: produces a doc/analysis, not code
- chore: mechanical maintenance (deps, config, cleanup) — no product/design decision needed

Title: {title}
Description: {description}

Respond with exactly one word: the tag."""
    result = get_runner("claude-code").run(
        cwd=Path.cwd(), prompt=prompt, model="claude-haiku-4-5", max_turns=1,
    )
    text = (result.raw.get("result") or "").strip().lower() if result.raw else ""
    for tag in QUEUE_TAGS:
        if tag in text:
            return tag
    raise LinearError(f"classifier returned unrecognized tag: {text!r}")


def handle_issue_created(payload: dict[str, Any], team_id: str, linear_api_key: str,
                          project_root: Path) -> dict[str, Any]:
    """Full intake: classify -> tag in Linear -> create the matching gantry run."""
    issue = payload["data"]
    issue_id = issue["id"]
    title = issue.get("title", "")
    description = issue.get("description") or ""

    tag = classify_ticket(title, description)
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
    resuming the investigation stage) invokes an agent, same as today."""
    from .cli.watch import _handle_reply
    from .state import RunStore

    comment = payload["data"]
    issue_id = comment.get("issueId") or (comment.get("issue") or {}).get("id")
    body = comment.get("body", "")
    if not issue_id:
        raise LinearError("comment payload missing issueId")

    cfg = load_config(project_root)
    store = RunStore(project_root)
    run_id = store.run_for_linear_issue(issue_id)
    if not run_id:
        # Not a reply to a run gantry created (e.g. a comment on an
        # unrelated issue, or one predating this deployment) — no-op, not
        # an error; every Comment event on the team hits this webhook.
        return {"handled": False, "reason": "no run tracked for this issue"}

    notifier = LinearNotifier(issue_id, linear_api_key)
    _handle_reply(store, cfg, notifier, run_id, body)
    return {"handled": True, "run_id": run_id, "issue_id": issue_id}
