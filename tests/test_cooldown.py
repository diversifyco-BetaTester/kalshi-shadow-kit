import asyncio
import time
from types import SimpleNamespace

import pytest

from local import ev_pool, sentinel
from local import prize_picks_ev_scraper as scanner
from local.parse_board import parse_json

KICK = "2099-01-01T17:00:00Z"  # noon ET: same game day as the 99JAN01 tickers


def prop(player, **kw):
    return {"player": player, "team": "BOS", "league": "NHL", "stat": "Points", "line": 0.5, "opponent": "NYR",
            "start_time": KICK, "odds_type": "standard", **kw}


def test_strict_filter_drops_more_only_discounted_and_combo_standard_lines():
    rows, bad = parse_json({"props": [
        prop("Two Way", allowed_wager_types="under_or_over"),
        prop("No Field"),                                                    # field absent: kept as two-way
        prop("More Only", allowed_wager_types="over"),
        prop("Free Square", discount_name="free_square", discount_percentage=98, is_promo=False),
        prop("Combo Event", event_type="combo"),
        prop("Adjusted", adjusted_odds=True, allowed_wager_types="under_or_over"),
        prop("Goblin", odds_type="goblin", allowed_wager_types="under_or_over"),
        prop("Demon", odds_type="demon")]}, "nhl")
    assert [r["player"] for r in rows] == ["Two Way", "No Field", "Adjusted"]
    for why in ("over-only line", "discounted line (free_square)", "combo projection", "goblin line", "demon line"):
        assert any(why in b for b in bad), why


def test_pool_leg_cooldown_is_shared_and_expires(tmp_path):
    path = tmp_path / "pool.json"
    nhl, mlb = ev_pool.EvPool(path), ev_pool.EvPool(path)
    k1, k2, k3 = ("nhl", "A", "points", 0.5, "More"), ("nhl", "B", "points", 0.5, "More"), ("mlb", "C", "hits", 0.5, "More")
    never = lambda keys, ev, sent: False
    assert nhl.claim(frozenset({k1, k2}), 0.1, 1000, 1800, never, 7200)
    assert mlb.recent_legs(1060, 7200) == {k1, k2}                           # the other sentinel sees them
    assert not mlb.claim(frozenset({k2, k3}), 0.5, 1060, 1800, never, 7200)  # shares B: refused, whatever the EV
    assert mlb.claim(frozenset({k3}), 0.1, 1060, 1800, never, 7200)
    assert nhl.recent_legs(1000 + 7200, 7200) == {k3}                        # A and B cooled down after 120 min


def run_cycles(tmp_path, monkeypatch, pool, players, cycles=2):
    """Sentinel cycles on one board of NHL 'points 0.5' legs priced at 60% More by fake Kalshi quotes."""
    sent = []

    async def fake_send(text, chat_id=None):
        sent.append(text)
        return True

    async def quotes(series):
        return [scanner.Quote("kalshi", f"KXNHLPTS-99JAN01G{i}-{p}", f"{p}: 1+ points", 0.60, 0.02,
                              title=f"{p}: 1+ points") for i, p in enumerate(players)]
    monkeypatch.setattr(sentinel, "send_async", fake_send)
    monkeypatch.setattr(sentinel.scanner, "kalshi_quotes", quotes)
    csv = tmp_path / "lines_nhl.csv"
    csv.write_text("player,stat,line,game,team,odds_type,start_time\n" + "".join(
        f"{p},points,0.5,H{i} v A{i},{'H' if i % 2 else 'A'}{i},standard,{KICK}\n" for i, p in enumerate(players)))  # one game each
    a = SimpleNamespace(sport="nhl", csv=str(csv), board="none", feed="none", seen=(), nudge={}, stale_min=45,
                        max_board_age=60, min_ev=0.545, flex=True, odds=None, pool=pool, dedupe_min=30,
                        leg_cooldown_min=120, kelly=0.25, unit_pct=0.01, ledger=tmp_path / "slips.csv", research=False)
    history = []
    for _ in range(cycles):
        asyncio.run(sentinel.cycle(a, history, chat_id="123"))
    return sent


@pytest.mark.parametrize("shared", [False, True])
def test_same_legs_are_never_resent_within_the_cooldown(tmp_path, monkeypatch, shared):
    pool = ev_pool.EvPool(tmp_path / "pool.json") if shared else None
    sent = run_cycles(tmp_path, monkeypatch, pool, [f"Player {c}" for c in "ABCDEF"], cycles=3)
    assert len(sent) == 1                                                    # 6 legs: one 6-pick, then quiet
    assert all(sent[0].count(f"<code>Player {c}</code>") == 1 for c in "ABCDEF")


def test_a_new_slip_is_built_from_legs_not_cooling_down(tmp_path, monkeypatch):
    pool = ev_pool.EvPool(tmp_path / "pool.json")
    run_cycles(tmp_path, monkeypatch, pool, [f"Player {c}" for c in "ABCDEF"], cycles=1)
    sent = run_cycles(tmp_path, monkeypatch, pool, [f"Player {c}" for c in "ABCDEFGHIJKL"], cycles=1)
    assert len(sent) == 1 and not any(f"<code>Player {c}</code>" in sent[0] for c in "ABCDEF")   # A-F replaced by G-L
