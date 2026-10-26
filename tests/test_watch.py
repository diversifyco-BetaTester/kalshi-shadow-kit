"""Eligible Kalshi legs are watched to their start (snapshots.watch_legs), even when the cycle alerts nothing."""
import asyncio
import os
import time
from types import SimpleNamespace

from local import sentinel, snapshots
from local import prize_picks_ev_scraper as scanner

START = "2098-12-31T17:00:00+00:00"
START_TS = scanner.Leg("x", "x", 0.5, "x", start_time=START).start_ts


def leg(player, venue="kalshi", sport="nhl", start=START, line=0.5):
    l = scanner.Leg(player, "points", line, "H v A", team="H", sport=sport, start_time=start)
    l.quotes = [scanner.Quote(venue, "x", "x", 0.58, 0.02)]
    return l


def kalshi_quote(player, p=0.57):
    return scanner.Quote("kalshi", f"KXNHLPTS-98DEC31G0-{player[:3]}", f"{player}: 1+ points", p, 0.02,
                         title=f"{player}: 1+ points", detail={"method": "proportional"})


def test_only_this_sports_upcoming_kalshi_legs_are_watched(tmp_path):
    db = snapshots.Snapshots("nhl", tmp_path / "snapshots_nhl.sqlite3")
    now = START_TS - 3600
    legs = [leg("Kalshi Leg"), leg("Books Leg", venue="books"), leg("Other Sport", sport="nba"),
            leg("Started", start="2098-12-31T15:00:00+00:00")]
    assert db.watch_eligible(legs, now) == 1
    assert db.watch_eligible(legs, now + 60) == 0                              # already watched: no duplicate
    assert [(o["player"], o["source"]) for o in db.open_legs(now)] == [("Kalshi Leg", "kalshi")]
    assert db.open_legs(START_TS + 1) == []                                    # started: off the watch list
    db.watch_eligible([], START_TS + 86401)                                     # a day after the start: cleaned up
    assert db.db.execute("SELECT COUNT(*) FROM watch_legs").fetchone()[0] == 0


def test_an_eligible_leg_that_leaves_the_board_is_priced_until_its_start(tmp_path):
    db = snapshots.Snapshots("nhl", tmp_path / "snapshots_nhl.sqlite3")
    entry_at = START_TS - 6 * 3600
    db.watch_eligible([leg("Moved Line")], entry_at)                           # eligible, never chosen
    a = SimpleNamespace(sport="nhl", odds=None)
    near_start = START_TS - 300                                                 # the line left the board hours ago
    watched = asyncio.run(sentinel.watch_legs(a, db, [], [kalshi_quote("Moved Line")], near_start))
    assert [l.player for l in watched] == ["Moved Line"] and watched[0].watch
    db.prices(1, near_start, watched)
    kind, ts = db.db.execute("SELECT kind, ts FROM prices").fetchone()
    assert kind == "watch" and START_TS - ts <= snapshots.STALE_CLOSE_S          # a clean close, not a stale one


def test_a_stale_board_still_prices_the_watch_list_from_kalshi_only(tmp_path, monkeypatch, capsys):
    csv = tmp_path / "lines_nhl.csv"
    csv.write_text("player,stat,line,game,team,odds_type,start_time,position\n"
                   f"Board Leg,points,0.5,H v A,H,standard,{START},F\n")
    old = time.time() - 3600
    os.utime(csv, (old, old))                                                  # a 60 min old board: no alerts
    db = snapshots.Snapshots("nhl", tmp_path / "snapshots_nhl.sqlite3")
    db.watch_eligible([leg("Watched Leg")], time.time())
    db.record_slip("live", 1, time.time(), [leg("Chosen Books", venue="books")], 0.1, True)

    async def quotes(series):
        return [kalshi_quote("Watched Leg")]

    class NoCredits:
        async def price(self, *a, **k):
            raise AssertionError("a watch-only cycle must not spend Odds API credits")
    sends = []

    async def send(text, chat_id=None):
        sends.append(text)
        return True
    monkeypatch.setattr(sentinel.scanner, "kalshi_quotes", quotes)
    monkeypatch.setattr(sentinel, "send_async", send)
    a = SimpleNamespace(sport="nhl", csv=str(csv), board="none", feed="none", seen=(), nudge={}, stale_min=999,
                        max_board_age=20, min_ev=0.545, flex=True, odds=NoCredits(), pool=None, dedupe_min=30,
                        leg_cooldown_min=120, kelly=0.25, unit_pct=0.01, ledger=tmp_path / "l.csv", research=False,
                        snapshots=db)
    asyncio.run(sentinel.cycle(a, [], chat_id="1"))
    assert "not pricing or alerting" in capsys.readouterr().out and sends == []
    rows = db.db.execute("SELECT player, kind, source FROM prices").fetchall()
    assert rows == [("Watched Leg", "watch", "kalshi")]                          # books leg left unpriced: no credit


def test_nothing_to_watch_means_no_kalshi_read(tmp_path, monkeypatch):
    db = snapshots.Snapshots("nhl", tmp_path / "snapshots_nhl.sqlite3")

    async def quotes(series):
        raise AssertionError("no watched legs: no Kalshi read")
    monkeypatch.setattr(sentinel.scanner, "kalshi_quotes", quotes)
    asyncio.run(sentinel.watch_only(SimpleNamespace(sport="nhl", flex=True, snapshots=db), time.time()))
