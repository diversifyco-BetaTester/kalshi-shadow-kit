"""Logged-only signals (local/signals.py, Builds 1-5, 2026-11-04): each is logged, never used by a builder.

Every build has a regression test proving the live builder's slips, stakes and JIT credits are unchanged, and
the harness counts distinct legs and reports closing-data quality."""
import asyncio
import inspect
import json
import sqlite3
import time
from types import SimpleNamespace

import pytest

from local import backtest, ev_pool, fetch_odds, optimizer, parse_board, pipeline_config, sentinel, signals, snapshots, steam
from local import correlation, integrity
from local import prize_picks_ev_scraper as scanner
from tests import test_fetch_odds as tfo
from tests.test_backtest import START, FakeGrader

START_TS = scanner.Leg("x", "points", 0.5, "", start_time=START).start_ts  # test_backtest.START_TS is ~1 day off START
from tests.test_jit_and_stats import FakeOdds, books_leg, entry as jit_entry

BOOK_REC = lambda book, over, under: {"book": book, "weight": 1.0, "over_price": over, "under_price": under,
                                      "fmt": "decimal", "fair_over": (1 / over) / (1 / over + 1 / under)}


def books_priced(name, p, fetched, books, game=None, line=0.5, stat="points"):
    l = scanner.Leg(name, stat, line, game or f"H{name} v A{name}", team=f"H{name}", sport="nhl", start_time=START)
    l.quotes = [scanner.Quote("books", "ev:m", "PIN,DK de-vigged", p, 0.0,
                              detail={"source": "books", "method": "weighted consensus of proportional",
                                      "fetched_ts": fetched, "books": books})]
    return l


# --------------------------------------------------------------------------- isolation boundary (all builds)
def test_no_builder_imports_or_reads_a_signal():
    for mod in (optimizer, ev_pool, correlation, integrity, scanner):
        imports = [l for l in inspect.getsource(mod).splitlines() if l.lstrip().startswith(("import ", "from "))]
        assert not any("signals" in l for l in imports), mod.__name__
        assert "signals." not in inspect.getsource(mod), mod.__name__
    for fn in (sentinel.flex_entries, sentinel.mixed_entries, sentinel.reevaluate, sentinel.stake_units,
               sentinel.would_send, sentinel.entry_ev, sentinel.leg_floor, sentinel.clears_floor, sentinel._jit_refresh):
        src = inspect.getsource(fn)
        for word in ("signals", "line_gap", "watchlist", "a.targeted", "shadow", "jit_events", "display"):
            assert word not in src, (fn.__name__, word)
    # the paper optimizer and the pool never see a signal attribute either
    assert "line_gap" not in inspect.getsource(optimizer.build) and "shadow" not in inspect.getsource(ev_pool.to_entry)


# --------------------------------------------------------------------------- Build 1
def test_american_odds_are_formatting_only():
    assert signals.american(1.8) == "-125" and signals.american(2.2) == "+120" and signals.american(2.0) == "+100"
    assert signals.american(None) == "?" and signals.american(1.0) == "?"


def test_one_synthetic_alert_carries_every_field_for_a_flagged_and_a_clean_leg():
    now = time.time()
    clean = books_priced("Clean Leg", 0.55, now - 420, [BOOK_REC("pinnacle", 1.8, 2.1), BOOK_REC("draftkings", 1.8333, 2.0),
                                                       BOOK_REC("fanduel", 1.8197, 2.02)])
    flagged = books_priced("Flagged Leg", 0.56, now - 30, [BOOK_REC("draftkings", 1.75, 2.1)])  # one book: single_book
    e = (0.08, "flex", (clean, flagged), [])
    display = {id(l): signals.display_record(l, now) for l in e[2]}
    for rec in display.values():
        json.dumps(rec)  # what record_slip stores
    msg = sentinel.alert_text(e, "nhl", 0, now, 1.0, display=display)
    lines = msg.splitlines()
    c = lines[lines.index("<code>Clean Leg</code>") + 3]
    f = lines[lines.index("<code>Flagged Leg</code>") + 3]
    fair = display[id(clean)]["fair_p"]["value"]
    assert c == f"   🔎 fair {fair:.1%} (PIN:-125 DK:-120 FD:-122) · age 7m · 3 src · no flags"
    assert f.startswith("   🔎 fair ") and "(DK:-133)" in f and "age 30s" in f and "1 src" in f and f.endswith("⚑ single_book")
    assert lines[lines.index("<code>Clean Leg</code>") + 2].startswith("   📈 ")      # the existing line is untouched
    plain = sentinel.alert_text(e, "nhl", 0, now, 1.0)                                # no display: byte-identical card
    assert "🔎" not in plain and plain == "\n".join(x for x in lines if not x.startswith("   🔎"))
    six = (0.08, "flex", tuple(books_priced(f"Player Number {i}", 0.58, now, [BOOK_REC("pinnacle", 1.8, 2.1)] * 4,
                                            game=f"G{i} v H{i}") for i in range(6)), [])
    big = sentinel.alert_text(six, "nhl", 0, now, 2.0, display={id(l): signals.display_record(l, now) for l in six[2]})
    assert len(big) < 4096                                                            # Telegram's message limit


def test_kalshi_pooled_and_unlogged_legs_show_explicit_missing_markers():
    now = time.time()
    k = scanner.Leg("Kal Shi", "points", 0.5, "A v B", team="A", sport="nhl", start_time=START)
    k.quotes = [scanner.Quote("kalshi", "KX-1", "x", 0.6, 0.02, detail={"over_price": 0.61, "under_price": 0.41, "fmt": "prob",
                                                                        "method": "proportional", "fetched_ts": now})]
    rk = signals.display_record(k, now)
    assert signals.display_line(rk).startswith("   🔎 fair 60.0% (KX:0.61) · age 0s · 1 src")
    pooled = ev_pool.from_entry(ev_pool.to_entry(k, "Kalshi", now))
    rp = signals.display_record(pooled, now)
    assert rp["flags"] == {"value": None, "missing": "pooled_leg_no_record"} and "pooled_leg_no_record" in signals.display_line(rp)
    assert signals.display_line(None) == "   🔎 fair n/a (not logged)"


def test_send_order_is_logged_kelly_growth_with_a_stable_ev_tie_break():
    a, b, c, d, e = ("a",), ("b",), ("c",), ("d",), ("e",)
    growth = {a: 0.01, b: 0.02, c: None, d: 0.02, e: 0.005}
    assert signals.send_order([a, b, c, d, e], growth.get) == [b, d, a, e, c]          # ties keep builder order
    assert signals.send_order([a, c], {a: None, c: None}.get) == [a, c]                # nothing logged: unchanged


def kalshi_board(tmp_path, monkeypatch, probs, extra_quotes=(), players=None):
    players = players or [f"Player {c}" for c in "ABCDEFG"][:len(probs)]

    async def quotes(series):
        return [scanner.Quote("kalshi", f"KXNHLPTS-98DEC31G{i}-{p}", f"{p}: 1+ points", probs[i], 0.02,
                              title=f"{p}: 1+ points", detail={"source": "kalshi", "method": "proportional",
                                                               "over_price": probs[i] + 0.01, "under_price": 1.01 - probs[i],
                                                               "fmt": "prob"})
                for i, p in enumerate(players)] + list(extra_quotes)
    sends = []

    async def fake_send(text, chat_id=None):
        sends.append(text)
        return True
    monkeypatch.setattr(sentinel.scanner, "kalshi_quotes", quotes)
    monkeypatch.setattr(sentinel, "send_async", fake_send)
    csv_path = tmp_path / "lines_nhl.csv"
    csv_path.write_text("player,stat,line,game,team,odds_type,start_time,position\n" + "".join(
        f"{p},points,0.5,H{i} v A{i},H{i},standard,{START},F\n" for i, p in enumerate(players)))
    return csv_path, sends


def cycle_args(tmp_path, csv_path, snaps=True, **kw):
    return SimpleNamespace(sport="nhl", csv=str(csv_path), board="none", feed="none", seen=(), nudge={}, stale_min=45,
                           max_board_age=0, min_ev=0.545, flex=True, odds=None, pool=None, dedupe_min=30,
                           leg_cooldown_min=120, kelly=0.25, unit_pct=0.01, ledger=tmp_path / "l.csv", research=False,
                           snapshots=snapshots.Snapshots("nhl", tmp_path / "snapshots_nhl.sqlite3") if snaps else None,
                           steam=steam.SteamTracker(), board_info={}, **kw)


def test_the_alert_reads_its_values_back_from_the_log_and_without_a_log_says_so(tmp_path, monkeypatch):
    csv_path, sends = kalshi_board(tmp_path, monkeypatch, [0.62 - i / 100 for i in range(7)])
    a = cycle_args(tmp_path, csv_path)
    asyncio.run(sentinel.cycle(a, [], chat_id="1"))
    con = sqlite3.connect(tmp_path / "snapshots_nhl.sqlite3")
    rows = con.execute("SELECT player, display_json, slip_json FROM slip_legs WHERE builder='live'").fetchall()
    assert len(sends) == 1 and rows
    for player, d, sj in rows:
        rec = json.loads(d)
        assert signals.display_line(rec) in sends[0]                     # the alert line IS the logged record
        assert signals.value(json.loads(sj), "kelly_growth") > 0
        assert json.loads(sj)["displayed_multiplier"] == {"value": None, "missing": "not_on_board"}
    tmp2 = tmp_path / "nolog"
    tmp2.mkdir()
    csv2, sends2 = kalshi_board(tmp2, monkeypatch, [0.62 - i / 100 for i in range(7)])
    asyncio.run(sentinel.cycle(cycle_args(tmp2, csv2, snaps=False), [], chat_id="1"))
    assert len(sends2) == 1 and "🔎 fair n/a (not logged)" in sends2[0]


# --------------------------------------------------------------------------- live behaviour unchanged (Builds 1-5)
def live_state(tmp_path, monkeypatch, sub, probs, **kw):
    d = tmp_path / sub
    d.mkdir()
    csv_path, sends = kalshi_board(d, monkeypatch, probs)
    a = cycle_args(d, csv_path, **kw)
    for _ in range(2):
        asyncio.run(sentinel.cycle(a, [], chat_id="1"))
    con = sqlite3.connect(d / "snapshots_nhl.sqlite3")
    live = [r[0] for r in con.execute("SELECT live_json FROM cycles ORDER BY id")]
    elig = [r[0] for r in con.execute("SELECT eligible_json FROM cycles ORDER BY id")]
    legs = con.execute("SELECT builder, player, side, p_entry, units, ev, size FROM slip_legs ORDER BY id").fetchall()
    strip = lambda m: "\n".join(x for x in m.splitlines() if not x.startswith("   🔎"))
    return live, elig, legs, [strip(m) for m in sends]


def test_slips_stakes_and_eligible_legs_are_identical_with_every_signal_on_or_off(tmp_path, monkeypatch):
    probs = [0.63 - i / 100 for i in range(7)]
    on = live_state(tmp_path, monkeypatch, "on", probs, targeted={"keys": {("player a", "points", None)}, "n": 1, "error": None},
                    shadow_rows=[{"player": "Goblin Guy", "stat": "points", "line": "0.5", "game": "H9 v A9", "team": "H9",
                                  "odds_type": "goblin", "start_time": START}])
    monkeypatch.setitem(pipeline_config.CONFIG, "signals", {"line_gap": False, "shadow_promo": False})
    off = live_state(tmp_path, monkeypatch, "off", probs)
    assert on == off


# --------------------------------------------------------------------------- Build 2: prob_delta
def ev_for(before, after, side="More", refreshed=True):
    l = scanner.Leg("P", "points", 0.5, "A v B", team="A", sport="nhl", start_time=START)
    l.quotes = [scanner.Quote("books", "e", "x", after["p_over"], 0.0)]
    return {**signals.jit_event(l, before, after, refreshed, None, 1.0, 2.0), "side": side}


OBS = lambda p, ts, books=("draftkings", "fanduel", "pinnacle"): {"p_over": p, "fetched_ts": ts, "source": "books",
                                                                   "venue": "books", "line": 0.5,
                                                                   "books": sorted(books), "method": "m"}


def test_prob_delta_tells_a_market_move_from_a_book_dropping_out():
    moved = signals.prob_delta(ev_for(OBS(0.55, 100), OBS(0.58, 900)), None)
    assert moved["prob_delta"]["value"] == pytest.approx(0.03) and moved["direction"]["value"] == "toward"
    assert moved["composition_changed"]["value"] is False and moved["prev_from"] == "pre_refresh_quote"
    assert moved["prev_ts"]["value"] == 100 and moved["cur_ts"]["value"] == 900 and moved["cur_n_books"]["value"] == 3
    dropped = signals.prob_delta(ev_for(OBS(0.58, 100), OBS(0.55, 900, ("draftkings", "fanduel"))), None)
    assert dropped["composition_changed"]["value"] is True and dropped["books_removed"]["value"] == ["pinnacle"]
    assert dropped["direction"]["value"] == "against"
    less = signals.prob_delta(ev_for(OBS(0.40, 100), OBS(0.38, 900), side="Less"), None)
    assert less["direction"]["value"] == "toward" and less["prob_delta"]["value"] == pytest.approx(0.02)


def test_prob_delta_missing_values_carry_reasons():
    assert signals.prob_delta(ev_for(OBS(0.55, 100), OBS(0.58, 100)), None)["prob_delta"]["missing"] == "no_new_fetch"
    assert signals.prob_delta(ev_for(OBS(0.55, 100), OBS(0.55, 900)), None)["prob_delta"]["missing"] == "no_previous_distinct"
    via_log = signals.prob_delta(ev_for(OBS(0.55, 100), OBS(0.55, 900)), {**OBS(0.52, 50), "row_ts": 60})
    assert via_log["prev_from"] == "price_log" and via_log["prob_delta"]["value"] == pytest.approx(0.03)
    assert signals.prob_delta(ev_for(OBS(0.55, 100), OBS(0.58, 900), refreshed=False), None)["prob_delta"]["missing"] == "not_refreshed"


def test_last_distinct_price_skips_heartbeat_repeats_and_the_current_fetch(tmp_path):
    db = snapshots.Snapshots("nhl", tmp_path / "snapshots_nhl.sqlite3")
    rec = lambda p, t: books_priced("P", p, t, [BOOK_REC("draftkings", 1.8, 2.0)])
    db.prices(1, 1000, [rec(0.52, 990)])
    db.prices(2, 2000, [rec(0.55, 1990)])
    db.prices(3, 2700, [rec(0.55, 2690)])                      # heartbeat repeat of 0.55 (same price, 700 s later)
    db.prices(4, 3500, [rec(0.58, 3490)])                      # the current fetch
    got = db.prev_distinct("nhl", "P", "points", 0.5, "books", START_TS, 3490, 0.58)
    assert got["p_over"] == pytest.approx(0.55) and got["fetched_ts"] == 2690
    assert db.prev_distinct("nhl", "P", "points", 0.5, "books", START_TS, 3490, 0.55)["p_over"] == pytest.approx(0.52)
    assert db.prev_distinct("nhl", "P", "points", 0.5, "books", START_TS, 900, 0.55) is None


def test_the_jit_wrapper_logs_every_refreshed_leg_and_returns_exactly_what_the_refresh_did():
    now = time.time()
    mk = lambda: [books_leg(c, 0.60, now - 60) for c in "ABCD"] + [books_leg("E", 0.60, now - 1800)]
    a1, a2 = SimpleNamespace(sport="nhl", odds=FakeOdds({"E": 0.62}), min_ev=0.545, kelly=0.25, unit_pct=0.01), \
        SimpleNamespace(sport="nhl", odds=FakeOdds({"E": 0.62}), min_ev=0.545, kelly=0.25, unit_pct=0.01)
    r1 = asyncio.run(sentinel.jit_refresh(a1, jit_entry(mk()), set()))
    r2 = asyncio.run(sentinel._jit_refresh(a2, jit_entry(mk()), set()))
    assert (r1[0][0], r1[1]) == (r2[0][0], r2[1]) and a1.odds.calls == a2.odds.calls == [["E"]]
    assert [ev["player"] for ev in a1.jit_events] == ["E"] and a1.jit_events[0]["refreshed"]
    assert a1.jit_events[0]["after"]["p_over"] == pytest.approx(0.62) and a1.jit_events[0]["before"]["p_over"] == 0.60


def slip_db(tmp_path, legs_by_slip, entry_ts, closes, jit=None, meta=None):
    """A snapshot file with live slips at entry_ts and price rows: entry reads at entry_ts, closes as given."""
    db = snapshots.Snapshots("nhl", tmp_path / "snapshots_nhl.sqlite3")
    allegs = {id(l): l for legs in legs_by_slip for l in legs}
    db.prices(1, entry_ts, list(allegs.values()))
    for i, legs in enumerate(legs_by_slip):
        db.record_slip("live", 1, entry_ts + i, legs, 0.05, True, 1.0,
                       leg_meta={id(l): (meta or {}).get(l.player) or signals.leg_meta(l, None, False, None) for l in legs})
    for t, legs in closes:
        db.prices(2, t, legs)
    if jit:
        db.record_jit_events(jit)
    return tmp_path / "snapshots_nhl.sqlite3"


def test_harness_slices_prob_delta_by_direction_and_composition_on_distinct_legs(tmp_path):
    t0 = START_TS - 3 * 3600
    a = books_priced("Alpha", 0.58, t0 - 10, [BOOK_REC("draftkings", 1.7, 2.2)])
    moved = books_priced("Alpha", 0.58, t0 - 10, [BOOK_REC("draftkings", 1.7, 2.2)], line=1.5)   # another line: new leg
    closes = [(START_TS - 600, [books_priced("Alpha", 0.60, START_TS - 610, [BOOK_REC("draftkings", 1.6, 2.4)]),
                                books_priced("Alpha", 0.60, START_TS - 610, [BOOK_REC("draftkings", 1.6, 2.4)], line=1.5)])]
    ev = {**ev_for(OBS(0.55, t0 - 1000), OBS(0.58, t0 - 10)), "player": "Alpha", "cycle_ts": t0, "start_ts": START_TS,
          "stat": "points", "line": 0.5, "source": "books", "sport": "nhl"}
    path = slip_db(tmp_path, [[a], [a], [a], [moved]], t0, closes, jit=[ev])
    h = backtest.History(path, "nhl")
    slips = backtest.live_slips(h)
    assert len(slips) == 4                                                       # four slip appearances...
    recs = backtest.leg_records(slips, {"nhl": h}, START_TS + 3600)
    out = "\n".join(backtest.signal_lines(recs, {"nhl": h}, backtest.Outcomes(tmp_path / "o.sqlite3"), START_TS + 3600))
    assert signals.CLOSING_WARNING in out
    toward = next(x for x in out.splitlines() if x.strip().startswith("toward"))
    assert "eligible 1, usable 1" in toward                                      # ...one distinct leg at 0.5
    block = out.split("by direction")[1].split("by book composition")[0]
    assert "no JIT refresh logged" in block and "n=1 " in block and "(insufficient data)" in block
    assert "games 1" in block and "players 1" in block and "median" in block and "95% CI" in block
    assert "composition unchanged" in out


# --------------------------------------------------------------------------- distinct legs and closing quality
def test_close_reasons_open_missing_stale_same_fetch_and_ok(tmp_path):
    t0 = START_TS - 3 * 3600
    same = books_priced("Same", 0.58, t0 - 10, [BOOK_REC("draftkings", 1.7, 2.2)])
    stale = books_priced("Stale", 0.58, t0 - 10, [BOOK_REC("draftkings", 1.7, 2.2)])
    ok = books_priced("Fine", 0.58, t0 - 10, [BOOK_REC("draftkings", 1.7, 2.2)])
    gone = books_priced("Gone", 0.58, t0 - 10, [BOOK_REC("draftkings", 1.7, 2.2)])
    closes = [(START_TS - 300, [books_priced("Same", 0.59, t0 - 10, [BOOK_REC("draftkings", 1.69, 2.2)]),  # same fetch
                                books_priced("Fine", 0.61, START_TS - 310, [BOOK_REC("draftkings", 1.6, 2.4)])]),
              (START_TS - 3600, [books_priced("Stale", 0.6, START_TS - 3610, [BOOK_REC("draftkings", 1.6, 2.4)])])]
    path = slip_db(tmp_path, [[same, stale, ok]], t0, closes)
    db = sqlite3.connect(path)
    db.execute("DELETE FROM prices WHERE player='Gone'")
    db.commit()
    h = backtest.History(path, "nhl")
    e = lambda p: backtest.LegEntry("nhl", p, "points", 0.5, "More", 0.58, "books", START_TS, f"H{p}", f"H{p} v A{p}")
    r = lambda p, now=START_TS + 60: backtest.close_reason(e(p), t0, h, now)
    assert (r("Same"), r("Stale"), r("Fine"), r("Gone"), r("Fine", now=START_TS - 60)) == \
        ("same_fetch", "stale", "ok", "missing", "open")
    assert "same fetch as entry 1" in signals.quality_line([{"close_reason": "same_fetch"}, {"close_reason": "ok"}])


def test_every_clv_report_carries_the_closing_warning(tmp_path, monkeypatch):
    from tests.test_backtest import run_cycles
    db = run_cycles(tmp_path, monkeypatch)
    rep = backtest.run([db], outcomes=backtest.Outcomes(tmp_path / "o.sqlite3", grader=FakeGrader({}), fetch=False),
                       now=START_TS + 5 * 3600)
    assert rep.count(snapshots.CLOSING_WARNING) >= 7      # CLV summary, CLV slices, and each signal section
    assert snapshots.CLOSING_WARNING in snapshots.clv_report([db])


# --------------------------------------------------------------------------- Build 3: line gap (coverage only)
def test_line_gap_is_read_from_the_cached_main_market_and_never_called_a_middle():
    cached = {"ts": 100, "books": tfo.odds_body(1.8, 2.05, point=3.5)["bookmakers"][0]["markets"][0]["outcomes"] and
              {"draftkings": tfo.odds_body(1.8, 2.05, point=3.5)["bookmakers"][0]["markets"][0]["outcomes"],
               "fanduel": tfo.odds_body(1.9, 1.9, point=2.5)["bookmakers"][0]["markets"][0]["outcomes"]}}
    l = tfo.leg(line=1.5)
    g = signals.line_gap(l, cached, "nhl", "ev1", "player_shots_on_goal")
    assert g["consensus_line"]["value"] == 3.0 and g["pp_book_line_gap_units"]["value"] == 1.5
    assert g["both_sides_available"]["value"] is True and "middle" not in json.dumps(g).lower()
    assert signals.line_gap(tfo.leg(player="Nobody Here"), cached, "nhl", "e", "m")["pp_book_line_gap_units"]["missing"] == \
        "player_not_in_main_market"
    assert signals.line_gap(l, None, "nhl", "e", "m")["pp_book_line_gap_units"]["missing"] == "market_not_cached"


def test_line_gap_logging_changes_no_price_no_request_and_no_credit(tmp_path, monkeypatch):
    def run(sub, on):
        monkeypatch.setitem(pipeline_config.CONFIG, "signals", {"line_gap": on, "shadow_promo": False})
        d = tmp_path / sub
        d.mkdir()
        api = tfo.FakeAPI()
        b = tfo.book(d, api, daily_cap=80)
        legs = [tfo.leg(), tfo.leg(line=3.5), tfo.leg(player="Tom Wilson")]
        priced = asyncio.run(b.price("nhl", legs, tfo.NOW))
        again = asyncio.run(b.reprice("nhl", [legs[0]], tfo.NOW + 1000, max_age_s=900))
        return (priced, [[(q.venue, q.p_yes, json.dumps(q.detail, sort_keys=True)) for q in l.quotes] for l in legs],
                api.calls, b.usage()["used"], again["credits"]), legs
    (on, legs_on), (off, legs_off) = run("on", True), run("off", False)
    assert on == off
    assert legs_on[1].line_gap["pp_book_line_gap_units"]["value"] == 1.0          # 3.5 vs the books' 2.5
    assert legs_on[2].line_gap["pp_book_line_gap_units"]["missing"] == "player_not_in_main_market"
    assert not hasattr(legs_on[0], "line_gap") and not any(hasattr(l, "line_gap") for l in legs_off)


def test_the_harness_reports_line_gaps_as_coverage_without_clv(tmp_path):
    db = snapshots.Snapshots("nhl", tmp_path / "snapshots_nhl.sqlite3")
    g = signals.line_gap(tfo.leg(line=1.5), {"ts": 1, "books": {"draftkings": tfo.odds_body(1.8, 2.0, 2.5)["bookmakers"][0]["markets"][0]["outcomes"]}},
                         "nhl", "e", "m")
    for t in (1, 2, 3):  # the same leg in three cycles: one distinct leg
        db.cycle(t, 0, {}, [], None, line_gap=[g])
    db.db.commit()
    out = "\n".join(backtest.line_gap_lines({"nhl": backtest.History(tmp_path / "snapshots_nhl.sqlite3", "nhl")}))
    assert "1 distinct legs: gap measured 1" in out and "by gap (stat units): 1 1" in out and "CLV" not in out.split("no CLV")[1]


# --------------------------------------------------------------------------- Build 4: demon/goblin
PROPS = [{"player": "Gob Lin", "team": "WSH", "league": "NHL", "stat": "Shots On Goal", "line": 1.5, "opponent": "MTL",
          "odds_type": "goblin", "start_time": START, "allowed_wager_types": "over"},
         {"player": "Dee Mon", "team": "WSH", "league": "NHL", "stat": "Points", "line": 1.5, "opponent": "MTL",
          "odds_type": "demon", "start_time": START},
         {"player": "Stan Dard", "team": "WSH", "league": "NHL", "stat": "Points", "line": 0.5, "opponent": "MTL",
          "odds_type": "standard", "start_time": START},
         {"player": "A + B", "team": "WSH", "league": "NHL", "stat": "Shots On Goal (Combo)", "line": 4.5, "opponent": "MTL",
          "odds_type": "goblin", "start_time": START},
         {"player": "Other League", "team": "BUF", "league": "NFL", "stat": "Receptions", "line": 3.5, "opponent": "MIA",
          "odds_type": "goblin", "start_time": START}]


def test_promo_rows_keep_only_this_leagues_single_stat_demons_and_goblins():
    rows = parse_board.promo_rows({"props": PROPS}, "nhl")
    assert [(r["player"], r["odds_type"], r["stat"]) for r in rows] == [("Gob Lin", "goblin", "shots on goal"),
                                                                        ("Dee Mon", "demon", "points")]
    std, _ = parse_board.parse_json({"props": PROPS}, "nhl")
    assert [r["player"] for r in std] == ["Stan Dard"]                         # the lines CSV is unchanged


def test_plus_ev_shadow_promo_legs_are_never_chosen_pooled_or_eligible(tmp_path, monkeypatch):
    probs = [0.63 - i / 100 for i in range(6)]
    goblins = [f"Goblin {c}" for c in "UVWXYZ"]
    extra = [scanner.Quote("kalshi", f"KXNHLPTS-98DEC31X{i}-{p}", f"{p}: 1+ points", 0.85, 0.02, title=f"{p}: 1+ points",
                           detail={"source": "kalshi", "method": "proportional", "fmt": "prob", "over_price": 0.86,
                                   "under_price": 0.16}) for i, p in enumerate(goblins)]
    csv_path, sends = kalshi_board(tmp_path, monkeypatch, probs, extra_quotes=extra)
    rows = [{"player": p, "stat": "points", "line": "0.5", "game": f"X{i} v Y{i}", "team": f"X{i}", "odds_type": "goblin",
             "start_time": START} for i, p in enumerate(goblins)]
    pool = ev_pool.EvPool(tmp_path / "pool.json")
    a = cycle_args(tmp_path, csv_path, shadow_rows=rows)
    a.pool = pool
    asyncio.run(sentinel.cycle(a, [], chat_id="1"))
    con = sqlite3.connect(tmp_path / "snapshots_nhl.sqlite3")
    chosen = {r[0] for r in con.execute("SELECT player FROM slip_legs")}
    elig = {ref[1] for (j,) in con.execute("SELECT eligible_json FROM cycles") for ref in json.loads(j)}
    pooled = {e["player"] for e in json.loads((tmp_path / "pool.json").read_text())["legs"]}
    priced = {r[0] for r in con.execute("SELECT player FROM prices")}
    shadow = con.execute("SELECT DISTINCT player, kind, odds_type, side, source FROM shadow_prices").fetchall()
    assert sends and not chosen & set(goblins) and not elig & set(goblins) and not pooled & set(goblins)
    assert not priced & set(goblins) and not any(g in m for m in sends for g in goblins)
    assert sorted(shadow) == sorted((g, "shadow_promo", "goblin", "More", "kalshi") for g in goblins)
    # on its own a goblin set would make a slip a builder takes: that is what the isolation is protecting
    l85 = [scanner.Leg(g, "points", 0.5, f"X{i} v Y{i}", team=f"X{i}", sport="nhl", start_time=START) for i, g in enumerate(goblins)]
    for l, q in zip(l85, extra):
        l.quotes = [q]
    assert sentinel.flex_entries(l85)


def test_entry_designation_slices_stay_zero_and_shadow_clv_is_labelled_apart(tmp_path):
    t0 = START_TS - 3 * 3600
    std = books_priced("Stan", 0.58, t0 - 10, [BOOK_REC("draftkings", 1.7, 2.2)])
    path = slip_db(tmp_path, [[std]], t0, [(START_TS - 300, [books_priced("Stan", 0.6, START_TS - 310,
                                                                            [BOOK_REC("draftkings", 1.6, 2.4)])])])
    db = snapshots.Snapshots("nhl", path)
    gob = scanner.Leg("Gob", "points", 1.5, "A v B", team="A", sport="nhl", odds_type="goblin", start_time=START)
    for t, p in ((t0, 0.70), (START_TS - 600, 0.74)):
        gob.quotes = [scanner.Quote("kalshi", "KX", "x", p, 0.02, detail={"fetched_ts": t})]
        db.shadow_prices(t, [gob])
    h = backtest.History(path, "nhl")
    recs = backtest.leg_records(backtest.live_slips(h), {"nhl": h}, START_TS + 60)
    out = "\n".join(backtest.signal_lines(recs, {"nhl": h}, backtest.Outcomes(tmp_path / "o.sqlite3"), START_TS + 60))
    entry = out.split("ENTRY CLV")[1].split("SHADOW PROMO")[0]
    assert "standard" in entry and "   demon                        n=0" in entry and "   goblin                       n=0" in entry
    shadow = out.split("SHADOW PROMO CLV")[1].split("BUILD 5")[0]
    assert "(shadow, Kalshi-only, not entry CLV)" in shadow and "goblin" in shadow and "+4.00" in shadow
    assert "demon legs alone take a long time" in shadow


# --------------------------------------------------------------------------- Build 5: watchlist (tag only)
def test_watchlist_file_is_capped_at_twenty_and_missing_means_not_configured(tmp_path):
    assert signals.load_targeted(tmp_path / "none.json") is None
    p = tmp_path / "t.json"
    p.write_text(json.dumps({"props": [{"player": f"P{i}", "stat": "points"} for i in range(25)]}))
    w = signals.load_targeted(p)
    assert w["n"] == 20 and "only the first 20" in w["notes"][0]
    l = scanner.Leg("P3", "Points", 0.5, "A v B")
    assert signals.watch_field(l, w) == {"value": True} and signals.watch_field(l, None)["missing"] == "no_watchlist_configured"


def test_a_three_entry_watchlist_tags_without_spending_or_changing_the_cycle(tmp_path, monkeypatch):
    players = [f"Player {c}" for c in "ABCDEF"]

    class CycleOdds(FakeOdds):
        notes = []

        async def price(self, sport, legs, now):
            for l in legs:
                l.quotes.append(scanner.Quote("books", "ev:m", "DK,FD de-vigged", 0.60, 0.0,
                                              detail={"fetched_ts": time.time() - 1800, "books": []}))
            return len(legs)

    def run(sub, targeted):
        d = tmp_path / sub
        d.mkdir()

        async def no_kalshi(series):
            return []
        sends = []

        async def fake_send(text, chat_id=None):
            sends.append(text)
            return True
        monkeypatch.setattr(sentinel.scanner, "kalshi_quotes", no_kalshi)
        monkeypatch.setattr(sentinel, "send_async", fake_send)
        csv = d / "lines_nhl.csv"
        csv.write_text("player,stat,line,game,team,odds_type,start_time,position\n" + "".join(
            f"{p},points,0.5,H{i} v A{i},H{i},standard,{START},F\n" for i, p in enumerate(players)))
        odds = CycleOdds({p: 0.61 for p in players})
        a = cycle_args(d, csv, targeted=targeted)
        a.odds = odds
        asyncio.run(sentinel.cycle(a, [], chat_id="1"))
        con = sqlite3.connect(d / "snapshots_nhl.sqlite3")
        meta = {r[0]: json.loads(r[1]) for r in con.execute("SELECT player, leg_json FROM slip_legs WHERE builder='live'")}
        cov = [json.loads(r[0]) for r in con.execute("SELECT watchlist_json FROM cycles") if r[0]]
        priced = con.execute("SELECT COUNT(DISTINCT player) FROM prices").fetchone()[0]
        return odds.calls, [m.split("\n🔎")[0] for m in sends], meta, cov, priced, con
    listed = {"keys": {("player a", "points", None), ("player b", "points", "nhl"), ("nobody", "points", None)},
              "n": 3, "error": None}
    calls_w, sends_w, meta_w, cov_w, priced_w, con = run("with", listed)
    calls_n, sends_n, meta_n, cov_n, priced_n, _ = run("without", None)
    assert calls_w == calls_n and len(calls_w) == 1                     # (a) the same JIT, nothing extra spent
    assert priced_w == priced_n == 6                                    # (b) every board leg still scanned and priced
    assert meta_w["Player A"]["watchlist"] == {"value": True} and meta_w["Player C"]["watchlist"] == {"value": False}
    assert meta_n["Player A"]["watchlist"]["missing"] == "no_watchlist_configured"          # (c) on the slip record
    assert cov_w[0]["on_board"] == 2 and cov_w[0]["jit_events"] == 2 and cov_n == []
    tagged = con.execute("SELECT player, refreshed, watchlist FROM jit_events ORDER BY player").fetchall()
    assert ("Player A", 1, 1) in tagged and ("Player C", 1, 0) in tagged
