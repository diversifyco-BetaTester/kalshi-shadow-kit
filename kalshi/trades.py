"""
trades.py — public trade prints, normalized.

Kalshi keeps recent trades on /markets/trades and moves older ones behind a cutoff
(/historical/cutoff) to /historical/trades; `fetch_trades` tries both.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .client import KalshiAPIError, KalshiClient


@dataclass(frozen=True)
class Trade:
    trade_id: str
    ticker: str
    ts: float              # unix seconds
    yes_price: float       # dollars
    count: float           # contracts
    taker_side: str        # "yes" = taker bought YES (hit a YES ask), "no" = taker bought NO

    @classmethod
    def from_api(cls, t: dict) -> "Trade":
        return cls(trade_id=t["trade_id"], ticker=t["ticker"],
                   ts=datetime.fromisoformat(t["created_time"].replace("Z", "+00:00")).timestamp(),
                   yes_price=float(t["yes_price_dollars"]), count=float(t["count_fp"]),
                   taker_side=t["taker_side"])


async def fetch_trades(k: KalshiClient, ticker: str, min_ts: float | None = None,
                       max_ts: float | None = None) -> list[Trade]:
    """All trades on `ticker` in [min_ts, max_ts], oldest first."""
    params = {"ticker": ticker, "min_ts": None if min_ts is None else int(min_ts),
              "max_ts": None if max_ts is None else int(max_ts)}
    for endpoint in ("/markets/trades", "/historical/trades"):
        try:
            out = [Trade.from_api(t) async for t in k.paginate(endpoint, "trades", params)]
        except KalshiAPIError:
            continue
        if out:
            return sorted(out, key=lambda t: t.ts)
    return []
