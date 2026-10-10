"""
rule.py — when is a Kalshi daily-high bucket logically dead?

The day's high can only go up. Once the station's running observed max has passed a
bucket, that bucket can no longer win. Two safeguards, both found the hard way in backtest:
  * Sensor spikes: a single bogus METAR (e.g. 80°F between two 71°F readings) is later
    removed by NWS QC. Require CONFIRM consecutive readings to all clear the bucket.
  * Whole-°C reports: a special METAR without a tenths group reports e.g. 21°C = 69.8°F,
    whose true value may be 68.9°F. Count such readings at their lower bound.
"""
from __future__ import annotations

import math
import re
from datetime import date

MONTHS = {m: i for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
                                      "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


def event_day(event_ticker: str) -> date | None:
    """KXHIGHNY-26OCT08 -> 2026-10-08."""
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})$", event_ticker)
    return date(2000 + int(m.group(1)), MONTHS[m.group(2)], int(m.group(3))) if m else None


def rnd(x: float) -> int:
    """Round half up (NWS convention), not Python's banker's rounding."""
    return math.floor(x + 0.5)


def safe_f(tmpf: float) -> int:
    """Whole °F the official high is guaranteed to reach given this reading."""
    return rnd(tmpf) if abs(tmpf - round(tmpf)) < 1e-6 else math.floor(tmpf - 0.9)


def kill_threshold(strike_type: str, floor: float | None, cap: float | None, margin: int = 0) -> int | None:
    """Running max (whole °F) at which the bucket is dead; None if a running max can't kill it.
    Kalshi strikes:  between: floor <= T <= cap   less: T < cap   greater: T > floor."""
    if strike_type == "between":
        return int(cap) + 1 + margin
    if strike_type == "less":
        return int(cap) + margin
    return None  # "greater" buckets can only be confirmed by a running max, never killed


def death_time(obs: list[tuple[float, float]], lo_ts: float, hi_ts: float, strike_type: str,
               floor: float | None, cap: float | None, margin: int = 0, confirm: int = 2,
               conv=safe_f) -> tuple[float | None, int | None]:
    """First obs time in [lo_ts, hi_ts) at which the confirmed running max kills the bucket,
    and that max. `obs` is sorted (unix ts, °F)."""
    need = kill_threshold(strike_type, floor, cap, margin)
    if need is None:
        return None, None
    m, last = -math.inf, []
    for t, v in obs:
        if t < lo_ts:
            continue
        if t >= hi_ts:
            break
        last = (last + [conv(v)])[-confirm:]
        if len(last) < confirm:
            continue
        m = max(m, min(last))
        if m >= need:
            return t, m
    return None, None


def in_bucket(t: int, strike_type: str, floor: float | None, cap: float | None) -> bool:
    return (floor <= t <= cap) if strike_type == "between" else (t < cap) if strike_type == "less" else (t > floor)
