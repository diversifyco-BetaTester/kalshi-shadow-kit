"""
quote.py — fair value for Kalshi's 15-minute crypto windows, and where a maker would rest.

FAIR VALUE (same model as the live bot's pricing.py)
  Settlement X = average of the 60 RTI prints before close. With S = spot, K = strike,
  σ = per-second vol, τ = seconds to close, W = 60:
      E[X] = S,   Var[X] = (σS)²·(τ − W + W/3) + basis²       (τ >= W; we never quote inside W)
  P(YES) = P(X >= K) under a unit-variance Student-t(4): fat tails, so it is not over-confident.

QUOTES (pure function, no orders)
  improve=False (default): rest at fair ± edge but never better than the touch, so we join the
      back of the best level or sit behind it. Jumping to the front is where informed flow hits.
  improve=True:  bid = min(fair − edge, best YES bid + 1c), ask = max(fair + edge, best YES ask − 1c)
  Either way a quote is strictly non-crossing, and with improve=False an empty side of the book
  (where any quote would be first in line) is not quoted.
  Quotes stay in [MIN_PRICE, MAX_PRICE]: outside it Kalshi ticks in 0.1c, and a 5c/95c
  contract is where fills are most one-sided. The position cap lives in shadow.py, which
  knows what is still resting.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass

SETTLEMENT_WINDOW_S = 60.0
VOL_FLOOR = 0.3e-4  # per-second σ floor (~0.3 bps/s), as in the live bot
MIN_PRICE, MAX_PRICE = 0.10, 0.90
TICK = 0.01


def student_t4_cdf(x: float) -> float:
    u = x * x / 4.0
    return 0.5 + 0.375 * (x / math.sqrt(1.0 + u)) * (1.0 - u / (3.0 * (1.0 + u)))


def realized_vol(prices: list[float], floor: float = VOL_FLOOR) -> float:
    """Std-dev of 1-second log returns from 1 Hz samples, floored."""
    rets = [math.log(b / a) for a, b in zip(prices, prices[1:]) if a > 0 and b > 0]
    return max(statistics.pstdev(rets), floor) if len(rets) >= 20 else floor


def p_yes(spot: float, strike: float, sigma_s: float, tau_s: float, basis_bps: float = 2.0) -> float:
    """P(60 s average settlement >= strike), for τ >= 60 s."""
    W = SETTLEMENT_WINDOW_S
    var = (sigma_s * spot) ** 2 * (max(tau_s, W) - W + W / 3.0) + (basis_bps * 1e-4 * spot) ** 2
    z = (spot - strike) / math.sqrt(var)
    return min(max(student_t4_cdf(z * math.sqrt(2.0)), 0.0), 1.0)


@dataclass(frozen=True)
class Quotes:
    bid: float | None  # YES bid we would rest, dollars
    ask: float | None  # YES ask we would rest (= NO bid at 1 − ask)


def _floor(x: float) -> float:
    return round(math.floor(x / TICK + 1e-9) * TICK, 2)


def _ceil(x: float) -> float:
    return round(math.ceil(x / TICK - 1e-9) * TICK, 2)


def make_quotes(fair: float, best_bid: float | None, best_ask: float | None, edge: float,
                improve: bool = False) -> Quotes:
    """Non-crossing maker quotes around `fair`."""
    step = TICK if improve else 0.0
    bid = _floor(fair - edge)
    if best_bid is not None:
        bid = min(bid, round(best_bid + step, 2))
    if best_ask is not None and bid >= best_ask - 1e-9:
        bid = round(best_ask - TICK, 2)
    ask = _ceil(fair + edge)
    if best_ask is not None:
        ask = max(ask, round(best_ask - step, 2))
    if best_bid is not None and ask <= best_bid + 1e-9:
        ask = round(best_bid + TICK, 2)
    ok_bid = (improve or best_bid is not None) and MIN_PRICE <= bid <= MAX_PRICE and fair - bid >= edge - 1e-9
    ok_ask = (improve or best_ask is not None) and MIN_PRICE <= ask <= MAX_PRICE and ask - fair >= edge - 1e-9
    return Quotes(bid if ok_bid else None, ask if ok_ask else None)
