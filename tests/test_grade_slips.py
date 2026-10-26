import json
from datetime import datetime, timezone

import pytest

from local import grade_slips as gs
from local import ledger

START = "2026-10-13T23:00:00+00:00"
START_TS = datetime.fromisoformat(START).timestamp()
LATER = START_TS + 6 * 3600


def team_block(abbr, groups):
    return {"team": {"abbreviation": abbr}, "statistics": [
        {"type": g, "keys": keys, "athletes": [{"athlete": {"displayName": n}, "stats": s} for n, s in rows]}
        for g, keys, rows in groups]}


NHL_KEYS = ["blockedShots", "hits", "timeOnIce", "goals", "assists", "shotsTotal", "shootoutGoals", "faceoffsWon"]
NHL = {"boxscore": {"players": [
    team_block("WSH", [("forwards", NHL_KEYS, [("Alex Ovechkin", ["0", "2", "18:30", "1", "1", "4", "0", "0"])]),
                       ("goalies", ["saves", "timeOnIce"], [("Charlie Lindgren", ["25", "60:00"])])]),
    team_block("MTL", [("forwards", NHL_KEYS, [("Nick Suzuki", ["1", "0", "20:00", "0", "0", "2", "1", "11"])])])]}}
MLB = {"boxscore": {
    "players": [team_block("CHW", [
        ("batting", ["hits-atBats", "runs", "hits", "RBIs", "homeRuns", "walks", "strikeouts"],
         [("Luis Robert Jr.", ["3-4", "2", "3", "1", "1", "0", "1"]), ("Andrew Vaughn", ["1-4", "0", "1", "0", "0", "0", "2"]),
          ("Ryan Vaughn", ["0-3", "0", "0", "0", "0", "0", "1"])]),
        ("pitching", ["fullInnings.partInnings", "hits", "walks", "earnedRuns", "strikeouts"],
         [("Garrett Crochet", ["6.2", "4", "1", "2", "9"])])])],
    "teams": [{"team": {"abbreviation": "CHW"}, "details": [
        {"stats": [{"name": "doubles", "displayValue": "Robert Jr. (10, Cole); Vaughn (3, Cole)"},
                   {"name": "stolenBases", "displayValue": "Robert Jr. 2 (20, 2nd base off Cole)"}]}]}]}}
NFL = {"boxscore": {"players": [team_block("WSH", [
    ("passing", ["completions/passingAttempts", "passingYards", "passingTouchdowns"], [("Jayden Daniels", ["20/31", "251", "2"])]),
    ("rushing", ["rushingAttempts", "rushingYards", "rushingTouchdowns"], [("Jayden Daniels", ["8", "52", "1"])]),
    ("receiving", ["receptions", "receivingYards", "receivingTouchdowns"], [("Terry McLaurin", ["6", "88", "1"])])])]}}


def board(abbrs, status="STATUS_FINAL", completed=True, eid="g1"):
    return {"events": [{"id": eid, "date": START.replace("+00:00", "Z"),
                        "status": {"type": {"name": status, "completed": completed}},
                        "competitions": [{"competitors": [{"team": {"abbreviation": a}} for a in abbrs]}]}]}


def stub(sport_boards, summaries):
    def get(url):
        for sport, b in sport_boards.items():
            if f"/{sport}/scoreboard" in url:
                return b
        for sport, s in summaries.items():
            if f"/{sport}/summary" in url:
                return s
        raise AssertionError(url)
    return get


def leg(sport, player, team, stat, line, side="More"):
    return {"sport": sport, "player": player, "stat": stat, "line": line, "side": side, "probability": 0.56,
            "source": "Kalshi", "team": team, "start_time": START}


@pytest.fixture
def grader():
    return gs.Grader(stub({"nhl": board(["WSH", "MTL"]), "mlb": board(["CHW", "DET"]), "nfl": board(["WSH", "DAL"])},
                          {"nhl": NHL, "mlb": MLB, "nfl": NFL}))


def test_payout_tables_and_voids():
    p = gs.payout
    assert [p(["hit"] * 5), p(["hit"] * 4 + ["miss"]), p(["hit"] * 3 + ["miss"] * 2), p(["hit"] * 2 + ["miss"] * 3)] == \
        [10.0, 2.0, 0.4, 0.0]
    assert [p(["hit"] * 6), p(["hit"] * 5 + ["miss"]), p(["hit"] * 4 + ["miss"] * 2), p(["hit"] * 3 + ["miss"] * 3)] == \
        [25.0, 2.0, 0.4, 0.0]
    assert p(["hit"] * 5 + ["void"]) == 10.0                     # a void 6-pick pays as a 5-pick
    assert p(["hit", "hit", "push"]) == 3.0                      # 3-pick down to 2: 2-pick Power
    assert p(["hit", "void", "void"]) == 1.0                     # refund


def test_nhl_stats_and_aliases(grader):
    g = lambda *a, **k: grader.leg(leg("nhl", *a, **k))
    assert g("Alex Ovechkin", "WSH", "shots on goal", 3.5) == {"result": "hit", "actual": 4.0, "note": ""}  # not shootout SOG
    assert g("Alex Ovechkin", "WSH", "points", 1.5)["result"] == "hit"
    assert g("Alex Ovechkin", "WSH", "time on ice", 19.5)["result"] == "miss"      # 18:30 -> 18.5 min
    assert g("Alex Ovechkin", "WSH", "time on ice", 18.5)["result"] == "push"
    assert g("Charlie Lindgren", "WSH", "saves", 25.0)["result"] == "push"
    assert g("Nick Suzuki", "MON", "faceoffs won", 10.5)["result"] == "hit"      # PrizePicks MON = ESPN MTL
    assert g("Nobody Here", "WSH", "points", 0.5)["result"] == "void"
    assert g("Alex Ovechkin", "WSH", "power play points", 0.5)["result"] == "review"


def test_mlb_batting_pitching_and_notes(grader):
    g = lambda *a, **k: grader.leg(leg("mlb", *a, **k))
    assert g("Luis Robert Jr.", "CWS", "total bases", 6.5)["actual"] == 7.0       # 3 H + 1 2B + 3 for the HR
    assert g("Luis Robert", "CWS", "singles", 0.5)["actual"] == 1.0               # suffix-insensitive match
    assert g("Luis Robert Jr.", "CWS", "stolen bases", 1.5)["result"] == "hit"    # "Robert Jr. 2 (...)"
    assert g("Luis Robert Jr.", "CWS", "hits + runs + rbis", 5.5, "Less")["result"] == "miss"
    assert g("Garrett Crochet", "CWS", "pitching outs", 19.5)["actual"] == 20.0   # 6.2 IP
    assert g("Garrett Crochet", "CWS", "strikeouts", 8.5)["result"] == "hit"
    assert g("Garrett Crochet", "CWS", "hits allowed", 4.5, "Less")["result"] == "hit"
    assert g("Andrew Vaughn", "CWS", "total bases", 1.5)["result"] == "review"    # "Vaughn" ambiguous: 2 Vaughns
    assert g("Luis Robert Jr.", "CWS", "strikeouts", 4.5)["result"] == "void"     # didn't pitch


def test_nfl_stats_and_missing_player_needs_review(grader):
    g = lambda *a, **k: grader.leg(leg("nfl", *a, **k))
    assert g("Jayden Daniels", "WAS", "passing attempts", 30.5)["actual"] == 31.0
    assert g("Jayden Daniels", "WAS", "passing completions", 20.5, "Less")["result"] == "hit"
    assert g("Jayden Daniels", "WAS", "rushing yards", 45.5)["result"] == "hit"
    assert g("Terry McLaurin", "WAS", "receiving yards", 90.5, "Less")["result"] == "hit"
    assert g("Zach Ertz", "WAS", "receptions", 2.5)["result"] == "review"          # absent: DNP or 0 catches?


def test_unfinished_postponed_and_unknown_games():
    pending = gs.Grader(stub({"nhl": board(["WSH", "MTL"], "STATUS_IN_PROGRESS", False)}, {}))
    assert pending.leg(leg("nhl", "Alex Ovechkin", "WSH", "points", 0.5))["result"] == "pending"
    ppd = gs.Grader(stub({"nhl": board(["WSH", "MTL"], "STATUS_POSTPONED", False)}, {}))
    assert ppd.leg(leg("nhl", "Alex Ovechkin", "WSH", "points", 0.5))["result"] == "void"
    assert pending.leg(leg("nhl", "X", "BOS", "points", 0.5))["result"] == "review"


def row(legs, kind, stake=1.0):
    return {"timestamp": "2026-10-13T20:00:00+00:00", "sports": "NHL", "slip_type": kind, "net_ev_pct": 9.0,
            "stake_units": stake, "earliest_start": START, "telegram": "sent", "legs": json.dumps(legs)}


def test_grade_waits_four_hours_grades_once_and_survives_appends(tmp_path, grader):
    path = tmp_path / "slips.csv"
    legs = [leg("nhl", "Alex Ovechkin", "WSH", "shots on goal", 3.5), leg("nhl", "Charlie Lindgren", "WSH", "saves", 24.5),
            leg("mlb", "Garrett Crochet", "CWS", "strikeouts", 8.5), leg("mlb", "Luis Robert Jr.", "CWS", "hits", 2.5),
            leg("nfl", "Terry McLaurin", "WAS", "receiving yards", 99.5)]                      # 4 hits, 1 miss
    ledger.append(row(legs, "5-Pick Flex", 2.0), path)
    assert gs.grade(path, START_TS + 3 * 3600, grader) == []                                 # < 4 h: wait
    done = gs.grade(path, LATER, grader)
    assert len(done) == 1 and done[0]["payout_multiplier"] == 2.0 and done[0]["net_units"] == 2.0
    ledger.append(row(legs[:1] * 5, "5-Pick Flex"), path)                                    # sentinel appends after
    assert len(gs.grade(path, LATER, grader)) == 1                                           # only the new one
    rows = ledger.read(path)
    assert [r["status"] for r in rows] == ["graded", "graded"] and rows[0]["net_units"] == "2.0"
    assert rows[0]["leg_results"][4]["result"] == "miss" and rows[0]["hits"] == "4"
    s = gs.summary(path)
    assert "Record: 2-0-0" in s and "Win rate: 100.0% of 2" in s                         # 2x and 10x
    assert "Net: +11.00u on 3.0u staked" in s and "Legs: 80.0% hit vs 56.0% predicted (5 distinct legs)" in s


def test_review_rows_wait_for_a_human(tmp_path, grader):
    path = tmp_path / "slips.csv"
    ledger.append(row([leg("nfl", "Zach Ertz", "WAS", "receptions", 2.5)] * 5, "5-Pick Flex"), path)
    assert gs.grade(path, LATER, grader) == []
    assert ledger.read(path)[0]["status"] == "review"
    assert "1 need review" in gs.summary(path)


# ---------------------------------------------------------------- morning digest
def graded_row(sports, kind, hits, n, mult, net, stake, graded_at, legs):
    return {"timestamp": "2026-10-13T20:00:00+00:00", "sports": sports, "slip_type": kind, "net_ev_pct": 9.0,
            "stake_units": stake, "earliest_start": START, "telegram": "sent", "legs": legs, "status": "graded",
            "hits": hits, "misses": n - hits, "pushes": 0, "payout_multiplier": mult, "net_units": net,
            "graded_at": graded_at, "leg_results": [{**l, "result": res} for l, res in legs]}


def test_digest_covers_last_24h_with_sport_hit_rates(tmp_path):
    path = tmp_path / "slips.csv"
    L = lambda sport, player, p=0.56: {"sport": sport, "player": player, "stat": "points", "line": 0.5,
                                       "side": "More", "probability": p, "team": "X", "start_time": START}
    shared = L("nhl", "Ovechkin")
    win = [(shared, "hit"), (L("mlb", "Judge"), "hit"), (L("mlb", "Soto"), "hit"), (L("nfl", "Allen"), "hit"),
           (L("nba", "Tatum"), "miss")]
    loss = [(shared, "hit"), (L("mlb", "Ohtani"), "miss"), (L("nfl", "Kelce"), "miss"), (L("nba", "Curry"), "hit"),
            (L("nhl", "McDavid"), "miss")]
    old = [(L("nhl", f"Old{i}"), "hit") for i in range(5)]
    rows = [graded_row("MLB+NBA+NFL+NHL", "5-Pick Flex", 4, 5, 2.0, 1.0, 1.0, "2026-10-14T13:00:00+00:00", win),
            graded_row("MLB+NBA+NFL+NHL", "5-Pick Flex", 2, 5, 0.0, -0.5, 0.5, "2026-10-14T13:00:00+00:00", loss),
            graded_row("NHL", "5-Pick Flex", 5, 5, 10.0, 9.0, 1.0, "2026-10-10T13:00:00+00:00", old)]
    for r in rows:
        r["legs"] = json.dumps([l for l, _ in r["legs"]])
    rows.append({**row([shared] * 5, "5-Pick Flex"), "status": "review"})
    with ledger.locked(path):
        ledger.rewrite(rows, path)
    now = datetime.fromisoformat("2026-10-14T13:05:00+00:00").timestamp()
    d = gs.digest(path, now)
    assert "Slips graded: <b>2</b> · Record <b>1-1-0</b>" in d
    assert "Net: <b>+0.50u</b> on 1.5u · ROI <b>+33.3%</b>" in d
    assert "Hit rate: MLB 2/3 (67%) · NHL 1/2 (50%) · NFL 1/2 (50%) · NBA 1/2 (50%) · CFB —" in d   # Ovechkin once
    assert "All-time: 2-1-0 · +9.50u" in d and "1 need review" in d
    assert d.count("• ") == 2                                                                # 4-day-old slip excluded


def test_empty_digest_and_no_chat_never_sends(tmp_path, monkeypatch):
    path = tmp_path / "slips.csv"
    assert "No slips settled in the last 24 h." in gs.digest(path)
    sent = []
    monkeypatch.setattr(gs.telegram, "send", lambda text, chat_id=None: sent.append(chat_id) or True)
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "")
    assert gs.send_digest("x") == "digest not sent: no TELEGRAM_ALERT_CHAT_ID" and sent == []
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "123")
    assert gs.send_digest("x") == "digest sent to Telegram" and sent == ["123"]


# ---------------------------------------------------------------- async entry, labels, per-sport summary
def test_async_entry_point_grades_like_grade(tmp_path, grader):
    import asyncio
    path = tmp_path / "slips.csv"
    legs = [leg("nhl", "Alex Ovechkin", "WSH", "shots on goal", 3.5), leg("nhl", "Charlie Lindgren", "WSH", "saves", 25.0),
            leg("mlb", "Garrett Crochet", "CWS", "strikeouts", 8.5), leg("mlb", "Luis Robert Jr.", "CWS", "hits", 2.5),
            leg("nfl", "Terry McLaurin", "WAS", "receiving yards", 99.5)]          # 3 won, 1 push, 1 lost
    ledger.append(row(legs, "5-Pick Flex", 1.0), path)
    done = asyncio.run(gs.grade_pending_slips(path, LATER, grader))
    assert len(done) == 1
    r = done[0]
    assert [gs.LABEL[x["result"]] for x in r["leg_results"]] == ["WON", "PUSH", "WON", "WON", "LOST"]
    assert r["pushes"] == 1 and r["payout_multiplier"] == 1.5 and r["net_units"] == 0.5   # push -> a 4-pick Flex, 3/4


def test_summary_reports_hit_rate_by_sport(tmp_path, grader):
    path = tmp_path / "slips.csv"
    legs = [leg("nhl", "Alex Ovechkin", "WSH", "shots on goal", 3.5), leg("nhl", "Nick Suzuki", "MON", "faceoffs won", 12.5),
            leg("mlb", "Garrett Crochet", "CWS", "strikeouts", 8.5), leg("mlb", "Luis Robert Jr.", "CWS", "hits", 2.5),
            leg("nfl", "Terry McLaurin", "WAS", "receiving yards", 99.5)]
    ledger.append(row(legs, "5-Pick Flex", 2.0), path)
    gs.grade(path, LATER, grader)
    s = gs.summary(path)
    assert "Hit rate by sport: MLB 2/2 (100%) · NHL 1/2 (50%) · NFL 0/1 (0%) · NBA — · CFB —" in s
    assert "Record: 0-1-0" in s and "Net: -1.20u on 2.0u staked (ROI -60.0%)" in s         # 3/5 -> 0.4x
