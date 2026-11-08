"""PropLine logged-only shadow pricing (2026-11-06). NEVER used for selection.

Each board leg the live books path could price (same filter as fetch_odds.OddsBook._price: a stat with a market,
a team and a start time, a game starting more than START_BUFFER_MIN and at most window_h hours away) is priced from
PropLine (api.prop-line.com, free tier) and written to snapshots' propline_prices / propline_sweeps tables. Nothing
here touches a live Leg, quote, slip, stake, alert, the pool, the JIT step or the Odds API (no OddsBook, no
.odds_cache.json write): the sentinel runs Shadow.tick in the background AFTER each cycle() returns, and it reads
the board from the lines CSV itself.

Three fair probabilities per leg, all P(over), all logged only:
  p_live   the live method: pipeline_config devig_method (proportional) per book, fetch_odds.weighted over the
           live books (PIN/DK/FD/MGM, fetch_odds.WEIGHTS: Pinnacle 1.5), exact line on the MAIN market only
  p_shin   the same books and weights, Shin de-vig per book
  p_sharp  Novig 7 / Kalshi 7 / DK 1.4 / ProphetX 1.4 / Pinnacle 1.4 / FD 1 (MGM 0), live de-vig method; needs a
           sharp anchor (Novig, Kalshi or Pinnacle; ProphetX counts toward the books, not as an anchor) and >= 3 books
Every value is {"value": v} or {"value": None, "missing": reason}; every record carries CALC_VERSION.

Differences from The Odds API that this adapter handles (checked against live responses 2026-11-06, step 0):
  - header auth X-API-Key (never the apiKey query param, so the key is in no URL, log or error text)
  - American odds only (no oddsFormat); no regions param; integer-like event ids; sport key hockey_nhl
  - alternate and milestone lines arrive INSIDE the same market key (market.line_type main/alternate/milestone):
    only main is used for the fair probabilities, alternates are recorded as presence
  - NHL saves are goalie_saves; faceoffs won and time on ice have no PropLine market
  - DK/FD NHL player_goals main markets can be YES-only (name = player, point = None): no two-way price
  - quota is per request (not per market), X-Daily-* headers, hard reset at 00:00 UTC (16:00 PT in winter); the
    X-Daily-Used header was seen to lag our own count by one, so used = max(ours, header)
  - DFS books (PrizePicks, Underdog, Sleeper...) are never requested; if any arrive they are dropped before
    anything is parsed or stored, and only their presence is counted
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import math
import os
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

from local import devig as dv
from local import fetch_odds, pipeline_config
from local import prize_picks_ev_scraper as scanner

API = "https://api.prop-line.com/v1"
CALC_VERSION = "propline/1"
SOURCE = "propline"
LABEL = "shadow, PropLine, logged only: never a slip, never a price the live path uses"
USAGE = Path("local/.propline_usage.json")
SPORT_KEYS = {"nba": "basketball_nba", "nhl": "hockey_nhl"}
LIVE_BOOKS = tuple(fetch_odds.BOOKS)  # pinnacle, draftkings, fanduel, betmgm: the live consensus books
SHARP_WEIGHTS = {"novig": 7.0, "kalshi": 7.0, "draftkings": 1.4, "prophetx": 1.4, "pinnacle": 1.4, "fanduel": 1.0}
ANCHORS = ("novig", "kalshi", "pinnacle")
MIN_SHARP_BOOKS = 3
PRESENCE = ("novig", "prophetx", "pinnacle", "kalshi", "draftkings", "fanduel")
REQUEST_BOOKS = LIVE_BOOKS + ("novig", "prophetx", "kalshi")
DFS = {"prizepicks", "underdog", "sleeper", "dabble", "parlayplay", "pick6"}
# our stat -> PropLine market: the live map (fetch_odds.MARKETS) with PropLine's own keys where they differ
KEY_FIX = {("nhl", "player_total_saves"): "goalie_saves"}
NO_MARKET = {("nhl", "player_faceoffs_won"), ("nhl", "player_time_on_ice")}
# stats the live books path has no market for at all (PropLine's docs list them; never requested by live)
EXTRA = {"nba": {"fg made": "player_field_goals_made", "fg attempted": "player_field_goals_attempted",
                 "free throws made": "player_free_throws_made", "free throws attempted": "player_free_throws_attempted",
                 "3-pt attempted": "player_threes_attempted", "two pointers made": "player_twos_made",
                 "two pointers attempted": "player_twos_attempted", "offensive rebounds": "player_offensive_rebounds",
                 "defensive rebounds": "player_defensive_rebounds"}}


def markets(sport: str) -> dict[str, str]:
    live = {stat: KEY_FIX.get((sport, k), k) for stat, k in fetch_odds.MARKETS.get(sport, {}).items()
            if (sport, k) not in NO_MARKET}
    return {**live, **EXTRA.get(sport, {})}


def val(v, missing: str | None = None) -> dict:
    return {"value": v} if v is not None else {"value": None, "missing": missing or "unknown"}


def cfg() -> dict:
    return pipeline_config.CONFIG["propline"]


def utc_day(now: float) -> str:
    return datetime.fromtimestamp(now, timezone.utc).date().isoformat()


def ts(s: str | None) -> float | None:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() if s else None
    except ValueError:
        return None


# --------------------------------------------------------------------------- budget
class Budget:
    """The shared daily request count (both sentinels), in a JSON file under a file lock. A request is charged
    BEFORE it is sent (so two sentinels can't both take the last one), then reconciled with X-Daily-Used.
    The day is PropLine's: the UTC date (their quota resets at 00:00 UTC = 16:00 PT in winter)."""

    def __init__(self, path: Path = USAGE):
        self.path = path

    def _locked(self, fn):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path.with_suffix(".lock"), "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                try:
                    state = json.loads(self.path.read_text())
                except (OSError, ValueError):
                    state = {}
                out = fn(state)
                tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
                tmp.write_text(json.dumps(state))
                os.replace(tmp, self.path)
                return out
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    @staticmethod
    def _day(state: dict, now: float) -> dict:
        day = utc_day(now)
        if state.get("utc_day") != day:
            state.clear()
            state.update(utc_day=day, used=0, by_sport={}, by_kind={}, header_used=None, header_limit=None,
                         header_reset=None, stopped=None, extras_off=False)
        return state

    @staticmethod
    def limits(state: dict, sport: str) -> dict:
        c = cfg()
        stop_at = int(c["stop_at"])
        if state.get("header_limit"):
            stop_at = min(stop_at, int(state["header_limit"]) - 25)
        stop_at = min(stop_at, int(c["daily_cap"]))
        share = int(c["shares"].get(sport, 0) * stop_at + 1e-9)
        return {"stop_at": stop_at, "share": share, "sweep": int(share * (1 - c["close_share"]) + 1e-9)}

    def take(self, sport: str, kind: str, now: float) -> tuple[bool, str | None]:
        """Charge one request for `sport` ("events" and "sweep" spend the sweep share; "close" the whole share)."""
        def fn(state):
            self._day(state, now)
            if state.get("stopped"):
                return False, f"stopped: {state['stopped']}"
            lim = self.limits(state, sport)
            used = max(state["used"], state.get("header_used") or 0)
            mine = state["by_sport"].get(sport, 0)
            if used + 1 > lim["stop_at"]:
                return False, f"daily stop {used}/{lim['stop_at']}"
            if mine + 1 > (lim["share"] if kind == "close" else lim["sweep"]):
                return False, f"{sport} {kind} share {mine}/{lim['share'] if kind == 'close' else lim['sweep']}"
            state["used"] += 1
            state["by_sport"][sport] = mine + 1
            state["by_kind"][kind] = state["by_kind"].get(kind, 0) + 1
            return True, None
        return self._locked(fn)

    def reconcile(self, headers: dict, now: float, stop: str | None = None) -> dict:
        h = {k.lower(): v for k, v in (headers or {}).items()}

        def num(k):
            try:
                return int(float(h[k]))
            except (KeyError, ValueError, TypeError):
                return None

        def fn(state):
            self._day(state, now)
            if num("x-daily-used") is not None:
                state["header_used"] = max(state.get("header_used") or 0, num("x-daily-used"))
                state["used"] = max(state["used"], state["header_used"])
            for k, name in (("x-daily-limit", "header_limit"), ("x-daily-reset", "header_reset")):
                if num(k) is not None:
                    state[name] = num(k)
            if stop and not state.get("stopped"):
                state["stopped"] = stop
            return dict(state)
        return self._locked(fn)

    def set_flag(self, name: str, value, now: float) -> None:
        def fn(state):
            self._day(state, now)
            state[name] = value
        self._locked(fn)

    def snapshot(self, now: float) -> dict:
        return self._locked(lambda state: dict(self._day(state, now)))


# --------------------------------------------------------------------------- pricing (pure)
def strip_dfs(body: dict) -> tuple[dict, dict]:
    """Drop DFS books and DFS-flagged outcomes BEFORE anything reads them; return (body, presence counts)."""
    seen: Counter = Counter()
    keep = []
    for b in body.get("bookmakers") or []:
        if b.get("key") in DFS:
            seen[b["key"]] += sum(len(m.get("outcomes") or []) for m in b.get("markets") or [])
            continue
        for m in b.get("markets") or []:
            outs = []
            for o in m.get("outcomes") or []:
                if o.get("dfs_odds_type") is not None or o.get("payout_multiplier") is not None:
                    seen[f"{b.get('key')}:dfs_flagged"] += 1
                    continue
                outs.append(o)
            m["outcomes"] = outs
        keep.append(b)
    return {**body, "bookmakers": keep}, dict(seen)


def _player(o: dict, want: set) -> bool:
    return set(scanner.norm(o.get("description") or "").split()) == want


def book_quote(b: dict, market: str, player: str, line: float) -> dict:
    """One book's two-way main-line price for this leg, or why there is none."""
    want = set(scanner.norm(player).split())
    mains = [m for m in b.get("markets") or [] if m.get("key") == market and (m.get("line_type") or "main") == "main"]
    alts = [m for m in b.get("markets") or [] if m.get("key") == market and m.get("line_type") == "alternate"]
    rec = {"book": b.get("key"), "book_last_update": b.get("last_update")}
    if not mains and not alts:
        return {**rec, "missing": "market_not_listed"}
    sides, mine, yes_only, m_used = {}, [], False, None
    for m in mains:
        for o in m.get("outcomes") or []:
            if not _player(o, want):
                continue
            mine.append(o)
            if o.get("point") is None:
                yes_only = True
                continue
            if abs(float(o["point"]) - line) < 1e-9 and (o.get("name") or "").lower() in ("over", "under"):
                sides[o["name"].lower()] = o
                m_used = m
    alt_two = any(abs(float(o["point"]) - line) < 1e-9 for m in alts for o in m.get("outcomes") or []
                  if _player(o, want) and o.get("point") is not None)
    rec["alt_at_line"] = alt_two
    if "over" in sides and "under" in sides:
        over, under = float(sides["over"]["price"]), float(sides["under"]["price"])
        live = dv.two_way(over, under, "american", pipeline_config.CONFIG["devig_method"])
        shin = dv.two_way(over, under, "american", "shin")
        return {**rec, "over": over, "under": under, "implied_over": round(live.implied_over, 6),
                "implied_under": round(live.implied_under, 6), "overround": round(live.overround, 6),
                "fair_live": live.fair_over, "fair_shin": shin.fair_over,
                "live_method": live.method, "market_last_update": m_used.get("last_update"),
                "over_last_change_at": sides["over"].get("last_change_at"),
                "under_last_change_at": sides["under"].get("last_change_at"),
                "player_id": sides["over"].get("player_id")}
    if not mine:
        return {**rec, "missing": "player_not_listed" if not alt_two else "main_line_absent_alt_only"}
    if sides:
        return {**rec, "missing": "one_sided"}
    if yes_only and not any(o.get("point") is not None for o in mine):
        return {**rec, "missing": "yes_only_no_two_way"}
    return {**rec, "missing": "line_not_offered",
            "main_points": sorted({float(o["point"]) for o in mine if o.get("point") is not None})}


WHY_ORDER = ("line_not_offered", "one_sided", "yes_only_no_two_way", "main_line_absent_alt_only", "player_not_listed",
             "market_not_listed", "book_not_returned")


def why_unpriced(quotes: dict[str, dict]) -> str:
    """The most specific reason no book priced the leg (a book offering the player at another line beats a book
    not listing the market at all)."""
    seen = {q.get("missing") for q in quotes.values()}
    if seen <= {"market_not_listed", "book_not_returned"}:
        return "market_not_returned"
    return next((w for w in WHY_ORDER if w in seen), "no_book_two_sided")


def weighted_mean(pairs: list[tuple[str, float]], weights: dict, default: float) -> float:
    w = [weights.get(b, default) for b, _ in pairs]
    return sum(wi * p for wi, (_, p) in zip(w, pairs)) / sum(w)


def fair_records(quotes: dict[str, dict]) -> dict:
    """The three logged fair probabilities from per-book quotes (book key -> book_quote record)."""
    priced = {b: q for b, q in quotes.items() if "fair_live" in q}
    live_pairs = [(b, priced[b]["fair_live"]) for b in LIVE_BOOKS if b in priced]
    out = {}
    if live_pairs:
        p, names = fetch_odds.weighted(live_pairs)  # the live function: same weights (Pinnacle 1.5), same order
        shin = weighted_mean([(b, priced[b]["fair_shin"]) for b, _ in live_pairs], fetch_odds.WEIGHTS, 1.0)
        out["p_live"] = {**val(p), "books": names, "method": "weighted consensus of "
                         + pipeline_config.CONFIG["devig_method"] + " (fetch_odds.weighted, live books, main line)"}
        out["p_shin"] = {**val(shin), "books": names, "method": "weighted consensus of shin (live books, main line)"}
    else:
        why = "no_live_book_two_sided" if priced else why_unpriced(quotes)
        out["p_live"], out["p_shin"] = val(None, why), val(None, why)
    sharp = [(b, priced[b]["fair_live"]) for b in SHARP_WEIGHTS if b in priced]
    anchors = [b for b, _ in sharp if b in ANCHORS]
    if not priced:
        out["p_sharp"] = val(None, why_unpriced(quotes))
    elif not anchors:
        out["p_sharp"] = val(None, "no_sharp_anchor")
    elif len(sharp) < MIN_SHARP_BOOKS:
        out["p_sharp"] = val(None, "fewer_than_3_books")
    else:
        out["p_sharp"] = {**val(weighted_mean(sharp, SHARP_WEIGHTS, 0.0)), "books": [b for b, _ in sharp],
                          "anchors": anchors, "weights": {b: SHARP_WEIGHTS[b] for b, _ in sharp},
                          "method": "sharp-weighted consensus of " + pipeline_config.CONFIG["devig_method"]}
    for k in out:
        out[k]["calc_version"] = CALC_VERSION
    return out


def leg_row(l: scanner.Leg, sport: str, market: str | None, event: dict | None, body: dict | None,
            fetched_ts: float | None, now: float, read_kind: str, reason: str | None = None, dfs: dict | None = None) -> dict:
    """One propline_prices row. With no body (no read for this leg this sweep), every fair value is missing with
    `reason`."""
    row = {"ts": now, "sport": sport, "player": l.player, "stat": l.stat, "line": float(l.line), "team": l.team,
           "game": l.game, "start_ts": l.start_ts, "event_id": (event or {}).get("id"), "market_key": market,
           "read_kind": read_kind, "fetched_ts": fetched_ts, "dfs": dfs or {},
           "event": ({k: event.get(k) for k in ("id", "home_team", "away_team", "commence_time")} if event else
                     val(None, reason or "no_propline_event"))}
    if body is None:
        r = reason or "not_read"
        fair = {k: {**val(None, r), "calc_version": CALC_VERSION} for k in ("p_live", "p_shin", "p_sharp")}
        return {**row, "fair": fair, "books": {}, "presence": {b: val(None, r) for b in PRESENCE},
                "kalshi": val(None, r), "ages": {"data_age_s": val(None, r)}, "unpriced_reason": r}
    quotes = {b.get("key"): book_quote(b, market, l.player, float(l.line)) for b in body.get("bookmakers") or []}
    for b in REQUEST_BOOKS:
        quotes.setdefault(b, {"book": b, "missing": "book_not_returned"})
    fair = fair_records(quotes)
    contributors = [q for q in quotes.values() if "fair_live" in q]
    stamps = [ts(q.get("market_last_update")) for q in contributors]
    stamps = [s for s in stamps if s]
    ages = {"data_age_s": val(round(now - min(stamps), 1) if stamps else None, "no_contributor_timestamp"),
            "newest_age_s": val(round(now - max(stamps), 1) if stamps else None, "no_contributor_timestamp"),
            "fetch_age_s": val(round(now - fetched_ts, 1) if fetched_ts else None, "not_fetched")}
    presence = {b: ({"value": True} if "fair_live" in quotes[b] else {"value": False, "why": quotes[b].get("missing")})
                for b in PRESENCE}
    k = quotes.get("kalshi") or {}
    kalshi = ({**val(k["fair_live"]), "over": k["over"], "under": k["under"], "fair_shin": k["fair_shin"],
               "note": "PropLine's Kalshi quote; Build 4's direct-Kalshi shadow is separate"}
              if "fair_live" in k else val(None, k.get("missing", "book_not_returned")))
    unpriced = None if fair["p_live"]["value"] is not None else fair["p_live"]["missing"]
    return {**row, "fair": fair, "books": quotes, "presence": presence, "kalshi": kalshi, "ages": ages,
            "unpriced_reason": unpriced}


def match_events(legs: list[scanner.Leg], events: list[dict], sport: str) -> dict[str, list[scanner.Leg]]:
    """event id -> its legs, by team codes and the Eastern game date (the same test as OddsBook._price)."""
    out: dict[str, list[scanner.Leg]] = {}
    for e in events:
        start = ts(e.get("commence_time"))
        if not start:
            continue
        day = datetime.fromtimestamp(start, scanner.EASTERN).strftime("%y%b%d").upper()
        teams = fetch_odds.team_codes(sport, e.get("home_team") or "") | fetch_odds.team_codes(sport, e.get("away_team") or "")
        mine = [l for l in legs if l.team in teams and l.game_date == day]
        if mine:
            out[str(e["id"])] = mine
    return out


# --------------------------------------------------------------------------- the shadow
class Shadow:
    """Per-sentinel PropLine shadow. `get(url, params, headers) -> (status, json, headers)` is injectable."""

    def __init__(self, key: str, sport: str, get=None, budget: Budget | None = None, clock=time.time):
        self._key = key  # only ever placed in the X-API-Key header
        self.sport, self.sport_key = sport, SPORT_KEYS[sport]
        self.get = get or self._http_get
        self.budget = budget or Budget()
        self.clock = clock
        self.events: dict | None = None  # {"ts", "data"}
        self.last_read: dict[str, float] = {}  # event id -> last read time
        self.last_missing: float = 0.0  # when legs with no read were last written (once per sweep interval)
        self.busy = False
        self.disabled: str | None = None
        self.notes: list[str] = []
        self.calls = 0  # requests sent this tick (each one charged to the budget first)

    def __repr__(self) -> str:  # never shows the key
        return f"Shadow(sport={self.sport!r}, disabled={self.disabled!r})"

    @classmethod
    def from_env(cls, sport: str, **kw) -> "Shadow | None":
        key = os.getenv("PROPLINE_API_KEY", "").strip()
        return cls(key, sport, **kw) if key and sport in SPORT_KEYS else None

    async def _http_get(self, url: str, params: dict, headers: dict):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=cfg()["timeout_s"])) as s:
            async with s.get(url, params=params, headers=headers) as r:
                return r.status, await r.json(content_type=None), dict(r.headers)

    async def call(self, path: str, params: dict, kind: str, now: float, errors: Counter) -> dict | list | None:
        ok, why = self.budget.take(self.sport, kind, now)
        if not ok:
            errors[f"budget:{why.split(':')[0].split(' ')[0]}"] += 1
            self.notes.append(f"budget: {why}")
            return None
        self.calls += 1
        try:
            status, body, headers = await self.get(f"{API}{path}", params,
                                                   {"X-API-Key": self._key, "Accept": "application/json"})
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            errors[f"network:{type(e).__name__}"] += 1
            return None
        detail = body.get("detail") if isinstance(body, dict) else None
        code = (detail.get("error") if isinstance(detail, dict) else None) or (body.get("error") if isinstance(body, dict) else None)
        stop = None
        if status == 401:
            self.disabled = f"auth:{code or 401}"
        elif status == 403:
            stop = f"upgrade_required:{path.split('/')[-1]}"
        elif status == 429 and code != "burst_limit_exceeded":
            stop = f"daily_limit:{code or 429}"
        self.budget.reconcile(headers, now, stop)
        if status != 200:
            errors[f"http{status}:{code or ''}"] += 1
            return {"_status": status, "_code": code}
        return body

    async def _events(self, now: float, errors: Counter) -> list[dict]:
        if self.events and now - self.events["ts"] < cfg()["events_ttl_min"] * 60:
            return self.events["data"]
        got = await self.call(f"/sports/{self.sport_key}/events", {}, "events", now, errors)
        if isinstance(got, list):
            self.events = {"ts": now, "data": got}
        return self.events["data"] if self.events else []

    def due(self, event_id: str, start: float, now: float) -> str | None:
        c, last = cfg(), self.last_read.get(event_id)
        if start - now <= c["close_window_min"] * 60 and (last is None or now - last >= c["close_min_gap_min"] * 60):
            return "close"
        if last is None or now - last >= c["sweep_min"] * 60:
            return "sweep"
        return None

    async def tick(self, csv_path: Path, snaps, now: float | None = None, board_ts: float | None = None) -> dict | None:
        """One shadow pass: read what is due, write rows and one sweep record. Never raises."""
        if self.disabled or self.busy:
            return None
        self.busy = True
        try:
            return await self._tick(csv_path, snaps, self.clock() if now is None else now, board_ts)
        except Exception as err:  # noqa: BLE001 — a shadow must never stop the sentinel
            print(f"{time.strftime('%H:%M:%S')} PropLine shadow ({self.sport.upper()}) failed: {type(err).__name__}")
            return None
        finally:
            self.busy = False

    async def _tick(self, csv_path: Path, snaps, now: float, board_ts: float | None) -> dict | None:
        self.notes, self.calls = [], 0
        c, mk = cfg(), markets(self.sport)
        try:
            legs = [l for l in scanner.load_legs(str(csv_path)) if not l.promo]
        except (OSError, ValueError, KeyError):
            return None
        for l in legs:
            l.sport = self.sport
        window = [l for l in legs if l.start_ts and l.team
                  and now + scanner.START_BUFFER_MIN * 60 < l.start_ts <= now + c["window_h"] * 3600]
        if not window:
            return None
        errors: Counter = Counter()
        state = self.budget.snapshot(now)
        extras_off = state.get("extras_off")
        priceable = [l for l in window if l.stat in mk and not l.whole_line
                     and not (extras_off and l.stat in EXTRA.get(self.sport, {}))]
        events = await self._events(now, errors) if priceable else []
        by_event = match_events(priceable, events, self.sport)
        ev_by_id = {str(e["id"]): e for e in events}
        rows, requested, kinds = [], 0, Counter()
        read_ids: set[int] = set()
        for eid, mine in sorted(by_event.items(), key=lambda kv: ts(ev_by_id[kv[0]].get("commence_time")) or 0):
            e = ev_by_id[eid]
            kind = self.due(eid, ts(e["commence_time"]), now)
            if kind is None:
                continue
            want = sorted({mk[l.stat] for l in mine})
            body = await self.call(f"/sports/{self.sport_key}/events/{eid}/odds",
                                   {"markets": ",".join(want), "bookmakers": ",".join(REQUEST_BOOKS)}, kind, now, errors)
            if body is None and self.disabled:
                break
            if body is None:
                continue  # budget or network: no read, those legs fall to the no-read rows below
            requested += 1
            if body.get("_status") == 422 and not extras_off and any(m in EXTRA.get(self.sport, {}).values() for m in want):
                self.budget.set_flag("extras_off", True, now)  # PropLine refused a non-live market key: drop them today
                extras_off = True
                continue
            if "_status" in body:
                continue
            fetched = self.clock()
            self.last_read[eid] = now
            body, dfs = strip_dfs(body)
            kinds[kind] += 1
            for l in mine:
                rows.append(leg_row(l, self.sport, mk[l.stat], e, body, fetched, now, kind, dfs=dfs))
                read_ids.add(id(l))
        write_missing = requested > 0 or now - self.last_missing >= c["sweep_min"] * 60
        if write_missing:
            self.last_missing = now
            matched = {id(l) for v in by_event.values() for l in v}
            for l in window:
                if id(l) in read_ids:
                    continue
                if l.whole_line:
                    why = "whole_line_push_unpriced"
                elif l.stat not in mk:
                    why = "no_propline_market"
                elif extras_off and l.stat in EXTRA.get(self.sport, {}):
                    why = "extra_market_refused"
                elif id(l) not in matched:
                    why = "no_propline_event" if events else "no_event_list"
                elif errors:
                    why = "request_refused_or_failed"  # budget, network or HTTP error: see the sweep's errors
                else:
                    continue  # read on an earlier sweep and not due yet: that read stands, no row
                rows.append(leg_row(l, self.sport, mk.get(l.stat), None, None, None, now, "none", reason=why))
        if not rows and not self.calls and not errors:
            return None
        state = self.budget.snapshot(now)
        lim = Budget.limits(state, self.sport)
        priced = sum(1 for r in rows if r["fair"]["p_live"]["value"] is not None)
        sweep = {"ts": now, "sport": self.sport, "utc_day": state.get("utc_day"), "requests": self.calls,
                 "kinds": dict(kinds), "x_daily_used": state.get("header_used"), "x_daily_limit": state.get("header_limit"),
                 "x_daily_reset": state.get("header_reset"), "budget_used": state.get("used"),
                 "budget_sport": state.get("by_sport", {}).get(self.sport, 0), "limits": lim,
                 "events_listed": len(events), "events_matched": len(by_event), "events_requested": requested,
                 "legs_in_window": len(window), "legs_read": len(read_ids), "legs_priced": priced,
                 "unpriced": dict(Counter(r["unpriced_reason"] for r in rows if r["unpriced_reason"])),
                 "errors": dict(errors), "stopped": state.get("stopped") or self.disabled,
                 "odds_api_usage": odds_api_usage(), "board_ts": board_ts, "calc_version": CALC_VERSION}
        if snaps is not None:
            sid = snaps.propline_sweep(sweep)
            snaps.propline_prices(sid, board_ts, rows)
        if self.calls or errors:
            print(f"{time.strftime('%H:%M:%S')} PropLine shadow ({self.sport.upper()}): {self.calls} request(s), "
                  f"{priced}/{len(window)} in-window legs priced, budget {sweep['budget_sport']}/{lim['share']} "
                  f"{self.sport.upper()} · {state.get('used')}/{lim['stop_at']} today (UTC day: resets 00:00 UTC)"
                  + (f", errors {dict(errors)}" if errors else "") + (f", stopped: {sweep['stopped']}" if sweep["stopped"] else ""))
        return sweep


def odds_api_usage(path: Path = fetch_odds.CACHE) -> dict:
    """Today's Odds API spend as the live cache last recorded it: READ only (never written here), for the cost
    comparison. Missing with a reason when the file can't be read."""
    try:
        u = json.loads(path.read_text()).get("usage") or {}
        return {"date": u.get("date"), "used": u.get("used"), "by_group": u.get("by_group"), "cap": u.get("cap")}
    except (OSError, ValueError) as err:
        return val(None, f"unreadable:{type(err).__name__}")


def p_quantile(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, max(0, math.ceil(q * len(xs)) - 1))]
