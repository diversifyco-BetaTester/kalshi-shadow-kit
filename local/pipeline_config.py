"""
pipeline_config.py — payout tables, de-vig method and lineup-optimizer settings, from config, not code.

Defaults live here; local/pipeline_config.json (optional, gitignored with local/) overrides any of them,
key by key. Payout tables are {legs: {hits: multiplier}} as JSON objects (string keys are fine).

  devig_method        "proportional" | "power" | "shin" (local/devig.py): sportsbook two-way markets
  kalshi_devig        Kalshi books by price: "outer" method (power) when the book's midpoint is outside
                      "band" (15-85c), where proportional drags fair value off the midpoint on lopsided
                      books; "inner" (proportional) inside it. Both fair values are logged on every leg.
  payouts.flex/power  standard PrizePicks tables (Power is for reporting only)
  reversion           what a slip pays once ties/DNPs shrink it below the smallest Flex size:
                      2 legs -> 2-pick Power, 1 or 0 legs -> refund
  optimizer.sizes             candidate slip sizes ([5, 6], the live builder's sentinel.FLEX_SIZES; 4-picks
                              only in a --config replay variant)
  optimizer.marginal_threshold  the fair probability a leg needs to EXTEND a slip (4->5, 5->6)
  optimizer.selection         "kelly_growth" (default: rank by full-Kelly log-growth, the same criterion the
                              stake uses), "sharpe" (EV / std dev) or "ev_per_var" (biased to fewer legs:
                              variance scales with stake^2; it took a +20% 4-pick over a +27% 5-pick)
  optimizer.allow_stacks      false: correlated same-game pairs are flagged and kept out of the pool
  odds_budget.shares          per-sport slices of the shared daily Odds API cap, as FRACTIONS of it (so they
                              follow --odds-budget: 80 -> NFL 40, the rest 40). A sport listed by name is its
                              own group; every unlisted sport spends from "other" together. Shares summing to
                              1 are also reservations: one group can't drain another's credits before its
                              games enter the pricing window. All sentinels read the same config, so they
                              enforce the same split on the one shared cap (restart them after editing it).
  min_fair_by_sport           per-sport floor on a leg's fair probability to join any slip ({"nhl": 0.57}); a sport
                              not listed uses the sentinel's --min-ev (54.5% in flex mode), and a floor is never
                              below --min-ev. Applies to a sentinel's own legs and to legs pooled from other sports
                              (each leg against ITS sport's floor), so cross-sport slips keep one standard
  books.ladder_interp         false (2026-10-23): a sportsbook price is never interpolated between two
                              alternate-ladder rungs (ladder_interp lost CLV on 7/7 NBA legs); exact rungs only
  books.alt_ladder            false (2026-10-23): no alternate-ladder market is ever requested (each costs a
                              credit per game, and with interpolation off most found no exact rung); a leg off the
                              books' main line stays unpriced by the books. Kalshi is unaffected
  books.whole_lines           false (2026-10-23): whole-number lines are never priced from the books (their push
                              needs an alternate-ladder fetch: credits); Kalshi prices them from adjacent markets
                              at no credit cost. Both can be turned back on here once the data supports it
  integrity                   market-quality flags on each leg's sharp price (local/integrity.py): stale books
                              price (max_age_s), fewer than min_books books, books disagreeing by more than
                              max_spread, Pinnacle against our side by more than pin_against, alternate-ladder
                              interpolation (flag_interp); Kalshi quotes: bid/ask wider than kalshi_max_spread
                              (kalshi_wide), a price interpolated between rungs (kalshi_interp, when
                              flag_kalshi_interp), fewer than kalshi_min_volume contracts traded on a rung
                              (kalshi_thin). enforce false = SHADOW: flagged, reported, never removed
  shading                     a Kalshi price is pulled toward 50% before floors, ranking, EV and stake
                              (local/integrity.py shade): the picked side loses base_pts on every Kalshi leg, plus
                              spread_mult x the bid/ask spread, interp_pts when interpolated and thin_pts when
                              thin, at most max_pts, never past 50%. 2026-10-25 integrity report (10-18 to 10-25):
                              clean Kalshi legs lost 0.40 pts of CLV (1.51 on the eligible ones, n=17) while wide
                              (+0.26, n=24) and interpolated (-0.20, n=50) ones did no worse, so the haircut is
                              mostly a flat base (the winner's curse of picking the top of noisy mids) and the
                              spread/interp terms are small. Refit once eligible Kalshi legs have 30+ clean
                              closes. Snapshots log the raw price (and the shade), so CLV stays raw-vs-raw
  jit_refresh                 just before a slip is claimed and alerted, its books legs older than max_age_s
                              (15 min) are re-priced from a targeted refresh of just their (event, market), within
                              the budget; the slip is re-evaluated (still +EV, every leg >= min fair, stake >=
                              min) or dropped unclaimed. on_refused "send" (default) | "skip" when no refresh
                              was possible (budget, reserve, no odds key)
  stat_feature                log a stats-vs-market gap (local/stat_research.py) on every chosen leg, in the
                              background; a FEATURE for the backtest slices, never a veto
  staking.kelly / unit_pct    quarter Kelly, 1u = 1% of bankroll
  staking.max_units           cap (2.0u): never stake more, whatever Kelly says
  staking.min_units           a slip whose Kelly stake is below this (0.5u) is...
  staking.below_min           "skip" (default: not sent) or "stake" (sent at the computed amount). Never
                              rounded UP: a floor that lifts a 0.2u Kelly stake to 0.5u over-stakes exactly
                              the weakest slips. Stakes round DOWN to 0.1u.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

PATH = Path(__file__).with_name("pipeline_config.json")

DEFAULTS = {
    "devig_method": "proportional",
    "kalshi_devig": {"band": [0.15, 0.85], "inner": "proportional", "outer": "power"},
    "payouts": {
        "flex": {3: {3: 3.0, 2: 1.0}, 4: {4: 6.0, 3: 1.5}, 5: {5: 10.0, 4: 2.0, 3: 0.4},
                 6: {6: 25.0, 5: 2.0, 4: 0.4}},
        "power": {2: {2: 3.0}, 3: {3: 6.0}, 4: {4: 10.0}, 5: {5: 20.0}, 6: {6: 37.5}},
    },
    "reversion": {"2": "power", "1": "refund", "0": "refund"},
    "optimizer": {"sizes": [5, 6], "marginal_threshold": 0.54, "selection": "kelly_growth",
                  "allow_stacks": False, "top_legs": 12},
    "min_fair_by_sport": {},
    "books": {"ladder_interp": False, "whole_lines": False, "alt_ladder": False},
    "integrity": {"enforce": False, "max_age_s": 1800, "min_books": 2, "max_spread": 0.04, "pin_against": 0.02,
                  "flag_interp": True, "kalshi_max_spread": 0.05, "kalshi_min_volume": 100, "flag_kalshi_interp": True},
    "shading": {"enabled": True, "base_pts": 0.01, "spread_mult": 0.1, "interp_pts": 0.005, "thin_pts": 0.01,
                "max_pts": 0.03},
    "jit_refresh": {"enabled": True, "max_age_s": 900, "on_refused": "send"},
    "stat_feature": {"enabled": True, "games": 10, "min_games": 5},
    "odds_budget": {"shares": {"nfl": 0.5, "other": 0.5}},
    "staking": {"kelly": 0.25, "unit_pct": 0.01, "min_units": 0.5, "max_units": 2.0, "below_min": "skip"},
}


def _intkeys(table: dict) -> dict:
    return {int(n): {int(k): float(m) for k, m in hits.items()} for n, hits in table.items()}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load(path: Path = PATH) -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    over: dict = {}
    try:
        over = json.loads(path.read_text())
    except FileNotFoundError:
        pass
    cfg = _merge(cfg, {k: v for k, v in over.items() if k != "payouts"})
    payouts = {kind: _intkeys(t) for kind, t in DEFAULTS["payouts"].items()}
    for kind, tables in (over.get("payouts") or {}).items():  # override size by size, keep the rest
        payouts.setdefault(kind, {}).update(_intkeys(tables))
    cfg["payouts"] = payouts
    return cfg


CONFIG = load()
