"""Live NBA L10 profile from ESPN's game log, with the column layout ESPN returned on 2026-10-23."""
import asyncio
from datetime import datetime, timezone

import pytest

from local import enricher, stat_research
from local import prize_picks_ev_scraper as scanner
from tests.test_enricher import gamelog, stub

NOW = datetime(2026, 11, 20, 18, 0, tzinfo=timezone.utc).timestamp()
# the exact `names` of a live ESPN NBA game log (2026-10-23)
NAMES = ["minutes", "fieldGoalsMade-fieldGoalsAttempted", "fieldGoalPct", "threePointFieldGoalsMade-threePointFieldGoalsAttempted",
         "threePointPct", "freeThrowsMade-freeThrowsAttempted", "freeThrowPct", "totalRebounds", "assists", "blocks",
         "steals", "fouls", "turnovers", "points"]


def cells(mins, pts, reb, ast):
    return [str(mins), "9-18", "50.0", "2-6", "33.3", "3-4", "75.0", str(reb), str(ast), "1", "1", "2", "3", str(pts)]


GAMES = [(f"2026-11-{19 - i:02d}T00:00:00.000+00:00", "DET", cells(m, p, r, a))
         for i, (m, p, r, a) in enumerate([(28, 18, 5, 4), (30, 22, 6, 7), (29, 20, 4, 5), (36, 27, 8, 6), (37, 25, 7, 9),
                                           (0, 0, 0, 0), (38, 30, 9, 8), (35, 24, 6, 5), (36, 26, 7, 6), (37, 28, 8, 7),
                                           (36, 21, 6, 4), (35, 19, 5, 3)])]
LOG = gamelog(NAMES, GAMES, preseason=[("2026-10-10T00:00:00.000+00:00", "BOS", cells(20, 40, 15, 15))])


def test_l10_profile_minutes_points_rebounds_assists():
    p = stat_research.profile_from_log(LOG, NOW)
    assert p["n"] == 10 and p["games"][0] == {"date": "2026-11-19", "opp": "DET", "min": 28.0, "pts": 18.0, "reb": 5.0, "ast": 4.0}
    assert all(g["min"] > 0 for g in p["games"])                      # the 0-minute DNP is skipped, not a 0-point game
    l10 = [g for g in GAMES if g[2][0] != "0"][:10]
    want = lambda col: sum(float(g[2][col]) for g in l10) / 10
    assert p["avg"] == {"min": pytest.approx(want(0)), "pts": pytest.approx(want(13)), "reb": pytest.approx(want(7)),
                        "ast": pytest.approx(want(8))}
    assert p["min_l3"] == pytest.approx(29.0) and p["min_trend"] == pytest.approx(29.0 / want(0) - 1, abs=1e-4)
    assert p["min_trend"] < -0.15                                      # rotation dip: logged, never a veto


def test_preseason_and_last_season_are_left_out():
    old = gamelog(NAMES, [("2026-04-10T00:00:00.000+00:00", "BOS", cells(35, 30, 8, 8))])
    assert stat_research.profile_from_log(old, NOW) == {"n": 0, "games": [], "avg": None, "min_l3": None,
                                                         "min_trend": None, "note": "no current-season games"}
    pre_only = gamelog(NAMES, [], preseason=[("2026-11-18T00:00:00.000+00:00", "BOS", cells(20, 40, 15, 15))])
    assert stat_research.profile_from_log(pre_only, NOW)["n"] == 0
    one = gamelog(NAMES, GAMES[:1])
    assert stat_research.profile_from_log(one, NOW)["min_trend"] is None   # needs more than 3 games


def test_profile_is_cached_per_player_no_repeat_requests():
    enricher._cache.clear()
    get = stub([("4065648", "Jayson Tatum", "nba", "Boston Celtics", True)], {"4065648": LOG})
    a = asyncio.run(stat_research.nba_profile("Jayson Tatum", "BOS", get, NOW))
    b = asyncio.run(stat_research.nba_profile("Jayson Tatum", "BOS", get, NOW))
    assert a == b and a["n"] == 10 and len(get.calls) == 2              # one search + one game log, then the cache
    l = scanner.Leg("Jayson Tatum", "points", 24.5, "BOS v DET", team="BOS", sport="nba")
    l.quotes = [scanner.Quote("kalshi", "x", "x", 0.45, 0.02)]           # Less 55%
    rec = asyncio.run(stat_research.stat_gap(l, get, NOW))
    assert len(get.calls) == 2                                          # the gap reuses the cached log too
    assert rec["profile"]["n"] == 10 and rec["side"] == "Less" and rec["gap"] == pytest.approx(rec["hit_rate"] - 0.55)


def test_failures_return_a_reason_and_never_raise():
    enricher._cache.clear()
    get = stub([], {})
    assert asyncio.run(stat_research.nba_profile("Nobody Atall", None, get, NOW)) == {"n": 0, "games": [],
                                                                                       "note": "player not found"}
    missing = stub([("9", "Some Player", "nba", "Boston Celtics", True)], {})   # game log 404s
    assert asyncio.run(stat_research.nba_profile("Some Player", None, missing, NOW))["note"] == "ClientResponseError"


def test_minutes_are_a_known_nba_stat_and_parse_both_formats():
    assert enricher.COLUMNS["nba"]["minutes"] == ["min:minutes"]
    assert enricher.row_value(["min:minutes"], {"minutes": "38"}) == 38.0
    assert enricher.row_value(["min:minutes"], {"minutes": "36:30"}) == 36.5
