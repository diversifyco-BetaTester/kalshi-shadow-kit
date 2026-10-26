"""Per-sport fair-probability floors (pipeline_config min_fair_by_sport) for joining any slip."""
import asyncio
import time
from types import SimpleNamespace

import pytest

from local import backtest, ev_pool, pipeline_config, sentinel, snapshots
from local import prize_picks_ev_scraper as scanner

FLOORS = {"nfl": 0.555, "nba": 0.555, "nhl": 0.57}


@pytest.fixture
def floors(monkeypatch):
    monkeypatch.setitem(pipeline_config.CONFIG, "min_fair_by_sport", dict(FLOORS))


def leg(sport, name, p, game=None):
    l = scanner.Leg(name, "points", 0.5, game or f"H{name} v A{name}", team=f"H{name}", sport=sport,
                    start_time="2098-12-31T17:00:00+00:00")
    l.quotes = [scanner.Quote("kalshi", name, name, p, 0.02)]
    return l


def test_floor_per_sport_never_below_min_ev(floors):
    a = SimpleNamespace(sport="nfl", min_ev=0.545)
    assert sentinel.leg_floor(a, "nhl") == 0.57 and sentinel.leg_floor(a, "nfl") == 0.555
    assert sentinel.leg_floor(a, "mlb") == 0.545 and sentinel.leg_floor(a, "cfb") == 0.545        # not listed
    assert sentinel.leg_floor(SimpleNamespace(sport="nfl", min_ev=0.60), "nhl") == 0.60           # CLI stricter
    assert sentinel.clears_floor(a, leg("nhl", "A", 0.57)) and not sentinel.clears_floor(a, leg("nhl", "B", 0.565))


def test_defaults_have_no_sport_floors():
    assert pipeline_config.DEFAULTS["min_fair_by_sport"] == {}
    assert sentinel.leg_floor(SimpleNamespace(sport="nhl", min_ev=0.545), "nhl") == 0.545


def test_pooled_legs_meet_their_own_sports_floor_in_a_cross_sport_slip(tmp_path, monkeypatch, floors):
    """An NFL sentinel takes NHL legs from the pool only at 57%+, its own NFL legs at 55.5%+."""
    pool = ev_pool.EvPool(tmp_path / "pool.json")
    now = time.time()
    pool.publish("nhl", [(leg("nhl", "N1", 0.58), "Kalshi"), (leg("nhl", "N2", 0.56), "Kalshi")], now)
    nfl = [f"Q{i}" for i in range(5)]

    async def quotes(series):
        return [scanner.Quote("kalshi", f"KXNFLRECYDS-98DEC31G{i}-{p}", f"{p}: 1+ points", [0.60, 0.59, 0.58, 0.556, 0.55][i],
                              0.02, title=f"{p}: 1+ points") for i, p in enumerate(nfl)]
    monkeypatch.setattr(sentinel.scanner, "kalshi_quotes", quotes)
    sends = []

    async def fake_send(text, chat_id=None):
        sends.append(text)
        return True
    monkeypatch.setattr(sentinel, "send_async", fake_send)
    csv = tmp_path / "lines_nfl.csv"
    csv.write_text("player,stat,line,game,team,odds_type,start_time,position\n" + "".join(
        f"{p},points,0.5,H{i} v A{i},H{i},standard,2098-12-31T17:00:00+00:00,WR\n" for i, p in enumerate(nfl)))
    seen = {}
    real = sentinel.mixed_entries

    def spy(own, others, sport):
        seen["own"], seen["others"] = sorted(l.player for l in own), sorted(l.player for l in others)
        return real(own, others, sport)
    monkeypatch.setattr(sentinel, "mixed_entries", spy)
    a = SimpleNamespace(sport="nfl", csv=str(csv), board="none", feed="none", seen=(), nudge={}, stale_min=45,
                        max_board_age=0, min_ev=0.545, flex=True, odds=None, pool=pool, dedupe_min=30,
                        leg_cooldown_min=120, kelly=0.25, unit_pct=0.01, ledger=tmp_path / "l.csv", research=False)
    asyncio.run(sentinel.cycle(a, [], chat_id=""))
    assert seen["own"] == ["Q0", "Q1", "Q2", "Q3"]                 # 55.6% clears 55.5%, 55.0% doesn't
    assert seen["others"] == ["N1"]                                # NHL 56% is under NHL's 57%
    assert all(" N2" not in s for s in sends)


def test_jit_reevaluation_uses_the_legs_own_sport_floor(floors):
    a = SimpleNamespace(sport="nfl", min_ev=0.545, kelly=0.25, unit_pct=0.01)
    legs = [leg("nfl", f"Q{i}", 0.60) for i in range(4)] + [leg("nhl", "N", 0.565)]
    e = (0.1, "flex", tuple(legs), [])
    new, why = sentinel.reevaluate(a, e)
    assert new is None and why == "N points fell below 57.0%"


def test_a_variant_replay_applies_the_floors_to_logged_eligible_legs(tmp_path, floors):
    from tests.test_steam_snapshots import leg as sleg, NOW
    db = snapshots.Snapshots("nhl", tmp_path / "snapshots_nhl.sqlite3")
    legs = []
    for i, p in enumerate([0.60, 0.59, 0.58, 0.575, 0.565, 0.56]):
        l = sleg(p, player=f"P{i}")
        l.game, l.team = f"H{i} v A{i}", f"H{i}"
        legs.append(l)
    cid = db.cycle(NOW, 0, {"legs": 6, "priced": 6, "good": 6}, [], None, eligible=[snapshots.leg_ref(l, NOW) for l in legs])
    db.prices(cid, NOW, legs)
    h = backtest.History(tmp_path / "snapshots_nhl.sqlite3", "nhl")
    loose = pipeline_config.load(pipeline_config.PATH.with_name("__none__.json"))
    assert backtest.replay_paper(h, {"nhl": h}, loose, 0.545).slips                       # 54.5%: a slip
    strict = backtest.replay_paper(h, {"nhl": h}, pipeline_config.CONFIG, 0.545)       # 57%: only 4 legs left
    assert strict.slips == []
