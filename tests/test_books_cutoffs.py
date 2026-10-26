"""Defaults since 2026-10-23: no sportsbook ladder interpolation, no sportsbook whole-number lines."""
import asyncio
import sys

from local import fetch_odds as fo, pipeline_config
from local import prize_picks_ev_scraper as scanner
from tests import test_fetch_odds as tfo
from tests.test_whole_lines import kq, START


def test_defaults_are_off():
    assert pipeline_config.DEFAULTS["books"] == {"ladder_interp": False, "whole_lines": False, "alt_ladder": False}


def test_a_line_between_rungs_is_not_interpolated_but_an_exact_alt_rung_still_prices(tmp_path, monkeypatch, books_alt_exact):
    monkeypatch.setattr(tfo, "ALT_LADDER", [tfo.o("Over", 1.5, 1.25), tfo.o("Over", 4.5, 6.00)])
    between, exact = tfo.leg_at(3.5), tfo.leg_at(4.5)                 # 3.5 sits between 2.5 and 4.5
    assert asyncio.run(tfo.book(tmp_path, tfo.LadderAPI()).price("nhl", [between, exact], tfo.NOW)) == 1
    assert between.quotes == [] and fo.sources(exact) == "Consensus: DK, FD · alt"
    rungs = [(1.5, 0.80), (3.5, 0.30)]
    assert fo.read_ladder(rungs, 2.5, interp=False) is None and fo.read_ladder(rungs, 3.5, interp=False) == (0.30, False)


def test_whole_lines_never_cost_a_credit(tmp_path):
    api = tfo.LadderAPI()
    w = tfo.leg_at(3.0)
    assert asyncio.run(tfo.book(tmp_path, api).price("nhl", [w], tfo.NOW)) == 0
    assert api.calls == [] and w.quotes == [] and w.push_unpriced       # no paid request at all


def test_kalshi_still_prices_whole_lines_from_adjacent_markets():
    l = scanner.Leg("Jack Hughes", "shots on goal", 3.0, "NJD v NYR", team="NJD", start_time=START)
    scanner.attach([l], [kq("Jack Hughes", 3, 0.60), kq("Jack Hughes", 4, 0.35)])
    assert l.quotes and abs(l.p_push - 0.25) < 1e-12


def test_no_alternate_ladder_is_ever_requested_by_default(tmp_path):
    api = tfo.LadderAPI()
    b = tfo.book(tmp_path, api, daily_cap=80)
    main, off = tfo.leg_at(2.5), tfo.leg_at(3.5)                     # 3.5 is only on the alternate ladder
    assert asyncio.run(b.price("nhl", [main, off], tfo.NOW)) == 1
    assert api.calls == ["player_shots_on_goal"] and off.quotes == [] and b.usage()["used"] == 1
    got = asyncio.run(b.reprice("nhl", [off], tfo.NOW + 1000, max_age_s=900))   # the JIT path too
    assert all("alternate" not in c for c in api.calls) and off.quotes == [] and got["credits"] <= 1
