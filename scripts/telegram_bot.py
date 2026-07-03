#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import ssl
import urllib.parse
import urllib.request
from typing import Any


class TelegramBotError(RuntimeError):
    pass


def bot_token() -> str | None:
    return os.environ.get("HARNESS_TELEGRAM_BOT_TOKEN")


def chat_id() -> str | None:
    return os.environ.get("HARNESS_TELEGRAM_CHAT_ID")


def is_configured() -> bool:
    return bool(bot_token() and chat_id())


def _api_url(method: str) -> str:
    token = bot_token()
    if not token:
        raise TelegramBotError("HARNESS_TELEGRAM_BOT_TOKEN is not set")
    return f"https://api.telegram.org/bot{token}/{method}"


def _request(method: str, payload: dict[str, Any]) -> dict[str, Any]:
    data = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(_api_url(method), data=data, method="POST")
    context = None
    try:
        import certifi

        context = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        context = None
    with urllib.request.urlopen(req, timeout=60, context=context) as resp:
        body = resp.read().decode()
    parsed = json.loads(body)
    if not parsed.get("ok"):
        raise TelegramBotError(json.dumps(parsed, indent=2))
    return parsed


def send_message(text: str, *, reply_to_message_id: int | None = None) -> dict[str, Any]:
    cid = chat_id()
    if not cid:
        raise TelegramBotError("HARNESS_TELEGRAM_CHAT_ID is not set")
    payload: dict[str, Any] = {
        "chat_id": cid,
        "text": text,
        "disable_web_page_preview": "true",
    }
    if reply_to_message_id is not None:
        payload["reply_to_message_id"] = str(reply_to_message_id)
        payload["allow_sending_without_reply"] = "true"
    return _request("sendMessage", payload)


def get_updates(offset: int | None = None, *, timeout: int = 0) -> list[dict[str, Any]]:
    payload: dict[str, Any] = {
        "timeout": str(timeout),
        "allowed_updates": json.dumps(["message"]),
    }
    if offset is not None:
        payload["offset"] = str(offset)
    return list(_request("getUpdates", payload).get("result") or [])
