import asyncio
import json
import subprocess
from pathlib import Path

from local import enricher as en
from local import fetch_odds as fo
from local import grade_slips as gs
from local import prize_picks_ev_scraper as scanner
from local.parse_board import SPORTS, parse_json

ESPN_NAMES = {"Ohio State Buckeyes": "OSU", "Michigan Buckeyes Fake": "XX", "Hawai'i Rainbow Warriors": "HAW",
              "Sam Houston Bearkats": "SHSU", "Michigan State Spartans": "MSU", "San José State Spartans": "SJSU",
              "Massachusetts Minutemen": "MASS", "Texas Longhorns": "TEX", "Texas A&M Aggies": "TA&M"}
KICK = "2026-10-17T16:00:00Z"


def test_sport_keys_markets_and_stats_follow_nfl():
    assert fo.SPORT_KEYS["cfb"] == ("americanfootball_ncaaf",) and fo.MARKETS["cfb"] == fo.MARKETS["nfl"]
    assert SPORTS["cfb"]["stats"] == SPORTS["nfl"]["stats"] and SPORTS["cfb"]["series"] == []
    assert fo.LADDER_GAP[("cfb", "player_reception_yds")] == 25.0
    assert en.PATHS["cfb"] == "football/college-football" and en.COLUMNS["cfb"] == en.COLUMNS["nfl"]
    assert gs.PATHS["cfb"] == "football/college-football"


def test_odds_api_team_names_match_espn_abbreviations(monkeypatch):
    monkeypatch.setattr(fo, "CFB_TEAMS", dict(ESPN_NAMES))
    assert fo.team_codes("cfb", "Ohio State Buckeyes") == {"OSU"}                  # exact
    assert fo.team_codes("cfb", "Hawaii Rainbow Warriors") == {"HAW"}              # spelling differs
    assert fo.team_codes("cfb", "Sam Houston State Bearkats") == {"SHSU"}
    assert fo.team_codes("cfb", "San Jose State Spartans") == {"SJSU"}
    assert fo.team_codes("cfb", "UMass Minutemen") == {"MASS"}                     # override
    assert fo.team_codes("cfb", "Texas Longhorns") == {"TEX"}
    assert fo.team_codes("cfb", "Michigan Wolverines") == set()                    # no close match: no guess


def test_espn_outage_means_no_cfb_matching_not_a_crash(monkeypatch):
    def down():
        raise OSError("network down")
    monkeypatch.setattr(fo, "_espn_teams_get", down)
    assert fo.team_codes("cfb", "Ohio State Buckeyes") == set() and fo.CFB_TEAMS is None   # retried next time


def test_cfb_leg_prices_from_the_books(tmp_path, monkeypatch):
    monkeypatch.setattr(fo, "CFB_TEAMS", dict(ESPN_NAMES))
    events = [{"id": "c1", "commence_time": KICK, "home_team": "Ohio State Buckeyes", "away_team": "Texas Longhorns"}]
    seen = []

    async def api(url, params):
        seen.append(url.split("/v4/sports/")[1])
        if url.endswith("/events"):
            return 200, events, {"x-requests-last": "0", "x-requests-remaining": "400"}
        outs = [{"name": "Over", "description": "Jeremiah Smith", "price": 1.80, "point": 84.5},
                {"name": "Under", "description": "Jeremiah Smith", "price": 2.00, "point": 84.5}]
        return 200, {"bookmakers": [{"key": "fanduel", "markets": [{"key": "player_reception_yds", "outcomes": outs}]}]}, \
            {"x-requests-last": "1", "x-requests-remaining": "399"}

    leg = scanner.Leg("Jeremiah Smith", "receiving yards", 84.5, "OSU v TEX", team="OSU", start_time=KICK, sport="cfb")
    now = fo.ts(KICK) - 3 * 3600
    assert asyncio.run(fo.OddsBook("k", cache=tmp_path / "c.json", get=api).price("cfb", [leg], now)) == 1
    assert "americanfootball_ncaaf/events/c1/odds" in seen and fo.sources(leg) == "Consensus: FD"


def test_parse_board_reads_cfb_props():
    props = [{"player": "Jeremiah Smith", "team": "OSU", "league": "CFB", "stat": "Receiving Yards", "line": 84.5,
              "opponent": "TEX", "start_time": KICK, "odds_type": "standard"},
             {"player": "Arch Manning", "team": "TEX", "league": "CFB1H", "stat": "Pass Yards", "line": 120.5,
              "opponent": "OSU", "start_time": KICK, "odds_type": "standard"}]
    rows, _ = parse_json({"props": props}, "cfb")
    assert [(r["player"], r["stat"]) for r in rows] == [("Jeremiah Smith", "receiving yards")]   # 1H props ignored


def test_enricher_finds_college_players_by_espn_league_slug():
    log = {"names": ["receptions", "receivingYards"], "events": {"e": {"gameDate": "2026-10-11T19:00:00Z",
                                                                      "opponent": {"abbreviation": "TEX"}}},
           "seasonTypes": [{"displayName": "2026 Regular Season", "categories": [{"events": [
               {"eventId": "e", "stats": ["7", "112"]}]}]}]}

    async def get(url, params):
        if url == en.SEARCH:
            return {"items": [{"id": "1", "displayName": "Jeremiah Smith", "league": "college-football",
                               "teamRelationships": [{"displayName": "Ohio State Buckeyes"}], "isActive": True}]}
        assert "football/college-football/athletes/1/gamelog" in url
        return log
    note = asyncio.run(en.enrich_leg("Jeremiah Smith", "cfb", "Receiving Yards", 84.5, "TEX", team="OSU", get=get,
                                     now=fo.ts("2026-10-16T12:00:00Z")))
    assert note == "L1 avg 112 · 1/1 over 84.5 · vs TEX 1/1 (112)"


def test_cfb_grades_like_nfl():
    summary = {"boxscore": {"players": [{"team": {"abbreviation": "OSU"}, "statistics": [
        {"type": "receiving", "keys": ["receptions", "receivingYards", "receivingTouchdowns"],
         "athletes": [{"athlete": {"displayName": "Jeremiah Smith"}, "stats": ["7", "112", "1"]}]}]}]}}
    board = {"events": [{"id": "g", "date": KICK, "status": {"type": {"name": "STATUS_FINAL", "completed": True}},
                         "competitions": [{"competitors": [{"team": {"abbreviation": a}} for a in ("OSU", "TEX")]}]}]}
    g = gs.Grader(lambda url: board if "scoreboard" in url else summary)
    leg = {"sport": "cfb", "player": "Jeremiah Smith", "team": "OSU", "stat": "receiving yards", "line": 84.5,
           "side": "More", "start_time": KICK}
    assert g.leg(leg) == {"result": "hit", "actual": 112.0, "note": ""}
    assert g.leg({**leg, "player": "Bench Guy"})["result"] == "review"            # football: DNP vs zero unknown


def test_sync_script_includes_cfb(tmp_path):
    (tmp_path / "board.json").write_text(json.dumps({"props": [{"league": l} for l in ("NHL", "MLB", "NFL", "NBA", "CFB")]}))
    out = subprocess.run([str(Path(__file__).parents[1] / "ops" / "sync_boards.sh")], env={"DOWNLOADS": str(tmp_path),
                         "PATH": "/usr/bin:/bin:/usr/local/bin:/Library/Frameworks/Python.framework/Versions/3.14/bin"},
                         capture_output=True, text=True)
    assert out.stdout.strip() == "Boards synced for NHL MLB NFL NBA CFB. Not in this download, previous board kept: SOCCER."
    assert (tmp_path / "cfb_board.json").exists()


def test_sync_script_includes_soccer(tmp_path):
    (tmp_path / "board.json").write_text(json.dumps({"props": [{"league": l} for l in ("NHL", "MLB", "NFL", "NBA", "CFB", "SOCCER")]}))
    out = subprocess.run([str(Path(__file__).parents[1] / "ops" / "sync_boards.sh")], env={"DOWNLOADS": str(tmp_path),
                         "PATH": "/usr/bin:/bin:/usr/local/bin:/Library/Frameworks/Python.framework/Versions/3.14/bin"},
                         capture_output=True, text=True)
    assert out.stdout.strip() == "Boards synced for NHL, MLB, NFL, NBA, CFB, and SOCCER."
    assert (tmp_path / "soccer_board.json").exists()
