"""
fetch_odds.py — sportsbook consensus for PrizePicks legs Kalshi doesn't price, from The Odds API.

For each leg the sentinel couldn't price on Kalshi (NHL shots on goal, blocked shots; MLB hits,
total bases...), find the same player, stat and line at DraftKings and FanDuel, remove each book's
vig (multiplicative: p_over = (1/over) / (1/over + 1/under), both sides at the same line), and
average the books.

OFF-MARKET LINES. When PrizePicks' line isn't a book's two-sided main line, the book's alternate
ladder (market key + "_alternate") is fetched for that market and game only. Alternates are
Over-only, so each is de-vigged with the overround of the same player's two-sided main line at
that book: fair = (1/price) / (1/over + 1/under). A player the main market doesn't list at all (books
post main lines for stars only: on 2026-10-14 DK's total bases had 6 players, all at 1.5) is read off
the ladder with that book's average overround on the market instead. The ladder is forced non-increasing in the line,
then read at PrizePicks' line exactly, or interpolated in log-odds between the nearest rungs on
either side at most MAX_LADDER_GAP apart (LADDER_GAP for yardage, whose rungs step 5-10 yards;
never extrapolated). Whole-number lines are skipped
unless quoted exactly: a tie is a push on PrizePicks, which one probability can't express.
Caveat: alternates far from the main line usually carry more vig than the main line, so this
slightly flatters long-shot Overs; off-market PrizePicks lines are usually 1 rung away.

QUOTA. The free plan is 500 credits a month. The events list is free; an event's odds cost one
credit per market that comes back (bookmakers=draftkings,fanduel count as one region). One MLB game
with every market is ~10 credits, a full NHL slate ~40, so an hourly refresh of everything would
spend the month in a day. So it only ever asks for:
  - games starting between START_BUFFER_MIN and the pre-game window (LOOKAHEAD_H = 3 h, the sentinel's
    --window-hours) from now that have an unpriced leg; later games stay on Kalshi or unpriced until they
    enter the window, so the budget isn't spent hours before lines settle,
  - the markets those legs need, when the cached copy is older than TTL_S (1 h), and an
    alternate market only when one of those legs is off the main line,
  - while today's spend stays under the daily cap and the account keeps RESERVE credits.
The daily cap is shared: it lives in the cache file with today's spend, and is the highest
--odds-budget any sentinel asked for today (DAILY_CAP by default). So launching one sentinel with
--odds-budget 35 lifts every running sentinel to 35 on its next pricing pass, and tomorrow it falls
back to 15 unless a sentinel still asks for more. The cap is split by sport (pipeline_config
odds_budget.shares, fractions of the cap: NFL 0.5, every other sport 0.5 together), tracked per group in the
same cache file, so early NFL games can't spend the credits evening NHL games need. The reserve is never touched: a paid request is
refused when it would leave fewer than RESERVE credits, or when the balance isn't known yet (the
free events call learns it first).
A market it can't refresh is still used for up to MAX_AGE_S. Everything is cached on disk
(local/.odds_cache.json, gitignored with local/), so restarts don't re-spend.

  python3 -m local.fetch_odds --sport nhl              # events, cached props, quota (spends nothing)
  python3 -m local.fetch_odds --sport mlb --refresh    # price the board's unpriced legs now (spends)
"""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import math
import os
import statistics
import time
from datetime import date, datetime
from pathlib import Path

import aiohttp
from dotenv import load_dotenv

from local import devig as dv
from local import pipeline_config
from local import prize_picks_ev_scraper as scanner

API = "https://api.the-odds-api.com/v4"
# our sport -> The Odds API sport keys, all queried. NBA preseason games are only under
# basketball_nba_preseason and regular-season games only under basketball_nba (2026-10-14: 7 preseason
# events through Oct 15, 46 regular-season from the Oct 20 opener), so NBA needs both.
SPORT_KEYS = {"mlb": ("baseball_mlb",), "nhl": ("icehockey_nhl",), "nfl": ("americanfootball_nfl",),
              "nba": ("basketball_nba_preseason", "basketball_nba"), "cfb": ("americanfootball_ncaaf",)}
# Four books, one request: the API bills per market and per group of up to 10 bookmakers, so adding
# Pinnacle and BetMGM costs no credits (checked 2026-10-16: all four post NHL SOG and NFL receiving yards).
BOOKS = {"pinnacle": "PIN", "draftkings": "DK", "fanduel": "FD", "betmgm": "MGM"}  # display order
# Pinnacle is the sharp book (low margin, high limits, moves first): its fair price counts 1.5x in the
# consensus. With only soft books present every weight is 1.0, a plain average.
WEIGHTS = {"pinnacle": 1.5}
CACHE = Path("local/.odds_cache.json")
TTL_S = 3600
MAX_AGE_S = 3 * 3600
DAILY_CAP = 15  # ~500 credits / 31 days
RESERVE = 25
LOOKAHEAD_H = 3  # pre-game window for paid requests (was 8 h: credits ran out before evening games)
TIMEOUT_S = 15
ALT = "_alternate"
MAX_LADDER_GAP = 2.0  # widest span between the two rungs used to interpolate (counting stats)
# (sport, market) -> widest rung gap. Yardage ladders step 5-10 yards (DK/FD receiving yards,
# 2026-10-14; 25 matches the Kalshi scanner); NBA point milestones step ~5 (10+, 15+, 20+...).
# Keyed by sport because NHL and NBA share "player_points" and hockey points step by 1.
LADDER_GAP = {("nfl", "player_pass_yds"): 25.0, ("nfl", "player_rush_yds"): 25.0,
              ("nfl", "player_reception_yds"): 25.0, ("nba", "player_points"): 5.0,
              ("nba", "player_points_rebounds_assists"): 5.0, ("nba", "player_points_rebounds"): 5.0,
              ("nba", "player_points_assists"): 5.0, ("nba", "player_rebounds_assists"): 3.0,
              ("cfb", "player_pass_yds"): 25.0, ("cfb", "player_rush_yds"): 25.0, ("cfb", "player_reception_yds"): 25.0}

# our stat phrase (parse_board's SPORTS[...]["stats"] values) -> Odds API market key
MARKETS = {
    "nhl": {"shots on goal": "player_shots_on_goal", "blocked shots": "player_blocked_shots",
            "saves": "player_total_saves", "points": "player_points", "assists": "player_assists",
            "goals": "player_goals", "power play points": "player_power_play_points",
            # valid keys (checked with a free 422), though DK/FD weren't posting them on 2026-10-14;
            # an unposted market costs nothing to ask for
            "hits": "player_hits", "faceoffs won": "player_faceoffs_won", "time on ice": "player_time_on_ice"},
    "mlb": {"hits": "batter_hits", "total bases": "batter_total_bases", "home runs": "batter_home_runs",
            "rbis": "batter_rbis", "hits + runs + rbis": "batter_hits_runs_rbis", "singles": "batter_singles",
            "walks": "batter_walks", "stolen bases": "batter_stolen_bases", "hitter strikeouts": "batter_strikeouts",
            "strikeouts": "pitcher_strikeouts", "hits allowed": "pitcher_hits_allowed",
            "walks allowed": "pitcher_walks", "earned runs": "pitcher_earned_runs", "pitching outs": "pitcher_outs"},
    # main and _alternate keys all checked with a free 422 on 2026-10-14
    "nfl": {"passing yards": "player_pass_yds", "rushing yards": "player_rush_yds",
            "receiving yards": "player_reception_yds", "receptions": "player_receptions",
            "passing touchdowns": "player_pass_tds", "passing attempts": "player_pass_attempts",
            "passing completions": "player_pass_completions", "rushing attempts": "player_rush_attempts"},
    # main and _alternate keys all checked with a free 422 on 2026-10-14 (season opens 2026-10-20)
    "nba": {"points": "player_points", "rebounds": "player_rebounds", "assists": "player_assists",
            "threes": "player_threes", "blocks": "player_blocks", "steals": "player_steals",
            "turnovers": "player_turnovers", "points + rebounds + assists": "player_points_rebounds_assists",
            "points + rebounds": "player_points_rebounds", "points + assists": "player_points_assists",
            "rebounds + assists": "player_rebounds_assists", "blocks + steals": "player_blocks_steals"},
}

MARKETS["cfb"] = dict(MARKETS["nfl"])  # same keys under americanfootball_ncaaf (checked with a free 422)

# team code as the CSV holds it (parse_board converts PrizePicks codes to Kalshi's) -> nickname in
# The Odds API's full team name. Matched as whole words on the normalized name.
NICKNAMES = {
    "nhl": {"ANA": "ducks", "BOS": "bruins", "BUF": "sabres", "CGY": "flames", "CAR": "hurricanes",
            "CHI": "blackhawks", "COL": "avalanche", "CBJ": "blue jackets", "DAL": "stars", "DET": "red wings",
            "EDM": "oilers", "FLA": "panthers", "LA": "kings", "MIN": "wild", "MTL": "canadiens",
            "NSH": "predators", "NJ": "devils", "NYI": "islanders", "NYR": "rangers", "OTT": "senators",
            "PHI": "flyers", "PIT": "penguins", "SJ": "sharks", "SEA": "kraken", "STL": "blues",
            "TB": "lightning", "TOR": "maple leafs", "UTA": "utah", "VAN": "canucks", "VGK": "golden knights",
            "WSH": "capitals", "WPG": "jets"},
    "mlb": {"AZ": "diamondbacks", "ATL": "braves", "BAL": "orioles", "BOS": "red sox", "CHC": "cubs",
            "CWS": "white sox", "CIN": "reds", "CLE": "guardians", "COL": "rockies", "DET": "tigers",
            "HOU": "astros", "KC": "royals", "LAA": "angels", "LAD": "dodgers", "MIA": "marlins",
            "MIL": "brewers", "MIN": "twins", "NYM": "mets", "NYY": "yankees", "ATH": "athletics",
            "PHI": "phillies", "PIT": "pirates", "SD": "padres", "SF": "giants", "SEA": "mariners",
            "STL": "cardinals", "TB": "rays", "TEX": "rangers", "TOR": "blue jays", "WSH": "nationals"},
    # both spellings where PrizePicks and Kalshi differ (parse_board turns JAX into JAC, LA into LAR)
    "nfl": {"ARI": "cardinals", "ATL": "falcons", "BAL": "ravens", "BUF": "bills", "CAR": "panthers",
            "CHI": "bears", "CIN": "bengals", "CLE": "browns", "DAL": "cowboys", "DEN": "broncos",
            "DET": "lions", "GB": "packers", "HOU": "texans", "IND": "colts", "JAC": "jaguars", "JAX": "jaguars",
            "KC": "chiefs", "LV": "raiders", "LAC": "chargers", "LAR": "rams", "LA": "rams", "MIA": "dolphins",
            "MIN": "vikings", "NE": "patriots", "NO": "saints", "NYG": "giants", "NYJ": "jets",
            "PHI": "eagles", "PIT": "steelers", "SF": "49ers", "SEA": "seahawks", "TB": "buccaneers",
            "TEN": "titans", "WAS": "commanders", "WSH": "commanders"},
    "nba": {"ATL": "hawks", "BOS": "celtics", "BKN": "nets", "BRK": "nets", "CHA": "hornets", "CHO": "hornets",
            "CHI": "bulls", "CLE": "cavaliers", "DAL": "mavericks", "DEN": "nuggets", "DET": "pistons",
            "GSW": "warriors", "GS": "warriors", "HOU": "rockets", "IND": "pacers", "LAC": "clippers",
            "LAL": "lakers", "MEM": "grizzlies", "MIA": "heat", "MIL": "bucks", "MIN": "timberwolves",
            "NOP": "pelicans", "NO": "pelicans", "NYK": "knicks", "NY": "knicks", "OKC": "thunder",
            "ORL": "magic", "PHI": "76ers", "PHX": "suns", "PHO": "suns", "POR": "trail blazers",
            "SAC": "kings", "SAS": "spurs", "SA": "spurs", "TOR": "raptors", "UTA": "jazz", "UTAH": "jazz",
            "WAS": "wizards", "WSH": "wizards"},
}


def ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


# College football has 130+ FBS teams: rather than a hand-written table, ESPN's team list (abbreviation,
# displayName) is fetched once per process, and an Odds API name is matched to the closest ESPN
# displayName (they agree for 117/125 teams; the rest differ by spelling: "Hawaii" vs "Hawai'i",
# "Sam Houston State" vs "Sam Houston"). UMass is the one that needs spelling out.
ESPN_CFB_TEAMS = "https://site.api.espn.com/apis/site/v2/sports/football/college-football/teams?limit=1000"
CFB_OVERRIDES = {"UMass Minutemen": "MASS"}
CFB_MATCH = 0.84  # SequenceMatcher ratio; the next-best candidate must trail by 0.1
CFB_TEAMS: dict[str, str] | None = None  # ESPN displayName -> abbreviation, loaded on first use


def _espn_teams_get() -> dict:
    import urllib.request
    req = urllib.request.Request(ESPN_CFB_TEAMS, headers={"User-Agent": "kalshi-shadow-kit"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        return json.loads(r.read())


def cfb_teams() -> dict[str, str]:
    global CFB_TEAMS
    if CFB_TEAMS is None:
        try:
            d = _espn_teams_get()
            CFB_TEAMS = {t["team"]["displayName"]: t["team"]["abbreviation"]
                         for t in d["sports"][0]["leagues"][0]["teams"]}
        except Exception:  # noqa: BLE001 — no team table means no CFB matching, not a crash
            return {}
    return CFB_TEAMS


def cfb_codes(name: str) -> set[str]:
    import difflib
    if name in CFB_OVERRIDES:
        return {CFB_OVERRIDES[name]}
    teams = cfb_teams()
    if name in teams:
        return {teams[name]}
    key = lambda n: " ".join(scanner.norm(n).split())
    scored = sorted(((difflib.SequenceMatcher(None, key(name), key(n)).ratio(), n) for n in teams), reverse=True)
    if scored and scored[0][0] >= CFB_MATCH and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.1):
        return {teams[scored[0][1]]}
    return set()


def team_codes(sport: str, name: str) -> set[str]:
    """Every code the CSV may use for The Odds API's team `name` (NFL has two spellings for some)."""
    if sport == "cfb":
        return cfb_codes(name)
    padded = f" {' '.join(scanner.norm(name).split())} "
    return {c for c, nick in NICKNAMES[sport].items() if f" {nick} " in padded}


def team_code(sport: str, name: str) -> str | None:
    return min(team_codes(sport, name), default=None)


def implied(price: float, fmt: str = "decimal") -> float:
    """Raw implied probability (vig included) of a decimal (1.91) or American (-110 / +120) price."""
    if fmt == "american":
        return -price / (-price + 100) if price < 0 else 100 / (price + 100)
    return 1 / price


def devig(over: float, under: float, fmt: str = "decimal", method: str | None = None) -> float:
    """Fair P(over) from one book's two-way price, by local/devig.py (pipeline_config devig_method;
    proportional p_over / (p_over + p_under) by default). P(under) = 1 - this."""
    return dv.two_way(over, under, fmt, method or pipeline_config.CONFIG["devig_method"]).fair_over


def weighted(fair: list[tuple[str, float]]) -> tuple[float, list[str]]:
    """Weighted mean of (book key, fair p) pairs (WEIGHTS, default 1.0) and the books' short names in
    BOOKS order."""
    w = [WEIGHTS.get(b, 1.0) for b, _ in fair]
    p = sum(wi * pi for wi, (_, pi) in zip(w, fair)) / sum(w)
    order = list(BOOKS)
    names = [BOOKS.get(b, b) for b, _ in sorted(fair, key=lambda x: order.index(x[0]) if x[0] in order else 99)]
    return p, names


def consensus(markets: dict, player: str, line: float, records: list | None = None) -> tuple[float, list[str]] | None:
    """Weighted mean (WEIGHTS) of de-vigged P(over) across books quoting both sides of exactly `line`
    for `player`, from cached {book: [outcomes]} of one market. Returns (p_over, books) or None.
    `records`, if given, collects each book's devig.TwoWay record (both prices, implied, fair, method)."""
    want = set(scanner.norm(player).split())
    fair = []
    for book, outcomes in markets.items():
        sides = {}
        for o in outcomes:
            if (o.get("point") is not None and abs(float(o["point"]) - line) < 1e-9
                    and set(scanner.norm(o.get("description", "")).split()) == want):
                sides[o["name"].lower()] = float(o["price"])
        if "over" in sides and "under" in sides:
            t = dv.two_way(sides["over"], sides["under"], "decimal", pipeline_config.CONFIG["devig_method"])
            fair.append((book, t.fair_over))
            if records is not None:
                records.append({"book": book, "weight": WEIGHTS.get(book, 1.0), **t.record()})
    return weighted(fair) if fair else None


def outcomes_for(outcomes: list[dict], player: str) -> list[dict]:
    want = set(scanner.norm(player).split())
    return [o for o in outcomes if o.get("point") is not None
            and set(scanner.norm(o.get("description", "")).split()) == want]


def market_overround(outcomes: list[dict]) -> float | None:
    """A book's mean 1/over + 1/under across every player's two-sided line in one market."""
    pairs: dict[tuple, dict[str, float]] = {}
    for o in outcomes:
        if o.get("point") is not None:
            pairs.setdefault((o.get("description"), float(o["point"])), {})[o["name"].lower()] = float(o["price"])
    rounds = [1 / x["over"] + 1 / x["under"] for x in pairs.values() if "over" in x and "under" in x]
    return statistics.fmean(rounds) if rounds else None


def ladder(main: list[dict], alt: list[dict], fallback_overround: float | None = None) -> list[tuple[float, float]]:
    """One book's fair P(over) by line for one player: two-sided points de-vigged directly, over-only
    points with the mean overround of the two-sided ones, or `fallback_overround` (the book's market
    average) when the player has no two-sided point. [] when there is neither."""
    sides: dict[float, dict[str, float]] = {}
    for o in main + alt:
        sides.setdefault(float(o["point"]), {}).setdefault(o["name"].lower(), float(o["price"]))
    two = {pt: x for pt, x in sides.items() if "over" in x and "under" in x}
    if not two and not fallback_overround:
        return []
    overround = (statistics.fmean(1 / x["over"] + 1 / x["under"] for x in two.values()) if two
                 else fallback_overround)
    fair = {pt: (devig(x["over"], x["under"]) if pt in two else (1 / x["over"]) / overround)
            for pt, x in sides.items() if "over" in x}
    out, cap = [], 1.0
    for pt in sorted(fair):  # P(X > line) can only fall as the line rises
        cap = min(cap, min(max(fair[pt], 1e-4), 1 - 1e-4))
        out.append((pt, cap))
    return out


def read_ladder(rungs: list[tuple[float, float]], line: float, max_gap: float = MAX_LADDER_GAP,
                interp: bool = True) -> tuple[float, bool] | None:
    """(P(over line), interpolated?) from a ladder, or None if line isn't bracketed closely enough
    (or isn't an exact rung, with interp off)."""
    for pt, p in rungs:
        if abs(pt - line) < 1e-9:
            return p, False
    if not interp:
        return None
    if abs(line % 1 - 0.5) > 1e-9:
        return None  # whole-number line: push risk, only an exact quote will do
    below = [(pt, p) for pt, p in rungs if pt < line]
    above = [(pt, p) for pt, p in rungs if pt > line]
    if not below or not above:
        return None
    (a, pa), (b, pb) = below[-1], above[0]
    if b - a > max_gap:
        return None
    logit = lambda p: math.log(p / (1 - p))
    x = logit(pa) + (line - a) / (b - a) * (logit(pb) - logit(pa))
    return 1 / (1 + math.exp(-x)), True


def ladder_consensus(main: dict, alt: dict, player: str, line: float, max_gap: float = MAX_LADDER_GAP,
                     allow_interp: bool | None = None) -> tuple[float, list[str], bool] | None:
    """Weighted mean (WEIGHTS) across books of each book's ladder read at `line`, from cached
    {book: outcomes} of the main and alternate markets. Returns (p_over, books, any interpolated) or None.
    Interpolation between rungs only with pipeline_config books.ladder_interp (off since 2026-10-23)."""
    if allow_interp is None:
        allow_interp = pipeline_config.CONFIG["books"]["ladder_interp"]
    fair, interp = [], False
    for book, outcomes in main.items():
        rungs = ladder(outcomes_for(outcomes, player), outcomes_for(alt.get(book, []), player),
                       market_overround(outcomes))
        hit = read_ladder(rungs, line, max_gap, allow_interp) if rungs else None
        if hit:
            fair.append((book, hit[0]))
            interp |= hit[1]
    return (*weighted(fair), interp) if fair else None


def whole_consensus(main: dict, alt: dict, player: str, line: float) -> tuple[float, float, list[str]] | None:
    """A whole line N from each book's ladder read EXACTLY at N - 0.5 and N + 0.5 (no interpolation: a push
    is a point mass): More wins = P(over N + 0.5), push = P(over N - 0.5) - P(over N + 0.5). A book's own
    whole-number line can't be used: its price refunds the push, so it carries no push probability.
    Weighted mean (WEIGHTS) of both across the books that post both rungs: (p_over, p_push, books) or None."""
    over, push = [], []
    for book, outcomes in main.items():
        rungs = ladder(outcomes_for(outcomes, player), outcomes_for(alt.get(book, []), player), market_overround(outcomes))
        at = lambda x: next((p for pt, p in rungs if abs(pt - x) < 1e-9), None)
        lo, hi = at(line - 0.5), at(line + 0.5)
        if lo is None or hi is None:
            continue
        over.append((book, hi))
        push.append((book, max(lo - hi, 0.0)))  # ladder() is monotone, so lo >= hi
    if not over:
        return None
    (p_over, names), (p_push, _) = weighted(over), weighted(push)
    return p_over, p_push, names


def group_limits(cap: int, shares: dict) -> dict[str, int]:
    return {g: int(f * cap + 1e-9) for g, f in shares.items()}


def budget_line(u: dict, shares: dict) -> str:
    """'Odds API today: 12/80 · nfl 10/40 · other 2/40' (spend per group against its share of the cap)."""
    cap = int(u.get("cap") or 0)
    groups = " · ".join(f"{g} {(u.get('by_group') or {}).get(g, 0)}/{n}" for g, n in group_limits(cap, shares).items())
    return f"Odds API today: {u.get('used', 0)}/{cap}" + (f" · {groups}" if groups else "")


class OddsBook:
    """Disk-cached, budgeted access to The Odds API. `get(url, params) -> (status, json, headers)`
    is injectable for tests; by default it is an aiohttp GET with a timeout."""

    def __init__(self, key: str, cache: Path = CACHE, ttl_s: float = TTL_S, daily_cap: int = DAILY_CAP,
                 reserve: int = RESERVE, get=None, window_h: float = LOOKAHEAD_H, shares: dict | None = None):
        self.key, self.cache_path, self.ttl_s, self.daily_cap, self.reserve = key, cache, ttl_s, daily_cap, reserve
        self.shares = dict(pipeline_config.CONFIG["odds_budget"]["shares"] if shares is None else shares)
        self.window_h = window_h  # only games starting within this many hours are priced from the books
        self.get = get or self._http_get
        self.load()
        self.notes: list[str] = []  # what happened this call, for the sentinel's log line

    def load(self) -> None:
        """(Re)read the disk cache. The NHL and MLB sentinels share it, and with it today's spend,
        so each pricing pass starts from what the other one last saved."""
        try:
            self.cache = json.loads(self.cache_path.read_text())
        except (OSError, ValueError):
            self.cache = {}
        for k in ("events", "odds", "usage"):
            self.cache.setdefault(k, {})

    @classmethod
    def from_env(cls, **kw) -> "OddsBook | None":
        key = os.getenv("ODDS_API_KEY", "").strip()
        return cls(key, **kw) if key else None

    async def _http_get(self, url: str, params: dict):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT_S)) as s:
            async with s.get(url, params=params) as r:
                return r.status, await r.json(content_type=None), dict(r.headers)

    def save(self) -> None:
        self.usage()  # records this book's daily_cap in the shared cap before writing
        tmp = self.cache_path.with_name(f".{self.cache_path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(self.cache))
        os.replace(tmp, self.cache_path)

    # ------------------------------------------------------------------ budget
    def usage(self) -> dict:
        """Today's shared spend and cap (resets at local midnight); `remaining` is the month's balance."""
        u = self.cache["usage"]
        if u.get("date") != date.today().isoformat():
            u.update(date=date.today().isoformat(), used=0, cap=0, by_group={})
        u["cap"] = max(int(u.get("cap") or 0), self.daily_cap)  # raise the shared cap, never lower it
        u.setdefault("by_group", {})  # credits spent before the split existed count toward the cap only
        return u

    def group(self, sport: str | None) -> str | None:
        """The budget group a sport spends from: its own name if it has a share, else "other" (if set)."""
        if sport in self.shares:
            return sport
        return "other" if "other" in self.shares else None

    def group_limit(self, sport: str | None) -> int | None:
        """Today's credits this sport's group may spend: floor(share * shared cap); None = no split."""
        g = self.group(sport)
        return None if g is None else group_limits(self.cap(), self.shares)[g]

    def cap(self) -> int:
        return self.usage()["cap"]

    def affordable(self, cost: int, sport: str | None = None) -> bool:
        u = self.usage()
        remaining = u.get("remaining")
        if remaining is None or u["used"] + cost > u["cap"] or remaining - cost < self.reserve:
            return False
        limit = self.group_limit(sport)
        return limit is None or u["by_group"].get(self.group(sport), 0) + cost <= limit

    def charge(self, headers: dict, sport: str | None = None) -> None:
        u = self.usage()
        h = {k.lower(): v for k, v in headers.items()}
        spent = int(float(h.get("x-requests-last") or 0))
        u["used"] += spent
        g = self.group(sport)
        if spent and g is not None:
            u["by_group"][g] = u["by_group"].get(g, 0) + spent
        if h.get("x-requests-remaining") is not None:
            u["remaining"] = int(float(h["x-requests-remaining"]))

    async def call(self, path: str, params: dict, sport: str | None = None) -> dict | list | None:
        try:
            status, body, headers = await self.get(f"{API}{path}", {"apiKey": self.key, **params})
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            self.notes.append(f"Odds API {path.split('/')[-1]}: {type(e).__name__}")
            return None
        self.charge(headers, sport)
        msg = body.get("message", "") if isinstance(body, dict) else ""
        if status == 422 and msg.startswith("Invalid markets:"):  # free; remember and let refresh retry
            bad = [m.strip() for m in msg.split(":", 1)[1].split(",")]
            self.cache["invalid"] = sorted(set(self.cache.get("invalid", [])) | set(bad))
            return {"_invalid": bad}
        if status != 200:
            msg = body.get("message") if isinstance(body, dict) else body
            self.notes.append(f"Odds API HTTP {status}: {str(msg)[:80]}")
            return None
        return body

    # ------------------------------------------------------------------ data
    async def events(self, sport: str, now: float) -> list[dict]:
        """Upcoming events (free endpoint), cached for ttl_s, or fetched anyway while the account
        balance is unknown: that free call is how affordable() learns it."""
        c = self.cache["events"].get(sport)
        if c and now - c["ts"] < self.ttl_s and self.usage().get("remaining") is not None:
            return c["data"]
        data, ok = [], False
        for key in SPORT_KEYS[sport]:
            got = await self.call(f"/sports/{key}/events", {})
            if isinstance(got, list):
                ok = True
                seen = {e["id"] for e in data}  # a game listed under two keys is priced once
                data += [{**e, "sport_key": key} for e in got if e["id"] not in seen]  # odds come from its key
        if ok:
            self.cache["events"][sport] = {"ts": now, "data": data}
            return data
        return c["data"] if c else []

    async def refresh(self, sport: str, event_id: str, markets: list[str], now: float,
                      sport_key: str | None = None) -> None:
        sport_key = sport_key or SPORT_KEYS[sport][0]
        body = None
        for _ in range(2):  # a 422 naming invalid markets costs nothing: drop them and ask once more
            markets = [m for m in markets if m not in self.cache.get("invalid", [])]
            if not markets:
                return
            body = await self.call(f"/sports/{sport_key}/events/{event_id}/odds",
                                   {"regions": "us", "bookmakers": ",".join(BOOKS), "oddsFormat": "decimal",
                                    "markets": ",".join(markets)}, sport)
            if not (isinstance(body, dict) and "_invalid" in body):
                break
        if not isinstance(body, dict) or "_invalid" in body:
            return
        got = {m: {} for m in markets}  # a market no book offers is cached empty, so it isn't re-asked
        for b in body.get("bookmakers", []):
            for m in b.get("markets", []):
                if m["key"] in got:
                    got[m["key"]][b["key"]] = m.get("outcomes", [])
        ev = self.cache["odds"].setdefault(event_id, {})
        for m, books in got.items():
            ev[m] = {"ts": now, "books": books}

    def fresh(self, cached: dict, market: str, now: float) -> dict | None:
        c = cached.get(market)
        return c if c and now - c["ts"] < MAX_AGE_S else None

    async def ensure(self, sport: str, event_id: str, markets: list[str], now: float,
                     sport_key: str | None = None, ttl: float | None = None) -> int:
        """Refresh the markets whose cache is older than ttl_s, if the budget allows. Returns the
        number of market refreshes skipped for budget."""
        cached = self.cache["odds"].get(event_id, {})
        stale = [m for m in markets if m not in self.cache.get("invalid", [])
                 and (m not in cached or now - cached[m]["ts"] >= (self.ttl_s if ttl is None else ttl))]
        if not stale:
            return 0
        if not self.affordable(len(stale), sport):
            return len(stale)
        await self.refresh(sport, event_id, stale, now, sport_key)
        return 0

    @staticmethod
    def attach(leg: scanner.Leg, event_id: str, market: str, p: float, books: list[str], how: str,
               c: dict, now: float) -> int:
        age = f", {(now - c['ts']) / 60:.0f} min old" if now - c["ts"] >= 60 else ""
        leg.quotes.append(scanner.Quote("books", f"{event_id}:{market}", f"{','.join(books)} {how}{age}", p, 0.0))
        return 1

    def publish_cap(self) -> None:
        """Raise the shared daily cap to this book's daily_cap now (a sentinel calls it at launch)."""
        with open(self.cache_path.with_suffix(".lock"), "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                self.load()
                self.save()
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    async def price(self, sport: str, legs: list[scanner.Leg], now: float | None = None) -> int:
        """Attach a sportsbook consensus quote to each leg it can price. Returns legs priced. Holds the
        cache's lock throughout, so sentinels pricing at the same moment can't overwrite each other's
        spend (and overshoot the shared daily cap)."""
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.cache_path.with_suffix(".lock"), "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                return await self._price(sport, legs, now)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    async def reprice(self, sport: str, legs: list[scanner.Leg], now: float | None = None,
                      max_age_s: float = 900) -> dict:
        """Just-in-time: re-price these books legs, first refreshing only the (event, market) entries
        behind them that are older than max_age_s (not the 1 h TTL). Budgeted like any paid call (daily
        cap, sport share, reserve). A leg nothing could re-price keeps its old quote. Returns
        {"credits": spent, "notes": [...]}."""
        now = time.time() if now is None else now
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.cache_path.with_suffix(".lock"), "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                self.load()
                before = self.usage()["used"]
                old = {id(l): l.quotes for l in legs}
                for l in legs:
                    l.quotes = [q for q in l.quotes if q.venue != "books"]
                await self._price(sport, legs, now, force_age_s=max_age_s)
                for l in legs:
                    if not any(q.venue == "books" for q in l.quotes):
                        l.quotes = old[id(l)]
                    else:
                        l.quotes = [q for q in l.quotes if q.venue == "books"]
                return {"credits": self.usage()["used"] - before, "notes": list(self.notes)}
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    async def _price(self, sport: str, legs: list[scanner.Leg], now: float | None = None,
                     force_age_s: float | None = None) -> int:
        now = time.time() if now is None else now
        self.notes = []
        self.load()
        markets = MARKETS.get(sport, {})
        whole_ok = pipeline_config.CONFIG["books"]["whole_lines"]  # off: whole lines never cost a credit
        want = [l for l in legs if l.stat in markets and l.start_ts and l.team
                and (whole_ok or not l.whole_line)
                and now + scanner.START_BUFFER_MIN * 60 < l.start_ts <= now + self.window_h * 3600]
        if not want:
            return 0
        priced, skipped = 0, 0
        for e in sorted(await self.events(sport, now), key=lambda e: e["commence_time"]):
            if ts(e["commence_time"]) < now - 6 * 3600:
                self.cache["odds"].pop(e["id"], None)  # long over: keep the cache file small
            start = ts(e["commence_time"])
            day = datetime.fromtimestamp(start, scanner.EASTERN).strftime("%y%b%d").upper()
            teams = team_codes(sport, e["home_team"]) | team_codes(sport, e["away_team"])
            mine = [l for l in want if l.team in teams and l.game_date == day]
            if not mine:
                continue
            needed = sorted({markets[l.stat] for l in mine})
            skipped += await self.ensure(sport, e["id"], needed, now, e.get("sport_key"), force_age_s)
            cached = self.cache["odds"].get(e["id"], {})
            off_main = []
            for l in mine:
                c = self.fresh(cached, markets[l.stat], now)
                if not c:
                    continue
                if l.whole_line:  # its push needs the N - 0.5 and N + 0.5 rungs: the alternate ladder
                    if any(market_overround(o) for o in c["books"].values()):
                        off_main.append(l)
                    continue
                records: list = []
                hit = consensus(c["books"], l.player, l.line, records)
                if hit:
                    priced += self.attach(l, e["id"], markets[l.stat], hit[0], hit[1], "de-vigged", c, now)
                    l.quotes[-1].detail = {"source": "books", "fair_over": hit[0], "method": "weighted consensus of "
                                           + pipeline_config.CONFIG["devig_method"], "books": records,
                                           "fetched_ts": c["ts"]}  # when the cache entry was fetched (price age)
                elif any(market_overround(o) for o in c["books"].values()):
                    off_main.append(l)  # not at PrizePicks' line (or not on the main market at all)
            if not off_main or not pipeline_config.CONFIG["books"]["alt_ladder"]:
                continue  # no alternate ladder: never requested (books.alt_ladder off since 2026-10-23)
            skipped += await self.ensure(sport, e["id"], sorted({markets[l.stat] + ALT for l in off_main}), now,
                                         e.get("sport_key"), force_age_s)
            cached = self.cache["odds"].get(e["id"], {})
            for l in off_main:
                main, alt = self.fresh(cached, markets[l.stat], now), self.fresh(cached, markets[l.stat] + ALT, now)
                if l.whole_line:
                    w = whole_consensus(main["books"], alt["books"] if alt else {}, l.player, l.line)
                    if w:
                        priced += self.attach(l, e["id"], markets[l.stat] + ALT, w[0], w[2], "whole", alt or main, now)
                        l.quotes[-1].p_push = w[1]
                        l.quotes[-1].detail = {"source": "books", "fair_over": w[0], "p_push": w[1], "books": w[2],
                                               "method": "whole line: alternate ladder rungs at line - 0.5 and + 0.5",
                                               "fetched_ts": min(main["ts"], alt["ts"]) if alt else main["ts"]}
                    continue
                hit = ladder_consensus(main["books"], alt["books"] if alt else {}, l.player, l.line,
                                       LADDER_GAP.get((sport, markets[l.stat]), MAX_LADDER_GAP))
                if hit:
                    priced += self.attach(l, e["id"], markets[l.stat] + ALT, hit[0], hit[1],
                                          "interp" if hit[2] else "alt", alt or main, now)
                    l.quotes[-1].detail = {"source": "books", "fair_over": hit[0], "books": hit[1],
                                           "method": "alternate ladder: Over-only, main-line overround"
                                                     + (", log-odds interpolation" if hit[2] else ""),
                                           "fetched_ts": min(main["ts"], alt["ts"]) if alt else main["ts"]}
        if skipped:
            u = self.usage()
            g, limit = self.group(sport), self.group_limit(sport)
            share = f", {g} share {u['by_group'].get(g, 0)}/{limit}" if limit is not None else ""
            self.notes.append(f"{skipped} market refresh(es) skipped: budget {u['used']}/{u['cap']} today{share}, "
                              f"{u.get('remaining', '?')} left (reserve {self.reserve})")
        self.save()
        return priced


def sources(leg: scanner.Leg) -> str:
    """'Consensus: PIN, DK, FD' for a main-line price, '... · alt' / '... · interp' off the alternate
    ladder, '' when no sportsbook priced the leg."""
    for q in leg.quotes:
        if q.venue == "books":
            books, how = (q.text.split(" ")[0], (q.text.split(" ") + [""])[1].rstrip(","))
            tag = "Consensus: " + ", ".join(books.split(","))
            return tag if how == "de-vigged" else f"{tag} · {how}"
    return ""


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sport", choices=sorted(SPORT_KEYS), required=True)
    ap.add_argument("--csv", help="line cache to price with --refresh (default: local/lines_<sport>.csv)")
    ap.add_argument("--refresh", action="store_true", help="price the CSV's unpriced legs now (spends credits)")
    a = ap.parse_args()
    load_dotenv(".env")
    book = OddsBook.from_env()
    if not book:
        raise SystemExit("ODDS_API_KEY is not set in .env")
    now = time.time()
    events = await book.events(a.sport, now)
    print(f"{len(events)} {a.sport.upper()} events listed")
    if a.refresh:
        from local.parse_board import SPORTS, lines_path
        legs, _ = scanner.pregame([l for l in scanner.load_legs(a.csv or str(lines_path(a.sport))) if not l.promo])
        scanner.attach(legs, await scanner.kalshi_quotes(SPORTS[a.sport]["series"]))
        unpriced = [l for l in legs if not l.quotes]
        n = await book.price(a.sport, unpriced, now)
        print(f"{n}/{len(unpriced)} Kalshi-unpriced legs priced by sportsbooks")
        for l in unpriced:
            if l.quotes:
                print(f"  {l.pick[0]} {l.player} {l.line} {l.stat}: {l.pick[1]:.1%} ({sources(l)})")
    cached = sum(len(m) for m in book.cache["odds"].values())
    u = book.usage()
    print(f"cache: {cached} event-markets | today {u['used']}/{u['cap']} credits | "
          f"{u.get('remaining', '?')} left this month (reserve {book.reserve})")
    for n in book.notes:
        print(" ", n)
    book.save()


if __name__ == "__main__":
    asyncio.run(main())
