"""
telegram.py — fire-and-forget Telegram messages. A Telegram outage never raises, and a send whose
reply timed out is never retried (see UNCONFIRMED).

Create a bot with @BotFather, put TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env.
For a public channel, add the bot as a channel admin and use the channel's @name or id.
"""
from __future__ import annotations

import asyncio
import http.client
import json
import logging
import os
import urllib.error
import urllib.request

log = logging.getLogger("telegram")


TIMEOUT_S = 20
_urlopen = urllib.request.urlopen  # tests/conftest.py replaces this: tests never reach Telegram

# A sendMessage outcome. "unconfirmed": the request was written but the reply never came (read timeout,
# connection dropped mid-reply). Telegram has almost always posted the message by then (2026-11-07: three
# read timeouts, all delivered), and sendMessage has no idempotency key, so a retry posts it AGAIN: callers
# treat "unconfirmed" as delivered. "failed" is only what certainly never reached Telegram (no token/chat,
# DNS, refused or timed-out connect) or what Telegram answered with an error status.
SENT, FAILED, UNCONFIRMED = "sent", "failed", "unconfirmed"


def send_status(text: str, token: str | None = None, chat_id: str | None = None, opener=None) -> str:
    token = token or os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        log.info("telegram not configured; message: %s", text)
        return FAILED
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage",
                                 data=json.dumps({"chat_id": chat_id, "parse_mode": "HTML", "text": text,
                                                  "disable_web_page_preview": True}).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with (opener or _urlopen)(req, timeout=TIMEOUT_S) as r:
            return SENT if r.status == 200 else FAILED
    except urllib.error.HTTPError as e:  # Telegram answered: it refused the message
        log.warning("telegram refused the message: %r", e)
        return FAILED
    except urllib.error.URLError as e:  # urlopen wraps connect-phase errors: nothing was sent
        log.warning("telegram unreachable: %r", e)
        return FAILED
    except (TimeoutError, OSError, http.client.HTTPException) as e:  # after the request went out
        log.warning("telegram reply not received (message probably delivered, not resent): %r", e)
        return UNCONFIRMED


def send(text: str, token: str | None = None, chat_id: str | None = None) -> bool:
    """True when the message was (or was probably) delivered: an unconfirmed send is not worth repeating."""
    return send_status(text, token, chat_id) != FAILED


async def send_async(text: str, token: str | None = None, chat_id: str | None = None) -> bool:
    return await asyncio.to_thread(send, text, token, chat_id)


async def send_status_async(text: str, token: str | None = None, chat_id: str | None = None) -> str:
    return await asyncio.to_thread(send_status, text, token, chat_id)
