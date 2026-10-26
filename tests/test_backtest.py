import asyncio
import csv
import json
import sqlite3
from types import SimpleNamespace

import pytest

from local import backtest, pipeline_config, sentinel, snapshots, steam
from local import prize_picks_ev_scraper as scanner

START = "2098-12-31T17:00:00+00:00"
START_TS = 4_070_970_000.0  # == START


class FakeGrader:
    def __init__(self, results):
        self.results, self.calls = results, []

    def leg(self, leg):
        self.calls.append(leg["player"])
        return {"result": self.results.get(leg["player"], "pending"), "actual": None, "note": ""}


def run_cycles(tmp_path, monkeypatch, n=2, probs=None):
    players = [f"Player {c}" for c in "ABCDEFG"]
    probs = probs or [0.60 - i / 100 for i in range(7)]

    async def quotes(series):
        return [scanner.Quote("kalshi", f"KXNHLPTS-98DEC31G{i}-{p}", f"{p}: 1+ points", probs[i], 0.02,
                              title=f"{p}: 1+ points", detail={"source": "kalshi", "method": "proportional"})
                for i, p in enumerate(players)]

    async def fake_send(text, chat_id=None):
        return True
    monkeypatch.setattr(sentinel.scanner, "kalshi_quotes", quotes)
    monkeypatch.setattr(sentinel, "send_async", fake_send)
    csv_path = tmp_path / "lines_nhl.csv"
    csv_path.write_text("player,stat,line,game,team,odds_type,start_time,position\n" + "".join(
        f"{p},points,0.5,H{i} v A{i},H{i},standard,{START},F\n" for i, p in enumerate(players)))
    db_path = tmp_path / "snapshots_nhl.sqlite3"
    a = SimpleNamespace(sport="nhl", csv=str(csv_path), board="none", feed="none", seen=(), nudge={}, stale_min=45,
                        max_board_age=0, min_ev=0.545, flex=True, odds=None, pool=None, dedupe_min=30,
                        leg_cooldown_min=120, kelly=0.25, unit_pct=0.01, ledger=tmp_path / "l.csv", research=False,
                        snapshots=snapshots.Snapshots("nhl", db_path), steam=steam.SteamTracker(),
                        board_info={"whole_dropped": 3})
    for _ in range(n):
        asyncio.run(sentinel.cycle(a, [], chat_id=""))
    return db_path


def test_harness_replays_settles_and_reports_both_builders_end_to_end(tmp_path, monkeypatch):
    db = run_cycles(tmp_path, monkeypatch)
    con = sqlite3.connect(db)
    elig, cov = con.execute("SELECT eligible_json, coverage_json FROM cycles LIMIT 1").fetchone()
    assert len(json.loads(elig)) == 6 and json.loads(cov) == {"points": [7, 7]}     # G at 54% isn't eligible
    assert con.execute("SELECT DISTINCT sport, source FROM slip_legs").fetchall() == [("nhl", "kalshi")]
    results = {f"Player {c}": r for c, r in zip("ABCDEF", ["hit", "hit", "hit", "hit", "miss", "push"])}
    grader = FakeGrader(results)
    out = backtest.Outcomes(tmp_path / "o.sqlite3", grader=grader, fetch=True)
    report = backtest.run([db], outcomes=out, now=START_TS + 5 * 3600, slips_csv=tmp_path / "slips.csv")
    print(report)
    assert "NHL: 2 cycles (2 from logged eligible legs, 0 rebuilt from price rows: approximate), replay vs logged pick: 2 agree, 0 differ" in report
    assert "LIVE builder: 1 slips chosen" in report and "PAPER builder: 1 slips chosen" in report  # 2nd cycle: deduped
    assert "settled 1" in report and "flat $1: ROI per $1" in report and "quarter Kelly:" in report
    assert "whole-number lines without a push price 0 a cycle" in report and "priced 7 of 7 legs" in report
    rows = list(csv.DictReader(open(tmp_path / "slips.csv")))
    assert {r["builder"] for r in rows} == {"live", "paper"} and all(r["payout"] for r in rows)
    # every distinct leg graded once, then cached: a second run makes no grader calls
    calls = len(grader.calls)
    backtest.run([db], outcomes=out, now=START_TS + 5 * 3600)
    assert len(grader.calls) == calls and len(set(grader.calls)) == calls


def test_settlement_follows_prizepicks_flex_with_pushes_removed_and_less_flipped(tmp_path):
    out = backtest.Outcomes(tmp_path / "o.sqlite3", grader=FakeGrader({"A": "hit", "B": "hit", "C": "hit", "D": "miss",
                                                                        "E": "push", "F": "miss"}), fetch=True)
    leg = lambda p, side="More": backtest.LegEntry("nhl", p, "points", 0.5, side, 0.56, "kalshi", START_TS, "H0", "H0 v A0")
    s = backtest.Slip("paper", "nhl", 0, [leg("A"), leg("B"), leg("C"), leg("D"), leg("E"), leg("F", "Less")], 0.05, 1.0)
    backtest.settle([s], out, START_TS + 5 * 3600)
    assert s.results == ["hit", "hit", "hit", "miss", "push", "hit"]       # Less on a More-miss is a hit
    assert s.payout == 2.0                                                  # push -> 5-pick Flex, 4 of 5 hits
    early = backtest.Slip("paper", "nhl", 0, [leg("Q")], 0.05, 1.0)
    backtest.settle([early], out, START_TS + 3600)                         # inside SETTLE_AFTER_H: never graded
    assert early.payout is None and early.unsettled == "pending"
    offline = backtest.Outcomes(tmp_path / "o2.sqlite3", fetch=False)
    s2 = backtest.Slip("live", "nhl", 0, [leg("A")], 0.05, 1.0)
    backtest.settle([s2], offline, START_TS + 5 * 3600)
    assert s2.unsettled == "ungraded"
    pend = backtest.Outcomes(tmp_path / "o3.sqlite3", grader=FakeGrader({}), fetch=True)
    s3 = backtest.Slip("live", "nhl", 0, [leg("Z")], 0.05, 1.0)
    backtest.settle([s3], pend, START_TS + 5 * 3600)
    assert s3.unsettled == "pending" and pend.db.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0] == 0  # not cached


def test_drawdown_and_builder_metrics():
    assert backtest.drawdown([1, -2, 0.5, -1, 3]) == pytest.approx(2.5)
    assert backtest.drawdown([1, 1]) == 0
    mk = lambda pay, units, ts: backtest.Slip("live", "nhl", ts, [backtest.LegEntry("nhl", "A", "points", 0.5, "More", 0.56,
                                                                                    "kalshi", 0)] * 5, 0.06, units, payout=pay)
    lines = "\n".join(backtest.builder_lines("live", [mk(10, 1.0, 1), mk(0, 1.0, 2), mk(0.4, 0.0, 3), mk(2, 2.0, 4)]))
    assert "ROI per $1 +210.0% (net +8.40 on 4), max drawdown $1.60, profit frequency 50%" in lines   # 9, -1, -0.6, +1
    assert "4.0u staked on 3, net +10.00u, ROI +250.0%, max drawdown 1.00u (1 would be skipped" in lines


def test_steam_is_recomputed_from_price_history_not_read_from_storage(tmp_path):
    db = snapshots.Snapshots("nhl", tmp_path / "snapshots_nhl.sqlite3")
    from tests.test_steam_snapshots import leg as sleg, NOW
    for ts, p in ((NOW, 0.55), (NOW + 400, 0.58), (NOW + 3000, 0.52)):
        l = sleg(p)
        l.steam = steam.SteamInfo("kalshi", 0.99, 9999, 0, 0, True, None, None)   # bogus stored value
        db.prices(1, ts, [l])
    h = backtest.History(tmp_path / "snapshots_nhl.sqlite3", "nhl")
    key = backtest.series_key("Alex Ovechkin", "shots on goal", 2.5, "kalshi", sleg(0.5).start_ts)
    assert h.steam_at(key, NOW + 120).score is None                          # history shorter than MIN_SPAN_S
    assert h.steam_at(key, NOW + 400).score == pytest.approx(0.03)           # More side gained 3 pts
    s = h.steam_at(key, NOW + 3000)                                          # 30-min window starts at NOW+1200: 0.58
    assert s.span_s == steam.WINDOW_S["kalshi"] and s.score == pytest.approx(-0.06)


def test_old_schema_cycles_are_rebuilt_from_alive_price_rows(tmp_path):
    """A database written before eligible_json/coverage_json/slip_legs.sport existed still runs."""
    path = tmp_path / "snapshots_nfl.sqlite3"
    con = sqlite3.connect(path)
    con.executescript("""
    CREATE TABLE cycles (id INTEGER PRIMARY KEY, ts REAL, board_ts REAL, legs INTEGER, priced INTEGER, good INTEGER,
        whole_dropped INTEGER, live_json TEXT, paper_json TEXT);
    CREATE TABLE prices (id INTEGER PRIMARY KEY, cycle_id INTEGER, ts REAL, kind TEXT, player TEXT, stat TEXT, line REAL,
        side TEXT, team TEXT, game TEXT, start_ts REAL, p_fair REAL, source TEXT, method TEXT, pricing_json TEXT, steam_json TEXT);
    """)
    t = 1_800_000_000.0
    con.execute("INSERT INTO cycles VALUES (1, ?, 0, 4, 4, 2, 7, '[]', ?)", (t, json.dumps({"chosen": None, "by_size": []})))
    rows = [("A", 1.5, 0.58, t - 60), ("B", 1.0, 0.70, t - 60),          # whole-number line: never in a slip
            ("C", 2.5, 0.60, t - 3600), ("D", 3.5, 0.51, t - 60)]          # C: last row an hour ago = gone; D: < min
    for p, line, prob, ts in rows:
        con.execute("INSERT INTO prices (cycle_id, ts, kind, player, stat, line, side, team, game, start_ts, p_fair, source) "
                    "VALUES (1, ?, 'tick', ?, 'receptions', ?, 'More', 'BUF', 'BUF v MIA', ?, ?, 'kalshi')",
                    (ts, p, line, t + 86400, prob))
    con.commit()
    h = backtest.History(path, "nfl")
    r = backtest.replay_paper(h, {"nfl": h}, pipeline_config.CONFIG, 0.545)
    assert r.approximate == 1 and r.exact == 0 and r.agree == 1
    alive = {k[0] for k, _ in h.alive_at(t)}
    assert alive == {"A", "B", "D"}
    report = backtest.run([path], outcomes=backtest.Outcomes(tmp_path / "o.sqlite3"), now=t)
    assert "1 rebuilt from price rows: approximate" in report and "per-stat coverage: not logged" in report


def test_clv_in_the_harness_uses_the_same_source_read_at_or_before_start(tmp_path):
    db = snapshots.Snapshots("nhl", tmp_path / "snapshots_nhl.sqlite3")
    from tests.test_steam_snapshots import leg as sleg, KICK_TS, NOW
    db.prices(1, NOW, [sleg(0.56)])
    db.prices(2, KICK_TS - 600, [sleg(0.60), sleg(0.30, venue="books")])
    db.prices(3, KICK_TS + 60, [sleg(0.10)])
    h = backtest.History(tmp_path / "snapshots_nhl.sqlite3", "nhl")
    e = backtest.LegEntry("nhl", "Alex Ovechkin", "shots on goal", 2.5, "More", 0.56, "kalshi", KICK_TS)
    recs = backtest.clv_records([backtest.Slip("paper", "nhl", NOW, [e], 0.05, 1.0)], {"nhl": h}, KICK_TS + 3600)
    assert recs[0]["clv"] == pytest.approx(0.04) and recs[0]["note"] == ""
    e2 = backtest.LegEntry("mlb", "X", "hits", 0.5, "More", 0.56, "kalshi", KICK_TS)   # other sport, file not loaded
    r2 = backtest.clv_records([backtest.Slip("live", "nhl", NOW, [e2], 0.05, 1.0)], {"nhl": h}, KICK_TS + 3600)
    assert r2[0]["clv"] is None and "file not loaded" in r2[0]["note"]


def test_default_grader_goes_through_the_blocked_espn_client(tmp_path):
    out = backtest.Outcomes(tmp_path / "o.sqlite3", fetch=True)
    e = backtest.LegEntry("nhl", "A", "points", 0.5, "More", 0.56, "kalshi", START_TS, "H0", "H0 v A0")
    with pytest.raises(AssertionError, match="ESPN for real"):
        out.result(e, START_TS + 5 * 3600)
