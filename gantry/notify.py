"""Pluggable notification backends for HITL and status updates.

Decoupled from the old Telegram-only path. Backends:
  - none     : no-op (default; strangers need nothing configured)
  - telegram : direct Bot API using GANTRY_TELEGRAM_BOT_TOKEN / _CHAT_ID env
  - webhook  : POST JSON to a configured URL (Slack/Discord/custom)
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from typing import Any

from .config import NotifyConfig


class Notifier:
    def send(self, text: str, meta: dict[str, Any] | None = None) -> dict[str, Any]:
        raise NotImplementedError


class NoopNotifier(Notifier):
    def send(self, text: str, meta: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"sent": False, "backend": "none"}


class TelegramNotifier(Notifier):
    def send(self, text: str, meta: dict[str, Any] | None = None) -> dict[str, Any]:
        token = os.environ.get("GANTRY_TELEGRAM_BOT_TOKEN")
        chat_id = os.environ.get("GANTRY_TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            return {"sent": False, "backend": "telegram", "error": "missing env GANTRY_TELEGRAM_BOT_TOKEN/GANTRY_TELEGRAM_CHAT_ID"}
        payload = json.dumps({
            "chat_id": chat_id, "text": text,
            "parse_mode": "Markdown",  # notify_message() emits *bold*/`code` for readability
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload, headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = json.loads(resp.read().decode())
            msg = (body.get("result") or {}) if isinstance(body, dict) else {}
            return {"sent": True, "backend": "telegram", "message_id": msg.get("message_id")}
        except Exception as exc:
            return {"sent": False, "backend": "telegram", "error": str(exc)}


class WebhookNotifier(Notifier):
    def __init__(self, url: str):
        self.url = url

    def send(self, text: str, meta: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.url:
            return {"sent": False, "backend": "webhook", "error": "no webhook_url configured"}
        payload = json.dumps({"text": text, "meta": meta or {}}).encode()
        req = urllib.request.Request(self.url, data=payload, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return {"sent": True, "backend": "webhook", "status": resp.status}
        except Exception as exc:
            return {"sent": False, "backend": "webhook", "error": str(exc)}


def get_notifier(cfg: NotifyConfig) -> Notifier:
    if cfg.backend == "telegram":
        return TelegramNotifier()
    if cfg.backend == "webhook":
        return WebhookNotifier(cfg.webhook_url)
    return NoopNotifier()


def fetch_telegram_replies(offset: int | None, timeout: int = 25) -> tuple[list[dict[str, Any]], int | None]:
    """Long-poll Telegram's getUpdates for new messages in the configured chat.

    Returns (messages, next_offset). `offset` is the update_id to resume after —
    None fetches from whatever Telegram currently has buffered. Telegram's
    long-poll `timeout` param holds the HTTP connection open server-side until a
    message arrives or it elapses, so this call blocks up to ~`timeout` seconds
    per invocation — callers loop it, they do not need their own sleep.
    """
    token = os.environ.get("GANTRY_TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("GANTRY_TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return [], offset
    params = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    url = f"https://api.telegram.org/bot{token}/getUpdates?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=timeout + 10) as resp:
            body = json.loads(resp.read().decode())
    except Exception:
        return [], offset
    results = body.get("result", []) if isinstance(body, dict) else []
    messages = []
    next_offset = offset
    for upd in results:
        next_offset = upd["update_id"] + 1
        msg = upd.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != str(chat_id):
            continue  # ignore replies from any chat other than the configured one
        text = msg.get("text")
        if text:
            reply_to = msg.get("reply_to_message") or {}
            messages.append({
                "text": text,
                "update_id": upd["update_id"],
                "date": msg.get("date"),
                "reply_to_message_id": reply_to.get("message_id"),
            })
    return messages, next_offset
