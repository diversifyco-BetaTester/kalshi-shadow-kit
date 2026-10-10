"""
orderbook.py — normalized Kalshi order book.

Kalshi books hold only BIDS on each side. A YES ask at p is a NO bid at 1 - p, so
"how much is resting at the 1c YES ask" = size of NO bids at 99c.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class OrderBook:
    """Prices in dollars, quantities in contracts; bids sorted best (highest) first."""
    ticker: str
    yes_bids: list[tuple[float, float]] = field(default_factory=list)
    no_bids: list[tuple[float, float]] = field(default_factory=list)

    @property
    def best_yes_bid(self) -> float | None:
        return self.yes_bids[0][0] if self.yes_bids else None

    @property
    def best_no_bid(self) -> float | None:
        return self.no_bids[0][0] if self.no_bids else None

    @property
    def best_yes_ask(self) -> float | None:
        b = self.best_no_bid
        return round(1.0 - b, 4) if b is not None else None

    @property
    def best_no_ask(self) -> float | None:
        b = self.best_yes_bid
        return round(1.0 - b, 4) if b is not None else None

    def asks(self, side: str) -> list[tuple[float, float]]:
        """Ask ladder for buying `side` ('yes'|'no'), cheapest first."""
        opposite = self.no_bids if side == "yes" else self.yes_bids
        return [(round(1.0 - p, 4), q) for p, q in opposite]

    def size_at_ask(self, side: str, price: float) -> float:
        """Contracts resting at exactly `price` on the `side` ask (the FIFO queue you would join)."""
        return sum(q for p, q in self.asks(side) if abs(p - price) < 1e-6)

    def depth_at_or_better(self, side: str, limit_price: float) -> float:
        """Contracts available to BUY `side` at prices <= limit_price."""
        return sum(q for p, q in self.asks(side) if p <= limit_price + 1e-9)

    @classmethod
    def from_api(cls, ticker: str, payload: dict) -> "OrderBook":
        """Parse GET /markets/{ticker}/orderbook. Kalshi has shipped two shapes; accept both:
          new:    {"orderbook_fp": {"yes_dollars": [["0.70","50.00"], ...], "no_dollars": [...]}}
          legacy: {"orderbook":    {"yes": [[70, 50], ...], "no": [...]}}   (cents, ints)
        """
        def norm(levels, in_cents: bool) -> list[tuple[float, float]]:
            out = []
            for lvl in levels or []:
                price, qty = float(lvl[0]), float(lvl[1])
                if in_cents:
                    price /= 100.0
                if qty > 0 and 0 < price < 1:
                    out.append((round(price, 4), qty))
            return sorted(out, key=lambda x: x[0], reverse=True)

        fp = payload.get("orderbook_fp")
        if fp:
            return cls(ticker, norm(fp.get("yes_dollars"), False), norm(fp.get("no_dollars"), False))
        ob = payload.get("orderbook") or {}
        if "yes_dollars" in ob or "no_dollars" in ob:
            return cls(ticker, norm(ob.get("yes_dollars"), False), norm(ob.get("no_dollars"), False))
        return cls(ticker, norm(ob.get("yes"), True), norm(ob.get("no"), True))
