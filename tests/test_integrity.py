import copy

import pytest

from local import backtest, integrity, pipeline_config, snapshots
from local import prize_picks_ev_scraper as scanner
from tests.test_steam_snapshots import KICK_TS, NOW


def books_leg(fairs: dict, fetched=NOW, p_over=None, method="weighted consensus of proportional", player="Alex Ovechkin"):
    from datetime import datetime, timezone
    l = scanner.Leg(player, "shots on goal", 2.5, "MTL v WSH", team="WSH", sport="nhl",
                    start_time=datetime.fromtimestamp(KICK_TS, timezone.utc).isoformat())
    recs = [{"book": b, "weight": 1.5 if b == "pinnacle" else 1.0, "fair_over": p} for b, p in fairs.items()]
    p = p_over if p_over is not None else sum(fairs.values()) / len(fairs)
    l.quotes = [scanner.Quote("books", "e:m", "x", p, 0.0, detail={"source": "books", "method": method, "books": recs,
                                                                  "fetched_ts": fetched})]
    return l


def cfg_with(**kw):
    c = copy.deepcopy(pipeline_config.CONFIG)
    c["integrity"].update(kw)
    return c


def test_features_from_the_pricing_record():
    l = books_leg({"pinnacle": 0.55, "draftkings": 0.58, "fanduel": 0.60})
    f = integrity.features(l, NOW + 600)
    assert f["kind"] == "books main" and f["age_s"] == 600 and f["n_books"] == 3
    assert f["spread"] == pytest.approx(0.05) and f["pin_dev"] == pytest.approx(0.55 - 0.59)   # More: PIN likes it less
    less = books_leg({"pinnacle": 0.40, "draftkings": 0.45})                                     # picked side Less
    assert less.pick[0] == "Less" and integrity.features(less, NOW)["pin_dev"] == pytest.approx(0.05)
    alt = books_leg({}, method="alternate ladder: Over-only, main-line overround, log-odds interpolation", p_over=0.56)
    alt.quotes[0].detail["books"] = ["DK", "FD"]
    fa = integrity.features(alt, NOW)
    assert fa["kind"] == "books interp" and fa["n_books"] == 2 and fa["spread"] is None
    k = scanner.Leg("A", "points", 0.5, "A v B")
    k.quotes = [scanner.Quote("kalshi", "x", "x", 0.56, 0.02, detail={"method": "proportional", "fetched_ts": NOW})]
    assert integrity.features(k, NOW + 5) == {"kind": "kalshi", "age_s": 5.0, "n_books": None, "spread": None, "pin_dev": None,
                                              "quote_spread": None, "volume": None}


def test_flags_and_thresholds():
    flags = lambda l, at=NOW, **kw: integrity.flags(integrity.features(l, at), cfg_with(**kw))
    assert flags(books_leg({"pinnacle": 0.56, "draftkings": 0.57})) == []                          # clean
    assert flags(books_leg({"pinnacle": 0.56, "draftkings": 0.57}), NOW + 1801) == ["stale"]
    assert flags(books_leg({"draftkings": 0.57})) == ["single_book"]
    assert set(flags(books_leg({"pinnacle": 0.53, "draftkings": 0.58, "fanduel": 0.59}))) == {"books_disagree", "pin_against"}
    interp = books_leg({}, method="alternate ladder: Over-only, main-line overround, log-odds interpolation", p_over=0.56)
    interp.quotes[0].detail["books"] = ["DK", "FD"]
    assert flags(interp) == ["ladder_interp"] and flags(interp, flag_interp=False) == []
    k = scanner.Leg("A", "points", 0.5, "A v B")
    k.quotes = [scanner.Quote("kalshi", "x", "x", 0.56, 0.02, detail={"fetched_ts": NOW})]
    assert flags(k, NOW + 99999) == []                                                             # age flags books only


def test_shadow_keeps_every_leg_enforce_removes_flagged_ones():
    clean, stale = books_leg({"pinnacle": 0.56, "draftkings": 0.57}, fetched=NOW), books_leg({"draftkings": 0.57})
    assert pipeline_config.CONFIG["integrity"]["enforce"] is False
    kept, got = integrity.screen([clean, stale], NOW)
    assert kept == [clean, stale] and got[id(stale)] == ["single_book"]
    kept, _ = integrity.screen([clean, stale], NOW, cfg_with(enforce=True))
    assert kept == [clean]


def test_harness_recomputes_flags_with_the_exact_past_age(tmp_path):
    db = snapshots.Snapshots("nhl", tmp_path / "snapshots_nhl.sqlite3")
    l = books_leg({"pinnacle": 0.56, "draftkings": 0.57}, fetched=NOW)
    db.prices(1, NOW + 60, [l])                          # one change-only row; the price stays in force
    h = backtest.History(tmp_path / "snapshots_nhl.sqlite3", "nhl")
    key = backtest.series_key("Alex Ovechkin", "shots on goal", 2.5, "books", KICK_TS)
    f, fl = h.integrity_at(key, NOW + 600, "More", pipeline_config.CONFIG)
    assert f["age_s"] == 600 and fl == []
    f, fl = h.integrity_at(key, NOW + 2400, "More", pipeline_config.CONFIG)   # same row, 40 min after the fetch
    assert f["age_s"] == 2400 and fl == ["stale"]


def test_enforce_variant_replay_drops_flagged_legs(tmp_path):
    import json
    db = snapshots.Snapshots("nhl", tmp_path / "snapshots_nhl.sqlite3")
    legs = [books_leg({"pinnacle": p, "draftkings": p}, player=f"P{i}") for i, p in enumerate([0.60, 0.59, 0.58, 0.57, 0.56, 0.555])]
    for i, l in enumerate(legs):
        l.game, l.team = f"H{i} v A{i}", f"H{i}"
    legs[0].quotes[0].detail["fetched_ts"] = NOW - 3600                       # the best leg is an hour stale
    cid = db.cycle(NOW, 0, {"legs": 6, "priced": 6, "good": 6}, [], None, eligible=[snapshots.leg_ref(l, NOW) for l in legs])
    db.prices(cid, NOW, legs)                            # the sentinel's order: prices() commits the cycle
    h = backtest.History(tmp_path / "snapshots_nhl.sqlite3", "nhl")
    shadow = backtest.replay_paper(h, {"nhl": h}, pipeline_config.CONFIG, 0.545)
    assert shadow.flagged == 1 and shadow.vetoed == 0 and "P0" in {e.player for e in shadow.slips[0].legs}
    assert shadow.slips[0].legs[0].flags == ["stale"] and shadow.slips[0].legs[0].price_kind == "books main"
    enforced = backtest.replay_paper(h, {"nhl": h}, cfg_with(enforce=True), 0.545)
    assert enforced.vetoed == 1 and "P0" not in {e.player for s in enforced.slips for e in s.legs}


def test_integrity_slices_render():
    rec = lambda i, flags, kind="books main": {"builder": "paper", "entry_ts": float(i), "source": "books", "clv": 0.01,
                                                "note": "", "identity": (i,), "age_s": 10.0, "steam": False, "stat": "sog",
                                                "p": 0.56, "result": "hit", "flags": flags, "kind": kind}
    text = "\n".join(backtest.slice_lines([rec(1, []), rec(2, ["stale", "single_book"]), rec(3, None, None)]))
    assert " by market integrity at entry" in text and " by price kind" in text
    rows = {l.split()[0]: l for l in text.splitlines() if l.strip() and l.split()[0] in ("clean", "stale", "single_book")}
    assert all("n=1" in v and v.endswith("(insufficient data)") for v in rows.values()) and len(rows) == 3
    assert "no price record" in text
