import asyncio
import json
from datetime import datetime
from pathlib import Path

from local import ev_pool
from local import fetch_odds as fo
from local import grade_slips as gs
from local import prize_picks_ev_scraper as scanner
from local.parse_board import SPORTS, parse_json

TIP = "2026-10-20T23:30:00Z"  # 7:30 PM ET -> 26OCT20
NOW = datetime.fromisoformat(TIP.replace("Z", "+00:00")).timestamp() - 2 * 3600  # 2 h before tip


def test_prizepicks_labels_map_to_odds_markets():
    props = [{"player": "Jayson Tatum", "team": "BOS", "league": "NBA", "stat": s, "line": 24.5, "opponent": "DET",
              "start_time": TIP, "odds_type": "standard"}
             for s in ("Points", "Rebounds", "Assists", "3-PT Made", "Blocked Shots", "Steals", "Turnovers",
                       "Pts+Rebs+Asts", "Pts+Rebs", "Pts+Asts", "Rebs+Asts", "Blks+Stls")]
    rows, _ = parse_json({"props": props}, "nba")
    assert len(rows) == 12 and all(r["stat"] in fo.MARKETS["nba"] for r in rows)
    assert "KXNBAPTS" in SPORTS["nba"]["series"]


def test_kalshi_titles_match_despite_wording():
    def leg(stat):
        return scanner.Leg("Jayson Tatum", stat, 2.5, "BOS v DET", team="BOS")
    k = lambda title: scanner.matches(leg(title_stat[title]), title, title)
    title_stat = {"Jayson Tatum: 3+ threes": "threes", "Jayson Tatum: 3+ three pointers": "threes",
                  "Jayson Tatum: 3+ 3-pointers made": "threes",
                  "Jayson Tatum: 3+ points, rebounds and assists": "points + rebounds + assists",
                  "Jayson Tatum: 3+ steals + blocks": "blocks + steals", "Jayson Tatum: 3+ points": "points"}
    assert all(k(t) for t in title_stat)
    assert not scanner.matches(leg("points"), "Jayson Tatum: 3+ points + rebounds", "Jayson Tatum: 3+ points + rebounds")
    assert scanner.stat_key("hits") != scanner.stat_key("hits allowed")
    assert scanner.stat_key("hits + runs + rbis") == scanner.stat_key("hits runs and rbis")


def test_nba_team_codes_and_shared_points_key_gaps():
    assert fo.team_codes("nba", "Golden State Warriors") == {"GS", "GSW"}
    assert fo.team_codes("nba", "Portland Trail Blazers") == {"POR"} and fo.team_codes("nba", "Philadelphia 76ers") == {"PHI"}
    assert fo.team_codes("nba", "Los Angeles Lakers") == {"LAL"} and fo.team_codes("nba", "Sacramento Kings") == {"SAC"}
    assert fo.LADDER_GAP[("nba", "player_points")] == 5.0 and ("nhl", "player_points") not in fo.LADDER_GAP


def test_nba_milestone_ladder_prices_off_market_points(tmp_path, books_ladder_full):
    def o(name, point, price):
        return {"name": name, "description": "Jayson Tatum", "price": price, "point": point}
    main = [o("Over", 26.5, 1.90), o("Under", 26.5, 1.90)]
    alt = [o("Over", 19.5, 1.25), o("Over", 24.5, 1.55), o("Over", 29.5, 2.60)]
    events = [{"id": "nba1", "commence_time": TIP, "home_team": "Detroit Pistons", "away_team": "Boston Celtics"}]

    async def api(url, params):
        if url.endswith("/events"):
            return 200, events, {"x-requests-last": "0", "x-requests-remaining": "400"}
        mk = params["markets"].split(",")
        markets = ([{"key": "player_points", "outcomes": main}] if "player_points" in mk else []) + \
                  ([{"key": "player_points_alternate", "outcomes": alt}] if "player_points_alternate" in mk else [])
        return 200, {"bookmakers": [{"key": "fanduel", "markets": markets}]}, {"x-requests-last": str(len(markets)), "x-requests-remaining": "400"}

    exact, between = (scanner.Leg("Jayson Tatum", "points", line, "BOS v DET", team="BOS", start_time=TIP, sport="nba")
                      for line in (24.5, 27.5))
    b = fo.OddsBook("k", cache=tmp_path / "c.json", get=api)
    assert asyncio.run(b.price("nba", [exact, between], NOW)) == 2
    assert fo.sources(exact) == "Consensus: FD · alt" and fo.sources(between) == "Consensus: FD · interp"
    assert exact.pick == ("More", exact.quotes[0].p_yes) and exact.pick[1] > 0.5


def test_nba_grading_and_dnp_is_void():
    keys = ["minutes", "points", "threePointFieldGoalsMade-threePointFieldGoalsAttempted", "rebounds", "assists",
            "turnovers", "steals", "blocks"]
    summary = {"boxscore": {"players": [{"team": {"abbreviation": "GS"}, "statistics": [{"keys": keys, "athletes": [
        {"athlete": {"displayName": "Stephen Curry"}, "stats": ["34", "31", "6-12", "5", "7", "3", "2", "0"]},
        {"athlete": {"displayName": "Draymond Green"}, "stats": [], "didNotPlay": True}]}]}]}}
    board = {"events": [{"id": "g", "date": TIP, "status": {"type": {"name": "STATUS_FINAL", "completed": True}},
                         "competitions": [{"competitors": [{"team": {"abbreviation": a}} for a in ("GS", "LAL")]}]}]}
    g = gs.Grader(lambda url: board if "scoreboard" in url else summary)
    leg = lambda player, stat, line, side="More": g.leg({"sport": "nba", "player": player, "team": "GSW", "stat": stat,
                                                         "line": line, "side": side, "start_time": TIP})
    assert leg("Stephen Curry", "threes", 4.5) == {"result": "hit", "actual": 6.0, "note": ""}
    assert leg("Stephen Curry", "points + rebounds + assists", 44.5)["actual"] == 43.0
    assert leg("Stephen Curry", "blocks + steals", 1.5, "Less")["result"] == "miss"
    assert leg("Draymond Green", "rebounds", 6.5)["result"] == "void"                 # DNP row, not a zero


def test_nba_legs_pool_with_other_sports(tmp_path):
    pool = ev_pool.EvPool(tmp_path / "pool.json")
    l = scanner.Leg("Jayson Tatum", "points", 26.5, "BOS v DET", team="BOS", start_time=TIP, sport="nba")
    l.quotes = [scanner.Quote("books", "x", "FD de-vigged", 0.58, 0.0)]
    pool.publish("nba", [(l, "FD")], NOW)
    seen = pool.others("nfl", NOW)
    assert [(x.sport, x.player, x.pick[0]) for x in seen] == [("nba", "Jayson Tatum", "More")]


def test_sync_script_writes_four_boards(tmp_path):
    import subprocess
    (tmp_path / "board.json").write_text(json.dumps({"props": [{"league": l} for l in ("NHL", "MLB", "NFL", "NBA")]}))
    out = subprocess.run([str(Path(__file__).parents[1] / "ops" / "sync_boards.sh")], env={"DOWNLOADS": str(tmp_path),
                         "PATH": "/usr/bin:/bin:/usr/local/bin:/Library/Frameworks/Python.framework/Versions/3.14/bin"},
                         capture_output=True, text=True)
    assert out.returncode == 0 and out.stdout.strip() == \
        "Boards synced for NHL MLB NFL NBA. Not in this download, previous board kept: CFB SOCCER."
    assert sorted(p.name for p in tmp_path.iterdir()) == ["mlb_board.json", "nba_board.json", "nfl_board.json", "nhl_board.json"]


def test_sync_keeps_boards_for_sports_missing_from_the_download(tmp_path):
    import os
    import subprocess
    old = tmp_path / "nhl_board.json"
    old.write_text('{"props": [{"league": "NHL"}]}')
    os.utime(old, (1_700_000_000, 1_700_000_000))
    (tmp_path / "board.json").write_text(json.dumps({"props": [{"league": "NFL"}, {"league": "WNBA"}]}))
    out = subprocess.run([str(Path(__file__).parents[1] / "ops" / "sync_boards.sh")], env={"DOWNLOADS": str(tmp_path),
                         "PATH": "/usr/bin:/bin:/usr/local/bin:/Library/Frameworks/Python.framework/Versions/3.14/bin"},
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "Boards synced for NFL. Not in this download, previous board kept: NHL MLB NBA CFB SOCCER."
    assert old.stat().st_mtime == 1_700_000_000 and "NHL" in old.read_text()          # untouched, age honest
    assert (tmp_path / "nfl_board.json").exists() and not (tmp_path / "mlb_board.json").exists()


def test_csv_carries_its_boards_age_through_a_board_without_this_sport(tmp_path):
    import os
    from local.sentinel import refresh_csv
    feed, out = tmp_path / "nhl_board.json", tmp_path / "lines_nhl.csv"
    feed.write_text(json.dumps({"props": [{"player": "A B", "team": "BOS", "league": "NHL", "stat": "Points", "line": 0.5,
                                           "opponent": "NYR", "start_time": TIP, "odds_type": "standard"}]}))
    os.utime(feed, (1_700_000_000, 1_700_000_000))
    seen = refresh_csv(feed, out, "nhl", ())
    assert out.stat().st_mtime == 1_700_000_000                                        # stamped with the board's time
    feed.write_text(json.dumps({"props": [{"player": "C D", "team": "BUF", "league": "NFL", "stat": "Rush Yards",
                                           "line": 50.5, "opponent": "MIA", "start_time": TIP, "odds_type": "standard"}]}))
    refresh_csv(feed, out, "nhl", seen)                                                 # NFL-only file: keep CSV
    assert out.stat().st_mtime == 1_700_000_000 and "A B" in out.read_text()


def test_nba_queries_preseason_and_regular_season_and_prices_each_from_its_own_key(tmp_path):
    pre = {"id": "pre1", "commence_time": TIP, "home_team": "Detroit Pistons", "away_team": "Boston Celtics"}
    reg = {"id": "reg1", "commence_time": "2026-10-21T00:00:00Z", "home_team": "Los Angeles Lakers",
           "away_team": "Golden State Warriors"}
    calls = []

    async def api(url, params):
        calls.append(url.split("/v4/sports/")[1])
        if url.endswith("/events"):
            return 200, [pre] if "preseason" in url else [reg], {"x-requests-last": "0", "x-requests-remaining": "400"}
        who = "Jayson Tatum" if "pre1" in url else "Stephen Curry"
        outs = [{"name": "Over", "description": who, "price": 1.80, "point": 24.5},
                {"name": "Under", "description": who, "price": 2.00, "point": 24.5}]
        return 200, {"bookmakers": [{"key": "draftkings", "markets": [{"key": "player_points", "outcomes": outs}]}]}, \
            {"x-requests-last": "1", "x-requests-remaining": "399"}

    assert fo.SPORT_KEYS["nba"] == ("basketball_nba_preseason", "basketball_nba")
    tatum = scanner.Leg("Jayson Tatum", "points", 24.5, "BOS v DET", team="BOS", start_time=TIP, sport="nba")
    curry = scanner.Leg("Stephen Curry", "points", 24.5, "GSW v LAL", team="GSW", start_time=reg["commence_time"], sport="nba")
    assert asyncio.run(fo.OddsBook("k", cache=tmp_path / "c.json", get=api).price("nba", [tatum, curry], NOW)) == 2
    assert "basketball_nba_preseason/events/pre1/odds" in calls and "basketball_nba/events/reg1/odds" in calls
