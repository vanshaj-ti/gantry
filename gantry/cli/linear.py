"""`gantry linear-serve`: minimal HTTP endpoint for Linear's Issue + Comment
webhooks.

Stdlib-only (http.server) — no new dependency for what's a couple of POST
handlers. Not meant to survive a restart mid-request; Linear retries failed
deliveries (1min/1hr/6hr backoff) so a crash just means a retry, not a lost
event. See gantry/linear.py for the actual verify/classify/create-run and
reply-dispatch logic — this file is purely HTTP plumbing, no decision logic
of its own beyond routing by (type, action).
"""
from __future__ import annotations

import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..linear import (
    LinearError,
    handle_comment_created,
    handle_issue_created,
    verify_webhook_signature,
    verify_webhook_timestamp,
)
from ._shared import _target

logger = logging.getLogger(__name__)


def _make_handler(secret: str, team_id: str, api_key: str, project_root: Path):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # route through logging, not stderr
            logger.info("%s - %s", self.address_string(), fmt % args)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            raw_body = self.rfile.read(length)

            sig = self.headers.get("Linear-Signature")
            if not verify_webhook_signature(raw_body, sig, secret):
                self.send_response(401)
                self.end_headers()
                return

            try:
                payload = json.loads(raw_body.decode())
            except Exception:
                self.send_response(400)
                self.end_headers()
                return

            if not verify_webhook_timestamp(payload.get("webhookTimestamp", 0)):
                self.send_response(401)
                self.end_headers()
                return

            resource_type = payload.get("type")
            action = payload.get("action")

            try:
                if resource_type == "Issue" and action == "create":
                    result = handle_issue_created(payload, team_id, api_key, project_root)
                    logger.info("classified+created run: %s", result)
                elif resource_type == "Comment" and action == "create":
                    result = handle_comment_created(payload, api_key, project_root)
                    logger.info("comment reply dispatched: %s", result)
                # Every other (type, action) — issue updates, label changes,
                # reactions, etc — is a no-op 200: Linear's webhook can be
                # scoped to fewer resourceTypes at registration time, but
                # this handler stays defensive regardless of what arrives.
            except LinearError as exc:
                logger.error("linear intake failed: %s", exc)
                self.send_response(500)
                self.end_headers()
                return

            self.send_response(200)
            self.end_headers()

    return Handler


def cmd_linear_serve(args) -> int:
    secret = os.environ.get("GANTRY_LINEAR_WEBHOOK_SECRET")
    api_key = os.environ.get("GANTRY_LINEAR_API_KEY")
    team_id = os.environ.get("GANTRY_LINEAR_TEAM_ID")
    if not secret or not api_key or not team_id:
        print("Missing env: GANTRY_LINEAR_WEBHOOK_SECRET, GANTRY_LINEAR_API_KEY, GANTRY_LINEAR_TEAM_ID all required")
        return 1

    project_root = _target()
    handler = _make_handler(secret, team_id, api_key, project_root)
    server = ThreadingHTTPServer(("0.0.0.0", args.port), handler)
    print(f"gantry linear-serve listening on :{args.port}, target={project_root}")
    server.serve_forever()
    return 0
