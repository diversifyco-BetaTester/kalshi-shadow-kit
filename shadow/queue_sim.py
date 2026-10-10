"""
queue_sim.py — would a resting order have filled? Price-time (FIFO) queue replay.

You "place" a hypothetical resting ask at time T, at the BACK of the queue at that price.
Every later trade is replayed through the queue:
    taker buys at our price      -> consumes the queue ahead of us first, then fills us
    taker buys ABOVE our price   -> our level must have been empty, so we were hit first
Two estimates of the queue ahead, because the API shows level size, not order order:
    conservative  only trades shrink it (nobody ahead ever cancels)
    realistic     also capped by the level's current size at each book snapshot — orders ahead
                  that cancel disappear from the level; orders that joined behind us only make
                  the cap looser, never wrong.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class QueuePosition:
    size: float               # our hypothetical order size
    price: float              # our resting ask price (dollars, on the side takers buy)
    ahead: float              # contracts ahead of us at placement
    filled: float = 0.0
    fill_ts: float | None = None   # time we became completely filled

    @property
    def done(self) -> bool:
        return self.filled >= self.size

    def on_trade(self, price: float, count: float, ts: float | None = None) -> float:
        """Replay one taker trade that BOUGHT at `price`. Returns contracts we filled from it."""
        if self.done or price < self.price - 1e-9:
            return 0.0
        left = self.size - self.filled
        if price > self.price + 1e-9:
            self.ahead = 0.0
            got = min(left, count)
        else:
            consumed = min(self.ahead, count)
            self.ahead -= consumed
            got = min(left, count - consumed)
        self.filled += got
        if self.done and self.fill_ts is None:
            self.fill_ts = ts
        return got

    def on_book(self, level_size: float) -> None:
        """Realistic estimate only: what is still ahead of us cannot exceed what is resting."""
        self.ahead = min(self.ahead, level_size)
