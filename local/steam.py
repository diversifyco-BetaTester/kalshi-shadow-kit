"""
steam.py — how far a leg's sharp fair price has moved, as a FEATURE (never a trigger), plus whether
PrizePicks had already moved its own line (the stale-line check).

Sources, tracked separately so a switch between them never reads as steam:
  kalshi  the fair price the sentinel reads every 60 s; window WINDOW_S["kalshi"] (30 min)
  books   the multi-book consensus, refreshed at most hourly on the credit budget; window 2 h
A leg's steam_score = its fair probability for the PICKED side now minus at the start of the window
(e.g. +0.031 = the market moved 3.1 points toward our side). Needs at least MIN_SPAN_S of history.

Stale-line check: PrizePicks lines come from the board dumps (every ~7 min). For each player/stat/
game the tracker keeps the line on every board; pp_line_moved says whether PrizePicks' line at the
start of the steam window differs from the line now (pp_prev_line is the old one). Steam with an
unmoved PrizePicks line is the case worth measuring for adverse selection later.

Steam never changes a leg's probability or EV, so it can't create a slip. The paper optimizer may use
it to rank legs into its pool: rank = p + STEAM_RANK_WEIGHT * max(steam_score, 0).
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from local import prize_picks_ev_scraper as scanner

WINDOW_S = {"kalshi": 30 * 60, "books": 2 * 3600}
MIN_SPAN_S = {"kalshi": 5 * 60, "books": 50 * 60}
DETECT = 0.02          # |steam_score| at or above this is logged as detected steam
STEAM_RANK_WEIGHT = 0.5


@dataclass
class SteamInfo:
    source: str
    score: float | None     # picked-side move over the window; None if history is shorter than MIN_SPAN_S
    span_s: float
    p_from: float
    p_to: float
    detected: bool
    pp_line_moved: bool | None   # None: no older board seen for this prop
    pp_prev_line: float | None

    def record(self) -> dict:
        return {"source": self.source, "score": None if self.score is None else round(self.score, 5),
                "span_s": round(self.span_s), "p_from": round(self.p_from, 5), "p_to": round(self.p_to, 5),
                "detected": self.detected, "pp_line_moved": self.pp_line_moved, "pp_prev_line": self.pp_prev_line}


def price_key(l: scanner.Leg, source: str) -> tuple:
    return l.sport, l.player, l.stat, l.line, source


def prop_key(l: scanner.Leg) -> tuple:
    return l.sport, l.player, l.stat, l.game_date


def primary(l: scanner.Leg) -> scanner.Quote | None:
    return l.quotes[0] if l.quotes else None


class SteamTracker:
    def __init__(self):
        self.history: dict[tuple, deque] = {}
        self.pp_lines: dict[tuple, list[tuple[float, float]]] = {}

    def board(self, board_ts: float, legs: list[scanner.Leg]) -> None:
        """Record each prop's PrizePicks line on a new board (only when it changes)."""
        for l in legs:
            lines = self.pp_lines.setdefault(prop_key(l), [])
            if not lines or lines[-1][1] != l.line:
                lines.append((board_ts, l.line))

    def _pp_line_at(self, l: scanner.Leg, ts: float) -> float | None:
        older = [line for t, line in self.pp_lines.get(prop_key(l), []) if t <= ts]
        return older[-1] if older else None

    def update(self, legs: list[scanner.Leg], now: float) -> None:
        """Add this cycle's fair prices and set `leg.steam` (SteamInfo) on every priced leg."""
        for l in legs:
            q = primary(l)
            if q is None or not l.pick:
                l.steam = None
                continue
            src = q.venue
            h = self.history.setdefault(price_key(l, src), deque(maxlen=600))
            if not h or h[-1][1] != q.p_yes or now - h[-1][0] >= 60:
                h.append((now, q.p_yes))
            window = WINDOW_S.get(src, WINDOW_S["kalshi"])
            inside = [(t, p) for t, p in h if t >= now - window]
            t0, p0 = inside[0]
            span = now - t0
            sign = 1 if l.pick[0] == "More" else -1
            score = sign * (q.p_yes - p0) if span >= MIN_SPAN_S.get(src, MIN_SPAN_S["kalshi"]) else None
            prev = self._pp_line_at(l, t0)
            l.steam = SteamInfo(src, score, span, p0 if sign > 0 else 1 - p0, l.pick[1],
                                score is not None and abs(score) >= DETECT,
                                None if prev is None else prev != l.line, prev)


def rank(l: scanner.Leg) -> float:
    """The paper optimizer's pool order: fair probability, nudged up (never down) by positive steam."""
    s = getattr(l, "steam", None)
    return l.pick[1] + STEAM_RANK_WEIGHT * max((s.score if s and s.score is not None else 0.0), 0.0)
