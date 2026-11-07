"""A Telegram reply that times out after the request went out is UNCONFIRMED, never retried
(2026-11-07: one 5-pick arrived 4 times: each read timeout was logged as failed and its legs released)."""
import asyncio
import http.client
import io
import json
import urllib.error

from ops import telegram
from local import grade_slips, sentinel


def opener(exc=None, status=200):
    def open_(req, timeout):
        if exc:
            raise exc
        class R(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False
        r = R(b"{}"); r.status = status
        return r
    return open_


def st(**kw):
    return telegram.send_status("hi", token="t", chat_id="c", opener=opener(**kw))


def test_read_timeout_and_dropped_reply_are_unconfirmed_not_failed():
    assert st(exc=TimeoutError("The read operation timed out")) == "unconfirmed"
    assert st(exc=http.client.RemoteDisconnected("gone")) == "unconfirmed"
    assert st(exc=ConnectionResetError()) == "unconfirmed"


def test_only_certain_non_delivery_is_failed(monkeypatch):
    assert st() == "sent"
    assert st(exc=urllib.error.URLError(TimeoutError("timed out"))) == "failed"     # connect timeout
    assert st(exc=urllib.error.URLError(OSError("nodename nor servname"))) == "failed"  # DNS
    assert st(exc=urllib.error.HTTPError("u", 429, "Too Many Requests", {}, None)) == "failed"
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert telegram.send_status("hi", token="", chat_id="") == "failed"   # not configured


def test_conftest_blocks_real_telegram_sends():
    import pytest
    with pytest.raises(RuntimeError, match="Telegram"):
        telegram._urlopen(None, timeout=1)


def test_bool_send_counts_unconfirmed_as_delivered(monkeypatch):
    monkeypatch.setattr(telegram, "send_status", lambda *a, **k: "unconfirmed")
    assert telegram.send("hi") is True
    monkeypatch.setattr(telegram, "send_status", lambda *a, **k: "failed")
    assert telegram.send("hi") is False


def test_sentinel_delivery_status():
    assert sentinel.delivery("unconfirmed") == "unconfirmed"
    assert sentinel.delivery("failed") == "failed" and sentinel.delivery("sent") == "sent"
    assert sentinel.delivery(True) == "sent" and sentinel.delivery(False) == "failed"
