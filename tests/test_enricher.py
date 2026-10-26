import asyncio
from datetime import datetime, timezone

import aiohttp

from local import enricher as en
from local import prize_picks_ev_scraper as scanner
from local.sentinel import alert_text

NOW = datetime(2026, 10, 16, 18, 0, tzinfo=timezone.utc).timestamp()


def gamelog(names, games, preseason=()):
    """games: [(iso date, opp, [cells])], newest first."""
    events, cats = {}, []
    for i, (d, opp, cells) in enumerate(games):
        events[f"e{i}"] = {"gameDate": d, "opponent": {"abbreviation": opp}}
    st = [{"displayName": "2026-27 Regular Season", "categories": [
        {"events": [{"eventId": f"e{i}", "stats": cells} for i, (_, _, cells) in enumerate(games)]}]}]
    if preseason:
        for j, (d, opp, cells) in enumerate(preseason):
            events[f"p{j}"] = {"gameDate": d, "opponent": {"abbreviation": opp}}
        st.append({"displayName": "2026-27 Preseason", "categories": [
            {"events": [{"eventId": f"p{j}", "stats": c} for j, (_, _, c) in enumerate(preseason)]}]})
    return {"names": names, "events": events, "seasonTypes": st}


def stub(players, logs):
    """players: [(id, name, league, team name, active)]; logs: {id: gamelog}."""
    calls = []

    async def get(url, params):
        calls.append(url)
        if url == en.SEARCH:
            return {"items": [{"id": i, "displayName": n, "league": lg, "isActive": act,
                               "teamRelationships": [{"displayName": t}]} for i, n, lg, t, act in players]}
        aid = url.split("/athletes/")[1].split("/")[0]
        if aid not in logs:
            raise aiohttp.ClientResponseError(None, (), status=404)
        return logs[aid]
    get.calls = calls
    return get


NHL_NAMES = ["goals", "assists", "points", "shotsTotal", "timeOnIcePerGame"]
OVI = gamelog(NHL_NAMES, [(f"2026-10-{15 - i}T23:00:00Z", "MTL" if i == 1 else "BOS", ["1", "0", "1", str(s), "18:30"])
                          for i, s in enumerate([4, 0, 3, 2, 5, 6])],
              preseason=[("2026-10-01T23:00:00Z", "MTL", ["0", "0", "0", "9", "15:00"])])


def run(*a, **k):
    return asyncio.run(en.enrich_leg(*a, now=NOW, **k))


def test_l5_average_record_and_head_to_head():
    get = stub([("3101", "Alex Ovechkin", "nhl", "Washington Capitals", True)], {"3101": OVI})
    note = run("Alex Ovechkin", "nhl", "Shots On Goal", 2.5, "MTL", team="WSH", get=get)
    assert note == "L5 avg 2.8 · 3/5 over 2.5 · vs MTL 0/1 (0)"                      # preseason 9-shot game ignored
    assert run("Alex Ovechkin", "nhl", "time on ice", 18.0, get=get).startswith("L5 avg 18.5 · 5/5 over 18")


def test_namesakes_are_split_by_team_or_reported():
    players = [("1", "Josh Allen", "nfl", "Buffalo Bills", True), ("2", "Josh Allen", "nfl", "Arizona Cardinals", False)]
    log = gamelog(["passingYards", "rushingYards"], [("2026-10-12T17:00:00Z", "LAR", ["210", "121"])])
    get = stub(players, {"1": log})
    assert run("Josh Allen", "nfl", "Pass Yards", 224.5, team="BUF", get=get).startswith("L1 avg 210 · 0/1 over 224.5")
    assert run("Josh Allen", "nfl", "Pass Yards", 224.5, get=get) == "No recent data (2 NFL players named Josh Allen)"
    assert run("Josh Allen", "mlb", "hits", 0.5, get=get) == "No recent data (player not found)"


def test_mlb_batting_columns_pitching_outs_and_role_check():
    bat = gamelog(["atBats", "runs", "hits", "doubles", "triples", "homeRuns", "RBIs"],
                  [("2026-10-14T00:00:00Z", "TB", ["4", "1", "3", "1", "0", "1", "2"])])
    pit = gamelog(["innings", "hits", "walks", "strikeouts", "earnedRuns"], [("2026-10-12T00:00:00Z", "MIL", ["6.2", "4", "1", "9", "2"])])
    get = stub([("10", "Luis Robert Jr.", "mlb", "Chicago White Sox", True), ("20", "Garrett Crochet", "mlb", "Boston Red Sox", True)],
               {"10": bat, "20": pit})
    assert run("Luis Robert Jr.", "mlb", "Total Bases", 6.5, get=get).startswith("L1 avg 7 ·")        # 3 H + 2B + 3 for HR
    assert run("Luis Robert Jr.", "mlb", "singles", 0.5, get=get).startswith("L1 avg 1 ·")
    assert run("Luis Robert Jr.", "mlb", "Hits+Runs+RBIs", 5.5, get=get).startswith("L1 avg 6 · 1/1 over 5.5")
    assert run("Garrett Crochet", "mlb", "Pitching Outs", 19.5, get=get).startswith("L1 avg 20 ·")    # 6.2 IP
    assert run("Garrett Crochet", "mlb", "Pitcher Strikeouts", 8.5, get=get).startswith("L1 avg 9 · 1/1")
    assert run("Luis Robert Jr.", "mlb", "Pitcher Strikeouts", 0.5, get=get) == "No recent data (no pitching log)"


def test_nba_split_columns_and_old_games_are_flagged():
    log = gamelog(["minutes", "threePointFieldGoalsMade-threePointFieldGoalsAttempted", "totalRebounds", "assists", "points"],
                  [("2026-05-02T23:30:00Z", "PHI", ["40", "3-9", "9", "4", "33"])])
    get = stub([("7", "Jaylen Brown", "nba", "Boston Celtics", True)], {"7": log})
    assert run("Jaylen Brown", "nba", "3-PT Made", 2.5, get=get) == "L1 avg 3 · 1/1 over 2.5 · last game May 2"
    assert run("Jaylen Brown", "nba", "Pts+Rebs+Asts", 44.5, get=get).startswith("L1 avg 46 · 1/1")


def test_failures_return_a_fallback_never_raise():
    async def boom(url, params):
        raise asyncio.TimeoutError
    assert run("Alex Ovechkin", "nhl", "Shots On Goal", 2.5, get=boom) == "No recent data (TimeoutError)"
    get = stub([("3101", "Alex Ovechkin", "nhl", "Washington Capitals", True)], {})
    assert run("Alex Ovechkin", "nhl", "Shots On Goal", 2.5, get=get) == "No recent data (ClientResponseError)"
    assert run("Alex Ovechkin", "nhl", "Hits", 1.5, get=get) == "No recent data (Hits isn't in ESPN game logs)"
    empty = stub([("3101", "Alex Ovechkin", "nhl", "Washington Capitals", True)], {"3101": gamelog(NHL_NAMES, [])})
    assert run("Alex Ovechkin", "nhl", "points", 0.5, get=empty) == "No recent data (no games in ESPN's log)"


def test_enrich_legs_is_concurrent_and_capped():
    async def slow(url, params):
        await asyncio.sleep(5)
    legs = [scanner.Leg(f"P{i}", "points", 0.5, "WSH v MTL", team="WSH", sport="nhl") for i in range(6)]
    start = datetime.now().timestamp()
    notes = asyncio.run(en.enrich_legs(legs, get=slow, timeout=0.2))
    assert datetime.now().timestamp() - start < 1.5                                     # 6 legs, one 0.2 s wait
    assert set(notes.values()) == {"No recent data (timed out)"} and len(notes) == 6


def test_alert_puts_each_note_indented_under_its_leg():
    l1 = scanner.Leg("Alex Ovechkin", "shots on goal", 2.5, "WSH v MTL", team="WSH", sport="nhl")
    l2 = scanner.Leg("Nick Suzuki", "points", 0.5, "WSH v MTL", team="MTL", sport="nhl")
    for l, p in ((l1, 0.58), (l2, 0.56)):
        l.quotes = [scanner.Quote("kalshi", l.player, l.player, p, 0.02)]
    msg = alert_text((0.1, "flex", (l1, l2), False), "nhl", 0, NOW, 1.0,
                     {id(l1): "L5 avg 2.8 · 3/5 over 2.5 · vs MTL 0/1 (0)", id(l2): "No recent data (player not found)"})
    lines = msg.splitlines()
    i = lines.index("<code>Alex Ovechkin</code>")
    assert lines[i + 2] == "   L5 avg 2.8 · 3/5 over 2.5 · vs MTL 0/1 (0)"
    j = lines.index("<code>Nick Suzuki</code>")
    assert lines[j + 2] == "   No recent data (player not found)"
    plain = alert_text((0.1, "flex", (l1, l2), False), "nhl", 0, NOW, 1.0)                  # --no-research
    assert "L5 avg" not in plain and "No recent data" not in plain
