import asyncio
from types import SimpleNamespace

import pytest

from local import fetch_odds as fo
from local import prize_picks_ev_scraper as scanner
from local import sentinel


def side(name, point, price, who="Alex Ovechkin"):
    return {"name": name, "description": who, "price": price, "point": point}


def two_way(over, under, point=2.5):
    return [side("Over", point, over), side("Under", point, under)]


def test_american_and_decimal_odds_devig_the_same():
    assert abs(fo.implied(-110, "american") - 110 / 210) < 1e-12 and abs(fo.implied(150, "american") - 0.4) < 1e-12
    assert abs(fo.devig(-110, -110, "american") - 0.5) < 1e-12
    assert abs(fo.devig(-150, 130, "american") - fo.devig(1 + 100 / 150, 2.30)) < 1e-12
    p_over = fo.devig(1.80, 2.05)
    raw_o, raw_u = 1 / 1.80, 1 / 2.05
    assert abs(p_over - raw_o / (raw_o + raw_u)) < 1e-12 and abs((1 - p_over) - raw_u / (raw_o + raw_u)) < 1e-12


def test_pinnacle_counts_one_and_a_half():
    books = {"pinnacle": two_way(1.70, 2.20), "draftkings": two_way(1.91, 1.91), "fanduel": two_way(1.87, 1.95)}
    p, names = fo.consensus(books, "Alex Ovechkin", 2.5)
    pin, dk, fd = fo.devig(1.70, 2.20), 0.5, fo.devig(1.87, 1.95)
    assert abs(p - (1.5 * pin + dk + fd) / 3.5) < 1e-12 and names == ["PIN", "DK", "FD"]


def test_soft_books_only_is_a_plain_average_and_one_sided_books_are_skipped():
    books = {"betmgm": two_way(1.80, 2.00), "fanduel": two_way(1.95, 1.87), "draftkings": [side("Over", 2.5, 1.70)]}
    p, names = fo.consensus(books, "Alex Ovechkin", 2.5)
    assert abs(p - (fo.devig(1.80, 2.00) + fo.devig(1.95, 1.87)) / 2) < 1e-12 and names == ["FD", "MGM"]
    assert fo.consensus({"pinnacle": two_way(1.9, 1.9, point=3.5)}, "Alex Ovechkin", 2.5) is None   # other line


def test_alternate_ladders_are_weighted_too():
    main = {"pinnacle": two_way(1.80, 2.05), "draftkings": two_way(1.80, 2.05)}
    alt = {"pinnacle": [side("Over", 3.5, 3.00)], "draftkings": [side("Over", 3.5, 3.40)]}
    p, names, interp = fo.ladder_consensus(main, alt, "Alex Ovechkin", 3.5)
    m = 1 / 1.80 + 1 / 2.05
    assert abs(p - (1.5 * (1 / 3.00) / m + (1 / 3.40) / m) / 2.5) < 1e-12 and names == ["PIN", "DK"] and not interp


def test_requests_ask_all_four_books(tmp_path):
    seen = []
    events = [{"id": "e1", "commence_time": "2099-01-01T17:00:00Z", "home_team": "Washington Capitals",
               "away_team": "Montréal Canadiens"}]

    async def api(url, params):
        seen.append(params.get("bookmakers"))
        if url.endswith("/events"):
            return 200, events, {"x-requests-last": "0", "x-requests-remaining": "300"}
        return 200, {"bookmakers": [{"key": b, "markets": [{"key": "player_shots_on_goal", "outcomes": two_way(1.9, 1.9)}]}
                                    for b in ("pinnacle", "betmgm")]}, {"x-requests-last": "1", "x-requests-remaining": "299"}
    l = scanner.Leg("Alex Ovechkin", "shots on goal", 2.5, "MTL v WSH", team="WSH", start_time=events[0]["commence_time"])
    now = fo.ts(events[0]["commence_time"]) - 3600
    assert asyncio.run(fo.OddsBook("k", cache=tmp_path / "c.json", get=api).price("nhl", [l], now)) == 1
    assert seen[-1] == "pinnacle,draftkings,fanduel,betmgm" and fo.sources(l) == "Consensus: PIN, MGM"


@pytest.mark.parametrize("spread,interp,kalshi_kept", [(0.02, False, True), (0.08, False, False), (0.02, True, False),
                                                         (None, False, False)])
def test_kalshi_first_when_liquid_else_consensus(tmp_path, monkeypatch, spread, interp, kalshi_kept):
    asked = []

    class Books:
        notes = []

        async def price(self, sport, legs, now):
            asked.extend(l.player for l in legs)
            for l in legs:
                l.quotes.append(scanner.Quote("books", "e:m", "PIN,DK de-vigged", 0.61, 0.0))
            return len(legs)

    async def quotes(series):
        if spread is None:
            return []
        return [scanner.Quote("kalshi", "KXNHLPTS-99JAN01BOSNYR-A", "A B: 1+ points", 0.57, spread,
                              title="A B: 1+ points", interpolated=interp)]
    monkeypatch.setattr(sentinel.scanner, "kalshi_quotes", quotes)
    if interp:  # an interpolated quote never comes straight from the market list: inject it after attach
        monkeypatch.setattr(sentinel.scanner, "attach", lambda legs, qs: [l.quotes.extend(qs) for l in legs])
    csv = tmp_path / "lines_nhl.csv"
    csv.write_text("player,stat,line,game,team,odds_type,start_time\nA B,points,0.5,BOS v NYR,BOS,standard,2099-01-01T17:00:00Z\n")
    a = SimpleNamespace(sport="nhl", csv=str(csv), board="none", feed="none", seen=(), nudge={}, stale_min=45,
                        max_board_age=60, min_ev=0.99, flex=True, odds=Books(), pool=None, dedupe_min=30,
                        leg_cooldown_min=120, kelly=0.25, unit_pct=0.01, ledger=tmp_path / "s.csv", research=False)
    captured = {}
    monkeypatch.setattr(sentinel, "flex_entries", lambda good, *x, **k: captured.setdefault("good", good) and [] or [])
    monkeypatch.setattr(sentinel.scanner, "pregame", lambda legs, now=None: (captured.setdefault("legs", legs), 0))
    asyncio.run(sentinel.cycle(a, [], chat_id=""))
    leg = captured["legs"][0]
    assert (asked == []) == kalshi_kept
    assert sentinel.source_tag(leg) == ("Kalshi" if kalshi_kept else "Consensus: PIN, DK")
    assert leg.pick[1] == pytest.approx(0.57 if kalshi_kept else 0.61)


def test_alert_shows_consensus_tag_and_books():
    l = scanner.Leg("Alex Ovechkin", "shots on goal", 2.5, "MTL v WSH", team="WSH", sport="nhl")
    l.quotes = [scanner.Quote("books", "e:m", "PIN,DK,FD de-vigged", 0.561, 0.0)]
    k = scanner.Leg("Nick Suzuki", "points", 0.5, "MTL v WSH", team="MTL", sport="nhl")
    k.quotes = [scanner.Quote("kalshi", "t", "t", 0.55, 0.02)]
    msg = sentinel.alert_text((0.1, "flex", (l, k), False), "nhl", 0, 0, 1.0)
    assert "   📈 56.1% · Consensus: PIN, DK, FD" in msg and "   📈 55.0% · Kalshi" in msg
