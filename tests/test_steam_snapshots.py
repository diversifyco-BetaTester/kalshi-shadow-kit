import asyncio
import gzip
import json
import sqlite3
import time
from types import SimpleNamespace

import pytest

from local import prize_picks_ev_scraper as scanner
from local import sentinel, snapshots, steam

KICK_TS = 4_070_000_000.0  # 2098: always pre-game
NOW = KICK_TS - 6 * 3600


def leg(p_over, side_more=True, venue="kalshi", line=2.5, player="Alex Ovechkin", start=KICK_TS, detail=None):
    from datetime import datetime, timezone
    l = scanner.Leg(player, "shots on goal", line, "MTL v WSH", team="WSH", sport="nhl",
                    start_time=datetime.fromtimestamp(start, timezone.utc).isoformat())
    l.quotes = [scanner.Quote(venue, "x", "x", p_over if side_more else 1 - p_over, 0.02, detail=detail)]
    return l


def test_steam_needs_history_follows_the_picked_side_and_keeps_sources_apart():
    t = steam.SteamTracker()
    t.update([leg(0.55)], NOW)
    first = leg(0.56)
    t.update([first], NOW + 120)
    assert first.steam.score is None                                       # under MIN_SPAN_S (5 min)
    up = leg(0.58)
    t.update([up], NOW + 400)
    assert up.steam.score == pytest.approx(0.03) and up.steam.detected and up.steam.source == "kalshi"
    less = scanner.Leg("Alex Ovechkin", "shots on goal", 2.5, "MTL v WSH", team="WSH", sport="nhl", start_time=up.start_time)
    less.quotes = [scanner.Quote("kalshi", "x", "x", 0.42, 0.02)]           # P(over) 42%: picked side is Less
    t2 = steam.SteamTracker()
    for ts, p in ((NOW, 0.45), (NOW + 400, 0.42)):
        less.quotes = [scanner.Quote("kalshi", "x", "x", p, 0.02)]
        t2.update([less], ts)
    assert less.pick[0] == "Less" and less.steam.score == pytest.approx(0.03)   # over fell = our Less gained
    books = leg(0.70, venue="books")
    t.update([books], NOW + 400)
    assert books.steam.source == "books" and books.steam.score is None      # a new source starts its own history


def test_stale_line_check_reads_prizepicks_line_history():
    t = steam.SteamTracker()
    t.board(NOW - 900, [leg(0.5, line=2.5)])
    t.board(NOW + 200, [leg(0.5, line=3.5)])                                # PrizePicks bumped 2.5 -> 3.5
    t.update([leg(0.55, line=3.5)], NOW)
    l = leg(0.59, line=3.5)
    t.update([l], NOW + 400)
    assert l.steam.pp_line_moved is True and l.steam.pp_prev_line == 2.5
    t3 = steam.SteamTracker()
    t3.board(NOW - 900, [leg(0.5)])
    t3.update([leg(0.55)], NOW)
    still = leg(0.59)
    t3.update([still], NOW + 400)
    assert still.steam.detected and still.steam.pp_line_moved is False      # steam, PrizePicks line unmoved


def test_steam_only_raises_rank_never_lowers_it_or_changes_ev():
    a, b = leg(0.56), leg(0.56)
    a.steam = steam.SteamInfo("kalshi", 0.04, 600, 0.52, 0.56, True, False, 2.5)
    b.steam = steam.SteamInfo("kalshi", -0.04, 600, 0.60, 0.56, True, False, 2.5)
    assert steam.rank(a) == pytest.approx(0.58) and steam.rank(b) == pytest.approx(0.56) and a.pick[1] == 0.56


def test_snapshot_rows_are_change_only_with_heartbeat_and_a_close_at_start(tmp_path):
    db = snapshots.Snapshots("nhl", tmp_path / "s.sqlite3")
    detail = {"source": "kalshi", "method": "proportional", "fair_proportional": 0.5588, "fair_power": 0.5606}
    cid = db.cycle(NOW, 1.0, {"legs": 1}, [], None)
    assert db.prices(cid, NOW, [leg(0.5588, detail=detail)]) == 1
    assert db.prices(cid, NOW + 60, [leg(0.5590, detail=detail)]) == 0                  # < 0.2 pt move: no row
    assert db.prices(cid, NOW + 120, [leg(0.5650, detail=detail)]) == 1                 # moved
    assert db.prices(cid, NOW + 120 + snapshots.HEARTBEAT_S, [leg(0.5651, detail=detail)]) == 1   # heartbeat
    soon = KICK_TS - 300                                                                 # inside START_BUFFER_MIN
    assert db.prices(cid, soon, []) == 1                                                 # the close row
    rows = sqlite3.connect(tmp_path / "s.sqlite3").execute("SELECT kind, p_fair, method, pricing_json FROM prices").fetchall()
    assert [r[0] for r in rows] == ["tick", "tick", "tick", "close"] and rows[-1][1] == pytest.approx(0.5651)
    assert json.loads(rows[0][3])["fair_power"] == 0.5606 and rows[0][2] == "proportional"


def test_board_snapshot_round_trips(tmp_path):
    db = snapshots.Snapshots("nhl", tmp_path / "s.sqlite3")
    props = [{"player": "A", "league": "NHL", "odds_type": "demon", "line": 3.5}]
    db.board(123.0, "2026-10-17T20:00:00Z", props)
    db.board(123.0, "dup", props)                                                        # same board: ignored
    (n, blob), = sqlite3.connect(tmp_path / "s.sqlite3").execute("SELECT n_props, props_gz FROM boards").fetchall()
    assert n == 1 and json.loads(gzip.decompress(blob)) == props and db.has_board(123.0)


def test_sentinel_cycle_logs_live_and_paper_choices(tmp_path, monkeypatch):
    players = [f"Player {c}" for c in "ABCDEFG"]

    async def quotes(series):
        return [scanner.Quote("kalshi", f"KXNHLPTS-98DEC31G{i}-{p}", f"{p}: 1+ points", 0.60 - i / 100, 0.02,
                              title=f"{p}: 1+ points", detail={"source": "kalshi", "method": "proportional"})
                for i, p in enumerate(players)]

    async def fake_send(text, chat_id=None):
        return True
    monkeypatch.setattr(sentinel.scanner, "kalshi_quotes", quotes)
    monkeypatch.setattr(sentinel, "send_async", fake_send)
    csv = tmp_path / "lines_nhl.csv"
    start = "2098-12-31T17:00:00+00:00"
    csv.write_text("player,stat,line,game,team,odds_type,start_time,position\n" + "".join(
        f"{p},points,0.5,H{i} v A{i},H{i},standard,{start},F\n" for i, p in enumerate(players)))
    db = snapshots.Snapshots("nhl", tmp_path / "s.sqlite3")
    a = SimpleNamespace(sport="nhl", csv=str(csv), board="none", feed="none", seen=(), nudge={}, stale_min=45,
                        max_board_age=0, min_ev=0.545, flex=True, odds=None, pool=None, dedupe_min=30,
                        leg_cooldown_min=120, kelly=0.25, unit_pct=0.01, ledger=tmp_path / "l.csv", research=False,
                        snapshots=db, steam=steam.SteamTracker(), board_info={"whole_dropped": 4})
    for _ in range(2):
        asyncio.run(sentinel.cycle(a, [], chat_id=""))
    con = sqlite3.connect(tmp_path / "s.sqlite3")
    cycles = con.execute("SELECT legs, priced, good, whole_dropped, live_json, paper_json FROM cycles").fetchall()
    assert len(cycles) == 2 and cycles[0][:4] == (7, 7, 6, 0)   # Player G is priced at 54.0% < 54.5%; no whole lines
    live, paper = json.loads(cycles[0][4]), json.loads(cycles[0][5])
    assert live and live[0]["sent"] is True and live[0]["size"] in (5, 6)
    assert paper["chosen"] in (5, 6) and {c["size"] for c in paper["by_size"]} == {5, 6}
    assert all("log_growth" in c and "ev_per_sd" in c for c in paper["by_size"] if "ev" in c)
    assert con.execute("SELECT COUNT(*) FROM prices WHERE kind='tick'").fetchone()[0] == 7   # 2nd cycle: unchanged


def test_interpolated_kalshi_legs_keep_both_rungs_pricing_records():
    def rung(n, bid, ask):
        p, spread, rec = scanner.kalshi_fair(bid, ask)
        return scanner.Quote("kalshi", f"KXNFLRECYDS-98DEC31G-{n}", f"Puka Nacua: {n}+ receiving yards", p, spread,
                             title=f"Puka Nacua: {n}+ receiving yards", detail=rec)
    l = scanner.Leg("Puka Nacua", "receiving yards", 84.5, "LAR v SF", team="LAR", sport="nfl",
                    start_time="2098-12-31T17:00:00+00:00")
    scanner.attach([l], [rung(75, 0.62, 0.64), rung(100, 0.33, 0.35)])
    q = l.quotes[0]
    assert q.interpolated and q.detail["method"] == "log-odds interpolation between Kalshi rungs"
    assert [r["rung"] for r in q.detail["rungs"]] == [75, 100] and q.detail["target_rung"] == 85
    assert all({"over_price", "under_price", "fair_proportional", "fair_power", "method"} <= set(r) for r in q.detail["rungs"])


# --------------------------------------------------------------------------- CLV
def priced(db, cid, ts, legs):
    db.prices(cid, ts, legs)


def test_clv_uses_the_same_source_close_for_the_side_taken(tmp_path):
    db = snapshots.Snapshots("nhl", tmp_path / "s.sqlite3")
    entry = leg(0.56)                                   # More at 56% on Kalshi
    db.prices(1, NOW, [entry])
    assert db.record_slip("live", 1, NOW, [entry], 0.08, True)
    assert [o["source"] for o in db.open_legs(NOW)] == ["kalshi"]
    db.prices(2, NOW + 3600, [leg(0.40, venue="books")])   # a books read must never be the Kalshi close
    db.prices(3, KICK_TS - 300, [leg(0.45)])              # Kalshi flipped: picked side now Less, P(over) 45%
    db.prices(4, KICK_TS + 600, [leg(0.10)])              # after start: never the close
    assert db.resolve_closes(KICK_TS - 1) == 0            # not started yet
    assert db.resolve_closes(KICK_TS + 60) == 1
    close, clv, age, note = db.db.execute("SELECT close_p, clv, close_age_s, close_note FROM slip_legs").fetchone()
    assert close == pytest.approx(0.45) and clv == pytest.approx(-0.11) and age == 300 and note == ""
    assert db.open_legs(KICK_TS + 60) == []


def test_clv_flags_stale_and_missing_closes_and_dedupes_paper_slips(tmp_path):
    db = snapshots.Snapshots("nhl", tmp_path / "s.sqlite3")
    a, b = leg(0.57), leg(0.58, venue="books", player="Nick Suzuki")
    db.prices(1, NOW, [a])                                 # Kalshi last read 6 h before start
    assert db.record_slip("paper", 1, NOW, [a, b], 0.05, False)
    assert not db.record_slip("paper", 2, NOW + 60, [b, a], 0.05, False)        # same legs re-chosen
    assert db.record_slip("paper", 3, NOW + snapshots.DEDUPE_S + 1, [a, b], 0.05, False)
    db.resolve_closes(KICK_TS + 60)
    notes = sorted(r[0] for r in db.db.execute("SELECT close_note FROM slip_legs"))
    assert notes[:2] == ["no books price before start"] * 2 and notes[2].startswith("stale close")
    report = snapshots.clv_report([tmp_path / "s.sqlite3"])
    assert "PAPER builder: 4 legs logged, 0 with a clean close (2 stale excluded, 0 open, 2 no close)" in report
    inc = snapshots.clv_report([tmp_path / "s.sqlite3"], include_stale=True)
    assert "mean CLV +0.00 pts · beat the close 0% (0/2, 2 unchanged)" in inc


def test_clv_report_splits_live_and_paper_with_running_means(tmp_path):
    db = snapshots.Snapshots("nhl", tmp_path / "s.sqlite3")
    a, b = leg(0.56), leg(0.55, player="Nick Suzuki")
    db.prices(1, NOW, [a, b])
    db.record_slip("live", 1, NOW, [a], 0.1, True)
    db.record_slip("paper", 1, NOW, [b], 0.1, False)
    db.prices(2, KICK_TS - 120, [leg(0.60), leg(0.53, player="Nick Suzuki")])
    db.resolve_closes(KICK_TS)
    r = snapshots.clv_report([tmp_path / "s.sqlite3"])
    assert "LIVE builder: 1 legs logged, 1 with a clean close" in r and "mean CLV +4.00 pts · beat the close 100%" in r
    assert "PAPER builder: 1 legs logged" in r and "mean CLV -2.00 pts · beat the close 0%" in r
    assert "running +4.00" in r and "running -2.00" in r and "kalshi: 1 legs" in r


def test_watch_list_reprices_chosen_legs_that_left_the_board_from_their_source(tmp_path):
    db = snapshots.Snapshots("nhl", tmp_path / "s.sqlite3")
    gone = leg(0.56)
    db.record_slip("live", 1, NOW, [gone], 0.1, True)
    quote = scanner.Quote("kalshi", "KXNHLSOG-98DEC21-X", "Alex Ovechkin: 3+ shots on goal", 0.52, 0.02,
                          title="Alex Ovechkin: 3+ shots on goal")
    a = SimpleNamespace(sport="nhl", odds=None)
    watched = asyncio.run(sentinel.watch_legs(a, db, [], [quote], NOW + 600))
    assert len(watched) == 1 and watched[0].quotes[0].venue == "kalshi" and watched[0].p_over == pytest.approx(0.52)
    on_board = leg(0.57)                                          # still on the board, same source: no copy
    assert asyncio.run(sentinel.watch_legs(a, db, [on_board], [quote], NOW + 600)) == []
    books_now = leg(0.57, venue="books")                          # on the board but priced by books now
    assert len(asyncio.run(sentinel.watch_legs(a, db, [books_now], [quote], NOW + 600))) == 1
    db.prices(2, NOW + 600, [books_now] + watched)                # both sources logged, neither overwrites
    assert {r[0] for r in db.db.execute("SELECT source FROM prices")} == {"kalshi", "books"}
