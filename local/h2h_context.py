"""
h2h_context.py — a player's head-to-head baseline against tonight's opponent, per minute, adjusted for who is
missing tonight and who was missing in each past meeting. Pure model + a cached ESPN fetch layer.

Data (ESPN public, no key; checked 2026-10-30):
  game log   .../athletes/{id}/gamelog?season=Y: one season per request, Y = the year it ENDS (2024-25 = 2025);
             no `season` = the current one. Per game: event id, the player's team, opponent, minutes (NBA
             "minutes", NHL "timeOnIcePerGame") and the stat.
  box score  site/v2/sports/{path}/summary?event=ID: every player who dressed, with minutes (NBA; DNPs carry
             didNotPlay) or time on ice (NHL; goalies in their own group). Players out injured are simply
             absent. The summary's "injuries" list is TODAY's report, not the game's: never used for the past.
  tonight    injury_guard's ESPN injury feed (status per player) and, NHL, the probable starting goalie.
  NFL        box scores and game logs carry no minutes or snap counts: not supported (Prediction says so).

Model (predict):
  1. H2H games: every game the player PLAYED against `opp` in the last SEASONS seasons (preseason excluded).
  2. Each gets a weight: SEASON_DECAY per season of age, times MISMATCH_W per "context mismatch" with tonight
     (counted up to MAX_MISMATCHES, so a changed roster can't zero a game out):
       a key teammate (KEY_MIN average minutes in this season's box scores of his team: tonight's roster) whose
       presence differs: played then and OUT tonight, or absent then and plays tonight;
       a key opponent (KEY_MIN in the H2H box scores) who played then and is OUT tonight (the reverse is
       ambiguous: an old box can't tell "injured" from "not on the roster yet");
       the player on another team then (a trade); NHL skater goals/assists/points: a different opposing starting
       goalie. Goalies are never key teammates. A game with no box score: MISSING_BOX_W.
  3. H2H rate = sum(w * stat) / sum(w * minutes): per minute, so blowouts, foul trouble and short nights don't
     distort it. Effective sample size n_eff = (sum w)^2 / sum(w^2). The model rate blends it with the player's own
     per-minute rate over his last RECENT_N games, counted as PRIOR_GAMES extra game(s): H2H stays primary (5
     effective H2H games -> 5/6 of the blend), but 0 steals in 9 meetings no longer means P(over 0.5) = 0.
  4. Usage: for each key teammate whose presence differs between tonight and the H2H weight, the player's
     per-minute rate with vs without him this season (box scores of RECENT_N recent games; at least
     USAGE_MIN_WITH games with him and USAGE_MIN_WITHOUT without), shrunk by n_without / (n_without + USAGE_K), boosts (teammate out tonight) or trims (teammate back) the rate in
     proportion to the share of H2H weight that differs.
  5. Expected minutes: mean of his last 5 played games; a minutes restriction (first game back after a
     RESTRICT_GAP_DAYS+ absence at under RESTRICT_RATIO of his usual minutes) caps it at that game's minutes;
     Questionable / Doubtful / Day-To-Day scale it by STATUS_MINUTES. Out / IR / suspended: no prediction.
  6. Projection mu = rate x usage x expected minutes. P(over), P(push) from a negative binomial (mu <= POISSON_MAX)
     or a normal, with variance DISPERSION x mu (game-to-game swings) + mu^2 / (n_eff + prior) (the rate itself is
     an estimate from a few games), capped to [1 - MAX_CONF, MAX_CONF] until graded calibration supports more.
     A side only when P(side | no push) >= threshold AND n_eff >= MIN_EFF_N.

Guards added 2026-10-30 after the first live board, whose most confident calls were all model artifacts:
  goalies    a saves prop needs the player to be his team's probable starter (else no prediction) and expects a
             full game (GOALIE_MINUTES): no minutes-restriction guess from a relief appearance, no skater usage.
  usage      only the strongest teammate factor is applied, capped at +/- USAGE_CAP: several factors from the
             same few games were multiplying (x1.94).
  role       a recent per-minute rate more than ROLE_RATIO away from the H2H one (a bench player now starting)
             weighs recent form equal to the H2H games, not as one extra game; and a projection more than
             LINE_GAP from a line of LINE_GAP_MIN+ gets no call: the line knows about a role the model misses.

All constants are starting values, not fitted: log predictions next to graded results and tune them.

  python3 -m local.h2h_context "LeBron James" --sport nba --team LAL --opp OKC --stat points --line 24.5
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import NormalDist, fmean

from local import enricher, injury_guard
from local.grade_slips import canon, name_key

SUPPORTED = ("nba", "nhl")
MINUTES = {"nba": ["min:minutes"], "nhl": ["min:timeOnIcePerGame"]}
SEASONS = 3                 # current + 2 previous
SEASON_DECAY = 0.75         # weight per season of age
MISMATCH_W = 0.5            # weight per context mismatch with tonight
MAX_MISMATCHES = 3          # mismatches counted per game (floor 0.5^3 = 0.125)
MISSING_BOX_W = 0.75        # weight of an H2H game whose box score couldn't be read
KEY_MIN = {"nba": 24.0, "nhl": 17.0}   # avg minutes (when playing) that make a key player
RECENT_N = 10               # recent games for expected minutes and with/without splits
USAGE_K = 5.0               # shrinkage of a with/without split toward no effect
USAGE_MIN_WITH, USAGE_MIN_WITHOUT = 3, 2
RESTRICT_RATIO, RESTRICT_GAP_DAYS = 0.75, 7
STATUS_MINUTES = {"questionable": 0.85, "day-to-day": 0.85, "doubtful": 0.6}
GOALIE_STATS = {"goals", "assists", "points", "power play points"}
POISSON_MAX, DISPERSION = 15.0, 1.5  # variance = DISPERSION x mu (+ rate uncertainty, see prob_over)
MAX_CONF = 0.85             # P(over) clamped to [0.15, 0.85] until graded calibration supports more
GOALIE_PROPS = {"saves"}    # needs the probable starter, expects a full game
GOALIE_MINUTES = 60.0
USAGE_CAP = 0.15            # the one usage factor applied, clamped to [0.85, 1.15]
ROLE_RATIO = 1.5            # recent vs H2H per-minute rate this far apart (either way): a role change
LINE_GAP, LINE_GAP_MIN = 0.40, 3.0  # projection > 40% from a line of 3+: no call
MIN_EFF_N = 2.0
PRIOR_GAMES = 1.0           # the player's recent per-minute rate, as this many extra H2H games
THRESHOLD = 0.56            # P(side | no push): 5/6-pick Flex breaks even near 54.2%, plus a margin

SUMMARY = "https://site.api.espn.com/apis/site/v2/sports/{path}/summary"
CACHE_DB = Path(__file__).resolve().parents[1] / "data" / "h2h_cache.sqlite3"
CURRENT_TTL_S = 3 * 3600    # current-season logs refresh; past seasons and final box scores never change


def nk(name: str) -> str:
    return " ".join(name_key(name))


def season_of(date: str) -> int:
    """The ESPN season (end year) a game date belongs to: NBA/NHL seasons start in the autumn."""
    d = datetime.fromisoformat(date.replace("Z", "+00:00")) if date else datetime.now(timezone.utc)
    return d.year + 1 if d.month >= 8 else d.year


# --------------------------------------------------------------------------- data
@dataclass(frozen=True)
class GameLine:
    event_id: str
    date: str          # ISO
    team: str          # the player's team in that game
    opp: str
    minutes: float
    value: float       # the prop's stat
    season: int = 0

    @property
    def ts(self) -> float:
        return datetime.fromisoformat(self.date.replace("Z", "+00:00")).timestamp() if self.date else 0.0


@dataclass(frozen=True)
class Box:
    event_id: str
    minutes: dict[str, dict[str, float]]          # team code -> {name key: minutes} (skaters / players who played)
    goalie: dict[str, str] = field(default_factory=dict)  # NHL: team code -> starting goalie (most TOI); not in minutes

    def played(self, team: str, player: str) -> bool:
        return self.minutes.get(canon(team), {}).get(player, 0) > 0


def lines_from_log(log: dict, sport: str, stat: str, season: int = 0) -> list[GameLine]:
    """The player's games (newest first) with minutes and the stat; games missing either are skipped."""
    cols = enricher.COLUMNS.get(sport, {}).get(enricher.stat_phrase(sport, stat))
    if not cols or sport not in MINUTES:
        return []
    out = []
    for g in enricher.games(log):
        v, m = enricher.row_value(cols, g["row"]), enricher.row_value(MINUTES[sport], g["row"])
        if v is not None and m is not None:
            out.append(GameLine(g["event"], g["date"], canon(g["team"]), canon(g["opp"]), m, v,
                                season or season_of(g["date"])))
    return out


def _clock(v) -> float:
    try:
        return enricher._minutes(str(v)) if ":" in str(v) else float(v)
    except ValueError:
        return 0.0


def parse_box(summary: dict, sport: str, event_id: str = "") -> Box:
    minutes: dict[str, dict[str, float]] = {}
    goalie: dict[str, str] = {}
    for team in (summary.get("boxscore") or {}).get("players") or []:
        code = canon((team.get("team") or {}).get("abbreviation") or "")
        seen = minutes.setdefault(code, {})
        for grp in team.get("statistics") or []:
            keys = grp.get("keys") or []
            col = "minutes" if "minutes" in keys else "timeOnIce" if "timeOnIce" in keys else None
            if col is None:
                continue
            i, best = keys.index(col), (None, 0.0)
            for a in grp.get("athletes") or []:
                stats = a.get("stats") or []
                if a.get("didNotPlay") or len(stats) <= i:
                    continue
                m = _clock(stats[i])
                name = nk((a.get("athlete") or {}).get("displayName") or "")
                if m <= 0 or not name:
                    continue
                if grp.get("name") == "goalies":  # a goalie's 60 minutes would make him every skater's "key teammate"
                    best = (name, m) if m > best[1] else best
                else:
                    seen[name] = max(m, seen.get(name, 0.0))
            if best[0]:
                goalie[code] = best[0]
    return Box(event_id or str((summary.get("header") or {}).get("id") or ""), minutes, goalie)


# --------------------------------------------------------------------------- model
def _status(injuries: dict, player: str, team: str) -> str:
    rec = injury_guard._find(injuries, player, team) if injuries else None
    return (rec or {}).get("status", "").lower()


def is_out(status: str) -> bool:
    return any(h in status for h in injury_guard.HARD)


def key_players(boxes: list[Box], team: str, exclude: str, sport: str) -> list[str]:
    """Players of `team` averaging KEY_MIN+ minutes over the box scores where they played."""
    played: dict[str, list[float]] = {}
    for b in boxes:
        for p, m in b.minutes.get(canon(team), {}).items():
            played.setdefault(p, []).append(m)
    return sorted(p for p, ms in played.items() if p != exclude and fmean(ms) >= KEY_MIN[sport])


def prob_over(mu: float, line: float, var: float | None = None) -> tuple[float, float]:
    """(P(stat > line), P(stat == line)); the push is 0 for a half line. `var`: the variance (default: Poisson,
    var = mu, for low projections; DISPERSION x mu above POISSON_MAX). A variance above mu makes the low-count
    model a negative binomial with that variance."""
    whole = abs(line % 1) < 1e-9
    if mu <= 0:
        return 0.0, 1.0 if whole and line == 0 else 0.0
    if mu <= POISSON_MAX:
        k = int(math.floor(line))
        if var is None or var <= mu * (1 + 1e-9):
            pmfs = [math.exp(-mu + i * math.log(mu) - math.lgamma(i + 1)) for i in range(k + 1)]
        else:
            r = mu * mu / (var - mu)
            lp, lq = math.log(r / (r + mu)), math.log(mu / (r + mu))
            pmfs = [math.exp(math.lgamma(i + r) - math.lgamma(r) - math.lgamma(i + 1) + r * lp + i * lq)
                    for i in range(k + 1)]
        return max(0.0, 1.0 - sum(pmfs)), (pmfs[-1] if whole else 0.0)
    nd = NormalDist(mu, math.sqrt(var if var is not None else DISPERSION * mu))
    if whole:
        return 1 - nd.cdf(line + 0.5), nd.cdf(line + 0.5) - nd.cdf(line - 0.5)
    return 1 - nd.cdf(line), 0.0


@dataclass
class Prediction:
    player: str
    stat: str
    line: float
    opp: str
    side: str | None = None           # "More" | "Less" | None (no call)
    p_over: float | None = None       # P(over | no push)
    p_push: float = 0.0
    mu: float | None = None           # projected stat
    rate: float | None = None         # model per-minute rate (H2H blended with recent form), before usage
    h2h_rate: float | None = None     # the weighted H2H per-minute rate alone
    recent_rate: float | None = None  # the player's per-minute rate over his last RECENT_N games
    usage: float = 1.0                # usage multiplier from absences
    exp_minutes: float | None = None
    n_h2h: int = 0
    n_eff: float = 0.0
    notes: list[str] = field(default_factory=list)
    games: list[dict] = field(default_factory=list)  # per H2H game: date, minutes, stat, weight, mismatches


def expected_minutes(lines: list[GameLine], status: str, notes: list[str]) -> float | None:
    played = sorted((g for g in lines if g.minutes > 0), key=lambda g: g.ts, reverse=True)[:RECENT_N]
    if not played:
        return None
    exp = fmean(g.minutes for g in played[:5])
    if len(played) >= 3:
        usual = fmean(g.minutes for g in played[1:])
        gap_days = (played[0].ts - played[1].ts) / 86400
        if gap_days >= RESTRICT_GAP_DAYS and played[0].minutes < RESTRICT_RATIO * usual:
            exp = played[0].minutes
            notes.append(f"minutes restriction suspected: {played[0].minutes:.0f} min back from a "
                         f"{gap_days:.0f}-day absence (usual {usual:.0f})")
    for s, mult in STATUS_MINUTES.items():
        if s in status:
            exp *= mult
            notes.append(f"{status.title()}: expected minutes x{mult:g}")
            break
    return exp


def usage_factor(recent: list[GameLine], boxes: dict[str, Box], team: str, mate: str) -> tuple[float, int]:
    """Shrunk per-minute rate without `mate` / with him, over recent games with box scores; (factor, n_without)."""
    w_v = w_m = wo_v = wo_m = 0.0
    n_wo = 0
    for g in recent:
        b = boxes.get(g.event_id)
        if b is None or g.team != canon(team) or g.minutes <= 0:
            continue
        if b.played(team, mate):
            w_v, w_m = w_v + g.value, w_m + g.minutes
        else:
            wo_v, wo_m, n_wo = wo_v + g.value, wo_m + g.minutes, n_wo + 1
    n_with = sum(1 for g in recent if g.event_id in boxes and g.team == canon(team) and g.minutes > 0
                 and boxes[g.event_id].played(team, mate))
    if n_with < USAGE_MIN_WITH or n_wo < USAGE_MIN_WITHOUT or not (w_m and wo_m and w_v):
        return 1.0, n_wo
    raw = (wo_v / wo_m) / (w_v / w_m)
    return 1 + n_wo / (n_wo + USAGE_K) * (raw - 1), n_wo


def predict(player: str, sport: str, stat: str, line: float, team: str, opp: str, lines: list[GameLine],
            boxes: dict[str, Box], injuries: dict | None = None, opp_goalie: str | None = None,
            threshold: float = THRESHOLD, starter: str | None = None) -> Prediction:
    """`starter`: the player's team's probable starting goalie (NHL; saves props need it)."""
    team, opp = canon(team), canon(opp)
    pred = Prediction(player, stat, line, opp)
    if sport not in SUPPORTED:
        pred.notes.append(f"{sport.upper()} not supported: ESPN has no minutes or snap counts for it")
        return pred
    if not enricher.COLUMNS.get(sport, {}).get(enricher.stat_phrase(sport, stat)):
        pred.notes.append(f"{stat} isn't in ESPN game logs: no prediction")  # NHL hits / faceoffs / blocks, NBA dunks
        return pred
    injuries, me = injuries or {}, nk(player)
    status = _status(injuries, player, team)
    if is_out(status):
        pred.notes.append(f"player is {status}: no prediction")
        return pred
    goalie_prop = sport == "nhl" and stat_is(sport, stat, GOALIE_PROPS)
    if goalie_prop and starter is None:
        pred.notes.append(f"no probable starting goalie posted for {team} yet: no prediction")
        return pred
    if goalie_prop and nk(starter) != me:
        pred.notes.append(f"not {team}'s probable starter ({starter}): no prediction")
        return pred
    played = [g for g in lines if g.minutes > 0]
    h2h = [g for g in played if g.opp == opp]
    pred.n_h2h = len(h2h)
    if not h2h:
        pred.notes.append(f"no games against {opp} in the last {SEASONS} seasons")
        return pred
    current = max(g.season for g in played)
    recent = [g for g in sorted(played, key=lambda g: g.ts, reverse=True) if g.team == team][:RECENT_N]
    this_season = [boxes[g.event_id] for g in recent if g.event_id in boxes and g.season == current]
    mates = key_players(this_season, team, me, sport)  # tonight's roster, not every player he ever played with
    rivals = key_players([boxes[g.event_id] for g in h2h if g.event_id in boxes], opp, me, sport)
    out_tonight = {p for p in mates if is_out(_status_by_key(injuries, p, team))}
    out_tonight |= {("opp", p) for p in rivals if is_out(_status_by_key(injuries, p, opp))}
    goalie = nk(opp_goalie) if opp_goalie and stat_is(sport, stat, GOALIE_STATS) else None

    weights, sv = [], {}
    for g in h2h:
        w, why = SEASON_DECAY ** (current - g.season), []
        b = boxes.get(g.event_id)
        if g.team != team:
            why.append(f"on {g.team} then")
        if b is None:
            w *= MISSING_BOX_W
            why.append("no box score")
        else:
            for p in mates:
                then, now = b.played(team, p), p not in out_tonight
                if g.team == team and then != now:
                    why.append(f"{p} {'played' if then else 'out'} then, {'out' if then else 'plays'} tonight")
                    sv.setdefault(p, []).append(g)
            for p in rivals:
                if b.played(opp, p) and ("opp", p) in out_tonight:
                    why.append(f"{opp} {p} played then, out tonight")
            if goalie and b.goalie.get(opp) and b.goalie[opp] != goalie:
                why.append(f"{opp} goalie {b.goalie[opp]} then, {goalie} tonight")
        w *= MISMATCH_W ** min(MAX_MISMATCHES, sum(x != "no box score" for x in why))  # missing box: once, above
        weights.append(w)
        pred.games.append({"date": g.date[:10], "minutes": round(g.minutes, 1), "stat": g.value,
                           "weight": round(w, 3), "context": why})
    total = sum(weights)
    pred.n_eff = round(total ** 2 / sum(w * w for w in weights), 2)
    pred.h2h_rate = sum(w * g.value for w, g in zip(weights, h2h)) / sum(w * g.minutes for w, g in zip(weights, h2h))
    form = sorted(played, key=lambda g: g.ts, reverse=True)[:RECENT_N]
    pred.recent_rate = sum(g.value for g in form) / sum(g.minutes for g in form)
    prior = PRIOR_GAMES
    lo, hi = sorted((pred.h2h_rate, pred.recent_rate))
    if hi > 0 and hi > ROLE_RATIO * lo:  # a bench player now starting, or the reverse: the H2H era is stale
        prior = max(PRIOR_GAMES, pred.n_eff)
        pred.notes.append(f"role change: {pred.recent_rate:.3f}/min recently vs {pred.h2h_rate:.3f}/min in the H2H "
                          f"games, recent form weighted equal to H2H")
    pred.rate = (pred.n_eff * pred.h2h_rate + prior * pred.recent_rate) / (pred.n_eff + prior)

    factors = []  # teammates whose presence differs between the H2H games and tonight
    for p, games in ([] if goalie_prop else sv.items()):
        share = sum(w for w, g in zip(weights, h2h) if g in games) / total
        f, n_wo = usage_factor(recent, boxes, team, p)
        if f != 1.0:
            sign = 1 if p in out_tonight else -1
            factors.append((f ** (sign * share), p, sign, n_wo, share))
    if factors:  # the strongest one only: they come from the same few games and would double-count
        f, p, sign, n_wo, share = max(factors, key=lambda x: abs(math.log(x[0])))
        pred.usage = min(1 + USAGE_CAP, max(1 - USAGE_CAP, f))
        pred.notes.append(f"usage {'with' if sign < 0 else 'without'} {p}: x{pred.usage:.3f}"
                          + (f" (x{f:.3f} capped)" if pred.usage != f else "")
                          + f" ({n_wo} recent games without him, {share:.0%} of H2H weight differs"
                          + (f"; strongest of {len(factors)}" if len(factors) > 1 else "") + ")")

    if goalie_prop:
        pred.exp_minutes = GOALIE_MINUTES
        pred.notes.append(f"probable starter: a full game ({GOALIE_MINUTES:g} min) expected")
    else:
        pred.exp_minutes = expected_minutes(lines, status, pred.notes)
    if pred.exp_minutes is None:
        pred.notes.append("no recent minutes")
        return pred
    pred.mu = pred.rate * pred.usage * pred.exp_minutes
    var = DISPERSION * pred.mu + pred.mu ** 2 / (pred.n_eff + prior)  # game swings + the rate's own uncertainty
    over, pred.p_push = prob_over(pred.mu, line, var)
    decided = 1 - pred.p_push
    pred.p_over = over / decided if decided > 0 else None
    if pred.p_over is None:
        return pred
    capped = min(MAX_CONF, max(1 - MAX_CONF, pred.p_over))
    if capped != pred.p_over:
        pred.notes.append(f"P(over) {pred.p_over:.1%} capped at {capped:.0%} until calibration supports more")
        pred.p_over = capped
    gap = abs(pred.mu - line) / line if line >= LINE_GAP_MIN else 0.0
    blocked = []  # every reason is logged, not just the first
    if gap > LINE_GAP:
        blocked.append(f"projection {pred.mu:.1f} is {gap:.0%} from the line {line:g}: likely a role or minutes "
                       f"change the model misses, no call")
    if pred.n_eff < MIN_EFF_N:
        blocked.append(f"thin H2H: effective sample {pred.n_eff:.1f} < {MIN_EFF_N:g}, no call")
    pred.notes += blocked
    if blocked:
        pass
    elif pred.p_over >= threshold:
        pred.side = "More"
    elif 1 - pred.p_over >= threshold:
        pred.side = "Less"
    if pred.side == "More" and any(s in status for s in STATUS_MINUTES):
        pred.side = None
        pred.notes.append("questionable status: no More (injury_guard rule)")
    return pred


def _status_by_key(injuries: dict, key: str, team: str) -> str:
    rec = injuries.get((canon(team), tuple(key.split()))) if injuries else None
    return (rec or {}).get("status", "").lower()


def stat_is(sport: str, stat: str, phrases: set[str]) -> bool:
    return enricher.stat_phrase(sport, stat) in phrases


# --------------------------------------------------------------------------- fetch (cached)
class Cache:
    """SQLite key/value store: final box scores and past seasons never expire; the current season does."""

    def __init__(self, path: Path | str = CACHE_DB):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT, ts REAL)")

    def get(self, key: str, ttl: float | None = None):
        row = self.db.execute("SELECT value, ts FROM kv WHERE key=?", (key,)).fetchone()
        if row and (ttl is None or time.time() - row[1] < ttl):
            return json.loads(row[0])
        return None

    def put(self, key: str, value) -> None:
        self.db.execute("INSERT OR REPLACE INTO kv VALUES (?,?,?)", (key, json.dumps(value), time.time()))
        self.db.commit()


async def _cached(cache: Cache, key: str, ttl: float | None, fetch):
    hit = cache.get(key, ttl)
    if hit is None:
        hit = await fetch()
        cache.put(key, hit)
    return hit


async def fetch_context(player: str, sport: str, stat: str, team: str, opp: str, get=None,
                        cache: Cache | None = None, now: float | None = None) -> dict:
    """Everything predict() needs: {"lines", "boxes", "injuries", "opp_goalie", "starter", "error"}."""
    get, cache = get or enricher.http_get, cache or Cache()
    now = time.time() if now is None else now
    out = {"lines": [], "boxes": {}, "injuries": {}, "opp_goalie": None, "starter": None, "error": ""}
    if sport not in SUPPORTED:
        return out
    aid, why = await enricher.find_athlete(player, sport, team, get)
    if not aid:
        out["error"] = why
        return out
    url = enricher.GAMELOG.format(path=enricher.PATHS[sport], id=aid)
    current = season_of(datetime.fromtimestamp(now, timezone.utc).isoformat())
    for y in range(current, current - SEASONS, -1):
        params = {} if y == current else {"season": y}

        async def fetch(params=params, past=y != current):
            try:
                return await get(url, params)
            except Exception as e:  # noqa: BLE001
                if past and getattr(e, "status", None) == 404:
                    return {}  # a rookie has no log for seasons before he played: zero games, not an error
                raise
        log = await _cached(cache, f"log:{sport}:{aid}:{y}", CURRENT_TTL_S if y == current else None, fetch)
        out["lines"] += lines_from_log(log, sport, stat, y)
    played = [g for g in out["lines"] if g.minutes > 0]
    wanted = [g for g in played if g.opp == canon(opp)]
    wanted += [g for g in sorted(played, key=lambda g: g.ts, reverse=True) if g.team == canon(team)][:RECENT_N]
    path = enricher.PATHS[sport]
    for eid in dict.fromkeys(g.event_id for g in wanted if g.event_id):
        async def fetch_box(eid=eid):  # cache the parsed box only: a full summary is ~500 KB of play-by-play
            parsed = parse_box(await get(SUMMARY.format(path=path), {"event": eid}), sport, eid)
            return {"minutes": parsed.minutes, "goalie": parsed.goalie}
        try:
            got = await _cached(cache, f"box:{sport}:{eid}", None, fetch_box)
            out["boxes"][eid] = Box(eid, got["minutes"], got["goalie"])
        except Exception as e:  # noqa: BLE001 — one missing box only lowers that game's weight
            out["error"] = f"box {eid}: {type(e).__name__}"
    get1 = lambda u: get(u, {})  # injury_guard's fetches take a full URL
    try:
        out["injuries"] = await injury_guard.injuries(sport, get1)
    except Exception as e:  # noqa: BLE001 — without the feed every player counts as available
        out["error"] = f"injury feed: {type(e).__name__}"
    if sport == "nhl":
        day = (datetime.fromtimestamp(now, timezone.utc) - timedelta(hours=5)).strftime("%Y%m%d")
        try:
            probables = await injury_guard.starters(sport, day, get1)
            out["opp_goalie"] = (probables.get(canon(opp)) or (None,))[0]
            out["starter"] = (probables.get(canon(team)) or (None,))[0]
        except Exception:  # noqa: BLE001
            pass
    return out


async def run(player: str, sport: str, stat: str, line: float, team: str, opp: str, **kw) -> Prediction:
    ctx = await fetch_context(player, sport, stat, team, opp, **kw)
    pred = predict(player, sport, stat, line, team, opp, ctx["lines"], ctx["boxes"], ctx["injuries"], ctx["opp_goalie"],
                   starter=ctx["starter"])
    if ctx["error"]:
        pred.notes.append(ctx["error"])
    return pred


def main() -> None:
    ap = argparse.ArgumentParser(description="H2H per-minute projection with absence context (ESPN, no key).")
    ap.add_argument("player")
    ap.add_argument("--sport", choices=SUPPORTED, required=True)
    ap.add_argument("--team", required=True, help="the player's team code, e.g. LAL")
    ap.add_argument("--opp", required=True, help="tonight's opponent code, e.g. OKC")
    ap.add_argument("--stat", required=True)
    ap.add_argument("--line", type=float, required=True)
    a = ap.parse_args()
    p = asyncio.run(run(a.player, a.sport, a.stat, a.line, a.team, a.opp))
    print(f"{p.player} {p.stat} {p.line:g} vs {p.opp}: "
          + (f"{p.side or 'no call'} · P(over) {p.p_over:.1%} · projection {p.mu:.2f} "
             f"({p.rate:.3f}/min [H2H {p.h2h_rate:.3f}, recent {p.recent_rate:.3f}] x usage {p.usage:.3f} "
             f"x {p.exp_minutes:.1f} min) · "
             f"{p.n_h2h} H2H games, effective {p.n_eff:.1f}" if p.p_over is not None else "no prediction"))
    for g in p.games:
        print(f"  {g['date']}  {g['minutes']:>5} min  stat {g['stat']:g}  weight {g['weight']:.3f}"
              + (f"  ({'; '.join(g['context'])})" if g["context"] else ""))
    for n in p.notes:
        print(f"  note: {n}")


if __name__ == "__main__":
    main()
