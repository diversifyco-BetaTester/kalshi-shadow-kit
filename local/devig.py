"""
devig.py — turn a two-way price into a fair probability, with the method on record.

    two_way(over, under, fmt="american", method="proportional") -> TwoWay

A TwoWay keeps everything a later audit or CLV check needs: both raw prices, the format, both raw
implied probabilities (vig included), their overround, the fair probabilities, and the method used.

Formats: "american" (-130 / +110), "decimal" (1.77), "prob" (an implied probability or a Kalshi ask
in dollars, e.g. YES ask 0.57 / NO ask 0.45).

Methods (config: pipeline_config.json "devig_method"):
  proportional  fair_i = p_i / sum(p)                       (multiplicative; the default)
  power         fair_i = p_i ** k, with k solving sum(p_i ** k) = 1  (shades favourites less)
  shin          Shin (1993): z = the share of insider money, solved so the fair probabilities sum to 1;
                fair_i = (sqrt(z^2 + 4(1 - z) p_i^2 / S) - z) / (2 (1 - z)), S = sum(p)
For a fair (no-vig) market every method returns the input.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass

METHODS = ("proportional", "power", "shin")


def implied(price: float, fmt: str = "decimal") -> float:
    """Raw implied probability (vig included) of one side's price."""
    if fmt == "american":
        if -100 < price < 100:
            raise ValueError(f"American odds must be <= -100 or >= +100, got {price}")
        return -price / (-price + 100) if price < 0 else 100 / (price + 100)
    if fmt == "decimal":
        if price <= 1:
            raise ValueError(f"decimal odds must be > 1, got {price}")
        return 1 / price
    if fmt == "prob":
        if not 0 < price < 1:
            raise ValueError(f"a probability price must be in (0, 1), got {price}")
        return float(price)
    raise ValueError(f"unknown odds format {fmt!r}")


def _bisect(f, lo: float, hi: float, n: int = 200) -> float:
    for _ in range(n):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if f(mid) > 0 else (lo, mid)
    return (lo + hi) / 2


def fair(probs: list[float], method: str = "proportional") -> list[float]:
    """Fair probabilities from raw implied ones (any number of outcomes)."""
    s = sum(probs)
    if method == "proportional" or abs(s - 1) < 1e-12:
        return [p / s for p in probs]
    if method == "power":
        if s < 1:  # an underround (arb) can't be fixed by shrinking: fall back to proportional
            return [p / s for p in probs]
        k = _bisect(lambda k: sum(p ** k for p in probs) - 1, 1.0, 50.0)
        return [p ** k for p in probs]
    if method == "shin":
        if s < 1:
            return [p / s for p in probs]
        def shin(z: float) -> list[float]:
            return [(math.sqrt(z * z + 4 * (1 - z) * p * p / s) - z) / (2 * (1 - z)) for p in probs]
        z = _bisect(lambda z: sum(shin(z)) - 1, 0.0, 0.99)
        return shin(z)
    raise ValueError(f"unknown de-vig method {method!r} (use one of {METHODS})")


@dataclass(frozen=True)
class TwoWay:
    over_price: float
    under_price: float
    fmt: str
    implied_over: float
    implied_under: float
    overround: float          # implied_over + implied_under - 1 (the vig)
    fair_over: float
    fair_under: float
    method: str

    def record(self) -> dict:
        return {k: (round(v, 6) if isinstance(v, float) else v) for k, v in asdict(self).items()}


def two_way(over: float, under: float, fmt: str = "american", method: str = "proportional") -> TwoWay:
    io, iu = implied(over, fmt), implied(under, fmt)
    fo, fu = fair([io, iu], method)
    return TwoWay(over, under, fmt, io, iu, io + iu - 1, fo, fu, method)
