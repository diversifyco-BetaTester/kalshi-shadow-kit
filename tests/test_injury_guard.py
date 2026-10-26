import asyncio
from types import SimpleNamespace

import aiohttp
import pytest

from local import injury_guard as ig
from local import prize_picks_ev_scraper as scanner
from local import sentinel

TIP = "2099-01-01T17:00:00Z"  # 99JAN01 ET


def inj(name, team, status, injury="Hamstring"):
    return {"status": status, "shortComment": f"{name} ({injury.lower()})", "details": {"type": injury},
            "athlete": {"displayName": name, "team": {"abbreviation": team}}}


FEEDS = {
    "football/nfl": {"injuries": [{"displayName": "Buffalo Bills", "injuries": [
        inj("Khalil Shakir", "BUF", "Questionable"), inj("James Cook III", "BUF", "Out", "Ankle"),
        inj("Von Miller", "BUF", "Injured Reserve", "Knee"), inj("Ray Davis", "BUF", "Suspension", "Not Specified"),
        inj("Dalton Kincaid", "BUF", "Doubtful", "Knee"), inj("Josh Allen", "BUF", "Active", "Hand")]}]},
    "basketball/nba": {"injuries": [{"displayName": "New York Knicks", "injuries": [
        inj("Jalen Brunson", "NY", "Day-To-Day", "Ankle")]}]},
    "baseball/mlb": {"injuries": [{"displayName": "Arizona", "injuries": [inj("Corbin Burnes", "ARI", "15-Day-IL", "Shoulder")]}]},
    "hockey/nhl": {"injuries": [{"displayName": "Colorado", "injuries": [inj("Gabriel Landeskog", "COL", "Day-To-Day", "Knee")]}]},
}
NHL_BOARD = {"events": [{"competitions": [{"competitors": [
    {"team": {"abbreviation": "COL"}, "probables": [{"name": "probableStartingGoalie", "athlete": {"displayName": "Mackenzie Blackwood"},
                                                    "status": {"name": "Confirmed"}}]},
    {"team": {"abbreviation": "PIT"}, "probables": [{"name": "probableStartingGoalie", "athlete": {"displayName": "Arturs Silovs"},
                                                    "status": {"name": "Expected"}}]},
    {"team": {"abbreviation": "NYR"}, "probables": []}]}]}]}


MLB_BOARD = {"events": [
    {"competitions": [{"competitors": [
        {"team": {"abbreviation": "CLE"}, "probables": [{"name": "probableStartingPitcher", "athlete": {"displayName": "Shawn Armstrong"}}]},
        {"team": {"abbreviation": "TB"}, "probables": [{"name": "probableStartingPitcher", "athlete": {"displayName": "Nick Martinez"}}]}]}]},
    {"competitions": [{"competitors": [{"team": {"abbreviation": "LAD"}, "probables": []},
                                       {"team": {"abbreviation": "MIL"}, "probables": []}]}]}]}


def stub(calls=None):
    async def get(url):
        if calls is not None:
            calls.append(url)
        if "/scoreboard?dates=" in url:
            assert url.endswith("dates=20990101")
            return MLB_BOARD if "/baseball/" in url else NHL_BOARD
        return FEEDS[url.split("/sports/")[1].rsplit("/injuries", 1)[0]]
    return get


def v(player, team, sport, side, stat="points", get=None):
    return asyncio.run(ig.verify_player_status(player, team, sport, side, stat, TIP, get or stub()))


def test_feed_parsing_keys_by_team_and_suffix_free_name():
    table = ig.parse_injuries(FEEDS["football/nfl"])
    assert table[("BUF", ("james", "cook"))]["status"] == "Out" and table[("BUF", ("james", "cook"))]["injury"] == "Ankle"
    assert ig.parse_starters(NHL_BOARD, "nhl") == {"COL": ("Mackenzie Blackwood", "Confirmed"), "PIT": ("Arturs Silovs", "Expected")}
    assert ig.parse_starters(MLB_BOARD, "mlb") == {"CLE": ("Shawn Armstrong", "Probable"), "TB": ("Nick Martinez", "Probable")}


@pytest.mark.parametrize("player,team,sport,why", [
    ("James Cook", "BUF", "nfl", "OUT (Ankle)"), ("Von Miller", "BUF", "nfl", "INJURED RESERVE (Knee)"),
    ("Ray Davis", "BUF", "nfl", "SUSPENSION"), ("Corbin Burnes", "AZ", "mlb", "15-DAY-IL (Shoulder)")])
def test_hard_veto_on_either_side(player, team, sport, why):
    for side in ("More", "Less"):
        safe, reason = v(player, team, sport, side, "rushing yards")
        assert not safe and reason.startswith(why)


@pytest.mark.parametrize("player,team,sport,tag", [("Khalil Shakir", "BUF", "nfl", "Q"), ("Dalton Kincaid", "BUF", "nfl", "D"),
                                                   ("Jalen Brunson", "NYK", "nba", "DTD"), ("Gabriel Landeskog", "COL", "nhl", "DTD")])
def test_questionable_doubtful_dtd_block_more_but_allow_less(player, team, sport, tag):
    safe, reason = v(player, team, sport, "More", "receiving yards")
    assert not safe and reason.startswith(f"{tag} with ") and "no More on a volume stat" in reason
    safe, reason = v(player, team, sport, "Less", "receiving yards")
    assert safe and reason.endswith(": Less only")


def test_healthy_and_listed_active_players_pass():
    assert v("Josh Allen", "BUF", "nfl", "More", "passing yards") == (True, "Active")
    assert v("Stefon Diggs", "BUF", "nfl", "More", "receptions") == (True, "Active")        # not on the report
    assert v("Jeremiah Smith", "OSU", "cfb", "More", "receiving yards") == (True, "no CFB injury feed")


def test_nhl_goalies_must_be_the_probable_starter():
    assert v("Mackenzie Blackwood", "COL", "nhl", "More", "saves") == (True, "Active / Confirmed Starter")
    assert v("Arturs Silovs", "PIT", "nhl", "Less", "saves") == (True, "Active / Expected Starter")
    safe, reason = v("Scott Wedgewood", "COL", "nhl", "More", "saves")
    assert not safe and reason == "not the starter (Mackenzie Blackwood is the confirmed starting goalie)"
    safe, reason = v("Igor Shesterkin", "NYR", "nhl", "More", "saves")
    assert not safe and reason.startswith("starter unconfirmed")
    assert v("Scott Wedgewood", "COL", "nhl", "More", "points")[0]                          # skater stat: no starter check


def test_espn_outage_lets_players_through_but_not_unconfirmable_goalies():
    async def down(url):
        raise aiohttp.ClientConnectionError("down")
    safe, reason = v("Khalil Shakir", "BUF", "nfl", "More", "receptions", down)
    assert safe and reason.startswith("status unavailable")
    safe, reason = v("Mackenzie Blackwood", "COL", "nhl", "More", "saves", down)
    assert not safe and reason.startswith("starter unconfirmed (ESPN unavailable")


def test_feeds_are_cached_for_fifteen_minutes(monkeypatch):
    calls = []
    for p in ("Khalil Shakir", "Josh Allen", "James Cook"):
        v(p, "BUF", "nfl", "More", "receptions", stub(calls))
    assert len(calls) == 1
    real = ig.time.time
    monkeypatch.setattr(ig.time, "time", lambda: real() + ig.CACHE_S + 1)
    v("Josh Allen", "BUF", "nfl", "More", "passing yards", stub(calls))
    assert len(calls) == 2


def test_sentinel_drops_vetoed_legs_logs_once_and_shows_status(tmp_path, monkeypatch, capsys):
    sent = []

    async def fake_send(text, chat_id=None):
        sent.append(text)
        return True
    players = [("Khalil Shakir", "BUF"), ("Josh Allen", "BUF")] + [(f"Player {c}", f"T{i}") for i, c in enumerate("ABCDE")]
    async def quotes(series):
        return [scanner.Quote("kalshi", f"KXNFLREC-99JAN01G{i}-{p}", f"{p}: 3+ receptions", 0.60, 0.02,
                              title=f"{p}: 3+ receptions") for i, (p, _) in enumerate(players)]
    monkeypatch.setattr(sentinel, "send_async", fake_send)
    monkeypatch.setattr(sentinel.scanner, "kalshi_quotes", quotes)
    real_verify = ig.verify_legs
    monkeypatch.setattr(sentinel.injury_guard, "verify_legs", lambda legs, get=None: real_verify(legs, stub()))
    csv = tmp_path / "lines_nfl.csv"
    csv.write_text("player,stat,line,game,team,odds_type,start_time\n" + "".join(
        f"{p},receptions,2.5,{t} v X{i},{t},standard,{TIP}\n" for i, (p, t) in enumerate(players)))
    a = SimpleNamespace(sport="nfl", csv=str(csv), board="none", feed="none", seen=(), nudge={}, stale_min=45,
                        max_board_age=60, min_ev=0.545, flex=True, odds=None, pool=None, dedupe_min=30,
                        leg_cooldown_min=120, kelly=0.25, unit_pct=0.01, ledger=tmp_path / "s.csv", research=False,
                        injury_guard=True)
    for _ in range(2):
        asyncio.run(sentinel.cycle(a, [], chat_id="123"))
    out = capsys.readouterr().out
    assert out.count("[Vetoed: Khalil Shakir 2.5 receptions More: Q with Hamstring: no More on a volume stat]") == 1
    assert len(sent) == 1 and "Khalil Shakir" not in sent[0]
    assert sent[0].count("✅ No injury") == 6


def test_mlb_pitcher_props_need_the_probable_starter():
    assert v("Nick Martinez", "TBR", "mlb", "More", "strikeouts") == (True, "Active / Probable Starter")   # TBR = TB
    assert v("Shawn Armstrong", "CLE", "mlb", "Less", "pitching outs") == (True, "Active / Probable Starter")
    safe, reason = v("Tanner Bibee", "CLE", "mlb", "More", "strikeouts")
    assert not safe and reason == "not the starter (Shawn Armstrong is the probable starting pitcher)"
    safe, reason = v("Yoshinobu Yamamoto", "LAD", "mlb", "More", "pitching outs")
    assert not safe and reason == "starter unconfirmed (no probable pitcher listed yet)"
    assert v("Steven Kwan", "CLE", "mlb", "More", "hitter strikeouts") == (True, "Active")     # batter K: no check
    assert v("Steven Kwan", "CLE", "mlb", "More", "hits") == (True, "Active")
    safe, reason = v("Corbin Burnes", "AZ", "mlb", "More", "strikeouts")
    assert not safe and reason.startswith("starter unconfirmed")                             # no ARI game listed
