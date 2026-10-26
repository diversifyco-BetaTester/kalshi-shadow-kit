import itertools
import json

import pytest

from local import optimizer as op
from local import pipeline_config
from local import prize_picks_ev_scraper as scanner

L = op.LegProb


def brute(legs, kind="flex", cfg=None):
    """Enumerate every win/tie/loss outcome: the reference the DP must match."""
    ev = var_terms = p_profit = 0.0
    rows = []
    for out in itertools.product("WTL", repeat=len(legs)):
        p = 1.0
        for o, leg in zip(out, legs):
            p *= {"W": leg.p_win, "T": leg.p_tie, "L": 1 - leg.p_win - leg.p_tie}[o]
        m = op.multiplier(kind, len(legs) - out.count("T"), out.count("W"), cfg)
        rows.append((p, m))
    ev = sum(p * m for p, m in rows)
    return ev, sum(p * (m - ev) ** 2 for p, m in rows), sum(p for p, m in rows if m > 1)


def test_breakevens_match_the_verified_numbers():
    assert round(op.breakeven(4), 4) == 0.5503       # 4-Flex ~ 55.0%
    assert round(op.breakeven(5), 4) == 0.5425       # 5-Flex ~ 54.25%
    assert round(op.breakeven(6), 4) == 0.5421       # 6-Flex ~ 54.21%


def test_dilution_example_from_the_spec():
    five = [L(0.56)] * 5
    assert round(op.slip_stats(five).ev, 3) == 1.119
    for p6, ev in ((0.50, 1.071), (0.52, 1.106), (0.54, 1.141)):
        assert round(op.slip_stats(five + [L(p6)]).ev, 3) == ev
    assert op.slip_stats(five + [L(0.54)]).variance / op.slip_stats(five).variance > 3   # ~3.5x, not ~2x


def test_heterogeneous_legs_with_ties_match_brute_force_enumeration():
    legs = [L(0.58), L(0.55, 0.08), L(0.53), L(0.61, 0.12), L(0.50), L(0.57, 0.05)]
    for n in (4, 5, 6):
        st = op.slip_stats(legs[:n])
        ev, var, pp = brute(legs[:n])
        assert st.ev == pytest.approx(ev, abs=1e-12) and st.variance == pytest.approx(var, abs=1e-12)
        assert st.p_profit == pytest.approx(pp, abs=1e-12)
    single_p = op.slip_stats([L(sum(l.p_win for l in legs[:5]) / 5)] * 5).ev     # the shortcut we must not use
    assert abs(single_p - op.slip_stats(legs[:5]).ev) > 1e-3


def test_ties_revert_one_size_down():
    core = [L(0.56)] * 5
    assert op.slip_stats(core + [L(0.0, 1.0)]).ev == pytest.approx(op.slip_stats(core).ev)          # 6 -> 5
    assert op.slip_stats(core[:4] + [L(0, 1), L(0, 1)]).ev == pytest.approx(op.slip_stats(core[:4]).ev)  # 6 -> 4
    two = op.slip_stats([L(0.6), L(0.6), L(0, 1), L(0, 1)])                                         # 4 -> 2: Power 3x
    assert two.ev == pytest.approx(0.36 * 3)
    assert op.slip_stats([L(0.6), L(0, 1), L(0, 1), L(0, 1)]).ev == pytest.approx(1.0)              # -> 1: refund
    assert op.multiplier("flex", 3, 2) == 1.0 and op.multiplier("flex", 5, 3) == 0.4


def test_p_profit_counts_only_payouts_above_the_stake():
    st = op.slip_stats([L(0.56)] * 5)
    d = scanner.hit_distribution([0.56] * 5)
    assert st.p_profit == pytest.approx(d[4] + d[5])   # 3/5 pays 0.4x: a loss


def leg(name, p, game, team="A", odds_type="standard", stat="points"):
    l = scanner.Leg(name, stat, 0.5, game, team=team, odds_type=odds_type, sport="nhl", start_time="2099-01-01T17:00:00Z")
    l.quotes = [scanner.Quote("kalshi", name, name, p, 0.02)]
    return l


def pool(ps):
    return [leg(f"P{i}", p, f"T{i} v U{i}", team=f"T{i}") for i, p in enumerate(ps)]


def test_filler_guardrail_ships_the_five_man_when_no_sixth_leg_qualifies():
    r = op.build(pool([0.56] * 5 + [0.52]))
    assert r.by_size[6].stats is None and "no leg >= 54%" in r.by_size[6].note
    assert r.by_size[5].stats is not None and r.chosen.size in (4, 5)
    r = op.build(pool([0.56] * 5 + [0.545]))                                    # a real sixth leg
    assert r.by_size[6].stats is not None and min(l.pick[1] for l in r.by_size[6].legs) >= 0.54


def test_report_shows_ev_variance_and_p_profit_for_every_size():
    r = op.build(pool([0.60, 0.59, 0.58, 0.57, 0.56, 0.555]))
    assert set(r.by_size) == {5, 6} and all(c.stats for c in r.by_size.values())
    lines = r.lines()
    assert len(lines) == 2 and all(k in x for x in lines for k in ("EV ", "variance", "EV/sd", "log-growth", "P(profit)"))
    assert sum("<- chosen" in x for x in lines) == 1
    assert pipeline_config.CONFIG["optimizer"]["selection"] == "kelly_growth"
    assert r.chosen is max(r.by_size.values(), key=lambda c: c.stats.growth)    # ranked by log-growth


def test_selection_metric_is_configurable():
    cfg = pipeline_config.load()
    legs = pool([0.60, 0.59, 0.58, 0.57, 0.56, 0.555])
    chosen = {}
    for sel in ("ev_per_var", "sharpe", "kelly_growth"):
        cfg["optimizer"]["selection"] = sel
        chosen[sel] = op.build(legs, cfg=cfg).chosen.size
    assert chosen["ev_per_var"] <= chosen["sharpe"]                              # EV/variance favours fewer legs


def test_non_standard_legs_and_same_game_pairs_are_kept_out():
    legs = pool([0.60, 0.59, 0.58, 0.57, 0.56])
    legs.append(leg("Goblin", 0.70, "G v H", odds_type="goblin"))
    qb = scanner.Leg("QB", "passing yards", 224.5, "BUF v MIA", team="BUF", sport="nfl", start_time="2099-01-01T17:00:00Z")
    wr = scanner.Leg("WR", "receiving yards", 49.5, "BUF v MIA", team="BUF", sport="nfl", start_time="2099-01-01T17:00:00Z")
    for x in (qb, wr):
        x.quotes = [scanner.Quote("kalshi", "x", "x", 0.62, 0.02)]
    r = op.build(legs + [qb, wr])
    assert any("Goblin: goblin" in e for e in r.excluded)
    assert any("correlated pair kept apart: BUF QB Pass + WR Rec" in w for w in r.warnings)
    for c in r.by_size.values():
        if c.stats:
            assert "Goblin" not in {l.player for l in c.legs}
            assert len({(l.sport, l.game) for l in c.legs}) == len(c.legs)          # never two legs of one game


def test_payout_tables_come_from_config(tmp_path):
    (tmp_path / "cfg.json").write_text(json.dumps({"payouts": {"flex": {"5": {"5": 9.0, "4": 2.0, "3": 0.4}}},
                                                   "optimizer": {"marginal_threshold": 0.55}}))
    cfg = pipeline_config.load(tmp_path / "cfg.json")
    assert cfg["payouts"]["flex"][5] == {5: 9.0, 4: 2.0, 3: 0.4} and cfg["payouts"]["flex"][6][6] == 25.0
    assert cfg["optimizer"]["marginal_threshold"] == 0.55 and cfg["optimizer"]["sizes"] == [5, 6]
    assert op.breakeven(5, cfg=cfg) > op.breakeven(5)
    assert scanner.PAYOUTS == pipeline_config.CONFIG["payouts"]
