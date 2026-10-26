"""Kalshi integrity flags, price-uncertainty shading, undelivered-alert release, and the whole-line hit rate."""
import asyncio
import copy
import time
from types import SimpleNamespace

import pytest

from local import ev_pool, integrity, pipeline_config, sentinel, snapshots, stat_research
from local import prize_picks_ev_scraper as scanner

START = "2098-12-31T17:00:00+00:00"
pytestmark = pytest.mark.usefixtures("shading_on")  # production shading: 1 pt base + 0.1 x spread + 0.5 interp + 1 thin


def cfg_with(section="integrity", **kw):
    c = copy.deepcopy(pipeline_config.CONFIG)
    c[section].update(kw)
    return c


def kalshi_leg(p_over, spread=0.02, volume=500.0, line=0.5, player="Player A", method="proportional"):
    l = scanner.Leg(player, "points", line, "H v A", team="H", sport="nhl", start_time=START)
    l.quotes = [scanner.Quote("kalshi", "KX-98DEC31-X", "x", p_over, spread,
                              detail={"method": method, "overround": spread, "volume": volume, "fetched_ts": time.time()})]
    return l


def interp_leg(p_over, spreads=(0.02, 0.04), volumes=(800.0, 300.0)):
    l = scanner.Leg("QB One", "passing yards", 244.5, "H v A", team="H", sport="nfl", start_time=START)
    rungs = [{"rung": r, "fair": 0.5, "overround": s, "volume": v} for r, s, v in zip((225, 250), spreads, volumes)]
    l.quotes = [scanner.Quote("kalshi", "a ~ 250", "x", p_over, max(spreads), interpolated=True,
                              detail={"method": "log-odds interpolation between Kalshi rungs", "rungs": rungs})]
    return l


# --------------------------------------------------------------------------- flags
def test_kalshi_features_take_the_worse_rung():
    f = integrity.features(interp_leg(0.6), time.time())
    assert f["kind"] == "kalshi interp" and f["quote_spread"] == 0.04 and f["volume"] == 300.0
    missing = interp_leg(0.6)
    del missing.quotes[0].detail["rungs"][1]["volume"]                      # one rung unreported: volume unknown
    assert integrity.features(missing, time.time())["volume"] is None


def test_kalshi_flags_wide_interpolated_and_thin():
    flags = lambda l, **kw: integrity.flags(integrity.features(l, time.time()), cfg_with(**kw))
    assert flags(kalshi_leg(0.58)) == []                                     # 2c, 500 contracts: clean
    assert flags(kalshi_leg(0.58, spread=0.05)) == []                        # exactly at the limit
    assert flags(kalshi_leg(0.58, spread=0.06)) == ["kalshi_wide"]
    assert flags(kalshi_leg(0.58, volume=40)) == ["kalshi_thin"]
    assert flags(kalshi_leg(0.58, volume=None)) == []                        # not reported: never guessed thin
    assert flags(interp_leg(0.6)) == ["kalshi_interp"]
    assert flags(interp_leg(0.6), flag_kalshi_interp=False) == []
    assert set(integrity.FLAGS) >= {"kalshi_wide", "kalshi_interp", "kalshi_thin"}


def test_kalshi_flags_are_shadow_until_enforced():
    wide = kalshi_leg(0.58, spread=0.08)
    kept, got = integrity.screen([wide], time.time())
    assert kept == [wide] and got[id(wide)] == ["kalshi_wide"]
    assert integrity.screen([wide], time.time(), cfg_with(enforce=True))[0] == []


def test_quote_volume_is_read_from_either_kalshi_field():
    assert scanner._count({"volume_fp": "123.00"}, "volume") == 123.0
    assert scanner._count({"volume": 7}, "volume") == 7.0
    assert scanner._count({"volume_fp": "n/a"}, "volume") is None and scanner._count({}, "open_interest") is None


# --------------------------------------------------------------------------- shading
def test_shade_amounts_and_cap():
    f = lambda l: integrity.features(l, time.time())
    assert integrity.shade(f(kalshi_leg(0.58, spread=0.0))) == pytest.approx(0.01)             # base: every Kalshi leg
    no_record = kalshi_leg(0.58)
    no_record.quotes[0].detail = None
    assert integrity.shade(f(no_record)) == pytest.approx(0.01)                                # base, even unrecorded
    assert integrity.shade(f(kalshi_leg(0.58, spread=0.04))) == pytest.approx(0.014)           # + 0.1 x 4c
    assert integrity.shade(f(kalshi_leg(0.58, spread=0.04, volume=40))) == pytest.approx(0.024)  # + thin
    assert integrity.shade(f(interp_leg(0.6))) == pytest.approx(0.019)                          # + 0.1 x 4c + interp
    assert integrity.shade(f(interp_leg(0.6, spreads=(0.10, 0.10), volumes=(1, 1)))) == pytest.approx(0.03)  # capped
    assert integrity.shade(f(kalshi_leg(0.58, spread=0.04)), cfg_with("shading", enabled=False)) == 0.0
    books = kalshi_leg(0.58)
    books.quotes[0].venue = "books"
    assert integrity.shade(f(books)) == 0.0                                                     # books: flags only


def test_pick_is_shaded_toward_half_never_past_it_and_raw_pick_is_kept():
    l = kalshi_leg(0.58, spread=0.08)
    assert integrity.apply_shading([l]) == 1 and l.shade == pytest.approx(0.018)
    assert l.raw_pick == ("More", pytest.approx(0.58)) and l.pick == ("More", pytest.approx(0.562))
    less = kalshi_leg(0.43, spread=0.08)
    integrity.apply_shading([less])
    assert less.pick == ("Less", pytest.approx(0.552))                       # 57% Less -> 55.2%: same side
    thin_edge = kalshi_leg(0.505)
    thin_edge.shade = 0.03
    assert thin_edge.pick == ("More", 0.5)                                   # stops at 50%, never flips
    assert thin_edge.p_win == pytest.approx(0.5)


def test_a_whole_lines_conditional_pick_is_shaded_and_ev_uses_it():
    from local import optimizer
    w = scanner.Leg("W", "shots on goal", 3.0, "H v A", team="H", sport="nhl", start_time=START)
    w.quotes = [scanner.Quote("kalshi", "x", "x", 0.30, 0.02, p_push=0.25)]  # Less = 45% of 75% decided = 60%
    assert w.raw_pick == ("Less", pytest.approx(0.6))
    w.shade = 0.02
    assert w.pick == ("Less", pytest.approx(0.58))
    assert optimizer.leg_prob(w).p_win == pytest.approx(0.58 * 0.75) and optimizer.leg_prob(w).p_tie == 0.25


def test_shading_changes_which_legs_clear_the_floor():
    a = SimpleNamespace(min_ev=0.545, sport="nhl")
    l = kalshi_leg(0.555, spread=0.02)                                       # 55.5% raw on a tight book: 1.2 pts
    assert sentinel.clears_floor(a, l)
    integrity.apply_shading([l])
    assert not sentinel.clears_floor(a, l) and l.pick[1] == pytest.approx(0.543)


def test_pooled_legs_carry_the_raw_price_and_the_shade(tmp_path):
    l = kalshi_leg(0.60, spread=0.08)
    integrity.apply_shading([l])
    pool = ev_pool.EvPool(tmp_path / "ev_pool.json")
    pool.publish("nhl", [(l, "Kalshi")], time.time())
    got = pool.others("nfl", time.time())[0]
    assert got.shade == pytest.approx(0.018) and got.raw_pick[1] == pytest.approx(0.60) and got.pick[1] == pytest.approx(0.582)
    integrity.apply_shading([got])                                           # its record isn't here: shade kept
    assert got.shade == pytest.approx(0.018)


def test_snapshots_log_the_raw_price_and_the_shade(tmp_path):
    import json
    db = snapshots.Snapshots("nhl", tmp_path / "snapshots_nhl.sqlite3")
    l = kalshi_leg(0.60, spread=0.08)
    integrity.apply_shading([l])
    db.prices(1, time.time(), [l])
    p, detail = db.db.execute("SELECT p_fair, pricing_json FROM prices").fetchone()
    assert p == pytest.approx(0.60) and json.loads(detail)["shade"] == pytest.approx(0.018)
    db.record_slip("live", 1, time.time(), [l], 0.1, True)
    assert db.db.execute("SELECT p_entry FROM slip_legs").fetchone()[0] == pytest.approx(0.60)
    ref = snapshots.leg_ref(l)
    assert ref[5] == pytest.approx(0.60) and ref[12] == pytest.approx(0.018)


# --------------------------------------------------------------------------- reevaluate
def test_reevaluate_reports_an_uncomputable_ev_instead_of_crashing(monkeypatch):
    legs = tuple(kalshi_leg(0.6, player=f"P{i}") for i in range(5))
    for i, l in enumerate(legs):
        l.team, l.game = f"T{i}", f"T{i} v O{i}"
    monkeypatch.setattr(sentinel, "entry_ev", lambda combo, kind, stacks, factor=None: None)
    a = SimpleNamespace(min_ev=0.545, sport="nhl", kelly=0.25, unit_pct=0.01)
    new, why = sentinel.reevaluate(a, (0.2, "flex", legs, []))
    assert new is None and "not computable" in why


# --------------------------------------------------------------------------- release
def cycle_args(tmp_path, players, pool=None):
    csv = tmp_path / "lines_nhl.csv"
    csv.write_text("player,stat,line,game,team,odds_type,start_time,position\n" + "".join(
        f"{p},points,0.5,H{i} v A{i},H{i},standard,{START},F\n" for i, p in enumerate(players)))
    return SimpleNamespace(sport="nhl", csv=str(csv), board="none", feed="none", seen=(), nudge={}, stale_min=45,
                           max_board_age=0, min_ev=0.545, flex=True, odds=None, pool=pool, dedupe_min=30,
                           leg_cooldown_min=120, kelly=0.25, unit_pct=0.01, ledger=tmp_path / "l.csv", research=False)


@pytest.mark.parametrize("delivered", [True, False])
def test_an_undelivered_alert_releases_its_claimed_legs(tmp_path, monkeypatch, capsys, delivered):
    players = [f"Player {c}" for c in "ABCDEF"]

    async def quotes(series):
        return [scanner.Quote("kalshi", f"KXNHLPTS-98DEC31G{i}-{p}", f"{p}: 1+ points", 0.62, 0.02, title=f"{p}: 1+ points")
                for i, p in enumerate(players)]

    async def send(text, chat_id=None):
        return delivered
    monkeypatch.setattr(sentinel.scanner, "kalshi_quotes", quotes)
    monkeypatch.setattr(sentinel, "send_async", send)
    a, sent = cycle_args(tmp_path, players), []
    asyncio.run(sentinel.cycle(a, sent, chat_id="1"))
    rows = (tmp_path / "l.csv").read_text()
    if delivered:
        assert sent and a.sent_props_cache and "sent" in rows
    else:
        assert sent == [] and a.sent_props_cache == {} and "failed" in rows
        assert "legs released" in capsys.readouterr().out


def test_an_alert_that_raises_still_releases_and_logs(tmp_path, monkeypatch, capsys):
    players = [f"Player {c}" for c in "ABCDEF"]

    async def quotes(series):
        return [scanner.Quote("kalshi", f"KXNHLPTS-98DEC31G{i}-{p}", f"{p}: 1+ points", 0.62, 0.02, title=f"{p}: 1+ points")
                for i, p in enumerate(players)]

    async def boom(text, chat_id=None):
        raise RuntimeError("telegram down")
    monkeypatch.setattr(sentinel.scanner, "kalshi_quotes", quotes)
    monkeypatch.setattr(sentinel, "send_async", boom)
    a = cycle_args(tmp_path, players)
    asyncio.run(sentinel.cycle(a, [], chat_id="1"))
    out = capsys.readouterr().out
    assert "alert failed: RuntimeError" in out and a.sent_props_cache == {} and "failed" in (tmp_path / "l.csv").read_text()


def test_pool_release_undoes_only_its_own_claim(tmp_path):
    pool = ev_pool.EvPool(tmp_path / "ev_pool.json")
    no_repeat = lambda k, ev, s: False
    mine = frozenset({("nhl", "A", "points", 0.5, "More"), ("nhl", "B", "points", 0.5, "More")})
    assert pool.claim(mine, 0.1, 1000.0, 1800, no_repeat, 7200)
    assert not pool.would_claim(mine, 0.1, 1001.0, 1800, no_repeat, 7200)   # cooled down
    other = frozenset({("nhl", "B", "points", 0.5, "More"), ("nhl", "C", "points", 0.5, "More")})
    pool.release(mine, 1000.0)
    assert pool.would_claim(mine, 0.1, 1001.0, 1800, no_repeat, 7200)       # free again
    assert pool.claim(other, 0.1, 1002.0, 1800, no_repeat, 7200)
    pool.release(mine, 1000.0)                                               # a stale release: B stays claimed
    assert ("nhl", "B", "points", 0.5, "More") in pool.recent_legs(1003.0, 7200)


# --------------------------------------------------------------------------- whole-line hit rate
def test_whole_line_pushes_leave_the_hit_rate_denominator():
    now = time.time()
    day = lambda i: time.strftime("%Y-%m-%dT00:00Z", time.gmtime(now - i * 86400))
    values = [(day(i), "X", v) for i, v in enumerate([2, 3, 3, 4, 1, 2, 3, 5, 2, 1])]   # 3 pushes on 3
    rec = stat_research.gap_record(values, 3.0, "Less", 0.60, now, 10, 5)
    assert rec["pushes"] == 3 and rec["hit_rate"] == pytest.approx(5 / 7, abs=1e-4)          # rounded to 4 places
    assert rec["gap"] == pytest.approx(5 / 7 - 0.60, abs=1e-4)                                # was 5/10 - 0.60: -10 pts
    half = stat_research.gap_record(values, 2.5, "Less", 0.60, now, 10, 5)
    assert half["pushes"] == 0 and half["hit_rate"] == pytest.approx(0.5)
    mostly_pushes = [(day(i), "X", v) for i, v in enumerate([3, 3, 3, 3, 3, 3, 2, 4, 3, 3])]
    rec = stat_research.gap_record(mostly_pushes, 3.0, "Less", 0.60, now, 10, 5)
    assert rec["hit_rate"] is None and "2 decided games" in rec["note"]
