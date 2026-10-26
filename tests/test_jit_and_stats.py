"""Just-in-time books refresh before a slip is claimed; the stats-vs-market gap as a logged feature."""
import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from local import backtest, enricher, ev_pool, sentinel, snapshots, stat_research
from local import prize_picks_ev_scraper as scanner
from tests import test_fetch_odds as tfo
from tests.test_enricher import OVI, stub


# --------------------------------------------------------------------------- OddsBook.reprice
class MovingAPI(tfo.FakeAPI):
    """Odds that move between calls: Over 1.80/2.05 first, then 2.05/1.80."""
    async def __call__(self, url, params):
        status, body, headers = await super().__call__(url, params)
        if "/odds" in url and sum("/odds" in c for c in self.calls) > 1:
            body = tfo.odds_body(2.05, 1.80)
        return status, body, headers


def test_reprice_refreshes_only_markets_older_than_the_jit_age(tmp_path):
    api = MovingAPI()
    b = tfo.book(tmp_path, api, daily_cap=80)
    l = tfo.leg()
    asyncio.run(b.price("nhl", [l], tfo.NOW))
    p0 = l.quotes[0].p_yes
    got = asyncio.run(b.reprice("nhl", [l], tfo.NOW + 600, max_age_s=900))       # 10 min old: no call
    assert got["credits"] == 0 and sum("/odds" in c for c in api.calls) == 1 and l.quotes[0].p_yes == p0
    got = asyncio.run(b.reprice("nhl", [l], tfo.NOW + 1000, max_age_s=900))      # 16.7 min (< 1 h TTL): refreshed
    assert got["credits"] == 1 and sum("/odds" in c for c in api.calls) == 2
    assert l.quotes[0].detail["fetched_ts"] == tfo.NOW + 1000 and l.quotes[0].p_yes < 0.5 < p0  # the market moved
    assert len(l.quotes) == 1 and l.quotes[0].venue == "books"


def test_reprice_refused_by_the_budget_keeps_the_old_quote(tmp_path):
    api = tfo.FakeAPI()
    b = tfo.book(tmp_path, api, daily_cap=80)
    l = tfo.leg()
    asyncio.run(b.price("nhl", [l], tfo.NOW))
    old = l.quotes
    b.cache["usage"]["by_group"]["other"] = 40                                  # NHL's share is spent
    b.save()
    got = asyncio.run(b.reprice("nhl", [l], tfo.NOW + 1000, max_age_s=900))
    q = l.quotes[0]                                 # re-attached from the same (still usable) cache entry
    assert got["credits"] == 0 and q.p_yes == old[0].p_yes and q.detail["fetched_ts"] == tfo.NOW
    assert any("other share 40/40" in n for n in got["notes"])


# --------------------------------------------------------------------------- sentinel.jit_refresh
def books_leg(name, p, fetched, game=None, pooled=False):
    l = scanner.Leg(name, "points", 0.5, game or f"H{name} v A{name}", team=f"H{name}", sport="nhl",
                    start_time="2098-12-31T17:00:00+00:00")
    if pooled:
        l.quotes = [scanner.Quote("pool", f"nhl:{name}", "Consensus: DK", p, 0.0)]
        l.source_venue, l.fetched_ts = "books", fetched
    else:
        l.quotes = [scanner.Quote("books", "ev:m", "DK,FD de-vigged", p, 0.0, detail={"fetched_ts": fetched, "books": []})]
    return l


class FakeOdds:
    def __init__(self, new_p=None, fetch=True):
        self.new_p, self.fetch, self.calls = new_p or {}, fetch, []

    async def reprice(self, sport, legs, now, max_age_s):
        self.calls.append(sorted(l.player for l in legs))
        if not self.fetch:
            return {"credits": 0, "notes": ["1 market refresh(es) skipped: budget 80/80 today"]}
        for l in legs:
            p = self.new_p.get(l.player, l.p_over)
            l.quotes = [scanner.Quote("books", "ev:m", "DK,FD de-vigged", p, 0.0, detail={"fetched_ts": now, "books": []})]
        return {"credits": len(legs), "notes": []}


def entry(legs):
    ev = sentinel.correlation.slip_ev(tuple(legs), scanner.PAYOUTS["flex"][len(legs)], [], 1.0)
    return (ev, "flex", tuple(legs), [])


def args(odds):
    return SimpleNamespace(sport="nhl", odds=odds, min_ev=0.545, kelly=0.25, unit_pct=0.01)


def run_jit(a, e, touched=None):
    return asyncio.run(sentinel.jit_refresh(a, e, set() if touched is None else touched))


def test_fresh_books_prices_are_not_refreshed():
    now = time.time()
    odds = FakeOdds()
    e = entry([books_leg(c, 0.60, now - 60) for c in "ABCDE"])
    assert run_jit(args(odds), e) == (e, "") and odds.calls == []


def test_stale_books_legs_are_refreshed_and_the_slip_re_evaluated():
    now = time.time()
    legs = [books_leg(c, 0.60, now - 60) for c in "ABCD"] + [books_leg("E", 0.60, now - 1800)]   # E is 30 min old
    odds = FakeOdds({"E": 0.62})
    new, note = run_jit(args(odds), entry(legs))
    assert odds.calls == [["E"]] and "1 stale books leg(s), 1 credit(s)" in note
    assert new[0] > entry([books_leg(c, 0.60, now) for c in "ABCD"] + [books_leg("E", 0.62, now)])[0] - 1e-9
    assert snapshots.price_age(legs[4], time.time()) < 5


def test_a_refreshed_leg_below_the_threshold_drops_the_slip_unclaimed():
    now = time.time()
    legs = [books_leg(c, 0.60, now - 60) for c in "ABCD"] + [books_leg("E", 0.58, now - 1800)]
    new, note = run_jit(args(FakeOdds({"E": 0.52})), entry(legs))               # the market moved against us
    assert new is None and "fell below 54.5%" in note


def test_refused_refresh_sends_by_default_and_skips_when_configured(monkeypatch):
    now = time.time()
    legs = [books_leg(c, 0.60, now - 60) for c in "ABCD"] + [books_leg("E", 0.60, now - 1800)]
    new, note = run_jit(args(FakeOdds(fetch=False)), entry(legs))
    assert new is not None and "1 still stale" in note and "budget 80/80" in note
    monkeypatch.setitem(sentinel.pipeline_config.CONFIG["jit_refresh"], "on_refused", "skip")
    new, note = run_jit(args(FakeOdds(fetch=False)), entry(legs))
    assert new is None and note.endswith("slip skipped")


def test_pooled_books_legs_are_refreshed_through_a_clean_copy():
    now = time.time()
    legs = [books_leg(c, 0.60, now - 60) for c in "ABCD"] + [books_leg("E", 0.60, now - 1800, pooled=True)]
    odds = FakeOdds({"E": 0.61})
    new, _ = run_jit(args(odds), entry(legs))
    e = legs[4]
    assert odds.calls == [["E"]] and e.quotes[0].venue == "books" and e.source_venue == "books" and e.fetched_ts is None
    assert e.p_over == pytest.approx(0.61) and snapshots.price_age(e, time.time()) < 5 and new is not None


def test_a_later_slip_sharing_a_refreshed_leg_is_re_evaluated_without_a_second_call():
    now = time.time()
    shared = books_leg("E", 0.60, now - 1800)
    first = entry([books_leg(c, 0.60, now - 60) for c in "ABCD"] + [shared])
    second = entry([books_leg(c, 0.60, now - 60) for c in "FGHI"] + [shared])
    odds, touched = FakeOdds({"E": 0.53}), set()
    assert run_jit(args(odds), first, touched)[0] is None
    new, note = run_jit(args(odds), second, touched)
    assert new is None and "fell below" in note and odds.calls == [["E"]]


def test_a_repeat_slip_spends_no_refresh(tmp_path):
    pool = ev_pool.EvPool(tmp_path / "pool.json")
    keys = frozenset({("nhl", "A", "points", 0.5, "More")})
    repeat = lambda k, ev, s: sentinel.repeat_keys(k, ev, s, 1000.0, 1800)
    assert pool.would_claim(keys, 0.1, 1000.0, 1800, repeat, 7200)
    assert pool.claim(keys, 0.1, 1000.0, 1800, repeat, 7200)
    assert not pool.would_claim(keys, 0.1, 1060.0, 1800, repeat, 7200)        # already sent: no JIT spent on it
    state = json.loads((tmp_path / "pool.json").read_text())
    assert len(state["sent"]) == 1                                             # would_claim records nothing


# --------------------------------------------------------------------------- stats-vs-market gap
NOW = tfo.NOW + 2 * 86400  # just after OVI's newest game (2026-10-15)


def test_gap_is_recent_hit_rate_of_our_side_minus_fair():
    vals = [(f"2026-10-{15 - i}T23:00:00Z", "BOS", v) for i, v in enumerate([4, 0, 3, 2, 5, 6])]
    r = stat_research.gap_record(vals, 2.5, "More", 0.58, NOW, games=10, min_games=5)
    assert r["n"] == 6 and r["hit_rate"] == pytest.approx(4 / 6, abs=1e-4) and r["gap"] == pytest.approx(4 / 6 - 0.58, abs=1e-4)
    less = stat_research.gap_record(vals, 2.5, "Less", 0.55, NOW, games=10, min_games=5)
    assert less["hit_rate"] == pytest.approx(2 / 6, abs=1e-4) and less["gap"] < -0.2
    few = stat_research.gap_record(vals[:3], 2.5, "More", 0.58, NOW, games=10, min_games=5)
    assert few["gap"] is None and few["note"] == "3 recent games"
    old = [("2026-04-10T23:00:00Z", "BOS", 5.0)] * 8                            # last season only (October)
    r = stat_research.gap_record(old, 2.5, "More", 0.58, NOW, games=10, min_games=5)
    assert r["gap"] is None and "8 older than 30 days" in r["note"]


def test_stat_gap_reads_the_enricher_game_log_and_never_raises():
    get = stub([("3101", "Alex Ovechkin", "nhl", "Washington Capitals", True)], {"3101": OVI})
    l = tfo.leg()
    l.sport = "nhl"
    l.quotes = [scanner.Quote("books", "x", "x", 0.58, 0.0)]
    enricher._cache.clear()
    r = asyncio.run(stat_research.stat_gap(l, get, NOW))
    assert r["n"] == 6 and r["gap"] == pytest.approx(4 / 6 - 0.58, abs=1e-4) and r["note"] == ""          # preseason ignored
    bad = scanner.Leg("Nobody", "points", 0.5, "A v B", sport="nhl")
    bad.quotes = [scanner.Quote("kalshi", "x", "x", 0.56, 0.02)]
    assert asyncio.run(stat_research.stat_gap(bad, get, NOW))["gap"] is None
    hits = scanner.Leg("Alex Ovechkin", "hits", 1.5, "A v B", sport="nhl")
    hits.quotes = [scanner.Quote("kalshi", "x", "x", 0.56, 0.02)]
    assert "isn't in ESPN game logs" in asyncio.run(stat_research.stat_gap(hits, get, NOW))["note"]


def test_gaps_are_logged_on_chosen_legs_and_sliced_in_the_harness(tmp_path):
    from tests.test_steam_snapshots import leg, NOW as SNOW
    db = snapshots.Snapshots("nhl", tmp_path / "snapshots_nhl.sqlite3")
    legs = [leg(0.56), leg(0.57, player="Nick Suzuki")]
    db.prices(1, SNOW, legs)
    sid = db.record_slip("paper", 1, SNOW, legs, 0.05, False)
    assert sid and db.record_slip("paper", 2, SNOW + 60, legs, 0.05, False) is None   # deduped: no id
    recs = {"Alex Ovechkin": {"gap": -0.2, "note": ""}, "Nick Suzuki": {"gap": None, "note": "3 recent games"}}

    async def fake_gap(l, get=None, now=None):
        return recs[l.player]
    import local.stat_research as sr
    orig, sr.stat_gap = sr.stat_gap, fake_gap
    try:
        assert asyncio.run(stat_research.log_gaps(db, [(sid, legs)])) == 2
    finally:
        sr.stat_gap = orig
    rows = dict(db.db.execute("SELECT player, stats_json FROM slip_legs"))
    assert json.loads(rows["Alex Ovechkin"])["gap"] == -0.2
    h = backtest.History(tmp_path / "snapshots_nhl.sqlite3", "nhl")
    e = backtest.LegEntry("nhl", "Alex Ovechkin", "shots on goal", 2.5, "More", 0.56, "kalshi", legs[0].start_ts)
    out = backtest.leg_records([backtest.Slip("paper", "nhl", SNOW, [e], 0.05, 1.0, results=["hit"])], {"nhl": h}, SNOW)
    assert out[0]["gap"] == -0.2                                     # a replayed paper leg finds its logged gap
    assert backtest.gap_bucket(-0.2, "") == "gap < -15 pts" and backtest.gap_bucket(None, "x") == "gap n/a"
    assert backtest.gap_bucket(None, None) == "not logged" and backtest.gap_bucket(0.0, "") == "-5 to +5 pts"


def test_the_cycle_logs_gaps_in_the_background_without_awaiting_them(tmp_path, monkeypatch):
    from tests.test_backtest import run_cycles
    seen = []

    async def fake_log(snaps, chosen, get=None, now=None):
        seen.append([(sid.split(":")[0], len(legs)) for sid, legs in chosen])
        return 0
    monkeypatch.setattr(sentinel.stat_research, "log_gaps", fake_log)
    real = SimpleNamespace

    def ns(**kw):
        return real(stat_feature=True, **kw)
    monkeypatch.setattr("tests.test_backtest.SimpleNamespace", ns)
    run_cycles(tmp_path, monkeypatch, n=1)
    assert seen and {b for b, _ in seen[0]} == {"live", "paper"}


def test_a_full_cycle_refreshes_stale_books_legs_before_sending(tmp_path, monkeypatch, capsys):
    players = [f"Player {c}" for c in "ABCDEF"]

    class CycleOdds(FakeOdds):
        notes = []

        async def price(self, sport, legs, now):            # the cycle's normal pass: a 30-minute-old cache
            for l in legs:
                l.quotes.append(scanner.Quote("books", "ev:m", "DK,FD de-vigged", 0.60, 0.0,
                                              detail={"fetched_ts": time.time() - 1800, "books": []}))
            return len(legs)

    async def no_kalshi(series):
        return []
    sends = []

    async def fake_send(text, chat_id=None):
        sends.append(text)
        return True
    monkeypatch.setattr(sentinel.scanner, "kalshi_quotes", no_kalshi)
    monkeypatch.setattr(sentinel, "send_async", fake_send)
    csv = tmp_path / "lines_nhl.csv"
    csv.write_text("player,stat,line,game,team,odds_type,start_time,position\n" + "".join(
        f"{p},points,0.5,H{i} v A{i},H{i},standard,2098-12-31T17:00:00+00:00,F\n" for i, p in enumerate(players)))
    odds = CycleOdds({p: 0.61 for p in players})
    a = SimpleNamespace(sport="nhl", csv=str(csv), board="none", feed="none", seen=(), nudge={}, stale_min=45,
                        max_board_age=0, min_ev=0.545, flex=True, odds=odds, pool=None, dedupe_min=30,
                        leg_cooldown_min=120, kelly=0.25, unit_pct=0.01, ledger=tmp_path / "l.csv", research=False)
    asyncio.run(sentinel.cycle(a, [], chat_id="1"))
    out = capsys.readouterr().out
    assert len(odds.calls) == 1 and "JIT refresh: 6 stale books leg(s), 6 credit(s): EV" in out   # best slip only
    assert len(sends) == 1 and "61.0%" in sends[0]                                 # sent at the refreshed price
