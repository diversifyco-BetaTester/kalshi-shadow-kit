"""
client.py — authenticated, READ-ONLY Kalshi REST client (API v2).

This kit deliberately contains no order-placing code: everything here reads.

AUTHENTICATION
Kalshi does not use a bearer token. Every request carries three headers:
    KALSHI-ACCESS-KEY        your API key ID
    KALSHI-ACCESS-TIMESTAMP  current time in MILLISECONDS, as a string
    KALSHI-ACCESS-SIGNATURE  base64(signature of timestamp + METHOD + PATH)
Signature: RSA-PSS-SHA256 (salt = digest length, MGF1-SHA256) for RSA keys, plain
Ed25519 for Ed25519 keys. Rules behind most 401s:
  * PATH includes "/trade-api/v2" and EXCLUDES the query string.
  * The timestamp is milliseconds, not seconds.
  * Demo keys only work on demo hosts and vice versa.
Your private key never leaves your machine; Kalshi only holds the public key.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from pathlib import Path
from typing import AsyncIterator
from urllib.parse import urlencode

import aiohttp
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from .config import Settings
from .orderbook import OrderBook

log = logging.getLogger("kalshi")


class KalshiAPIError(RuntimeError):
    def __init__(self, status: int, body: str, method: str, path: str):
        super().__init__(f"{method} {path} -> HTTP {status}: {body[:500]}")
        self.status, self.body = status, body


class RequestSigner:
    """Loads the PEM private key once and signs Kalshi requests."""

    def __init__(self, key_id: str, private_key_path: Path | None = None, *, private_key=None):
        if not key_id:
            raise ValueError("KALSHI_API_KEY_ID is empty")
        self.key_id = key_id
        self._key = private_key or serialization.load_pem_private_key(Path(private_key_path).read_bytes(),
                                                                      password=None)

    def message(self, ts: str, method: str, path: str) -> bytes:
        return f"{ts}{method.upper()}{path.split('?', 1)[0]}".encode()

    def headers(self, method: str, path: str, ts: str | None = None) -> dict[str, str]:
        ts = ts or str(int(time.time() * 1000))
        msg = self.message(ts, method, path)
        if isinstance(self._key, rsa.RSAPrivateKey):
            sig = self._key.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                                  salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
        else:  # Ed25519 signs the raw message
            sig = self._key.sign(msg)
        return {"KALSHI-ACCESS-KEY": self.key_id, "KALSHI-ACCESS-TIMESTAMP": ts,
                "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode()}


class RateLimiter:
    """Evenly spaced requests. Kalshi's basic tier allows ~20 reads/s; we stay well under."""

    def __init__(self, per_second: float):
        self.interval = 1.0 / per_second
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if self._next > now:
                await asyncio.sleep(self._next - now)
            self._next = max(now, self._next) + self.interval


class KalshiClient:
    """Use as `async with KalshiClient(load_settings()) as k:`."""

    def __init__(self, settings: Settings, session: aiohttp.ClientSession | None = None, reads_per_s: float = 8):
        self.s = settings
        self.signer = RequestSigner(settings.api_key_id, settings.private_key_path)
        self._session = session
        self._own_session = session is None
        self._rl = RateLimiter(reads_per_s)

    async def __aenter__(self) -> "KalshiClient":
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        return self

    async def __aexit__(self, *exc) -> None:
        if self._own_session and self._session:
            await self._session.close()

    async def get(self, endpoint: str, params: dict | None = None, retries: int = 3) -> dict:
        """Signed GET with retry on 429/5xx and network errors (exponential backoff)."""
        assert self._session is not None, "use `async with KalshiClient(...)`"
        path = f"{self.s.rest_path_prefix}{endpoint}"
        url = f"{self.s.rest_base}{endpoint}"
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        if clean:
            url += "?" + urlencode(clean)
        attempt = 0
        while True:
            attempt += 1
            await self._rl.wait()
            try:
                async with self._session.get(url, headers=self.signer.headers("GET", path)) as r:
                    text = await r.text()
                    if r.status in (429, 500, 502, 503, 504) and attempt <= retries:
                        await asyncio.sleep(min(8.0, 0.5 * 2 ** (attempt - 1)))
                        continue
                    if r.status >= 400:
                        raise KalshiAPIError(r.status, text, "GET", endpoint)
                    return json.loads(text) if text else {}
            except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as e:
                if attempt <= retries:
                    log.warning("GET %s network error %r, retry %d", endpoint, e, attempt)
                    await asyncio.sleep(0.5 * attempt)
                    continue
                raise

    async def paginate(self, endpoint: str, key: str, params: dict | None = None) -> AsyncIterator[dict]:
        """Yield every item of a cursor-paginated list endpoint."""
        cursor = None
        while True:
            d = await self.get(endpoint, {**(params or {}), "limit": 1000, "cursor": cursor})
            items = d.get(key, []) or []
            for it in items:
                yield it
            cursor = d.get("cursor")
            if not cursor or not items:
                return

    # ------------------------------------------------------------------ markets
    async def list_markets(self, series_ticker: str, status: str = "open", limit: int = 100) -> list[dict]:
        d = await self.get("/markets", {"series_ticker": series_ticker, "status": status, "limit": limit})
        return d.get("markets", []) or []

    async def get_market(self, ticker: str) -> dict:
        d = await self.get(f"/markets/{ticker}")
        return d.get("market", d)

    async def get_orderbook(self, ticker: str, depth: int = 100) -> OrderBook:
        return OrderBook.from_api(ticker, await self.get(f"/markets/{ticker}/orderbook", {"depth": depth}))

    async def get_balance(self) -> dict:
        """Read-only account check — confirms your key works."""
        return await self.get("/portfolio/balance")
