"""Whole-number lines priced with their push from two adjacent market rungs (never a model)."""
import asyncio
import itertools
import time
from types import SimpleNamespace

import pytest

from local import backtest, ev_pool, fetch_odds as fo, optimizer, sentinel, snapshots
from local import prize_picks_ev_scraper as scanner
from tests import test_fetch_odds as tfo

START = "2098-12-31T17:00:00+00:00"


def kq(player, rung, p, stat="shots on goal", day="98DEC31", spread=0.02):
    return scanner.Quote("kalshi", f"KXNHLSOG-{day}G-{player[:3]}{rung}", f"{player}: {rung}+ {stat}", p, spread,
                         title=f"{player}: {rung}+ {stat}", detail={"method": "proportional", "fetched_ts": time.time()})


def whole(player, line, p_over, push, team="H", game="H v A", venue="kalshi"):
    l = scanner.Leg(player, "shots on goal", line, game, team=team, sport="nhl", start_time=START)
    l.quotes = [scanner.Quote(venue, "x", "x", p_over, 0.02, p_push=push)]
    return l


def half(player, p, game, team):
    l = scanner.Leg(player, "points", 0.5, game, team=team, sport="nhl", start_time=START)
    l.quotes = [scanner.Quote("kalshi", player, player, p, 0.02)]
    return l


# --------------------------------------------------------------------------- the leg
def test_whole_line_pick_is_win_given_no_push():
    more = whole("A", 3.0, 0.48, 0.20)                       # More 48%, push 20%, Less 32%
    assert more.pick == ("More", pytest.approx(0.48 / 0.80)) and more.p_win == pytest.approx(0.48)
    less = whole("B", 3.0, 0.30, 0.25)                       # Less 45%
    assert less.pick == ("Less", pytest.approx(0.45 / 0.75)) and less.p_win == pytest.approx(0.45)
    lp = optimizer.leg_prob(less)
    assert lp.p_win == pytest.approx(0.45) and lp.p_tie == 0.25
    unpriced = scanner.Leg("C", "shots on goal", 3.0, "H v A")
    unpriced.quotes = [scanner.Quote("kalshi", "x", "x", 0.40, 0.02)]       # no push price
    assert unpriced.push_unpriced and unpriced.p_push is None
    h = half("D", 0.56, "H v A", "H")
    assert h.p_push == 0.0 and not h.push_unpriced and h.pick == ("More", 0.56)


# --------------------------------------------------------------------------- Kalshi: adjacent rungs
def test_kalshi_prices_a_whole_line_from_the_n_and_n_plus_1_rungs():
    l = scanner.Leg("Jack Hughes", "shots on goal", 3.0, "NJD v NYR", team="NJD", start_time=START)
    scanner.attach([l], [kq("Jack Hughes", 3, 0.60), kq("Jack Hughes", 4, 0.35), kq("Jack Hughes", 5, 0.15)])
    q = l.quotes[0]
    assert len(l.quotes) == 1 and q.p_yes == pytest.approx(0.35) and q.p_push == pytest.approx(0.25)
    assert q.detail["method"] == "whole line: adjacent Kalshi rungs" and [r["rung"] for r in q.detail["rungs"]] == [3, 4]
    assert l.pick == ("Less", pytest.approx(0.40 / 0.75))     # Less = 1 - P(3+) = 40%


def test_kalshi_whole_line_without_both_rungs_or_with_crossed_rungs_stays_unpriced():
    only_win = scanner.Leg("Jack Hughes", "shots on goal", 3.0, "NJD v NYR", team="NJD", start_time=START)
    scanner.attach([only_win], [kq("Jack Hughes", 4, 0.35), kq("Jack Hughes", 2, 0.80)])   # no 3+ rung
    assert only_win.quotes == [] and only_win.push_unpriced
    crossed = scanner.Leg("Jack Hughes", "shots on goal", 3.0, "NJD v NYR", team="NJD", start_time=START)
    scanner.attach([crossed], [kq("Jack Hughes", 3, 0.30), kq("Jack Hughes", 4, 0.35)])
    assert crossed.quotes == []
    wide = scanner.Leg("Jack Hughes", "shots on goal", 3.0, "NJD v NYR", team="NJD", start_time=START)
    scanner.attach([wide], [kq("Jack Hughes", 3, 0.60, spread=0.20), kq("Jack Hughes", 4, 0.35)])   # untradeable N rung
    assert wide.quotes == []
    other_day = scanner.Leg("Jack Hughes", "shots on goal", 3.0, "NJD v NYR", team="NJD", start_time=START)
    scanner.attach([other_day], [kq("Jack Hughes", 3, 0.60, day="98DEC30"), kq("Jack Hughes", 4, 0.35)])
    assert other_day.quotes == []                            # never pair rungs from two games


# --------------------------------------------------------------------------- books: N +- 0.5 rungs
def test_books_price_a_whole_line_from_exact_rungs_at_n_minus_and_plus_half(tmp_path, books_ladder_full):
    api = tfo.LadderAPI()
    l, far = tfo.leg_at(3.0), tfo.leg_at(5.0)               # 2.5 and 3.5 posted; 5.5 isn't
    assert asyncio.run(tfo.book(tmp_path, api).price("nhl", [l, far], tfo.NOW)) == 1
    rungs = dict(fo.ladder(tfo.MAIN, tfo.ALT_LADDER))
    q = l.quotes[0]
    assert q.p_yes == pytest.approx(rungs[3.5]) and q.p_push == pytest.approx(rungs[2.5] - rungs[3.5])
    assert q.detail["method"].startswith("whole line") and fo.sources(l) == "Consensus: DK, FD · whole"
    assert far.quotes == [] and far.push_unpriced            # no interpolated push
    assert api.calls == ["player_shots_on_goal", "player_shots_on_goal_alternate"]   # main first, then the ladder


def test_a_books_own_whole_line_is_never_used_for_the_push():
    main = {"draftkings": [{"name": "Over", "description": "A B", "price": 1.9, "point": 3.0},
                           {"name": "Under", "description": "A B", "price": 1.9, "point": 3.0}]}
    assert fo.whole_consensus(main, {}, "A B", 3.0) is None  # push-refunded price: no push information


# --------------------------------------------------------------------------- slip EV with pushes
def brute_ev(legs: list[tuple[float, float]], payouts) -> float:
    """Every outcome per leg (win / push / loss), PrizePicks' rule: pushes remove legs, the slip pays the
    table of the legs left (2 left: Power 3x if both win; 1 or 0: refund)."""
    ev = 0.0
    for outcome in itertools.product("wpl", repeat=len(legs)):
        p = 1.0
        for (w, t), o in zip(legs, outcome):
            p *= w if o == "w" else t if o == "p" else 1 - w - t
        left, wins = outcome.count("w") + outcome.count("l"), outcome.count("w")
        if left >= 3:
            mult = payouts["flex"][left].get(wins, 0.0)
        elif left == 2:
            mult = 3.0 if wins == 2 else 0.0
        else:
            mult = 1.0
        ev += p * mult
    return ev - 1


def test_slip_ev_with_a_push_matches_brute_force_prizepicks_rules():
    legs = [half(f"P{i}", 0.58, f"G{i} v X{i}", f"G{i}") for i in range(4)] + [whole("W", 3.0, 0.45, 0.20)]
    want = brute_ev([(0.58, 0.0)] * 4 + [(0.45, 0.20)], scanner.PAYOUTS)
    assert sentinel.entry_ev(tuple(legs), "flex", []) == pytest.approx(want, abs=1e-12)
    six = legs + [whole("V", 2.0, 0.30, 0.25, team="J", game="J v K")]
    want6 = brute_ev([(0.58, 0.0)] * 4 + [(0.45, 0.20), (0.45, 0.25)], scanner.PAYOUTS)   # V: Less wins 45%
    assert sentinel.entry_ev(tuple(six), "flex", []) == pytest.approx(want6, abs=1e-12)
    assert sentinel.entry_ev(tuple(legs), "flex", ["a stack"]) is None   # correlated push: not modeled


def test_stake_uses_kelly_over_the_exact_outcomes_and_never_exceeds_it():
    legs = tuple([half(f"P{i}", 0.62, f"G{i} v X{i}", f"G{i}") for i in range(4)] + [whole("W", 3.0, 0.50, 0.20)])
    full = optimizer.slip_stats([optimizer.leg_prob(l) for l in legs], "flex").kelly_f
    units = sentinel.stake_units((0.2, "flex", legs, []))
    assert 0 < units <= 0.25 * full / 0.01 + 1e-9 and units <= sentinel.MAX_UNITS


def test_flex_entries_take_push_priced_whole_lines_and_never_unpriced_ones():
    halves = [half(f"P{i}", 0.57, f"G{i} v X{i}", f"G{i}") for i in range(4)]
    priced = whole("W", 3.0, 0.50, 0.20)                     # P(win | no push) 62.5%
    unpriced = scanner.Leg("U", "shots on goal", 3.0, "U v V", team="U", sport="nhl", start_time=START)
    unpriced.quotes = [scanner.Quote("kalshi", "x", "x", 0.70, 0.02)]
    entries = sentinel.flex_entries(halves + [priced, unpriced])
    assert entries and all("U" not in {l.player for l in e[2]} for e in entries)
    assert any("W" in {l.player for l in e[2]} for e in entries)


# --------------------------------------------------------------------------- pool, logs, replay
def test_pushes_travel_through_the_pool(tmp_path):
    pool = ev_pool.EvPool(tmp_path / "pool.json")
    w = whole("W", 3.0, 0.30, 0.25)                          # Less 60% given no push
    pool.publish("nhl", [(w, "Kalshi")], time.time())
    got = pool.others("nfl", time.time())[0]
    assert got.pick == ("Less", pytest.approx(0.60)) and got.p_push == pytest.approx(0.25)
    lp = optimizer.leg_prob(got)
    assert lp.p_win == pytest.approx(0.45) and lp.p_tie == pytest.approx(0.25)


def test_replay_restores_the_push_from_the_logged_eligible_ref(tmp_path):
    w = whole("W", 3.0, 0.30, 0.25)
    ref = snapshots.leg_ref(w, time.time())
    assert len(ref) == 13 and ref[11] == 0.25 and ref[5] == pytest.approx(0.6) and ref[12] == 0.0
    e = backtest.LegEntry("nhl", "W", "shots on goal", 3.0, "Less", ref[5], "kalshi", w.start_ts, "H", "H v A",
                          p_push=ref[11])
    l = backtest.to_leg(e)
    assert l.pick == ("Less", pytest.approx(0.6)) and optimizer.leg_prob(l).p_tie == pytest.approx(0.25)
    assert backtest.from_leg(l).p_push == pytest.approx(0.25)


# --------------------------------------------------------------------------- a full cycle
def test_a_cycle_prices_whole_lines_and_alerts_them_with_the_push(tmp_path, monkeypatch, capsys):
    players = [f"Player {c}" for c in "ABCDEF"]

    async def quotes(series):
        out = [kq(p, 1, 0.60 - i / 100, stat="points") for i, p in enumerate(players[:4])]   # 0.5 points legs
        out += [kq("Player E", 3, 0.70), kq("Player E", 4, 0.52)]         # E at 3.0: More 52%, push 18%
        out += [kq("Player F", 4, 0.52)]                                  # F at 3.0: no 3+ rung -> unpriced
        return out
    sends = []

    async def fake_send(text, chat_id=None):
        sends.append(text)
        return True
    monkeypatch.setattr(sentinel.scanner, "kalshi_quotes", quotes)
    monkeypatch.setattr(sentinel, "send_async", fake_send)
    csv = tmp_path / "lines_nhl.csv"
    rows = [f"{p},points,0.5,H{i} v A{i},H{i},standard,{START},F" for i, p in enumerate(players[:4])]
    rows += [f"Player E,shots on goal,3,H4 v A4,H4,standard,{START},F", f"Player F,shots on goal,3,H5 v A5,H5,standard,{START},F"]
    csv.write_text("player,stat,line,game,team,odds_type,start_time,position\n" + "\n".join(rows) + "\n")
    a = SimpleNamespace(sport="nhl", csv=str(csv), board="none", feed="none", seen=(), nudge={}, stale_min=45,
                        max_board_age=0, min_ev=0.545, flex=True, odds=None, pool=None, dedupe_min=30,
                        leg_cooldown_min=120, kelly=0.25, unit_pct=0.01, ledger=tmp_path / "l.csv", research=False)
    asyncio.run(sentinel.cycle(a, [], chat_id="1"))
    out = capsys.readouterr().out
    assert "whole-number lines: 1 push-priced, 1 without a push price" in out
    assert len(sends) == 1 and "Player E" in sends[0] and "Player F" not in sends[0]
    assert "63.4% · push 18%" in sends[0]                                  # 52 / (1 - 18) = 63.4% given no push
