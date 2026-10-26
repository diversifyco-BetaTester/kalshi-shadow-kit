import asyncio
import pytest
import sys
from datetime import datetime, timezone

from local import fetch_odds as fo
from local import prize_picks_ev_scraper as scanner
from local.sentinel import should_nudge

NOW = datetime(2026, 10, 14, 21, 0, tzinfo=timezone.utc).timestamp()  # 2 h 40 before START
START = "2026-10-14T23:40:00Z"  # 7:40 PM ET, 26OCT14
EVENTS = [{"id": "ev1", "commence_time": START, "home_team": "Washington Capitals",
           "away_team": "Montréal Canadiens"}]


def odds_body(over, under, point=2.5, who="Alex Ovechkin"):
    outcomes = [{"name": "Over", "description": who, "price": over, "point": point},
                {"name": "Under", "description": who, "price": under, "point": point}]
    return {"bookmakers": [{"key": "draftkings", "markets": [{"key": "player_shots_on_goal", "outcomes": outcomes}]},
                           {"key": "fanduel", "markets": [{"key": "player_shots_on_goal", "outcomes": outcomes}]}]}


class FakeAPI:
    def __init__(self, remaining=400):
        self.calls, self.remaining = [], remaining

    async def __call__(self, url, params):
        self.calls.append(url)
        if url.endswith("/events"):
            return 200, EVENTS, {"x-requests-last": "0", "x-requests-remaining": str(self.remaining)}
        n = len(params["markets"].split(","))
        self.remaining -= n
        return 200, odds_body(1.80, 2.05), {"x-requests-last": str(n), "x-requests-remaining": str(self.remaining)}


def leg(player="Alex Ovechkin", stat="shots on goal", line=2.5, team="WSH"):
    return scanner.Leg(player, stat, line, "MTL v WSH", team=team, start_time=START)


def book(tmp_path, api, **kw):
    return fo.OddsBook("k", cache=tmp_path / "c.json", get=api, **kw)


def test_devig_removes_the_vig():
    assert abs(fo.devig(1.91, 1.91) - 0.5) < 1e-12
    p = fo.devig(1.80, 2.05)
    assert abs(p - (1 / 1.80) / (1 / 1.80 + 1 / 2.05)) < 1e-12 and 0.5 < p < 1 / 1.80


def test_team_names_map_to_csv_codes():
    assert fo.team_code("nhl", "Montréal Canadiens") == "MTL"
    assert fo.team_code("nhl", "St Louis Blues") == "STL"
    assert fo.team_code("nhl", "Columbus Blue Jackets") == "CBJ"
    assert fo.team_code("mlb", "Boston Red Sox") == "BOS" and fo.team_code("mlb", "Cincinnati Reds") == "CIN"


def test_prices_unpriced_leg_from_consensus_exact_line_only(tmp_path):
    api = FakeAPI()
    b = book(tmp_path, api)
    hit, other_line = leg(), leg(line=3.5)
    assert asyncio.run(b.price("nhl", [hit, other_line], NOW)) == 1
    assert other_line.quotes == []
    q = hit.quotes[0]
    assert q.venue == "books" and abs(q.p_yes - fo.devig(1.80, 2.05)) < 1e-12
    assert fo.sources(hit) == "Consensus: DK, FD" and hit.pick[0] == "More"


def test_cache_ttl_means_one_paid_call_per_hour(tmp_path):
    api = FakeAPI()
    b = book(tmp_path, api)
    asyncio.run(b.price("nhl", [leg()], NOW))
    asyncio.run(b.price("nhl", [leg()], NOW + 60))          # next sentinel cycle: cache only
    b2 = book(tmp_path, api)                                 # restart: disk cache
    asyncio.run(b2.price("nhl", [leg()], NOW + 120))
    assert sum("/odds" in c for c in api.calls) == 1
    asyncio.run(b2.price("nhl", [leg()], NOW + fo.TTL_S + 1))
    assert sum("/odds" in c for c in api.calls) == 2


def test_budget_guard_stops_spending(tmp_path):
    api = FakeAPI(remaining=fo.RESERVE)                      # at the reserve: may not spend
    b = book(tmp_path, api)
    l = leg()
    assert asyncio.run(b.price("nhl", [l], NOW)) == 0 and not any("/odds" in c for c in api.calls)
    assert any("skipped" in n for n in b.notes)
    api2 = FakeAPI()
    (tmp_path / "b2").mkdir()
    b2 = book(tmp_path / "b2", api2, daily_cap=0)
    assert asyncio.run(b2.price("nhl", [leg()], NOW)) == 0 and not any("/odds" in c for c in api2.calls)


def test_no_request_without_a_mappable_leg(tmp_path):
    api = FakeAPI()
    b = book(tmp_path, api)
    assert asyncio.run(b.price("nhl", [leg(stat="goals allowed"), leg(stat="goalie fantasy score")], NOW)) == 0
    assert api.calls == []


def test_stale_nudge_once_per_window_and_reset_by_new_board(tmp_path):
    f = tmp_path / "nhl_board.json"
    f.write_text("{}")
    state = {}
    assert not should_nudge(state, f, 30, NOW, 45)
    assert should_nudge(state, f, 50, NOW, 45)
    assert not should_nudge(state, f, 51, NOW + 60, 45)
    assert should_nudge(state, f, 96, NOW + 46 * 60, 45)     # still stale 45 min later: remind again
    f.write_text('{"new": 1}')
    import os
    os.utime(f, (NOW + 3000, NOW + 3000))
    assert not should_nudge(state, f, 1, NOW + 3000, 45)     # fresh board: quiet


# ---------------------------------------------------------------- alternate ladders
def o(name, point, price, who="Alex Ovechkin"):
    return {"name": name, "description": who, "price": price, "point": point}


MAIN = [o("Over", 2.5, 1.80), o("Under", 2.5, 2.05)]                 # overround ~1.0434
ALT_LADDER = [o("Over", 1.5, 1.25), o("Over", 3.5, 3.10), o("Over", 4.5, 6.00)]


def test_ladder_devigs_overs_with_the_main_line_overround():
    rungs = dict(fo.ladder(MAIN, ALT_LADDER))
    m = 1 / 1.80 + 1 / 2.05
    assert abs(rungs[2.5] - fo.devig(1.80, 2.05)) < 1e-12
    assert abs(rungs[3.5] - (1 / 3.10) / m) < 1e-12
    assert rungs[1.5] > rungs[2.5] > rungs[3.5] > rungs[4.5]
    assert fo.ladder([], ALT_LADDER) == []                            # no two-sided point: no vig estimate


def test_ladder_is_forced_monotone():
    rungs = fo.ladder(MAIN, [o("Over", 3.5, 1.50)])                    # 3.5 priced shorter than 2.5: stale rung
    assert rungs[1][1] <= rungs[0][1]


def test_read_ladder_exact_interpolated_and_refusals():
    rungs = [(1.5, 0.80), (3.5, 0.30)]
    assert fo.read_ladder(rungs, 3.5) == (0.30, False)
    p, interp = fo.read_ladder(rungs, 2.5)
    assert interp and 0.30 < p < 0.80
    assert fo.read_ladder(rungs, 4.5) is None                          # no extrapolation
    assert fo.read_ladder(rungs, 3.0) is None                          # whole number, not quoted: push risk
    assert fo.read_ladder([(0.5, 0.9), (3.5, 0.2)], 1.5) is None       # rungs too far apart


def leg_at(line):
    return scanner.Leg("Alex Ovechkin", "shots on goal", line, "MTL v WSH", team="WSH", start_time=START)


class LadderAPI(FakeAPI):
    async def __call__(self, url, params):
        if url.endswith("/events"):  # free: not recorded, so calls == the paid odds requests
            return 200, EVENTS, {"x-requests-last": "0", "x-requests-remaining": "400"}
        self.calls.append(params["markets"])
        mk = params["markets"].split(",")
        if any(m.startswith("zz") for m in mk):
            return 422, {"message": "Invalid markets: " + ",".join(m for m in mk if m.startswith("zz"))}, \
                {"x-requests-last": "0"}
        books = []
        for b in ("draftkings", "fanduel"):
            markets = [{"key": "player_shots_on_goal", "outcomes": MAIN}] if "player_shots_on_goal" in mk else []
            if "player_shots_on_goal_alternate" in mk:
                markets.append({"key": "player_shots_on_goal_alternate", "outcomes": ALT_LADDER})
            books.append({"key": b, "markets": markets})
        return 200, {"bookmakers": books}, {"x-requests-last": str(len(mk)), "x-requests-remaining": "400"}


def test_main_line_match_never_fetches_alternates(tmp_path):
    api = LadderAPI()
    assert asyncio.run(book(tmp_path, api).price("nhl", [leg_at(2.5)], NOW)) == 1
    assert api.calls == ["player_shots_on_goal"]


def test_off_market_line_fetches_alternate_once_and_prices_it(tmp_path, books_alt_exact):
    api = LadderAPI()
    b = book(tmp_path, api)
    exact_alt, between = leg_at(3.5), leg_at(1.5)
    assert asyncio.run(b.price("nhl", [exact_alt, between], NOW)) == 2
    assert api.calls == ["player_shots_on_goal", "player_shots_on_goal_alternate"]
    m = 1 / 1.80 + 1 / 2.05
    assert abs(exact_alt.quotes[0].p_yes - (1 / 3.10) / m) < 1e-12 and fo.sources(exact_alt) == "Consensus: DK, FD · alt"
    assert exact_alt.pick == ("Less", 1 - exact_alt.quotes[0].p_yes)
    assert b.usage()["used"] == 2
    asyncio.run(b.price("nhl", [leg_at(3.5)], NOW + 60))               # cached: no new calls
    assert len(api.calls) == 2


def test_interpolated_line_between_rungs(tmp_path, monkeypatch, books_ladder_full):
    above_top = scanner.Leg("Alex Ovechkin", "shots on goal", 5.5, "MTL v WSH", team="WSH", start_time=START)
    assert asyncio.run(book(tmp_path, LadderAPI()).price("nhl", [above_top], NOW)) == 0   # no extrapolation
    monkeypatch.setattr(sys.modules[__name__], "ALT_LADDER", [o("Over", 1.5, 1.25), o("Over", 4.5, 6.00)])
    (tmp_path / "gap").mkdir()
    l = leg_at(3.5)                                                     # between 2.5 (main) and 4.5 (alt)
    assert asyncio.run(book(tmp_path / "gap", LadderAPI()).price("nhl", [l], NOW)) == 1
    assert fo.sources(l) == "Consensus: DK, FD · interp"


def test_invalid_market_422_is_remembered_and_retried_free(tmp_path):
    api = LadderAPI()
    b = book(tmp_path, api)
    asyncio.run(b.refresh("nhl", "ev1", ["zz_bad", "player_shots_on_goal"], NOW))
    assert api.calls == ["zz_bad,player_shots_on_goal", "player_shots_on_goal"]
    assert b.cache["invalid"] == ["zz_bad"] and "player_shots_on_goal" in b.cache["odds"]["ev1"]


def test_player_missing_from_main_market_uses_book_average_vig():
    stars = [o("Over", 1.5, 2.10, "Star"), o("Under", 1.5, 1.75, "Star")]          # DK lists stars only
    alt = {"draftkings": [o("Over", 0.5, 1.60, "Depth Guy")]}
    hit = fo.ladder_consensus({"draftkings": stars}, alt, "Depth Guy", 0.5)
    assert hit and abs(hit[0] - (1 / 1.60) / (1 / 2.10 + 1 / 1.75)) < 1e-12 and hit[1] == ["DK"]
    assert fo.ladder_consensus({"draftkings": []}, alt, "Depth Guy", 0.5) is None  # no vig to borrow


# ---------------------------------------------------------------- shared budget
def test_higher_budget_raises_the_shared_cap_for_every_sentinel(tmp_path):
    api = FakeAPI()
    default = book(tmp_path, api)                                   # an NHL sentinel on the default 15
    assert default.cap() == fo.DAILY_CAP
    big = book(tmp_path, api, daily_cap=35)                         # MLB sentinel launched with --odds-budget 35
    big.save()
    default.load()                                                  # next pricing pass re-reads the cache
    assert default.cap() == 35
    small = book(tmp_path, api, daily_cap=5)                        # a lower budget never lowers it
    small.save()
    default.load()
    assert default.cap() == 35


def test_cap_resets_with_the_day(tmp_path):
    b = book(tmp_path, FakeAPI(), daily_cap=35)
    b.cache["usage"].update(date="2000-01-01", used=30, cap=35)
    fifteen = book(tmp_path, FakeAPI())
    fifteen.cache = b.cache
    assert fifteen.cap() == fo.DAILY_CAP and fifteen.usage()["used"] == 0


def test_reserve_is_never_spent_even_with_a_big_budget(tmp_path):
    b = book(tmp_path, FakeAPI(), daily_cap=500)
    b.cache["usage"].update(remaining=fo.RESERVE + 2)
    assert b.affordable(2) and not b.affordable(3)                  # 25 must remain
    b.cache["usage"].pop("remaining")
    assert not b.affordable(1)                                      # balance unknown: no paid call


def test_unknown_balance_is_learned_from_the_free_events_call(tmp_path):
    api = FakeAPI(remaining=300)
    b = book(tmp_path, api)
    b.cache["events"]["nhl"] = {"ts": NOW, "data": EVENTS}          # events cached, balance unknown
    assert asyncio.run(b.price("nhl", [leg()], NOW)) == 1
    assert api.calls[0].endswith("/events") and b.usage()["remaining"] < 300


def test_nhl_shots_on_goal_requests_player_shots_on_goal_from_icehockey_nhl(tmp_path):
    from local.parse_board import parse_json
    rows, _ = parse_json({"props": [{"player": "Alex Ovechkin", "team": "WSH", "league": "NHL", "stat": "Shots On Goal",
                                     "line": 2.5, "opponent": "MTL", "start_time": START, "odds_type": "standard"}]}, "nhl")
    assert rows[0]["stat"] == "shots on goal" and fo.MARKETS["nhl"]["shots on goal"] == "player_shots_on_goal"
    urls = []

    async def api(url, params):
        urls.append((url.split("/v4/sports/")[1], params.get("markets")))
        return await FakeAPI()(url, params)

    l = leg()  # Alex Ovechkin, shots on goal 2.5, WSH
    assert asyncio.run(book(tmp_path, api).price("nhl", [l], NOW)) == 1
    assert urls == [("icehockey_nhl/events", None), ("icehockey_nhl/events/ev1/odds", "player_shots_on_goal")]
    assert l.quotes[0].ref == "ev1:player_shots_on_goal"


# ---------------------------------------------------------------- pre-game window
def test_only_games_inside_the_window_are_priced_from_the_books(tmp_path):
    from datetime import datetime as dt
    start = dt.fromisoformat(START.replace("Z", "+00:00")).timestamp()
    api = FakeAPI()
    assert fo.LOOKAHEAD_H == 3
    assert asyncio.run(book(tmp_path, api).price("nhl", [leg()], start - 3.1 * 3600)) == 0   # 3 h 6 min out: wait
    assert not any("/odds" in c for c in api.calls)                                           # and spend nothing
    (tmp_path / "w").mkdir()
    assert asyncio.run(book(tmp_path / "w", FakeAPI()).price("nhl", [leg()], start - 2.9 * 3600)) == 1
    (tmp_path / "w5").mkdir()
    wide = fo.OddsBook("k", cache=tmp_path / "w5" / "c.json", get=FakeAPI(), window_h=5)     # --window-hours 5
    assert asyncio.run(wide.price("nhl", [leg()], start - 4.5 * 3600)) == 1
    assert asyncio.run(book(tmp_path, FakeAPI()).price("nhl", [leg()], start - 5 * 60)) == 0  # inside the 10-min buffer


def test_sentinel_passes_window_hours_to_the_odds_book(monkeypatch):
    import sys
    from local import sentinel
    captured = {}

    class Stop(Exception):
        pass

    def from_env(**kw):
        captured.update(kw)
        raise Stop
    monkeypatch.setattr(sentinel.fetch_odds.OddsBook, "from_env", staticmethod(from_env))
    monkeypatch.setattr(sys, "argv", ["sentinel", "--sport", "nhl", "--window-hours", "1.5"])
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "")
    with pytest.raises(Stop):
        asyncio.run(sentinel.main())
    assert captured["window_h"] == 1.5
