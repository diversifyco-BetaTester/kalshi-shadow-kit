"""
prize_picks_ev_scraper.py — price PrizePicks legs with Kalshi + Polymarket, rank +EV entries.

PrizePicks pays fixed multipliers, so a leg is +EV when the market's probability beats the
entry's break-even (2-pick Power at 3x needs each leg above sqrt(1/3) = 57.7%).

INPUT  a CSV of the PrizePicks lines you see in the app (we don't scrape PrizePicks):
           player,stat,line,game
           Ja'Marr Chase,receiving yards,74.5,CIN@BAL
       `stat` must be words that appear in the Kalshi/Polymarket market title.
       Optional columns kalshi_ticker / poly_slug pin a market and skip auto-matching.
DATA   Kalshi: read-only signed client from this repo (prod keys via .env, KALSHI_ENV=prod).
       Polymarket: public Gamma API, no key.
OUTPUT every matched leg with its fair probability, then the best entries by EV.

  python3 -m local.prize_picks_ev_scraper --list-series NFL          # find Kalshi prop series
  python3 -m local.prize_picks_ev_scraper lines.csv --kalshi-series KXNFLRECYDS KXNFLRUSHYDS

Assumptions to know: legs are treated as independent (two legs from the same game usually
aren't), mid-price is used as fair probability, and the payout table below is a default —
edit it to what the app shows today. Demon/goblin lines (CSV column odds_type) are priced on
"More" only, the one side PrizePicks offers on them, and never enter an entry's EV: their
multipliers aren't the table below.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import itertools
import json
import math
import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp

from kalshi.client import KalshiClient
from local import devig, pipeline_config
from kalshi.config import load_settings

# entry type -> {legs: {hits: multiplier}}. Defaults only: CHECK THE APP and edit.
PAYOUTS = pipeline_config.CONFIG["payouts"]  # local/pipeline_config.py (defaults) + pipeline_config.json
PROMO = {"demon", "goblin"}  # odds_type values PrizePicks only lets you take "More" on
START_BUFFER_MIN = 10  # a leg whose game starts sooner than this is dropped: a live Kalshi book isn't a pre-game price
EASTERN = ZoneInfo("America/New_York")  # Kalshi's ticker dates
MAX_SPREAD = 0.10  # ignore a market whose bid/ask is wider than 10c: its mid isn't a price
INTERPOLATE_STATS = {"passing yards", "rushing yards", "receiving yards"}  # wide rung gaps (10-25 yds)
INTERPOLATE_MAX_GAP = 25  # yards between the two rungs used; wider (missing/illiquid rungs) is a guess
GAMMA = "https://gamma-api.polymarket.com"


@dataclass
class Quote:
    venue: str
    ref: str
    text: str
    p_yes: float   # mid-price probability that the market's YES happens
    spread: float
    title: str = ""  # Kalshi's own title ("Player: N+ stat"), compared exactly when present
    interpolated: bool = False  # priced between two Kalshi rungs, not read off one market
    detail: dict | None = None  # how p_yes was made: raw two-way prices, implied, fair, method (devig.TwoWay)
    p_push: float | None = None  # whole-number line only: P(stat == line), from two adjacent market rungs

    @property
    def game_date(self) -> str:
        """Kalshi's game date from the ticker, e.g. "26OCT13" in KXNHLAST-26OCT13COLDAL-...; "" if none."""
        parts = self.ref.split("-")
        m = re.match(r"\d{2}[A-Z]{3}\d{2}", parts[1]) if self.venue == "kalshi" and len(parts) > 1 else None
        return m.group(0) if m else ""


@dataclass
class Leg:
    player: str
    stat: str
    line: float
    game: str
    pin_kalshi: str = ""
    pin_poly: str = ""
    quotes: list[Quote] = field(default_factory=list)
    team: str = ""  # the player's team; PrizePicks entries need players from 2+ teams
    odds_type: str = "standard"  # "demon"/"goblin": More only, and not on the standard payout table
    start_time: str = ""  # ISO 8601 from board.json; empty for pasted text
    sport: str = ""  # set by the sentinel; tells a cross-sport slip's legs apart
    position: str = ""  # PrizePicks' position ("G", "F-C", "SP", "QB"...): correlation roles
    bat_order: int | None = None  # MLB lineup spot once ESPN posts the lineup (injury_guard.annotate_lineups)
    shade: float = 0.0  # points the picked side's probability is pulled toward 50% (integrity.shade): price uncertainty

    @property
    def game_date(self) -> str:
        """The start date in Kalshi's ticker form and timezone (US Eastern), e.g. "26OCT14"."""
        ts = self.start_ts
        return datetime.fromtimestamp(ts, EASTERN).strftime("%y%b%d").upper() if ts else ""

    @property
    def start_ts(self) -> float | None:
        try:
            return datetime.fromisoformat(self.start_time.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None

    @property
    def promo(self) -> bool:
        return self.odds_type in PROMO

    @property
    def whole_line(self) -> bool:
        """A whole-number line (1 TD, 5 rush attempts) pushes when the stat lands on it: the leg is
        removed and the entry pays as a smaller one. One win probability can't express that (pricing
        "Less 1 TD" as 1 - P(2+) counts every push as a win: a fake +170% NFL slip), so a whole line
        enters slips only with a market-priced push probability (p_push: two adjacent rungs)."""
        return abs(self.line % 1) < 1e-9

    @property
    def p_push(self) -> float | None:
        """P(the stat lands exactly on the line): 0 for a half line; for a whole line the mean of its
        quotes' push prices, or None when any quote lacks one (no adjacent rung: not priceable)."""
        if not self.whole_line:
            return 0.0
        ps = [q.p_push for q in self.quotes]
        return sum(ps) / len(ps) if ps and all(p is not None for p in ps) else None

    @property
    def push_unpriced(self) -> bool:
        """A whole line without a market push price: it stays out of every slip."""
        return self.whole_line and self.p_push is None

    @property
    def threshold(self) -> int:
        """PrizePicks 74.5 'More' == a prediction market's '75+'."""
        return math.floor(self.line) + 1

    @property
    def p_over(self) -> float | None:
        return sum(q.p_yes for q in self.quotes) / len(self.quotes) if self.quotes else None

    @property
    def pick(self) -> tuple[str, float] | None:
        """(side, fair probability). A half line: P(side). A push-priced whole line: P(side wins | no
        push) = P(side) / (1 - p_push), the same quantity a graded leg's hit rate measures (pushes are
        removed), so floors, ranking, CLV and calibration compare like with like; the exact slip EV
        uses P(win) and p_push separately (optimizer.leg_prob). p_over is P(More wins) either way.
        Shaded: the side's probability less `shade` points, never below 50% (raw_pick is the market's own)."""
        k = self.raw_pick
        if k is None or not self.shade or k[1] <= 0.5:
            return k
        return k[0], max(0.5, k[1] - self.shade)

    @property
    def raw_pick(self) -> tuple[str, float] | None:
        """pick before shading: what the market says, the price CLV is measured on."""
        p = self.p_over
        if p is None:
            return None
        push = self.p_push if self.whole_line else 0.0
        if not push:  # half line, or a whole line nobody priced (kept out of slips: push_unpriced)
            if self.promo:  # no "Less" button on demons/goblins: never price or suggest it
                return "More", p
            return ("More", p) if p >= 0.5 else ("Less", 1 - p)
        decided = 1 - push
        less = max(decided - p, 0.0)
        if decided <= 0:
            return None
        if self.promo or p >= less:
            return "More", p / decided
        return "Less", less / decided

    @property
    def p_win(self) -> float | None:
        """Unconditional P(our side wins): pick probability x (1 - p_push)."""
        k = self.pick
        return None if k is None else k[1] * (1 - (self.p_push or 0.0))


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9+ ]", " ", s)


STAT_WORDS = {"pts": "points", "rebs": "rebounds", "reb": "rebounds", "asts": "assists", "ast": "assists",
              "blks": "blocks", "blk": "blocks", "stls": "steals", "stl": "steals"}


def stat_key(s: str) -> frozenset[str]:
    """A stat phrase as a word set, so wording differences don't block a match: "Pts+Rebs+Asts",
    "points + rebounds + assists" and "points, rebounds and assists" agree; "3-PT Made", "threes" and
    "three pointers" agree; "blocked shots" == "blocks". Different stats stay different ("hits" vs
    "hits allowed"). Kalshi had only ever listed NBA points when this was written (2026-10-14), so the
    other NBA titles' wording was unknown."""
    words = [STAT_WORDS.get(w, w) for w in norm(s).replace("+", " ").split()]
    ws = set(words) - {"and", "made", "player"}
    if ws & {"three", "3", "3pt", "threes", "3pm"} and (ws & {"pointers", "point", "pt", "threes", "3pt", "3pm"}):
        ws = (ws - {"three", "3", "3pt", "threes", "3pm", "pointers", "point", "pt", "field", "goals"}) | {"threes"}
    if "blocked" in ws and "shots" in ws:
        ws = (ws - {"blocked", "shots"}) | {"blocks"}
    return frozenset(ws)


KALSHI_TITLE = re.compile(r"(?P<player>[^:]+): (?P<rung>\d+)\+ (?P<stat>[^?]+)\??")  # "Jo Adell: 2+ hits?"
KALSHI_SOA = re.compile(r"(?P<player>[^:]+): score or assist\??")  # soccer: 1+ goals or assists, no rung


def matches(leg: Leg, text: str, title: str = "") -> bool:
    """Same player, same stat, same threshold.

    Kalshi titles ("Player: N+ stat") are compared on the exact stat phrase, so "hits" never
    matches "hits allowed" or "hits + runs + RBIs". Other titles fall back to: player name,
    every stat word, and the threshold all appear somewhere.
    """
    soa = KALSHI_SOA.fullmatch(title.strip())
    if soa:  # "Player: score or assist?" == PrizePicks Goal + Assist 0.5 (1+ goals plus assists)
        return (leg.threshold == 1 and stat_key(leg.stat) == stat_key("goal+assist")
                and set(norm(leg.player).split()) <= set(norm(soa["player"]).split()))
    k = KALSHI_TITLE.fullmatch(title.strip())
    if k:
        return (int(k["rung"]) == leg.threshold
                and stat_key(k["stat"]) == stat_key(leg.stat)
                and set(norm(leg.player).split()) <= set(norm(k["player"]).split()))
    t = norm(text)
    words = set(t.split())
    if not all(w in words for w in norm(leg.player).split()):
        return False
    if not all(w in t for w in norm(leg.stat).split()):
        return False
    return str(leg.threshold) in re.findall(r"\d+", t)


def mid(bid: float | None, ask: float | None) -> tuple[float, float] | None:
    if bid is None or ask is None or not 0 < ask <= 1 or not 0 <= bid < ask:
        return None
    return (bid + ask) / 2, ask - bid


def kalshi_fair(bid: float | None, ask: float | None) -> tuple[float, float, dict] | None:
    """(fair P(YES), spread, record) from a Kalshi book read as a two-way price: buying YES costs the YES
    ask, buying NO costs 1 - YES bid. The two asks are de-vigged, not averaged: proportional when the
    midpoint is inside pipeline_config kalshi_devig "band" (15-85c), power outside it. The record keeps
    both sides, implied, the fair value used, and BOTH methods' fair values (fair_proportional,
    fair_power) so the backtest can test which predicted closing prices better."""
    if mid(bid, ask) is None or not 0 < bid < ask < 1:
        return None  # one side empty (YES ask $1.00 or YES bid $0): not a two-way price
    rule = pipeline_config.CONFIG["kalshi_devig"]
    lo, hi = rule["band"]
    method = rule["inner"] if lo <= (bid + ask) / 2 <= hi else rule["outer"]
    try:
        both = {m: devig.two_way(ask, 1 - bid, "prob", m) for m in {"proportional", "power", method}}
    except ValueError:
        return None
    t = both[method]
    return t.fair_over, ask - bid, {"source": "kalshi", **t.record(), "fair_proportional": round(both["proportional"].fair_over, 6),
                                    "fair_power": round(both["power"].fair_over, 6)}


def dollars(m: dict, key: str) -> float | None:
    """Kalshi has shipped both `yes_bid` (cents) and `yes_bid_dollars`."""
    if m.get(f"{key}_dollars") is not None:
        return float(m[f"{key}_dollars"])
    return m[key] / 100 if m.get(key) is not None else None


def _count(m: dict, key: str) -> float | None:
    """A contract count Kalshi ships as `volume_fp` ("123.00") or `volume` (123); None when absent."""
    for k in (f"{key}_fp", key):
        try:
            if m.get(k) is not None:
                return float(m[k])
        except (TypeError, ValueError):
            pass
    return None


# --------------------------------------------------------------------------- data
async def kalshi_quotes(series: list[str]) -> list[Quote]:
    out = []
    async with KalshiClient(load_settings()) as k:
        for s in series:
            async for m in k.paginate("/markets", "markets", {"series_ticker": s, "status": "open"}):
                q = kalshi_fair(dollars(m, "yes_bid"), dollars(m, "yes_ask"))
                if q:
                    text = " ".join(str(m.get(f, "")) for f in ("title", "subtitle", "yes_sub_title"))
                    # contracts traded / open: integrity's thin-book flag and the shading (None: not reported)
                    detail = {**q[2], "volume": _count(m, "volume"), "open_interest": _count(m, "open_interest")}
                    out.append(Quote("kalshi", m["ticker"], text, q[0], q[1], title=m.get("title", ""), detail=detail))
    return out


def is_binary(m: dict) -> bool:
    """Only Yes/No markets map onto a single Kalshi YES/NO (or More/Less)."""
    return [o.lower() for o in json.loads(m.get("outcomes") or "[]")] == ["yes", "no"]


async def gamma_markets(s: aiohttp.ClientSession, tag: str):
    """Yield (event, market) for every open Polymarket market under a tag slug."""
    offset = 0
    while True:
        params = {"tag_slug": tag, "closed": "false", "limit": 100, "offset": offset}
        async with s.get(f"{GAMMA}/events", params=params) as r:
            r.raise_for_status()
            events = await r.json()
        for ev in events:
            for m in ev.get("markets", []):
                yield ev, m
        if len(events) < 100:
            return
        offset += 100


async def gamma_market(s: aiohttp.ClientSession, slug: str) -> dict | None:
    async with s.get(f"{GAMMA}/markets", params={"slug": slug}) as r:
        r.raise_for_status()
        found = await r.json()
    return found[0] if found else None


async def poly_quotes(tag: str) -> list[Quote]:
    out = []
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
        async for ev, m in gamma_markets(s, tag):
            if not is_binary(m):
                continue
            q = mid(_f(m.get("bestBid")), _f(m.get("bestAsk")))
            if q:
                out.append(Quote("poly", m.get("slug", ""), f"{ev.get('title', '')} {m.get('question', '')}", *q))
    return out


def _f(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


async def ladders(series: list[str], day: str, min_p: float, max_spread: float, min_volume: float) -> None:
    """Cheat sheet: per player, the PrizePicks lines at which More / Less clear `min_p` on Kalshi.

    Kalshi lists each prop as rungs ("75+", "100+", ...). A PrizePicks More at line L wins when
    the stat reaches L+0.5, so More on any line <= (rung - 0.5) is at least as likely as that rung.
    Less at L wins when the stat stays below L+0.5, so Less on any line >= (rung - 0.5) is at
    least 1 - P(rung+). Only rungs with a tight, traded book are used.
    """
    rows = []
    async with KalshiClient(load_settings()) as k:
        for s in series:
            groups: dict[str, list] = {}
            async for m in k.paginate("/markets", "markets", {"series_ticker": s, "status": "open"}):
                if day not in m["event_ticker"]:
                    continue
                q = mid(dollars(m, "yes_bid"), dollars(m, "yes_ask"))
                vol = float(m.get("volume_fp") or m.get("volume") or 0)
                rung = re.search(r"(\d+)\+", m.get("yes_sub_title") or "")
                if q and rung and q[1] <= max_spread and vol >= min_volume:
                    player = (m.get("yes_sub_title") or "").split(":")[0]
                    groups.setdefault(f"{player}|{m['title'].split('+', 1)[1].strip()}", []).append((int(rung.group(1)), q[0], vol))
            for key, rungs in groups.items():
                player, stat = key.split("|")
                rungs.sort()
                more = [(t, p) for t, p, _ in rungs if p >= min_p]
                less = [(t, p) for t, p, _ in rungs if 1 - p >= min_p]
                rows.append((sum(v for *_, v in rungs), player, stat, more[-1] if more else None,
                             less[0] if less else None, rungs))
    rows.sort(key=lambda r: -r[0])
    print(f"{'player':24} {'stat':18} {'More if PP line <=':>20} {'Less if PP line >=':>20}  traded rungs (threshold: P)")
    for vol, player, stat, more, less, rungs in rows:
        mo = f"{more[0] - 0.5:g}  ({more[1]:.0%})" if more else "-"
        le = f"{less[0] - 0.5:g}  ({1 - less[1]:.0%})" if less else "-"
        print(f"{player[:24]:24} {stat[:18]:18} {mo:>20} {le:>20}  " + " ".join(f"{t}:{p:.0%}" for t, p, _ in rungs))


async def list_series(word: str) -> None:
    async with KalshiClient(load_settings()) as k:
        d = await k.get("/series", {"category": "Sports"})
        for s in d.get("series", []) or []:
            if word.lower() in f"{s.get('ticker', '')} {s.get('title', '')}".lower():
                print(f"{s['ticker']:28} {s.get('title', '')}")


# --------------------------------------------------------------------------- math
def hit_distribution(ps: list[float]) -> list[float]:
    """P(exactly k legs hit) for independent legs (Poisson-binomial)."""
    dist = [1.0]
    for p in ps:
        dist = [a * (1 - p) + b * p for a, b in zip(dist + [0.0], [0.0] + dist)]
    return dist


def entry_ev(ps: list[float], table: dict[int, float]) -> float:
    """Expected return per $1 entered, minus the $1."""
    dist = hit_distribution(ps)
    return sum(dist[k] * mult for k, mult in table.items()) - 1


def break_even(mult: float, n: int) -> float:
    return mult ** (-1 / n)


# --------------------------------------------------------------------------- main
def allowed(combo: tuple[Leg, ...]) -> bool:
    """PrizePicks rules: different players, and players from at least two different teams.
    Opposing players from one game are fine. With no team column (older CSVs) only the
    different-players rule can be checked."""
    if len({l.player for l in combo}) < len(combo):
        return False
    teams = [l.team for l in combo]
    return not all(teams) or len(set(teams)) >= 2


def rank_entries(good: list[Leg]) -> list[tuple[float, str, tuple[Leg, ...], bool]]:
    """Every Power/Flex entry PrizePicks allows from `good`, best EV first: (ev, kind, legs, same_game).
    same_game is a warning, not a filter: legs from one game are correlated, so the EV (which
    assumes independent legs) is only approximate. Demon/goblin legs are left out: PAYOUTS is
    the standard table, and theirs differ."""
    good = [l for l in good if not l.promo and not l.whole_line]
    entries = []
    for kind, by_n in PAYOUTS.items():
        for n, table in by_n.items():
            for combo in itertools.combinations(good, n):
                if not allowed(combo):
                    continue
                ev = entry_ev([l.pick[1] for l in combo], table)
                same_game = len({l.game for l in combo if l.game}) < sum(1 for l in combo if l.game)
                entries.append((ev, kind, combo, same_game))
    return sorted(entries, key=lambda e: -e[0])


def load_legs(path: str) -> list[Leg]:
    with open(path, newline="") as f:
        return [Leg(r["player"].strip(), r["stat"].strip(), float(r["line"]), r.get("game", "").strip(),
                    (r.get("kalshi_ticker") or "").strip(), (r.get("poly_slug") or "").strip(),
                    team=(r.get("team") or "").strip(),
                    odds_type=(r.get("odds_type") or "standard").strip().lower(),
                    start_time=(r.get("start_time") or "").strip(), position=(r.get("position") or "").strip())
                for r in csv.DictReader(f)]


def pregame(legs: list[Leg], now: float | None = None, buffer_min: float = START_BUFFER_MIN) -> tuple[list[Leg], int]:
    """Legs whose game starts more than buffer_min from now, and how many were dropped. Once a game
    is live its Kalshi rungs go to 1c/99c as stats come in, which reads as a huge fake edge. Legs
    with no start time (pasted text) are kept: nothing says they started."""
    cutoff = (time.time() if now is None else now) + buffer_min * 60
    keep = [l for l in legs if l.start_ts is None or l.start_ts > cutoff]
    return keep, len(legs) - len(keep)


def interpolate(leg: Leg, quotes: list[Quote]) -> Quote | None:
    """Price a line that falls between two Kalshi rungs, e.g. 244.5 passing yards (needs 245+)
    from the 225+ and 250+ markets.

    Uses the two nearest tradeable rungs on either side, both mid-priced, and interpolates in
    log-odds (smooth and always between the two rung prices, unlike a straight line near 0 or 1).
    Never extrapolates past the outermost rungs. The result's spread is the wider of the two.
    """
    if " ".join(norm(leg.stat).split()) not in INTERPOLATE_STATS:
        return None
    rungs = []
    for q in quotes:
        k = KALSHI_TITLE.fullmatch(q.title.strip()) if q.venue == "kalshi" else None
        if (k and q.spread <= MAX_SPREAD and stat_key(k["stat"]) == stat_key(leg.stat)
                and set(norm(leg.player).split()) <= set(norm(k["player"]).split())):
            rungs.append((int(k["rung"]), q))
    below = max(((r, q) for r, q in rungs if r < leg.threshold), default=None, key=lambda x: x[0])
    above = min(((r, q) for r, q in rungs if r > leg.threshold), default=None, key=lambda x: x[0])
    if not (below and above):
        return None
    (a, qa), (b, qb) = below, above
    if b - a > INTERPOLATE_MAX_GAP:
        return None
    logit = lambda p: math.log(p / (1 - p))
    la, lb = logit(min(max(qa.p_yes, 0.01), 0.99)), logit(min(max(qb.p_yes, 0.01), 0.99))
    p = 1 / (1 + math.exp(-(la + (leg.threshold - a) / (b - a) * (lb - la))))
    note = f"interpolated {leg.threshold}+ from {a}+ {qa.p_yes:.1%} and {b}+ {qb.p_yes:.1%}"
    detail = {"source": "kalshi", "method": "log-odds interpolation between Kalshi rungs", "fair_over": round(p, 6),
              "target_rung": leg.threshold,
              "rungs": [{"rung": a, "fair": round(qa.p_yes, 6), **(qa.detail or {})},
                        {"rung": b, "fair": round(qb.p_yes, 6), **(qb.detail or {})}]}
    return Quote("kalshi", f"{qa.ref} ~ {b}", note, p, max(qa.spread, qb.spread), interpolated=True, detail=detail)


def kalshi_rung(leg: Leg, q: Quote) -> int | None:
    """The N of a Kalshi "Player: N+ stat" market for this leg's player and stat, else None."""
    k = KALSHI_TITLE.fullmatch(q.title.strip()) if q.venue == "kalshi" else None
    if (k and stat_key(k["stat"]) == stat_key(leg.stat)
            and set(norm(leg.player).split()) <= set(norm(k["player"]).split())):
        return int(k["rung"])
    return None


def whole_line_quote(leg: Leg, win: Quote, quotes: list[Quote]) -> Quote | None:
    """A whole line N priced from two adjacent Kalshi rungs of one read: More wins on (N+1)+ (the market
    `win`, already matched), the push is P(N+) - P((N+1)+), Less wins on 1 - P(N+). None (the leg stays
    unpriced) without a tradeable N+ rung, or when the two rungs cross (N+ below (N+1)+: no push price)."""
    if win.venue != "kalshi" or kalshi_rung(leg, win) != leg.threshold:
        return None
    n = leg.threshold - 1
    base = next((q for q in quotes if q.spread <= MAX_SPREAD and q.game_date == win.game_date
                 and kalshi_rung(leg, q) == n), None) if n >= 1 else None
    if base is None:
        return None
    push = base.p_yes - win.p_yes
    if push < 0:
        return None
    detail = {"source": "kalshi", "method": "whole line: adjacent Kalshi rungs", "fair_over": round(win.p_yes, 6),
              "p_push": round(push, 6), "rungs": [{"rung": n, "fair": round(base.p_yes, 6), **(base.detail or {})},
                                                    {"rung": n + 1, "fair": round(win.p_yes, 6), **(win.detail or {})}]}
    if (win.detail or {}).get("fetched_ts"):
        detail["fetched_ts"] = min(win.detail["fetched_ts"], (base.detail or {}).get("fetched_ts", win.detail["fetched_ts"]))
    note = f"{n + 1}+ {win.p_yes:.1%}, push = {n}+ {base.p_yes:.1%} - {n + 1}+ = {push:.1%}"
    return Quote("kalshi", f"{win.ref} & {base.ref}", note, win.p_yes, max(win.spread, base.spread), title=win.title,
                 detail=detail, p_push=push)


def attach(legs: list[Leg], quotes: list[Quote]) -> None:
    """Matching is by player, stat and line, so tonight's live game would also price tomorrow's prop
    for the same player (a 99c rung reads as a 99.5% edge). A dated leg (board.json) only sees
    Kalshi markets from its own game day; undated legs (pasted text) and undated quotes see all."""
    by_day: dict[str, list[Quote]] = {}
    for q in quotes:
        by_day.setdefault(q.game_date, []).append(q)
    undated = by_day.get("", [])
    for leg in legs:
        day = leg.game_date
        quotes = undated + by_day.get(day, []) if day else [q for qs in by_day.values() for q in qs]
        for q in quotes:
            pin = leg.pin_kalshi if q.venue == "kalshi" else leg.pin_poly
            if (q.ref == pin) if pin else matches(leg, q.text, q.title):
                if q.spread <= MAX_SPREAD:
                    leg.quotes.append(q)
        if leg.whole_line:
            leg.quotes = [w for q in leg.quotes if (w := whole_line_quote(leg, q, quotes)) is not None]
            continue  # no interpolated whole lines: a push needs two real adjacent rungs
        if not leg.pin_kalshi and not any(q.venue == "kalshi" for q in leg.quotes):
            est = interpolate(leg, quotes)
            if est:
                leg.quotes.append(est)


async def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", nargs="?", help="your PrizePicks lines")
    ap.add_argument("--kalshi-series", nargs="*", default=[], help="Kalshi series tickers holding player props")
    ap.add_argument("--poly-tag", default="nfl", help="Polymarket tag slug ('' to skip Polymarket)")
    ap.add_argument("--list-series", metavar="WORD", help="list Kalshi sports series containing WORD, then exit")
    ap.add_argument("--min-p", "--min-ev", dest="min_p", type=float, default=0.58,
                    help="win probability a leg needs to be highlighted and used in entries (e.g. 0.60)")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--ladders", metavar="DAY", help="cheat sheet of 60%% lines for Kalshi event date DAY (e.g. 26OCT11)")
    ap.add_argument("--max-spread", type=float, default=0.04, help="--ladders: widest bid/ask to trust")
    ap.add_argument("--min-volume", type=float, default=100, help="--ladders: contracts traded on a rung")
    a = ap.parse_args(argv)

    if a.list_series:
        return await list_series(a.list_series)
    if a.ladders:
        return await ladders(a.kalshi_series or ["KXNFLPASSYDS", "KXNFLRSHYDS", "KXNFLRECYDS", "KXNFLREC"],
                             a.ladders, a.min_p, a.max_spread, a.min_volume)
    if not a.csv:
        ap.error("give a CSV of PrizePicks lines, or --list-series")

    legs, started = pregame(load_legs(a.csv))
    if started:
        print(f"{started} legs dropped: game started or starts within {START_BUFFER_MIN} min")
    quotes = (await kalshi_quotes(a.kalshi_series)) if a.kalshi_series else []
    if a.poly_tag:
        quotes += await poly_quotes(a.poly_tag)
    attach(legs, quotes)

    print(f"{len(quotes)} market quotes, {sum(bool(l.quotes) for l in legs)}/{len(legs)} legs matched\n")
    print(f"  {'pick':6} {'p':>6}  leg  ->  markets used (CHECK the title really is the same prop)   * = >= {a.min_p:.0%}")
    for leg in sorted(legs, key=lambda l: -(l.pick[1] if l.pick else 0)):
        if not leg.pick:
            print(f"  {'--':6} {'':>6}  {leg.player} {leg.stat} {leg.line}  ->  no match")
            continue
        side, p = leg.pick
        print(f"{'*' if p >= a.min_p else ' '} {side:6} {p:6.1%}  {leg.player} {leg.stat} {leg.line}")
        for q in leg.quotes:
            tag = "kalshi~" if q.interpolated else q.venue
            print(f"{'':18}{tag:7} {q.p_yes:5.1%} yes  spread {q.spread*100:.0f}c  {q.ref}  | {q.text.strip()[:80]}")

    good = [l for l in legs if l.pick and l.pick[1] >= a.min_p]
    print(f"\nPLAYS >= {a.min_p:.0%} WIN PROBABILITY: {len(good)}")
    for l in sorted(good, key=lambda l: -l.pick[1]):
        print(f"  * {l.pick[0]} {l.player} {l.line} {l.stat}  ({l.pick[1]:.1%})  {l.game}")
    print("\nPower break-even per leg: " + ", ".join(
        f"{n}-pick {break_even(t[n], n):.1%}" for n, t in PAYOUTS["power"].items()))
    entries = rank_entries(good)

    print(f"\nTop entries (legs >= {a.min_p:.0%}), EV per $1:")
    if not entries:
        print("  none: no set of qualifying legs. Don't force an entry.")
    for ev, kind, combo, same_game in entries[:a.top]:
        legs_txt = "; ".join(f"{l.pick[0]} {l.player} {l.line} {l.stat}" for l in combo)
        flag = "  [same game: correlated, EV unreliable]" if same_game else ""
        print(f"  {ev:+6.1%}  {len(combo)}-pick {kind:5}  {legs_txt}{flag}")


if __name__ == "__main__":
    asyncio.run(main())
