"""Reference price age: logged for every priced leg and every chosen-slip leg; sliced in the backtest."""
import asyncio
import json
import sqlite3
import time
from pathlib import Path

import pytest

from local import backtest, ev_pool, snapshots
from local import prize_picks_ev_scraper as scanner
from tests import test_fetch_odds as tfo
from tests.test_backtest import START_TS, run_cycles


def test_books_price_age_is_the_cache_entry_fetch_time(tmp_path):
    b = tfo.book(tmp_path, tfo.FakeAPI())
    asyncio.run(b.price("nhl", [tfo.leg()], tfo.NOW))              # paid fetch at NOW
    later = tfo.leg()
    asyncio.run(b.price("nhl", [later], tfo.NOW + 1800))            # 30 min on: served from the cache
    assert later.quotes[0].detail["fetched_ts"] == tfo.NOW
    assert snapshots.price_age(later, tfo.NOW + 1800) == 1800.0


def test_books_alternate_ladder_price_carries_its_fetch_time(tmp_path, books_alt_exact):
    l = tfo.leg_at(3.5)
    asyncio.run(tfo.book(tmp_path, tfo.LadderAPI()).price("nhl", [l], tfo.NOW))
    assert l.quotes[0].detail["fetched_ts"] == tfo.NOW and snapshots.price_age(l, tfo.NOW + 90) == 90.0


def test_kalshi_price_age_is_seconds_since_the_cycles_read_and_lands_in_every_log(tmp_path, monkeypatch):
    t0 = time.time()
    db = run_cycles(tmp_path, monkeypatch, n=1)
    con = sqlite3.connect(db)
    ages = [r[0] for r in con.execute("SELECT price_age_s FROM prices")]
    assert ages and all(a is not None and 0 <= a < 60 for a in ages)
    rec = json.loads(con.execute("SELECT pricing_json FROM prices LIMIT 1").fetchone()[0])
    assert rec["source"] == "kalshi" and rec["method"] and rec["price_age_s"] == ages[0] and rec["fetched_ts"] >= t0
    slip_ages = [r[0] for r in con.execute("SELECT price_age_s FROM slip_legs")]
    assert slip_ages and all(a is not None and 0 <= a < 60 for a in slip_ages)
    refs = json.loads(con.execute("SELECT eligible_json FROM cycles").fetchone()[0])
    assert all(len(r) == 13 and 0 <= r[10] < 60 and r[11] == 0.0 for r in refs)   # half lines: push 0


def test_interpolated_kalshi_price_is_as_old_as_its_older_rung():
    l = scanner.Leg("A", "passing yards", 244.5, "BUF v MIA")
    l.quotes = [scanner.Quote("kalshi", "x", "x", 0.5, 0.02, interpolated=True,
                              detail={"method": "log-odds interpolation", "rungs": [{"fetched_ts": 100.0}, {"fetched_ts": 40.0}]})]
    assert snapshots.price_age(l, 160.0) == 120.0
    l.quotes[0].detail = {"method": "x"}
    assert snapshots.price_age(l, 160.0) is None                    # never guessed


def test_pooled_legs_keep_the_other_sentinels_fetch_time(tmp_path):
    pool = ev_pool.EvPool(tmp_path / "pool.json")
    l = scanner.Leg("A", "points", 0.5, "COL v DAL", team="COL", sport="nhl",
                    start_time="2098-12-31T17:00:00+00:00")
    l.quotes = [scanner.Quote("books", "e:m", "DK", 0.58, 0.0, detail={"fetched_ts": 1000.0})]
    pool.publish("nhl", [(l, "Consensus: DK")], 2000.0)
    got = pool.others("mlb", 2000.0)[0]
    assert got.source_venue == "books" and snapshots.price_age(got, 2600.0) == 1600.0


def rec(builder, i, source="kalshi", clv=0.01, age=30.0, steam=False, stat="points", p=0.57, result="hit"):
    return {"builder": builder, "entry_ts": float(i), "source": source, "clv": clv, "note": "",
            "identity": ("nhl", f"P{i}", stat, 0.5, "More", source, 0), "age_s": age, "steam": steam,
            "stat": stat, "p": p, "result": result}


def test_slices_print_n_and_mark_small_slices_insufficient():
    recs = [rec("paper", i) for i in range(30)]                                     # 30 Kalshi legs, 0-60s
    recs += [rec("paper", 100 + i, "books", -0.02, age=2400.0, steam=True, stat="saves", p=0.55, result="miss")
             for i in range(5)]
    recs += [rec("live", 0)]                                       # the same leg as paper P0: counted once in "all"
    text = "\n".join(backtest.slice_lines(recs))
    lines = text.splitlines()
    row = lambda label: next(l for l in lines if l.strip().startswith(label))
    assert "n=30" in row("kalshi") and "insufficient" not in row("kalshi") and "+1.00 pts" in row("kalshi")
    assert "n=5" in row("books") and row("books").endswith("(insufficient data)") and "-2.00 pts" in row("books")
    assert "n=30" in row("0-60s") and "n=5" in row("15-60m") and "(insufficient data)" in row("15-60m")
    assert "n=5" in row("steam detected") and "n=30" in row("no steam")
    assert "n=30" in row("points") and "n=5" in row("saves")
    assert "n=30   mean fair 57.0%  hit 100.0%" in row("56-60%") and "hit  0.0%  (insufficient data)" in row("54-56%")
    # every slice line prints its n, and every n < 30 carries the mark
    for l in lines:
        if "n=" in l:
            n = int(l.split("n=")[1].split()[0])
            assert (n < backtest.MIN_N) == l.endswith("(insufficient data)") or n == 0


def test_age_buckets():
    assert [backtest.age_bucket(a) for a in (0, 59.9, 60, 299, 300, 899, 900, 3599, 3600, 99999, None)] == [
        "0-60s", "0-60s", "1-5m", "1-5m", "5-15m", "5-15m", "15-60m", "15-60m", ">60m", ">60m", "age not logged"]
    assert [backtest.p_bucket(p) for p in (0.5, 0.539, 0.54, 0.559, 0.56, 0.599, 0.60, 0.8)] == [
        "50-54%", "50-54%", "54-56%", "54-56%", "56-60%", "56-60%", "60%+", "60%+"]


DATA = snapshots.ROOT / "data"


@pytest.mark.skipif(not (DATA / "snapshots_nfl.sqlite3").exists(), reason="no Day-1 snapshot database on this machine")
def test_slices_render_on_the_day1_database(tmp_path):
    """The real Day-1 files, copied (backup API: includes the WAL) so the test never touches data/."""
    paths = []
    for p in sorted(DATA.glob("snapshots_*.sqlite3")):
        src, dst = sqlite3.connect(f"file:{p}?mode=ro", uri=True), sqlite3.connect(tmp_path / p.name)
        src.backup(dst)
        src.close(), dst.close()
        paths.append(tmp_path / p.name)
    outcomes = tmp_path / "outcomes.sqlite3"
    if (DATA / "backtest_outcomes.sqlite3").exists():
        src, dst = sqlite3.connect(f"file:{DATA / 'backtest_outcomes.sqlite3'}?mode=ro", uri=True), sqlite3.connect(outcomes)
        src.backup(dst)
        src.close(), dst.close()
    report = backtest.run(paths, outcomes=backtest.Outcomes(outcomes, fetch=False))     # cached results only
    for header in ("CLV SLICES", " by source", " by price age at entry", " by steam at entry (recomputed)",
                   " by stat (top 10 by legs)", " calibration by entry fair probability"):
        assert header in report
    assert "age not logged" in report and "(insufficient data)" in report   # Day 1 predates age logging; n < 30
