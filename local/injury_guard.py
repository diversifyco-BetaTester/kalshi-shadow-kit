"""
injury_guard.py — veto legs whose player won't play, may play limited, or (NHL) isn't the starting goalie.

Sources (ESPN, public, no key), cached in memory for CACHE_S (15 min):
  injuries   site.api.espn.com/apis/site/v2/sports/{sport}/injuries: every listed player per team
             with a status (Out, Injured Reserve, Suspension, 10/15/60-Day-IL, Questionable, Doubtful,
             Day-To-Day, Active) and the injury ("Ankle", "Upper Body"). NFL, NBA, NHL and MLB keep it
             current. CFB's feed is effectively empty (three entries, the newest from 2020), so CFB legs
             pass with "no CFB injury feed".
  starters   the scoreboard's probable starter per team: NHL probableStartingGoalie (status Confirmed or
             Expected), MLB probableStartingPitcher (no status field: "Probable").

verify_player_status(player_name, team, sport, side, stat=...) -> (is_safe, reason):
  OUT / IR / IL / SUSPENDED (or paternity/bereavement leave)      -> not safe, whatever the side
  QUESTIONABLE / DOUBTFUL / DAY-TO-DAY + "More" on a volume stat  -> not safe: limited minutes sink a
                                                                     More; a "Less" passes, flagged
  starter props (NHL saves; MLB pitcher strikeouts and pitching outs): not his team's probable
  starter, or no starter listed yet -> not safe
  otherwise safe: "Active", "Active / Confirmed Starter", "Q (Hamstring): Less only" ...
Every prop line in this pipeline is a counting stat (yards, points, shots, saves, attempts, hits...),
so all of them count as volume stats.

If ESPN can't be reached, players pass with "status unavailable" (an outage must not silence every
alert), except starter props (goalies, pitchers), whose starter then can't be confirmed: vetoed.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Awaitable, Callable

import aiohttp

from local import prize_picks_ev_scraper as scanner
from local.grade_slips import canon, name_key

ESPN = "https://site.api.espn.com/apis/site/v2/sports/{}"
PATHS = {"nfl": "football/nfl", "cfb": "football/college-football", "nba": "basketball/nba",
         "nhl": "hockey/nhl", "mlb": "baseball/mlb"}
NO_FEED = {"cfb"}  # checked 2026-10-16: 3 entries, newest 2020
# sport -> (stats that need the player to start, scoreboard probables name, status when none is given)
STARTERS = {"nhl": ({"saves"}, "probableStartingGoalie", "Expected"),
            "mlb": ({"strikeouts", "pitching outs"}, "probableStartingPitcher", "Probable")}  # pitcher K = "strikeouts"
ROLE = {"nhl": "goalie", "mlb": "pitcher"}
CACHE_S = 15 * 60
TIMEOUT_S = 8

HARD = ("out", "injured reserve", "suspension", "suspended", "-day-il", "day il", "paternity", "bereavement")
RISK = ("questionable", "doubtful", "day-to-day")
SHORT = {"questionable": "Q", "doubtful": "D", "day-to-day": "DTD"}

Get = Callable[[str], Awaitable[dict]]
_cache: dict[tuple, tuple[float, object]] = {}


async def http_get(url: str) -> dict:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT_S),
                                     headers={"User-Agent": "kalshi-shadow-kit injury_guard"}) as s:
        async with s.get(url) as r:
            r.raise_for_status()
            return await r.json(content_type=None)


async def _cached(key: tuple, fetch: Callable[[], Awaitable[object]]) -> object:
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_S:
        return hit[1]
    value = await fetch()
    _cache[key] = (time.time(), value)
    return value


def parse_injuries(feed: dict) -> dict[tuple, dict]:
    """{(team code, name key): {"status", "injury", "comment"}} from an ESPN injuries feed."""
    out = {}
    for team in feed.get("injuries") or []:
        for i in team.get("injuries") or []:
            a = i.get("athlete") or {}
            code = canon(((a.get("team") or {}).get("abbreviation")) or "")
            if not a.get("displayName"):
                continue
            details = i.get("details") or {}
            out[(code, name_key(a["displayName"]))] = {
                "status": (i.get("status") or "").strip(), "injury": details.get("type") or "",
                "comment": i.get("shortComment") or ""}
    return out


def parse_starters(scoreboard: dict, sport: str = "nhl") -> dict[str, tuple[str, str]]:
    """{team code: (starter name, "Confirmed" | "Expected" | "Probable")} from a scoreboard's probables."""
    _, kind, default = STARTERS[sport]
    out = {}
    for e in scoreboard.get("events") or []:
        for c in (e.get("competitions") or [{}])[0].get("competitors") or []:
            code = canon((c.get("team") or {}).get("abbreviation") or "")
            for p in c.get("probables") or []:
                if p.get("name") == kind and (p.get("athlete") or {}).get("displayName"):
                    out[code] = (p["athlete"]["displayName"], (p.get("status") or {}).get("name") or default)
    return out


async def injuries(sport: str, get: Get) -> dict[tuple, dict]:
    return await _cached(("injuries", sport), lambda: _fetch_injuries(sport, get))


async def _fetch_injuries(sport: str, get: Get) -> dict:
    return parse_injuries(await get(ESPN.format(PATHS[sport]) + "/injuries"))


async def starters(sport: str, day: str, get: Get) -> dict[str, tuple[str, str]]:
    """Probable starters (NHL goalies, MLB pitchers) for a US-Eastern date (YYYYMMDD)."""
    async def fetch():
        return parse_starters(await get(ESPN.format(PATHS[sport]) + f"/scoreboard?dates={day}"), sport)
    return await _cached(("starters", sport, day), fetch)


def _find(table: dict[tuple, dict], player: str, team: str) -> dict | None:
    k, code = name_key(player), canon(team)
    if (code, k) in table:
        return table[(code, k)]
    cands = [v for (c, n), v in table.items() if c == code and (set(k) <= set(n) or set(n) <= set(k))]
    return cands[0] if len(cands) == 1 else None


async def verify_player_status(player_name: str, team: str, sport: str, side: str, stat: str = "",
                               start_time: str = "", get: Get | None = None) -> tuple[bool, str]:
    """(is_safe, reason) for one leg. See the module docstring for the rules."""
    get = get or http_get
    sport = sport.lower()
    if sport not in PATHS:
        return True, "status unavailable (sport not covered)"
    starter_note = ""
    if stat in STARTERS.get(sport, (set(),))[0]:
        role = ROLE[sport]
        try:
            ts = scanner.Leg("x", stat, 0.5, "", start_time=start_time).start_ts or time.time()
            day = datetime.fromtimestamp(ts, scanner.EASTERN).strftime("%Y%m%d")
            probable = (await starters(sport, day, get)).get(canon(team))
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError, KeyError) as e:
            return False, f"starter unconfirmed (ESPN unavailable: {type(e).__name__})"
        if probable is None:
            return False, f"starter unconfirmed (no probable {role} listed yet)"
        if name_key(probable[0]) != name_key(player_name):
            return False, f"not the starter ({probable[0]} is the {probable[1].lower()} starting {role})"
        starter_note = f"{probable[1]} Starter"
    if sport in NO_FEED:
        return True, f"no {sport.upper()} injury feed"
    try:
        entry = _find(await injuries(sport, get), player_name, team)
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError, KeyError) as e:
        return True, f"status unavailable ({type(e).__name__})" + (f" / {starter_note}" if starter_note else "")
    status = (entry or {}).get("status", "").lower()
    injury = (entry or {}).get("injury") or "undisclosed"
    if entry is None or status in ("", "active", "probable"):
        return True, "Active" + (f" / {starter_note}" if starter_note else "")
    if any(h in status for h in HARD):
        return False, f"{entry['status'].upper()} ({injury})"
    if any(r in status for r in RISK):
        tag = SHORT.get(next(r for r in RISK if r in status), status.upper())
        if side == "More":
            return False, f"{tag} with {injury}: no More on a volume stat"
        return True, f"{tag} ({injury}): Less only" + (f" / {starter_note}" if starter_note else "")
    return True, f"{entry['status']} ({injury})" + (f" / {starter_note}" if starter_note else "")


async def batting_orders(day: str, get: Get) -> dict[tuple, int]:
    """{(team code, name key): lineup spot 1-9} for MLB games on a US-Eastern date, from each game summary's
    rosters once the lineup is posted (ESPN fills batOrder a few hours before first pitch; empty before)."""
    async def fetch():
        board = await get(ESPN.format(PATHS["mlb"]) + f"/scoreboard?dates={day}")
        out = {}
        for e in board.get("events") or []:
            summary = await get(ESPN.format(PATHS["mlb"]) + f"/summary?event={e['id']}")
            for t in summary.get("rosters") or []:
                code = canon((t.get("team") or {}).get("abbreviation") or "")
                for r in t.get("roster") or []:
                    name = (r.get("athlete") or {}).get("displayName")
                    if name and r.get("batOrder"):
                        out[(code, name_key(name))] = int(r["batOrder"])
        return out
    return await _cached(("lineups", day), fetch)


async def annotate_lineups(legs: list[scanner.Leg], get: Get | None = None) -> None:
    """Set bat_order on MLB legs whose lineup is posted (correlation.py's runs/RBI stack needs it).
    Never raises: a leg without a known spot simply can't be part of that stack."""
    get = get or http_get
    for day in {datetime.fromtimestamp(l.start_ts, scanner.EASTERN).strftime("%Y%m%d")
                for l in legs if l.sport == "mlb" and l.start_ts}:
        try:
            orders = await batting_orders(day, get)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError, KeyError):
            continue
        for l in legs:
            if l.sport == "mlb" and l.start_ts and datetime.fromtimestamp(l.start_ts, scanner.EASTERN).strftime("%Y%m%d") == day:
                l.bat_order = orders.get((canon(l.team), name_key(l.player)))


async def verify_legs(legs: list[scanner.Leg], get: Get | None = None) -> dict[int, tuple[bool, str]]:
    """{id(leg): (is_safe, reason)} for many legs at once (the feeds are cached, so this is cheap)."""
    results = await asyncio.gather(*(verify_player_status(l.player, l.team, l.sport, l.pick[0], l.stat,
                                                          l.start_time, get) for l in legs))
    return dict(zip(map(id, legs), results))
