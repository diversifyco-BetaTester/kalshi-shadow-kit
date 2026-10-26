"""
stat_research.py — the stats-vs-market gap of a chosen leg, as a logged FEATURE. Never a veto.

  gap = recent hit rate of the side we took - its fair probability (both in points)

e.g. Less 24.5 points at 57% fair, the player went under 24.5 in 3 of his last 10 games: gap = 30 - 57 =
-27 pts (the box score is against us; the market says otherwise). The open question it is logged to answer:
does a large negative gap predict CLV loss? The backtest slices CLV by gap; until a slice has 30+ distinct
legs, nobody acts on it (see local/backtest.py). It changes no probability, EV, slip or alert.

Game logs come from local/enricher.py (ESPN's public game logs, no key, cached CACHE_S, preseason left out,
so the alert card's research note and this feature share one fetch). The last `games` (10) regular-season or
postseason games are used; fewer than `min_games` (5) of them in the current season (October: the NBA log can
still be last season's, the enricher's STALE_DAYS) and the gap is None with the reason, never a guess.

Logged per chosen leg (both builders: live = sent, paper = the optimizer's pick) into
snapshots slip_legs.stats_json, in a background task after the cycle, so it can never delay an alert.

NBA legs also carry `profile` (nba_profile): the last 10 current-season games' minutes, points, rebounds
and assists, their averages, and the minutes trend (last 3 vs last 10): logged, never acted on.

Source and caching: ESPN's public JSON (no key, no new dependency; nba_api's stats.nba.com needs spoofed
headers and throttles non-residential IPs). Search results and game logs are cached in memory per process
for enricher.CACHE_S (3 h), so a player is fetched at most once per sentinel per 3 h, not every cycle.

  python3 -m local.stat_research "Jayson Tatum" [--team BOS]     # live L10 profile (one ESPN request)
"""
from __future__ import annotations

import asyncio
import statistics
import time
from datetime import datetime

import aiohttp

from local import enricher, pipeline_config
from local import prize_picks_ev_scraper as scanner


NBA_METRICS = {"min": ["min:minutes"], "pts": ["points"], "reb": ["totalRebounds"], "ast": ["assists"]}


def _ts(d: str) -> float:
    try:
        return datetime.fromisoformat(d.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def profile_from_log(log: dict, now: float, games: int = 10) -> dict:
    """The last `games` current-season games (newest first, preseason excluded, none older than
    enricher.STALE_DAYS): minutes, points, rebounds, assists per game; averages; minutes L3 vs L10."""
    rows = []
    for g in enricher.games(log):
        if now - _ts(g["date"]) > enricher.STALE_DAYS * 86400:
            continue
        vals = {k: enricher.row_value(cols, g["row"]) for k, cols in NBA_METRICS.items()}
        if vals["min"] is None or vals["min"] == 0:  # DNP: no minutes, not a 0-point game
            continue
        rows.append({"date": g["date"][:10], "opp": g["opp"], **{k: v for k, v in vals.items()}})
        if len(rows) == games:
            break
    out = {"n": len(rows), "games": rows, "avg": None, "min_l3": None, "min_trend": None,
           "note": "" if rows else "no current-season games"}
    if rows:
        out["avg"] = {k: round(statistics.fmean(r[k] for r in rows if r[k] is not None), 2) for k in NBA_METRICS
                      if any(r[k] is not None for r in rows)}
        l3 = [r["min"] for r in rows[:3]]
        out["min_l3"] = round(statistics.fmean(l3), 2)
        if len(rows) > 3 and out["avg"].get("min"):
            out["min_trend"] = round(out["min_l3"] / out["avg"]["min"] - 1, 4)  # -0.2: L3 minutes 20% under L10
    return out


async def player_log(player: str, sport: str, team: str | None, get) -> tuple[dict | None, str]:
    """(ESPN game log, "") or (None, why); cached in memory by the enricher."""
    aid, why = await enricher.find_athlete(player, sport, team, get)
    if not aid:
        return None, why
    log = await enricher.cached(("log", sport, aid),
                                lambda: get(enricher.GAMELOG.format(path=enricher.PATHS[sport], id=aid), {}))
    return log, ""


async def nba_profile(player: str, team: str | None = None, get=None, now: float | None = None,
                      games: int = 10) -> dict:
    """A player's live L10 NBA profile (profile_from_log); {"n": 0, "note": why} on any failure."""
    get = get or enricher.http_get
    now = time.time() if now is None else now
    try:
        log, why = await player_log(player, "nba", team, get)
        return profile_from_log(log, now, games) if log else {"n": 0, "games": [], "note": why}
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError, KeyError, TypeError) as e:
        return {"n": 0, "games": [], "note": type(e).__name__}


def gap_record(values: list[tuple[str, str, float]], line: float, side: str, p_fair: float, now: float,
               games: int, min_games: int) -> dict:
    """From (date, opponent, value) newest first: n, hit rate of `side`, mean, and the gap to p_fair.
    A whole line's p_fair is P(win | no push), so games landing exactly on it are pushes: left out of
    the hit rate's denominator (and counted in `pushes`), never scored as misses."""
    def ts(d):
        try:
            return datetime.fromisoformat(d.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
    recent = [(d, v) for d, _, v in values if now - ts(d) <= enricher.STALE_DAYS * 86400][:games]
    vals = [v for _, v in recent]
    pushes = sum(v == line for v in vals)  # only a whole line can be hit exactly
    decided = len(vals) - pushes
    base = {"line": line, "side": side, "p_fair": round(p_fair, 5), "n": len(recent), "pushes": pushes,
            "hit_rate": None, "mean": None, "gap": None}
    if len(recent) < min_games:
        stale = len(values) - len(recent)
        return {**base, "note": f"{len(recent)} recent games" + (f" ({stale} older than {enricher.STALE_DAYS} days)" if stale else "")}
    if decided < min_games:
        return {**base, "mean": round(statistics.fmean(vals), 3),
                "note": f"{decided} decided games ({pushes} pushes on the line)"}
    hits = sum((v > line) if side == "More" else (v < line) for v in vals)
    rate = hits / decided
    return {**base, "hit_rate": round(rate, 4), "mean": round(statistics.fmean(vals), 3),
            "gap": round(rate - p_fair, 4), "note": ""}


async def stat_gap(l: scanner.Leg, get=None, now: float | None = None, cfg: dict | None = None) -> dict:
    """The gap record for one leg; {"gap": None, "note": why} on any failure. Never raises."""
    c = (cfg or pipeline_config.CONFIG)["stat_feature"]
    get = get or enricher.http_get
    now = time.time() if now is None else now
    side, p = l.pick
    sport = (l.sport or "").lower()
    none = lambda why: {"line": l.line, "side": side, "p_fair": round(p, 5), "n": 0, "hit_rate": None, "mean": None,
                        "gap": None, "note": why}
    cols = enricher.COLUMNS.get(sport, {}).get(enricher.stat_phrase(sport, l.stat))
    if not cols:
        return none(f"{l.stat} isn't in ESPN game logs")
    try:
        log, why = await player_log(l.player, sport, l.team, get)
        if log is None:
            return none(why)
        values = [(g["date"], g["opp"], v) for g in enricher.games(log)
                  if (v := enricher.row_value(cols, g["row"])) is not None]
        rec = gap_record(values, float(l.line), side, p, now, c["games"], c["min_games"])
        if sport == "nba":  # same cached log: minutes / points / rebounds / assists, a feature only
            rec["profile"] = profile_from_log(log, now, c["games"])
        return rec
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError, KeyError, TypeError) as e:
        return none(type(e).__name__)


async def log_gaps(snaps, slips: list[tuple[str, list[scanner.Leg]]], get=None, now: float | None = None) -> int:
    """Compute and store the gap for every leg of each (slip_id, legs); returns rows written. Errors in one
    leg never stop the others, and nothing here is ever awaited by the alert path."""
    written = 0
    for slip_id, legs in slips:
        for l in legs:
            rec = await stat_gap(l, get, now)
            written += snaps.record_stats(slip_id, l, rec)
    return written


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Live L10 NBA profile from ESPN's public game log (no key).")
    ap.add_argument("player")
    ap.add_argument("--team", help="team code, when two players share the name")
    a = ap.parse_args()
    p = asyncio.run(nba_profile(a.player, a.team))
    if not p["n"]:
        print(f"{a.player}: no profile ({p['note']})")
        return
    print(f"{a.player}: {p['n']} game(s) · avg " + " · ".join(f"{k} {v:g}" for k, v in p["avg"].items())
          + (f" · minutes L3 {p['min_l3']:g} ({p['min_trend']:+.0%} vs L10)" if p["min_trend"] is not None else ""))
    for g in p["games"]:
        print(f"  {g['date']} vs {g['opp']:<4} min {g['min']:g}  pts {g['pts']:g}  reb {g['reb']:g}  ast {g['ast']:g}")


if __name__ == "__main__":
    main()
