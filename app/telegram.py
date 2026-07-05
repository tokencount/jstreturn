"""Telegram bot push (notifications only — no bot logic).

Uses plain HTTPS POST to api.telegram.org. No bot framework needed.
"""
from __future__ import annotations

import os
from typing import Optional

import httpx


_BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
_API = "https://api.telegram.org/bot{token}/{method}"


async def send_message(chat_id: str | int, text: str, parse_mode: str = "HTML") -> bool:
    if not _BOT_TOKEN:
        return False
    url = _API.format(token=_BOT_TOKEN, method="sendMessage")
    payload = {
        "chat_id": str(chat_id),
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, json=payload)
        return r.status_code == 200
    except Exception:
        return False


async def send_to_admins(text: str) -> bool:
    admin_id = os.environ.get("ADMIN_CHAT_ID", "")
    if not admin_id:
        return False
    return await send_message(admin_id, text)