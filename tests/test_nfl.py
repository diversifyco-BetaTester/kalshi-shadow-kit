import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from local import ev_pool
from local import fetch_odds as fo
from local import prize_picks_ev_scraper as scanner
from local.parse_board import parse_json
from local.sentinel import mixed_entries

NOW = datetime(2026, 10, 15, 22, 0, tzinfo=timezone.utc).timestamp()  # 2 h 15 before kickoff
KICKOFF = "2026-10-16T00:15:00Z"  # Thu 8:15 PM ET -> 26OCT15
EVENTS = [{"id": "nfl1", "commence_time": KICKOFF, "home_team": "Denver Broncos", "away_team": "Seattle Seahawks"}]


def o(name, point, price, who="Jaxon Smith-Njigba"):
    return {"name": name, "description": who, "price": price, "point": point}


MAIN = [o("Over", 92.5, 1.87), o("Under", 92.5, 1.95)]
ALT = [o("Over", 79.5, 1.45), o("Over", 89.5, 1.80), o("Over", 99.5, 2.25), o("Over", 109.5, 2.80)]


class NflAPI:
    def __init__(self):
        self.calls = []

    async def __call__(self, url, params):
        if url.endswith("/events"):
            return 200, EVENTS, {"x-requests-last": "0", "x-requests-remaining": "400"}
        self.calls.append(params["markets"])
        mk = params["markets"].split(",")
        markets = ([{"key": "player_reception_yds", "outcomes": MAIN}] if "player_reception_yds" in mk else []) + \
                  ([{"key": "player_reception_yds_alternate", "outcomes": ALT}]
                   if "player_reception_yds_alternate" in mk else [])
        return 200, {"bookmakers": [{"key": "draftkings", "markets": markets}]}, \
            {"x-requests-last": str(len(markets)), "x-requests-remaining": "400"}


def test_nfl_team_codes_cover_both_spellings():
    assert fo.team_codes("nfl", "Los Angeles Rams") == {"LA", "LAR"}
    assert fo.team_codes("nfl", "Washington Commanders") == {"WAS", "WSH"}
    assert fo.team_codes("nfl", "Jacksonville Jaguars") == {"JAC", "JAX"}
    assert fo.team_codes("nfl", "San Francisco 49ers") == {"SF"}
    assert fo.team_codes("nfl", "Los Angeles Chargers") == {"LAC"} and fo.team_codes("nfl", "New York Jets") == {"NYJ"}


def test_yardage_ladders_interpolate_across_ten_yard_rungs():
    rungs = fo.ladder(MAIN, ALT)
    assert fo.read_ladder(rungs, 94.5) is None                                   # 2-unit limit: counting stats
    p, interp = fo.read_ladder(rungs, 94.5, fo.LADDER_GAP[("nfl", "player_reception_yds")])
    assert interp and dict(rungs)[99.5] < p < dict(rungs)[92.5]
    assert fo.read_ladder(rungs, 139.5, 25.0) is None                            # past the top rung


def leg(line):
    return scanner.Leg("Jaxon Smith-Njigba", "receiving yards", line, "DEN v SEA", team="SEA", start_time=KICKOFF,
                       sport="nfl")


def test_nfl_main_and_off_market_lines_price_end_to_end(tmp_path, books_ladder_full):
    api = NflAPI()
    b = fo.OddsBook("k", cache=tmp_path / "c.json", get=api)
    main, exact_alt, between = leg(92.5), leg(79.5), leg(84.5)
    assert asyncio.run(b.price("nfl", [main, exact_alt, between], NOW)) == 3
    assert api.calls == ["player_reception_yds", "player_reception_yds_alternate"]
    assert fo.sources(main) == "Consensus: DK" and fo.sources(exact_alt) == "Consensus: DK · alt" and fo.sources(between) == "Consensus: DK · interp"
    assert b.usage()["used"] == 2


def test_nfl_outside_window_spends_nothing(tmp_path):
    api = NflAPI()
    b = fo.OddsBook("k", cache=tmp_path / "c.json", get=api)
    assert asyncio.run(b.price("nfl", [leg(92.5)], NOW - 24 * 3600)) == 0         # kickoff > 8 h away
    assert api.calls == []


BOARD = Path.home() / "Downloads" / "nhl_board.json"  # every bookmarklet download carries all leagues


@pytest.mark.skipif(not BOARD.exists(), reason="no bookmarklet board on this machine")
def test_real_board_nfl_stats_map_to_priced_markets():
    rows, _ = parse_json(json.loads(BOARD.read_text()), "nfl")
    stats = {r["stat"] for r in rows if r["odds_type"] == "standard"}
    for want in ("receiving yards", "rushing yards", "passing yards", "receptions", "passing attempts"):
        assert want in stats and want in fo.MARKETS["nfl"]
    assert all("/" not in r["team"] for r in rows)                               # combo props skipped


def test_nfl_legs_pool_into_mixed_slips(tmp_path):
    def l(sport, name, team, game, p):
        x = scanner.Leg(name, "receiving yards", 50.5, game, team=team, start_time=KICKOFF, sport=sport)
        x.quotes = [scanner.Quote("kalshi", name, name, p, 0.02)]
        return x
    pool = ev_pool.EvPool(tmp_path / "pool.json")
    pool.publish("nfl", [(l("nfl", f"F{i}", "SEA", f"SEA{i} v DEN{i}", .58), "DK") for i in range(3)], NOW)
    pool.publish("nhl", [(l("nhl", f"H{i}", "COL", f"COL{i} v DAL{i}", .57), "") for i in range(3)], NOW)
    nfl_seen = pool.others("nhl", NOW)
    assert {x.sport for x in nfl_seen} == {"nfl"} and len(nfl_seen) == 3
    own = [l("nhl", f"H{i}", "COL", f"COL{i} v DAL{i}", .57) for i in range(3)]
    entries = mixed_entries(own, nfl_seen, "nhl")
    assert entries and all({x.sport for x in e[2]} == {"nfl", "nhl"} for e in entries)


def test_whole_number_lines_never_enter_slips():
    def l(name, line, p, team):
        x = scanner.Leg(name, "touchdowns", line, "A v B", team=team, sport="nfl")
        x.quotes = [scanner.Quote("kalshi", name, name, p, 0.02)]
        return x
    whole = [l(f"W{i}", 1.0, 0.2, "AB"[i % 2]) for i in range(6)]          # "Less 1 TD" at 80%: pushes ignored
    assert all(x.whole_line for x in whole) and not l("H", 0.5, 0.4, "A").whole_line
    assert scanner.rank_entries(whole) == []
    from local.sentinel import flex_entries
    half = [l(f"H{i}", 0.5, 0.42, "AB"[i % 2]) for i in range(6)]
    assert all(not any(x.whole_line for x in e[2]) for e in scanner.rank_entries(whole + half))
