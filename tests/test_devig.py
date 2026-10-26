import math

import pytest

from local import devig as dv


def test_spec_example_minus130_minus110_proportional():
    t = dv.two_way(-130, -110, "american")
    assert round(t.implied_over, 4) == 0.5652 and round(t.implied_under, 4) == 0.5238
    assert round(t.overround, 4) == 0.0890
    assert round(t.fair_over, 3) == 0.519 and round(t.fair_under, 3) == 0.481 and t.method == "proportional"
    assert abs(t.fair_over + t.fair_under - 1) < 1e-12


def test_formats_agree():
    a = dv.two_way(-150, 130, "american")
    d = dv.two_way(1 + 100 / 150, 2.30, "decimal")
    assert abs(a.fair_over - d.fair_over) < 1e-12
    k = dv.two_way(0.57, 0.45, "prob")                          # Kalshi: YES ask 57c, NO ask 45c (YES bid 55c)
    assert abs(k.fair_over - 0.57 / 1.02) < 1e-12 and abs(k.overround - 0.02) < 1e-12


@pytest.mark.parametrize("method", dv.METHODS)
def test_every_method_sums_to_one_and_keeps_a_fair_market(method):
    t = dv.two_way(-200, 165, "american", method)
    assert abs(t.fair_over + t.fair_under - 1) < 1e-9 and t.fair_over > 0.5 and t.method == method
    even = dv.two_way(2.0, 2.0, "decimal", method)              # no vig: unchanged
    assert abs(even.fair_over - 0.5) < 1e-9


def test_power_and_shin_shade_the_favourite_less_than_proportional():
    prop, power, shin = (dv.two_way(-300, 240, "american", m).fair_over for m in dv.METHODS)
    assert power > prop and shin > prop                          # the known favourite-longshot correction
    p_o, p_u = dv.implied(-300, "american"), dv.implied(240, "american")
    k = math.log(power) / math.log(p_o)
    assert abs(p_o ** k + p_u ** k - 1) < 1e-9


def test_record_keeps_everything_for_an_audit():
    r = dv.two_way(-130, -110).record()
    assert set(r) == {"over_price", "under_price", "fmt", "implied_over", "implied_under", "overround",
                      "fair_over", "fair_under", "method"}


@pytest.mark.parametrize("price,fmt", [(-50, "american"), (1.0, "decimal"), (1.2, "prob"), (2.0, "fractional")])
def test_bad_prices_are_rejected(price, fmt):
    with pytest.raises(ValueError):
        dv.implied(price, fmt)


def test_kalshi_books_are_read_as_two_way_prices_and_one_sided_books_are_skipped():
    from local import prize_picks_ev_scraper as scanner
    p, spread, rec = scanner.kalshi_fair(0.55, 0.57)
    assert abs(p - 0.57 / (0.57 + 0.45)) < 1e-12 and abs(spread - 0.02) < 1e-12
    assert rec["source"] == "kalshi" and rec["over_price"] == 0.57 and rec["under_price"] == 0.45
    for bid, ask in ((0.55, 1.0), (0.0, 0.40), (None, 0.5), (0.6, 0.5)):   # no sellers / no buyers / missing / crossed
        assert scanner.kalshi_fair(bid, ask) is None


def test_kalshi_uses_power_outside_15_85_and_logs_both_methods():
    from local import prize_picks_ev_scraper as scanner
    p, _, rec = scanner.kalshi_fair(0.55, 0.57)                            # mid 56c: proportional
    assert rec["method"] == "proportional" and p == pytest.approx(rec["fair_proportional"], abs=1e-6)
    assert p == pytest.approx(0.57 / 1.02)
    p, _, rec = scanner.kalshi_fair(0.94, 0.97)                            # mid 95.5c: power
    assert rec["method"] == "power" and p == pytest.approx(rec["fair_power"], abs=1e-6)
    mid = (0.94 + 0.97) / 2                                                 # power overshoots the midpoint, but by less
    assert rec["fair_proportional"] < mid < rec["fair_power"]
    assert abs(rec["fair_power"] - mid) < abs(rec["fair_proportional"] - mid)
    assert {"fair_proportional", "fair_power"} <= set(scanner.kalshi_fair(0.10, 0.12)[2])
    assert scanner.kalshi_fair(0.10, 0.12)[2]["method"] == "power"         # mid 11c: outside the band
