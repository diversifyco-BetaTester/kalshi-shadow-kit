"""
correlation.py — which legs may share a slip, and what a same-game pair does to the slip's EV.

The slip EV in prize_picks_ev_scraper assumes independent legs. Two legs from one game are not:
a QB's passing yards and his receiver's receiving yards rise and fall together; two running backs
on one team split the same carries. So, per slip:

  * at most 2 legs from any one game, and two same-game legs must be a recognized positive STACK;
  * stacks only when pipeline_config optimizer.allow_stacks is true (default FALSE since 2026-10-17: a
    recognized stack is then rejected like any same-game pair, live builder and paper optimizer alike,
    until joint outcomes are modeled from graded data); when allowed, at most MAX_STACKS (1) per slip, so every other leg comes from a different game: the
    correlations are assumptions, and several of them compound into an inflated EV and stake;
  * a known conflict is a hard VETO with a reason;
  * any other same-game pair is rejected as UNMODELED (its correlation could be either sign);
  * legs from different games stay independent.

STACKS (both legs "More"; RHO is an assumed correlation of the two stats, capped at RHO_CAP = 0.22,
to be calibrated against graded slips in local/slips_history.csv):
  passing stack   NFL/CFB QB passing yards/TDs/completions + same-team receiving yards/receptions
  goalie stack    NHL opposing goalies' saves: both rise with total shot volume. There is no pace data
                  in any feed, so the low RHO stands in for "high-pace game"; each goalie leg must still
                  clear --min-ev on its own.
  playmaker stack NBA guard (position has G) assists + same-team teammate points or threes
  table setter    MLB batter in lineup spots 1-3 runs + same-team teammate in spots 2-5 RBIs. Lineup
                  spots come from ESPN once the lineup is posted (injury_guard.annotate_lineups); until
                  then the pair isn't recognized, so it's rejected like any unmodeled same-game pair
  hits allowed    MLB pitcher hits allowed + an opposing batter's hits or total bases
VETOES: same team, two players "More" on one capped volume pool (NFL/CFB rushing, NBA points);
an RB "More" rushing attempts with his QB "More" passing attempts (opposite game scripts); two NBA
frontcourt players (C, PF, F) on one team both "More" rebounds; an NBA player's turnovers "More" with
an opposing player's steals "Less"; an MLB pitcher's strikeouts "More" with an opposing batter's
strikeouts "Less" or hits "More"; an MLB pitcher's earned runs "Less" with an opposing batter's RBIs
"More". Every pair is checked in both orders. Roles come from PrizePicks' position field, so a leg
without one can't start a role-based stack.

EV with a stack: the pair's joint hit probability comes from a bivariate normal copula with the
pair's RHO (sign flipped for each "Less"), convolved with the independent legs. PrizePicks may
shade the payout of a correlated entry; the board data shows no such field, so the factor is a
setting (--stack-payout-factor, default 1.0 = none observed). A stacked slip whose EV after that
factor is <= 0 is rejected, and the builder keeps the best independent slip instead.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache
from statistics import NormalDist

from local import pipeline_config
from local import prize_picks_ev_scraper as scanner

PASSING = {"passing yards", "passing touchdowns", "passing completions"}
RECEIVING = {"receiving yards", "receptions"}
RHO_CAP = 0.22  # no assumed correlation above this: 0.35 let stacks dominate EV and stakes (2026-10-16)
RHO = {"passing": 0.22, "goalies": 0.15, "playmaker": 0.15, "table_setter": 0.18, "hits_allowed": 0.12}
assert all(v <= RHO_CAP for v in RHO.values()), "every assumed correlation must stay at or below RHO_CAP"
NBA_SCORING = {"points", "threes"}
MLB_PITCHER = {"strikeouts", "hits allowed", "walks allowed", "earned runs", "pitching outs"}
FOOTBALL = {"nfl", "cfb"}
CAPPED = {  # sport -> stat families two teammates can't both go "More" on
    "nfl": [{"rushing yards", "rushing attempts"}], "cfb": [{"rushing yards", "rushing attempts"}],
    "nba": [{"points", "points + rebounds + assists", "points + rebounds", "points + assists"}],
}
MAX_PER_GAME = 2
MAX_STACKS = 1
_N = NormalDist()


@dataclass(frozen=True)
class Stack:
    a: scanner.Leg
    b: scanner.Leg
    rho: float
    label: str


def game_id(l: scanner.Leg) -> tuple:
    return l.sport, frozenset(t.strip() for t in l.game.replace("@", " v ").split(" v ") if t.strip()), l.game_date


def _more(l: scanner.Leg) -> bool:
    return bool(l.pick) and l.pick[0] == "More"


def _less(l: scanner.Leg) -> bool:
    return bool(l.pick) and l.pick[0] == "Less"


def _short(l: scanner.Leg) -> str:
    return {"passing yards": "Pass", "passing touchdowns": "Pass TD", "passing completions": "Comp",
            "receiving yards": "Rec", "receptions": "Rec", "saves": "Saves", "assists": "Ast", "points": "Pts",
            "threes": "3PM", "rebounds": "Reb", "runs": "Runs", "rbis": "RBI", "hits": "Hits",
            "total bases": "TB", "hits allowed": "H Allowed"}.get(l.stat, l.stat.title())


def _last(l: scanner.Leg) -> str:
    words = [w for w in l.player.split() if w.lower().rstrip(".") not in {"jr", "sr", "ii", "iii", "iv", "v"}]
    return words[-1] if words else l.player


def _roles(l: scanner.Leg) -> set[str]:
    return {p for p in l.position.upper().replace("/", "-").split("-") if p}


def _guard(l: scanner.Leg) -> bool:
    return bool(_roles(l) & {"G", "PG", "SG"})


def _frontcourt(l: scanner.Leg) -> bool:
    r = _roles(l)
    return bool(r & {"C", "PF"}) or r == {"F"}


def _stack(rule: str, a: scanner.Leg, b: scanner.Leg, opposing: bool = False) -> tuple[str, Stack]:
    label = (f"{a.team} {_last(a)} {_short(a)} + {b.team} {_last(b)} {_short(b)}" if opposing
             else f"{a.team} {_last(a)} {_short(a)} + {_last(b)} {_short(b)}")
    return "stack", Stack(a, b, RHO[rule], label)


def _directional(x: scanner.Leg, y: scanner.Leg) -> tuple[str, Stack | str] | None:
    """Rules where the two legs play different parts (QB/receiver, pitcher/batter...): x as the first."""
    same_team = bool(x.team) and x.team == y.team
    opposing = bool(x.team) and bool(y.team) and x.team != y.team
    # --- positive stacks
    if x.sport in FOOTBALL and same_team and _more(x) and _more(y) and x.stat in PASSING and y.stat in RECEIVING:
        return _stack("passing", x, y)
    if (x.sport == "nba" and same_team and _more(x) and _more(y) and x.stat == "assists" and _guard(x)
            and y.stat in NBA_SCORING):
        return _stack("playmaker", x, y)
    if (x.sport == "mlb" and same_team and _more(x) and _more(y) and x.stat == "runs" and y.stat == "rbis"
            and x.bat_order in (1, 2, 3) and y.bat_order in (2, 3, 4, 5)):
        return _stack("table_setter", x, y)
    if (x.sport == "mlb" and opposing and _more(x) and _more(y) and x.stat == "hits allowed"
            and y.stat in ("hits", "total bases")):
        return _stack("hits_allowed", x, y, opposing=True)
    # --- toxic conflicts
    if x.sport in FOOTBALL and same_team and _more(x) and _more(y) and (x.stat, y.stat) == ("rushing attempts", "passing attempts"):
        return "veto", f"{x.player} and {y.player}: More rush attempts with More pass attempts (opposite scripts)"
    if x.sport == "nba" and opposing and x.stat == "turnovers" and _more(x) and y.stat == "steals" and _less(y):
        return "veto", f"{x.player} More turnovers with opponent {y.player} Less steals (the same possessions)"
    if x.sport == "mlb" and opposing and x.stat == "strikeouts" and _more(x) and (
            (y.stat == "hitter strikeouts" and _less(y)) or (y.stat == "hits" and _more(y))):
        return "veto", f"pitcher {x.player} More strikeouts against opposing batter {y.player} {y.pick[0]} {y.stat}"
    if x.sport == "mlb" and opposing and x.stat == "earned runs" and _less(x) and y.stat == "rbis" and _more(y):
        return "veto", f"pitcher {x.player} Less earned runs against opposing batter {y.player} More RBIs"
    return None


def classify(x: scanner.Leg, y: scanner.Leg) -> tuple[str, Stack | str]:
    """('stack', Stack) | ('veto', reason) | ('unmodeled', reason) for two legs from one game."""
    same_team = bool(x.team) and x.team == y.team
    for a, b in ((x, y), (y, x)):
        hit = _directional(a, b)
        if hit:
            return hit
    if (x.sport == "nhl" and x.stat == y.stat == "saves" and _more(x) and _more(y)
            and x.team and y.team and x.team != y.team):
        return "stack", Stack(x, y, RHO["goalies"], f"{_last(x)} {_short(x)} + {_last(y)} {_short(y)} (opposing goalies)")
    if same_team and _more(x) and _more(y):
        for family in CAPPED.get(x.sport, []):
            if x.stat in family and y.stat in family:
                return "veto", f"{x.player} and {y.player} both More on {x.team}'s {sorted(family)[0].split()[0]} volume"
        if x.sport == "nba" and x.stat == y.stat == "rebounds" and _frontcourt(x) and _frontcourt(y):
            return "veto", f"{x.player} and {y.player}: two {x.team} frontcourt players both More rebounds (same boards)"
    return "unmodeled", f"{x.player} + {y.player}: same game, correlation not modeled"


def check(combo) -> tuple[bool, list[Stack], str]:
    """(allowed, stacks, why not). Legs from different games are independent."""
    by_game: dict[tuple, list[scanner.Leg]] = {}
    for l in combo:
        by_game.setdefault(game_id(l), []).append(l)
    stacks = []
    for legs in by_game.values():
        if len(legs) > MAX_PER_GAME:
            return False, [], f"{len(legs)} legs from one game (max {MAX_PER_GAME})"
        if len(legs) == 2:
            kind, what = classify(*legs)
            if kind != "stack":
                return False, [], what
            stacks.append(what)
    limit = MAX_STACKS if pipeline_config.CONFIG["optimizer"]["allow_stacks"] else 0
    if len(stacks) > limit:
        if not limit:  # stacks excluded (pipeline_config optimizer.allow_stacks: false, the default)
            return False, [], f"correlated pair excluded until joint outcomes are modeled: {stacks[0].label}"
        return False, [], f"{len(stacks)} correlated pairs (max {limit} per slip)"
    return True, stacks, ""


# --------------------------------------------------------------------------- probability
@lru_cache(maxsize=4096)
def both_win(p1: float, p2: float, rho: float, n: int = 400) -> float:
    """P(A and B) for win probabilities p1, p2 under a bivariate normal copula with correlation rho:
    Phi2(z1, z2; rho), by integrating phi(x) * Phi((z2 - rho x) / sqrt(1 - rho^2)) up to z1."""
    if abs(rho) < 1e-12:
        return p1 * p2
    z1, z2 = _N.inv_cdf(min(max(p1, 1e-9), 1 - 1e-9)), _N.inv_cdf(min(max(p2, 1e-9), 1 - 1e-9))
    lo, s = -8.0, math.sqrt(1 - rho * rho)
    h = (z1 - lo) / n
    total = 0.0
    for i in range(n + 1):  # Simpson's rule
        x = lo + i * h
        w = 1 if i in (0, n) else 4 if i % 2 else 2
        total += w * _N.pdf(x) * _N.cdf((z2 - rho * x) / s)
    return total * h / 3


def hit_distribution(combo, stacks: list[Stack]) -> list[float]:
    """P(exactly k legs hit): stacked pairs from their joint distribution, every other leg independent."""
    paired = {id(s.a) for s in stacks} | {id(s.b) for s in stacks}
    dist = scanner.hit_distribution([l.pick[1] for l in combo if id(l) not in paired])
    for s in stacks:
        p1, p2 = s.a.pick[1], s.b.pick[1]
        sign = (1 if _more(s.a) else -1) * (1 if _more(s.b) else -1)
        p11 = both_win(p1, p2, s.rho * sign)
        pair = [1 - p1 - p2 + p11, p1 + p2 - 2 * p11, p11]  # 0, 1, 2 hits
        out = [0.0] * (len(dist) + 2)
        for i, a in enumerate(dist):
            for j, b in enumerate(pair):
                out[i + j] += a * b
        dist = out
    return dist


def slip_ev(combo, table: dict[int, float], stacks: list[Stack], payout_factor: float = 1.0) -> float:
    """Expected return per $1 minus the $1; a stacked slip's payouts are scaled by payout_factor."""
    dist = hit_distribution(combo, stacks)
    factor = payout_factor if stacks else 1.0
    return sum(dist[k] * mult * factor for k, mult in table.items() if k < len(dist)) - 1
