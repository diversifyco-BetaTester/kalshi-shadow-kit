"""
optimizer.py — exact slip EV with ties, and a size-agnostic (5/6-pick Flex by default) lineup builder.

EV ENGINE
  Each leg has a fair win probability and a tie probability (a .5 line never ties; a whole-number line
  ties when the stat lands on it). The exact joint distribution of (wins, ties) over heterogeneous legs
  is built by dynamic programming, never a single-p binomial. A tie (or DNP) removes the leg, as on
  PrizePicks, so an n-leg Flex with t ties pays the (n - t)-leg table at its win count; below the
  smallest Flex table it follows pipeline_config "reversion" (2 legs -> 2-pick Power, 1-0 -> refund).
  Returns gross EV per $1, its variance, and P(profit) = P(payout > 1x).

BUILDER (paper mode: runs beside the live sentinel builder until the backtest is reviewed)
  * pool: standard two-way legs only. Anything else (goblin, demon, discounted, promo, unknown odds
    type) makes a slip NON-STANDARD: PrizePicks changes the payout and the board doesn't show it, so
    such legs are excluded from EV ranking instead of priced on the standard table.
  * correlation guard: with allow_stacks false (default), two legs from one game never share a slip;
    pairs correlation.py recognizes as positive stacks are logged as warnings.
  * candidates per size (optimizer.sizes, default [5, 6] like the live builder: no 4-picks since
    2026-10-19). A 4-pick (if configured) needs only to be +EV. 5- and 6-pick, whatever the sizes: the
    weakest leg must clear marginal_threshold (default 54%) AND the slip without it must itself be
    +EV: a leg only extends a slip that already stands, so a 50-52% filler never makes the 6-man.
  * the best candidate of each size by `selection` (default kelly_growth; sharpe | ev_per_var) is
    reported with EV, variance, EV per std dev, log-growth and P(profit); the overall pick is the best.
    Why not more legs by default: at equal leg probabilities 5- and 6-Flex break even at ~54.25% vs
    ~54.21%, but 6-Flex variance is ~3.3-3.5x the 5-Flex's (std ~1.8x), not ~2x, and a 6th leg only
    helps when it is itself ~54%+ (five 56% legs: 1.119 per $1; plus a 50% leg 1.071, 52% 1.106,
    54% 1.141).
"""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field

from local import correlation, pipeline_config, steam
from local import prize_picks_ev_scraper as scanner


@dataclass(frozen=True)
class LegProb:
    p_win: float
    p_tie: float = 0.0

    def __post_init__(self):
        if not (0 <= self.p_win and 0 <= self.p_tie and self.p_win + self.p_tie <= 1 + 1e-12):
            raise ValueError(f"bad leg probabilities {self}")


def outcomes(legs: list[LegProb]) -> dict[tuple[int, int], float]:
    """{(wins, ties): probability} for independent legs, exactly."""
    dist = {(0, 0): 1.0}
    for leg in legs:
        p_loss = 1 - leg.p_win - leg.p_tie
        nxt: dict[tuple[int, int], float] = {}
        for (w, t), p in dist.items():
            for (dw, dt), q in (((1, 0), leg.p_win), ((0, 1), leg.p_tie), ((0, 0), p_loss)):
                if q > 0:
                    nxt[(w + dw, t + dt)] = nxt.get((w + dw, t + dt), 0.0) + p * q
        dist = nxt
    return dist


def multiplier(kind: str, legs_left: int, wins: int, cfg: dict | None = None) -> float:
    """What the slip pays once ties have left `legs_left` live legs with `wins` of them won."""
    cfg = cfg or pipeline_config.CONFIG
    table = cfg["payouts"].get(kind, {}).get(legs_left)
    if table is not None:
        return table.get(wins, 0.0)
    rule = cfg["reversion"].get(str(legs_left), "refund")
    if rule == "refund":
        return 1.0
    return cfg["payouts"].get(rule, {}).get(legs_left, {}).get(wins, 0.0)


@dataclass(frozen=True)
class SlipStats:
    ev: float          # gross expected return per $1 (1.0 = break-even)
    variance: float    # of the gross return per $1
    p_profit: float    # P(payout > 1x)

    @property
    def net(self) -> float:
        return self.ev - 1

    def metric(self, selection: str) -> float:
        if selection == "ev_per_var":
            return self.net / self.variance if self.variance > 0 else -math.inf
        if selection == "sharpe":
            return self.net / math.sqrt(self.variance) if self.variance > 0 else -math.inf
        if selection == "kelly_growth":
            return self.growth
        raise ValueError(f"unknown selection {selection!r}")

    growth: float = 0.0  # best log-growth per entry at full Kelly (filled by slip_stats)
    kelly_f: float = 0.0  # the full-Kelly fraction of bankroll that achieves it (0 when not +EV)


def slip_stats(legs: list[LegProb], kind: str = "flex", cfg: dict | None = None) -> SlipStats:
    n = len(legs)
    pay = [(p, multiplier(kind, n - t, w, cfg)) for (w, t), p in outcomes(legs).items()]
    ev = sum(p * m for p, m in pay)
    var = sum(p * (m - ev) ** 2 for p, m in pay)
    growth = f = 0.0
    if ev > 1:
        g = lambda f: sum(p * math.log(1 - f + f * m) for p, m in pay if p > 0)
        lo, hi = 0.0, 0.999
        for _ in range(80):
            a, b = lo + (hi - lo) / 3, hi - (hi - lo) / 3
            lo, hi = (a, hi) if g(a) < g(b) else (lo, b)
        f = (lo + hi) / 2
        growth = g(f)
    return SlipStats(ev, var, sum(p for p, m in pay if m > 1 + 1e-12), growth, f)


def breakeven(n: int, kind: str = "flex", cfg: dict | None = None) -> float:
    """The equal per-leg win probability at which an n-pick slip's gross EV is exactly 1."""
    lo, hi = 0.0, 1.0
    for _ in range(100):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if slip_stats([LegProb(mid)] * n, kind, cfg).ev < 1 else (lo, mid)
    return (lo + hi) / 2


# --------------------------------------------------------------------------- builder
@dataclass
class Candidate:
    size: int
    legs: tuple
    stats: SlipStats | None
    metric: float
    note: str = ""


@dataclass
class Report:
    chosen: Candidate | None
    by_size: dict[int, Candidate]
    excluded: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = []
        for n, c in sorted(self.by_size.items()):
            if c.stats is None:
                out.append(f"  {n}-pick: none ({c.note})")
            else:
                mark = "  <- chosen" if self.chosen is c else ""
                out.append(f"  {n}-pick: EV {c.stats.ev:.3f} per $1 ({c.stats.net:+.1%}), variance {c.stats.variance:.2f}, "
                           f"EV/sd {c.stats.metric('sharpe'):.3f}, log-growth {c.stats.growth:.4f}, "
                           f"P(profit) {c.stats.p_profit:.1%}{mark}")
        return out


def leg_prob(l: scanner.Leg) -> LegProb:
    """P(our side wins) and P(push) for one leg: a half line never pushes; a push-priced whole line's
    pick probability is conditional on no push (Leg.pick), so P(win) = pick x (1 - p_push)."""
    push = l.p_push or 0.0
    return LegProb(l.pick[1] * (1 - push), push)


def standard(l: scanner.Leg) -> str | None:
    """Why a leg makes a slip NON-STANDARD (its payout isn't the standard table), or None."""
    if (l.odds_type or "standard") != "standard":
        return f"{l.player}: {l.odds_type} (non-standard payout)"
    return None


GUARD_FROM = 5  # the filler guardrail applies from this size up, even when 4-picks aren't candidates


def build(legs: list[scanner.Leg], kind: str = "flex", cfg: dict | None = None) -> Report:
    cfg = cfg or pipeline_config.CONFIG
    o = cfg["optimizer"]
    excluded, warnings, pool = [], [], []
    for l in legs:
        why = standard(l)
        (excluded.append(why) if why else pool.append(l))
    # pool order: fair probability, nudged up (never down) by positive steam; EV still uses p alone
    pool = sorted((l for l in pool if l.pick), key=lambda l: -steam.rank(l))[: o["top_legs"]]
    if not o["allow_stacks"]:
        for a, b in itertools.combinations(pool, 2):
            if correlation.game_id(a) == correlation.game_id(b) and correlation.classify(a, b)[0] == "stack":
                warnings.append(f"correlated pair kept apart: {correlation.classify(a, b)[1].label}")

    def allowed(combo) -> bool:
        if o["allow_stacks"]:
            return correlation.check(combo)[0]
        return len({correlation.game_id(l) for l in combo}) == len(combo) and scanner.allowed(combo)

    best: dict[int, Candidate] = {}
    for n in sorted(o["sizes"]):
        top = None
        for combo in itertools.combinations(pool, n):
            if not allowed(combo):
                continue
            ordered = sorted(combo, key=lambda l: -l.pick[1])
            if n >= GUARD_FROM or n > min(o["sizes"]):
                if ordered[-1].pick[1] < o["marginal_threshold"]:
                    continue  # filler guardrail: the extending leg must clear the marginal threshold
                if slip_stats([leg_prob(l) for l in ordered[:-1]], kind, cfg).ev <= 1:
                    continue  # ...and only extends a slip that is +EV without it
            st = slip_stats([leg_prob(l) for l in combo], kind, cfg)
            if st.ev <= 1:
                continue
            m = st.metric(o["selection"])
            if top is None or m > top.metric:
                top = Candidate(n, tuple(ordered), st, m)
        best[n] = top or Candidate(n, (), None, -math.inf,
                                   "no +EV slip" if n < GUARD_FROM and n == min(o["sizes"]) else
                                   f"no leg >= {o['marginal_threshold']:.0%} extends a +EV {n - 1}-pick")
    live = [c for c in best.values() if c.stats is not None]
    return Report(max(live, key=lambda c: c.metric) if live else None, best, excluded, warnings)
