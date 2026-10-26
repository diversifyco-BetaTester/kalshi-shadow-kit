"""
enricher.py — a one-line research note for a leg: last-5 average, last-5 record against the line,
and the season's games against tonight's opponent, from ESPN's public game logs (no key).

  "L5 avg 2.6 · 3/5 over 2.5 · vs MTL 1/1 (3)"

Player lookup: site.web.api.espn.com/apis/common/v3/search (type=player) gives the ESPN id and league.
Two players can share a name in one league (two NFL "Josh Allen"s), so a `team` code picks the one on
that team (fetch_odds.team_codes maps ESPN's team names to the CSV's codes); still ambiguous, or not
found, and it says so instead of guessing.

Game log: .../apis/common/v3/sports/{sport}/{league}/athletes/{id}/gamelog. One season (the current or
latest), columns by position (a QB's has passing, a goalie's has saves). Preseason rows are left out.
Games older than STALE_DAYS are flagged: in October the NBA log is still last season's.
Not in the logs at all: NHL skater hits, blocked shots and faceoffs; NFL longest-reception style stats.

Every failure (unknown stat, no player, HTTP error, timeout) returns a short "No recent data (...)"
string; enrich_leg never raises. Results are cached in memory for CACHE_S.
"""
from __future__ import annotations

import asyncio
import statistics
import time
from datetime import datetime, timezone
from typing import Awaitable, Callable

import aiohttp

from local import fetch_odds
from local import prize_picks_ev_scraper as scanner
from local.grade_slips import canon
from local.parse_board import SPORTS

SEARCH = "https://site.web.api.espn.com/apis/common/v3/search"
GAMELOG = "https://site.web.api.espn.com/apis/common/v3/sports/{path}/athletes/{id}/gamelog"
PATHS = {"nhl": "hockey/nhl", "nfl": "football/nfl", "mlb": "baseball/mlb", "nba": "basketball/nba",
         "cfb": "football/college-football"}
LEAGUE_SLUG = {"cfb": "college-football"}  # how ESPN's search names the league
TIMEOUT_S = 8
CACHE_S = 3 * 3600
STALE_DAYS = 30
N = 5


def _outs(ip: str) -> float:
    whole, _, part = ip.partition(".")
    return int(whole) * 3 + int(part or 0)


def _minutes(v: str) -> float:
    m, _, s = v.partition(":")
    return int(m) + int(s or 0) / 60


# our stat phrase (parse_board's SPORTS[...]["stats"] values) -> gamelog columns summed per game.
# A column name prefixed "outs:"/"min:" is converted ("5.2" innings -> 17 outs, "18:30" -> 18.5 minutes).
COLUMNS = {
    "nhl": {"points": ["points"], "goals": ["goals"], "assists": ["assists"], "shots on goal": ["shotsTotal"],
            "saves": ["saves"], "time on ice": ["min:timeOnIcePerGame"],
            "power play points": ["powerPlayGoals", "powerPlayAssists"]},
    "nfl": {"passing yards": ["passingYards"], "passing touchdowns": ["passingTouchdowns"],
            "passing attempts": ["passingAttempts"], "passing completions": ["completions"],
            "rushing yards": ["rushingYards"], "rushing attempts": ["rushingAttempts"],
            "receiving yards": ["receivingYards"], "receptions": ["receptions"],
            "receiving targets": ["receivingTargets"], "touchdowns": ["rushingTouchdowns", "receivingTouchdowns"]},
    "mlb": {"hits": ["hits"], "home runs": ["homeRuns"], "rbis": ["RBIs"], "runs": ["runs"], "walks": ["walks"],
            "stolen bases": ["stolenBases"], "hitter strikeouts": ["strikeouts"],
            "hits + runs + rbis": ["hits", "runs", "RBIs"], "total bases": ["hits", "doubles", "triples:2", "homeRuns:3"],
            "singles": ["hits", "doubles:-1", "triples:-1", "homeRuns:-1"],
            "strikeouts": ["strikeouts"], "hits allowed": ["hits"], "walks allowed": ["walks"],
            "earned runs": ["earnedRuns"], "pitching outs": ["outs:innings"]},
    "nba": {"points": ["points"], "rebounds": ["totalRebounds"], "assists": ["assists"],
            "threes": ["threePointFieldGoalsMade"], "blocks": ["blocks"], "steals": ["steals"],
            "turnovers": ["turnovers"], "points + rebounds + assists": ["points", "totalRebounds", "assists"],
            "points + rebounds": ["points", "totalRebounds"], "points + assists": ["points", "assists"],
            "rebounds + assists": ["totalRebounds", "assists"], "blocks + steals": ["blocks", "steals"],
            "minutes": ["min:minutes"]},  # ESPN: whole minutes ("38"); "MM:SS" also parses
}
COLUMNS["cfb"] = COLUMNS["nfl"]
# The same phrase means a batter's or a pitcher's column: pick by which the log has.
PITCHING = {"strikeouts", "hits allowed", "walks allowed", "earned runs", "pitching outs"}


def stat_phrase(sport: str, stat: str) -> str:
    """'Shots On Goal' / 'SOG' / 'shots on goal' -> 'shots on goal' (parse_board's mapping)."""
    s = " ".join(stat.lower().replace(" + ", "+").split())
    return SPORTS.get(sport, {}).get("stats", {}).get(s, s)


def row_value(cols: list[str], row: dict[str, str]) -> float | None:
    """Sum the gamelog cells for one game; None if a needed column is missing or not a number."""
    total = 0.0
    for c in cols:
        conv, _, name = c.rpartition(":") if c.startswith(("outs:", "min:")) else ("", "", c)
        name, _, weight = name.partition(":")
        raw = row.get(name)
        if raw in (None, "", "-"):
            return None
        try:
            v = _outs(raw) if conv == "outs" else _minutes(raw) if conv == "min" else float(raw)
        except ValueError:
            return None
        total += v * float(weight or 1)
    return total


def games(log: dict) -> list[dict]:
    """Every non-preseason game in the log, newest first: {"date", "opp", "row": {column: cell}}."""
    names, meta, out = log.get("names") or [], log.get("events") or {}, []
    for st in log.get("seasonTypes") or []:
        if "preseason" in (st.get("displayName") or "").lower():
            continue
        for cat in st.get("categories") or []:
            for ev in cat.get("events") or []:
                m = meta.get(ev.get("eventId"), {})
                stats = ev.get("stats") or []
                row = {}
                for name, cell in zip(names, stats):
                    if "-" in name and isinstance(cell, str) and "-" in cell:  # "3PM-3PA" style pairs
                        for n, c in zip(name.split("-"), cell.split("-")):
                            row[n] = c
                    else:
                        row[name] = cell
                out.append({"date": m.get("gameDate", ""), "opp": (m.get("opponent") or {}).get("abbreviation", ""),
                            "row": row})
    return sorted(out, key=lambda g: g["date"], reverse=True)


def summarize(values: list[tuple[str, str, float]], line: float, opponent: str | None, now: float) -> str:
    """values: (date, opponent, stat) newest first."""
    if not values:
        return "No recent data (no games in ESPN's log)"
    last = values[:N]
    avg = statistics.fmean(v for _, _, v in last)
    over = sum(v > line for _, _, v in last)
    fmt = lambda x: f"{x:.1f}".rstrip("0").rstrip(".") if x % 1 else f"{x:.0f}"
    parts = [f"L{len(last)} avg {fmt(avg)}", f"{over}/{len(last)} over {fmt(line)}"]
    if opponent:
        vs = [v for _, opp, v in values if canon(opp) == canon(opponent)]
        if vs:
            parts.append(f"vs {opponent} {sum(v > line for v in vs)}/{len(vs)} ({', '.join(fmt(v) for v in vs[:3])})")
    newest = datetime.fromisoformat(last[0][0].replace("Z", "+00:00")).timestamp() if last[0][0] else now
    if now - newest > STALE_DAYS * 86400:
        parts.append(f"last game {datetime.fromtimestamp(newest, timezone.utc):%b %-d}")
    return " · ".join(parts)


# --------------------------------------------------------------------------- HTTP
Get = Callable[[str, dict], Awaitable[dict]]
_cache: dict[tuple, tuple[float, object]] = {}


async def http_get(url: str, params: dict) -> dict:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT_S),
                                     headers={"User-Agent": "kalshi-shadow-kit enricher"}) as s:
        async with s.get(url, params=params) as r:
            r.raise_for_status()
            return await r.json(content_type=None)


async def cached(key: tuple, fetch: Callable[[], Awaitable[object]]) -> object:
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_S:
        return hit[1]
    value = await fetch()
    _cache[key] = (time.time(), value)
    return value


async def find_athlete(name: str, sport: str, team: str | None, get: Get) -> tuple[str | None, str]:
    """(ESPN athlete id or None, why not)."""
    d = await cached(("search", name.lower(), sport),
                     lambda: get(SEARCH, {"query": name, "limit": 10, "type": "player"}))
    want = scanner.norm(name).split()
    same = [i for i in d.get("items") or []
            if i.get("league") == LEAGUE_SLUG.get(sport, sport) and scanner.norm(i.get("displayName", "")).split() == want]
    if team and len(same) > 1:
        def on_team(i):
            teams = [t.get("displayName", "") for t in i.get("teamRelationships") or []]
            return any(canon(team) in {canon(c) for c in fetch_odds.team_codes(sport, t)} for t in teams)
        same = [i for i in same if on_team(i)] or [i for i in same if i.get("isActive")]
    if not same:
        return None, "player not found"
    if len(same) > 1:
        return None, f"{len(same)} {sport.upper()} players named {name}"
    return str(same[0]["id"]), ""


async def enrich_leg(player_name: str, sport: str, stat: str, line: float, opponent: str | None = None,
                     team: str | None = None, get: Get | None = None, now: float | None = None) -> str:
    """L5 average, L5 record over `line`, and games vs `opponent` this season; never raises."""
    get = get or http_get
    now = time.time() if now is None else now
    sport = sport.lower()
    phrase = stat_phrase(sport, stat)
    cols = COLUMNS.get(sport, {}).get(phrase)
    if not cols:
        return f"No recent data ({stat} isn't in ESPN game logs)"
    try:
        aid, why = await find_athlete(player_name, sport, team, get)
        if not aid:
            return f"No recent data ({why})"
        log = await cached(("log", sport, aid), lambda: get(GAMELOG.format(path=PATHS[sport], id=aid), {}))
        names = set(log.get("names") or [])
        if sport == "mlb" and phrase in PITCHING and "innings" not in names:
            return "No recent data (no pitching log)"
        if sport == "mlb" and phrase not in PITCHING and "atBats" not in names:
            return "No recent data (no batting log)"
        values = [(g["date"], g["opp"], v) for g in games(log) if (v := row_value(cols, g["row"])) is not None]
        return summarize(values, float(line), opponent, now)
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError, KeyError, TypeError) as e:
        return f"No recent data ({type(e).__name__})"


async def enrich_legs(legs: list[scanner.Leg], get: Get | None = None, timeout: float = TIMEOUT_S + 2) -> dict:
    """{leg: note} for a slip's legs, fetched concurrently; a leg that runs past `timeout` gets a fallback,
    so research can only ever delay an alert by about `timeout` seconds."""
    async def one(l: scanner.Leg) -> str:
        opp = next((t.strip() for t in l.game.replace("@", " v ").split(" v ") if t.strip() and t.strip() != l.team), None)
        try:
            return await asyncio.wait_for(enrich_leg(l.player, l.sport, l.stat, l.line, opp, l.team, get), timeout)
        except asyncio.TimeoutError:
            return "No recent data (timed out)"
    notes = await asyncio.gather(*(one(l) for l in legs))
    return dict(zip(map(id, legs), notes))
