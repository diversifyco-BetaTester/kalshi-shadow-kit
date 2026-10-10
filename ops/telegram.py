"""
telegram.py — fire-and-forget Telegram messages. A Telegram outage never raises.

Create a bot with @BotFather, put TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env.
For a public channel, add the bot as a channel admin and use the channel's @name or id.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.error
import urllib.request

log = logging.getLogger("telegram")


def send(text: str, token: str | None = None, chat_id: str | None = None) -> bool:
    token = token or os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        log.info("telegram not configured; message: %s", text)
        return False
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage",
                                 data=json.dumps({"chat_id": chat_id, "parse_mode": "HTML", "text": text,
                                                  "disable_web_page_preview": True}).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status == 200
    except (urllib.error.URLError, TimeoutError) as e:
        log.warning("telegram unreachable: %r", e)
        return False


async def send_async(text: str, token: str | None = None, chat_id: str | None = None) -> bool:
    return await asyncio.to_thread(send, text, token, chat_id)
