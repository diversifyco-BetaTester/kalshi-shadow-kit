"""PropLine logged-only shadow (local/propline_shadow.py, 2026-11-06). Never a live price: these tests prove the
mapping, the missing markers, the request budget, error handling, key hygiene, and that live behaviour (slips,
stakes, alerts, eligible legs, Odds API calls) is identical with the shadow on or off. No network: every PropLine
response is injected."""
import asyncio
import inspect
import json
import sqlite3
from types import SimpleNamespace

import pytest

from local import backtest, ev_pool, fetch_odds, optimizer, pipeline_config, propline_shadow as ps, sentinel, snapshots
from local import correlation, integrity, signals
from local import prize_picks_ev_scraper as scanner
from tests.test_backtest import START, FakeGrader
from tests.test_signals import START_TS, cycle_args, kalshi_board

KEY = "0123456789abcdef0123456789abcdef"  # a fake key: must never appear in output, rows or repr
TEAMS = [("BUF", "Buffalo Sabres", "CBJ", "Columbus Blue Jackets"), ("BOS", "Boston Bruins", "TOR", "Toronto Maple Leafs"),
         ("NYR", "New York Rangers", "NJ", "New Jersey Devils"), ("EDM", "Edmonton Oilers", "CGY", "Calgary Flames"),
         ("VAN", "Vancouver Canucks", "SEA", "Seattle Kraken"), ("DAL", "Dallas Stars", "STL", "St Louis Blues"),
         ("COL", "Colorado Avalanche", "MIN", "Minnesota Wild")]


def dec_to_american(d: float) -> float:
    return (d - 1) * 100 if d >= 2 else -100 / (d - 1)


def ou(player, point, over, under, **kw):
    return [{"name": "Over", "description": player, "price": over, "point": point, "last_change_at": "2098-12-31T14:00:00Z",
             "player_id": "nhl:1", "dfs_odds_type": None, "payout_multiplier": None, **kw},
            {"name": "Under", "description": player, "price": under, "point": point, "last_change_at": "2098-12-31T14:00:00Z",
             "player_id": "nhl:1", "dfs_odds_type": None, "payout_multiplier": None, **kw}]


def book(key, market, outcomes, line_type="main"):
    return {"key": key, "title": key, "last_update": "2098-12-31T14:30:00Z",
            "markets": [{"key": market, "line_type": line_type, "last_update": "2098-12-31T14:30:00Z", "outcomes": outcomes}]}


def event(i, books):
    h, hn, a, an = TEAMS[i]
    return {"id": str(1000 + i), "home_team": hn, "away_team": an, "commence_time": START, "bookmakers": books}


def leg(player="Player A", stat="points", line=0.5, team="BUF", i=0):
    h, _, a, _ = TEAMS[i]
    return scanner.Leg(player, stat, line, f"{a} v {h}", team=team, start_time=START)


def row_for(l, books, market="player_points", now=START_TS - 7200):
    ev = event(0, books)
    return ps.leg_row(l, "nhl", market, ev, ev, now - 1, now, "sweep")


# --------------------------------------------------------------------------- mapping
def test_markets_map_live_keys_with_propline_differences():
    m = ps.markets("nhl")
    assert m["saves"] == "goalie_saves"                                   # live: player_total_saves
    assert "faceoffs won" not in m and "time on ice" not in m             # no PropLine market
    assert m["shots on goal"] == fetch_odds.MARKETS["nhl"]["shots on goal"]
    n = ps.markets("nba")
    assert all(n[s] == k for s, k in fetch_odds.MARKETS["nba"].items())   # NBA keys identical
    assert n["fg made"] == "player_field_goals_made"                      # extras live never requests
    assert ps.SPORT_KEYS == {"nba": "basketball_nba", "nhl": "hockey_nhl"}


def test_live_method_matches_fetch_odds_consensus_exactly_with_pinnacle_at_1_5():
    dec = {"pinnacle": (1.80, 2.05), "draftkings": (1.87, 1.95), "fanduel": (1.91, 1.91), "betmgm": (1.83, 2.00)}
    cached = {b: [{"name": "Over", "description": "Player A", "price": o, "point": 0.5},
                  {"name": "Under", "description": "Player A", "price": u, "point": 0.5}] for b, (o, u) in dec.items()}
    p_live, names = fetch_odds.consensus(cached, "Player A", 0.5)
    books = [book(b, "player_points", ou("Player A", 0.5, dec_to_american(o), dec_to_american(u))) for b, (o, u) in dec.items()]
    r = row_for(leg(), books)
    assert r["fair"]["p_live"]["value"] == pytest.approx(p_live, abs=1e-12)
    assert r["fair"]["p_live"]["books"] == names
    assert fetch_odds.WEIGHTS == {"pinnacle": 1.5}
    shin = sum(w * ps.dv.two_way(dec_to_american(o), dec_to_american(u), "american", "shin").fair_over
               for w, (o, u) in zip((1.5, 1, 1, 1), dec.values())) / 4.5
    assert r["fair"]["p_shin"]["value"] == pytest.approx(shin)


def test_sharp_consensus_needs_an_anchor_and_three_books_and_prophetx_is_not_an_anchor():
    q = lambda k: book(k, "player_points", ou("Player A", 0.5, -120, 100))
    no_anchor = row_for(leg(), [q("prophetx"), q("draftkings"), q("fanduel")])
    assert no_anchor["fair"]["p_sharp"] == {"value": None, "missing": "no_sharp_anchor", "calc_version": ps.CALC_VERSION}
    two = row_for(leg(), [q("novig"), q("draftkings")])
    assert two["fair"]["p_sharp"]["missing"] == "fewer_than_3_books"
    ok = row_for(leg(), [book("novig", "player_points", ou("Player A", 0.5, -150, 130)), q("prophetx"), q("betmgm"),
                         q("draftkings")])
    s = ok["fair"]["p_sharp"]
    assert s["anchors"] == ["novig"] and set(s["books"]) == {"novig", "prophetx", "draftkings"}   # MGM weight 0
    fair = lambda o, u: ps.dv.two_way(o, u, "american", "proportional").fair_over
    assert s["value"] == pytest.approx((7 * fair(-150, 130) + 1.4 * fair(-120, 100) + 1.4 * fair(-120, 100)) / 9.8)


def test_only_the_main_line_prices_and_alternates_milestones_and_yes_only_are_recorded_not_used():
    alt = book("pinnacle", "player_points", ou("Player A", 0.5, -200, 160), line_type="alternate")
    milestone = book("draftkings", "player_points", [{"name": "1+ Points", "description": "Player A", "price": -300,
                                                      "point": None}], line_type="milestone")
    r = row_for(leg(), [alt, milestone])
    assert r["fair"]["p_live"]["value"] is None and r["books"]["pinnacle"]["alt_at_line"] is True
    yes = book("fanduel", "player_goals", [{"name": "Player A", "description": "Player A", "price": 230, "point": None}])
    r = row_for(leg(stat="goals"), [yes], market="player_goals")
    assert r["books"]["fanduel"]["missing"] == "yes_only_no_two_way"


def test_every_unpriced_leg_carries_a_specific_reason():
    pin = book("pinnacle", "player_points", ou("Player A", 1.5, -120, 100))
    assert row_for(leg(), [pin])["unpriced_reason"] == "line_not_offered"
    assert row_for(leg(player="Nobody"), [pin])["unpriced_reason"] == "player_not_listed"
    assert row_for(leg(), [book("pinnacle", "player_assists", [])])["unpriced_reason"] == "market_not_returned"
    novig_only = row_for(leg(), [book("novig", "player_points", ou("Player A", 0.5, -120, 100))])
    assert novig_only["unpriced_reason"] == "no_live_book_two_sided" and novig_only["presence"]["novig"]["value"]
    none = ps.leg_row(leg(), "nhl", "player_points", None, None, None, START_TS - 7200, "none", reason="no_propline_event")
    assert all(none["fair"][k]["missing"] == "no_propline_event" for k in ("p_live", "p_shin", "p_sharp"))
    assert none["kalshi"]["missing"] == "no_propline_event" and none["ages"]["data_age_s"]["value"] is None


def test_dfs_books_and_flagged_outcomes_are_dropped_before_parsing_and_only_counted():
    body = event(0, [book("prizepicks", "player_points", ou("Player A", 0.5, -119, -119, dfs_odds_type="standard")),
                     book("pinnacle", "player_points", ou("Player A", 0.5, -120, 100)
                          + ou("Player B", 0.5, -110, -110, payout_multiplier=1.0))])
    clean, seen = ps.strip_dfs(json.loads(json.dumps(body)))
    assert [b["key"] for b in clean["bookmakers"]] == ["pinnacle"]
    assert seen == {"prizepicks": 2, "pinnacle:dfs_flagged": 2}
    assert all(o["description"] == "Player A" for o in clean["bookmakers"][0]["markets"][0]["outcomes"])
    r = ps.leg_row(leg(), "nhl", "player_points", clean, clean, 1.0, START_TS - 7200, "sweep", dfs=seen)
    assert "prizepicks" not in json.dumps(r["books"]) and r["dfs"] == seen


# --------------------------------------------------------------------------- budget
def test_budget_caps_at_500_stops_at_475_splits_by_sport_and_keeps_a_close_share(tmp_path):
    b = ps.Budget(tmp_path / "u.json")
    now = START_TS - 7200
    lim = ps.Budget.limits({}, "nhl")
    assert lim == {"stop_at": 475, "share": 237, "sweep": 201}
    assert ps.Budget.limits({"header_limit": 300}, "nhl")["stop_at"] == 275   # a smaller real limit wins
    for _ in range(201):
        assert b.take("nhl", "sweep", now)[0]
    assert b.take("nhl", "sweep", now) == (False, "nhl sweep share 201/201")
    for _ in range(36):
        assert b.take("nhl", "close", now)[0]                                # the close share is kept
    assert not b.take("nhl", "close", now)[0]
    assert b.take("nba", "events", now)[0]                                  # the other sport's share is untouched
    other = ps.Budget(tmp_path / "u.json")                                  # a second sentinel sees the same count
    assert other.snapshot(now)["used"] == 238


def test_budget_trusts_the_higher_used_count_and_resets_at_midnight_utc(tmp_path):
    b = ps.Budget(tmp_path / "u.json")
    now = 1794009600.0 - 3600                                               # 2026-11-06 23:00 UTC = 15:00 PT
    b.take("nhl", "sweep", now)
    s = b.reconcile({"X-Daily-Used": "470", "X-Daily-Limit": "1000", "X-Daily-Reset": "1794009600"}, now)
    assert s["used"] == 470 and s["header_limit"] == 1000
    assert b.take("nhl", "close", now)[0] and b.take("nhl", "close", now)[0]  # 471, 472 ... up to 475
    for _ in range(3):
        b.take("nhl", "close", now)
    assert b.take("nhl", "close", now) == (False, "daily stop 475/475")
    later = 1794009600.0 + 60                                               # 00:01 UTC = 16:01 PT: PropLine's new day
    assert b.snapshot(later)["used"] == 0 and b.take("nhl", "sweep", later)[0]


# --------------------------------------------------------------------------- the shadow end to end (no network)
class FakePropLine:
    """Answers /events and per-event /odds from fixtures; records every request (path, params, headers)."""

    def __init__(self, events, status=200, code=None):
        self.events, self.status, self.code, self.calls = events, status, code, []

    async def __call__(self, url, params, headers):
        self.calls.append((url, dict(params), dict(headers)))
        hdr = {"X-Daily-Used": str(len(self.calls)), "X-Daily-Limit": "1000", "X-Daily-Reset": "4071000000"}
        if self.status != 200:
            return self.status, {"detail": {"error": self.code}}, hdr
        if url.endswith("/events"):
            return 200, [{k: e[k] for k in ("id", "home_team", "away_team", "commence_time")} for e in self.events], hdr
        eid = url.split("/events/")[1].split("/")[0]
        return 200, json.loads(json.dumps(next(e for e in self.events if e["id"] == eid))), hdr


def board_csv(tmp_path, players, stat="points", line=0.5):
    p = tmp_path / "lines_nhl.csv"
    p.write_text("player,stat,line,game,team,odds_type,start_time,position\n" + "".join(
        f"{pl},{stat},{line},{TEAMS[i][2]} v {TEAMS[i][0]},{TEAMS[i][0]},standard,{START},F\n" for i, pl in enumerate(players)))
    return p


def priced_events(players, p=-130):
    return [event(i, [book(b, "player_points", ou(pl, 0.5, p, 110)) for b in ("pinnacle", "draftkings", "novig", "prophetx")])
            for i, pl in enumerate(players)]


def shadow(tmp_path, fake, now):
    return ps.Shadow(KEY, "nhl", get=fake, budget=ps.Budget(tmp_path / "u.json"), clock=lambda: now)


def test_a_tick_prices_board_legs_once_per_event_and_logs_requests_and_reasons(tmp_path):
    players = [f"Player {c}" for c in "ABC"]
    csv = board_csv(tmp_path, players)
    with csv.open("a") as f:
        f.write(f"Whole Guy,points,1,CBJ v BUF,BUF,standard,{START},F\n"
                f"Faceoff Guy,faceoffs won,10.5,CBJ v BUF,BUF,standard,{START},F\n")
    fake = FakePropLine(priced_events(players))
    snaps = snapshots.Snapshots("nhl", tmp_path / "s.sqlite3")
    now = START_TS - 2 * 3600
    sw = asyncio.run(shadow(tmp_path, fake, now).tick(csv, snaps, now=now, board_ts=1.0))
    assert sw["requests"] == 4 and sw["events_requested"] == 3 and sw["legs_priced"] == 3     # 1 events list + 3 events
    assert sw["unpriced"] == {"whole_line_push_unpriced": 1, "no_propline_market": 1}
    odds_calls = [c for c in fake.calls if c[0].endswith("/odds")]
    assert all(c[1]["bookmakers"] == "pinnacle,draftkings,fanduel,betmgm,novig,prophetx,kalshi" for c in odds_calls)
    assert all("apiKey" not in c[1] and c[2]["X-API-Key"] == KEY for c in fake.calls)       # header only
    con = sqlite3.connect(tmp_path / "s.sqlite3")
    rows = con.execute("SELECT player, p_live, p_sharp, read_kind, unpriced_reason, calc_version FROM propline_prices").fetchall()
    assert len(rows) == 5 and {r[5] for r in rows} == {ps.CALC_VERSION}
    assert all(r[1] is not None and r[2] is not None and r[3] == "sweep" for r in rows if r[0].startswith("Player"))
    assert con.execute("SELECT requests, legs_in_window FROM propline_sweeps").fetchall() == [(4, 5)]


def test_events_are_reread_every_15_minutes_and_once_more_near_the_start(tmp_path):
    csv = board_csv(tmp_path, ["Player A"])
    fake = FakePropLine(priced_events(["Player A"]))
    snaps = snapshots.Snapshots("nhl", tmp_path / "s.sqlite3")
    t0 = START_TS - 60 * 60
    s = shadow(tmp_path, fake, t0)
    n = lambda: sum(1 for c in fake.calls if c[0].endswith("/odds"))
    asyncio.run(s.tick(csv, snaps, now=t0))
    asyncio.run(s.tick(csv, snaps, now=t0 + 10 * 60))
    assert n() == 1                                                         # not due inside 15 minutes
    asyncio.run(s.tick(csv, snaps, now=t0 + 15 * 60))
    assert n() == 2
    asyncio.run(s.tick(csv, snaps, now=START_TS - 18 * 60))                 # 12 min after the last read, 18 to go: close
    asyncio.run(s.tick(csv, snaps, now=START_TS - 15 * 60))                 # 3 min later: under the 5-min gap
    assert n() == 3
    kinds = [r[0] for r in sqlite3.connect(tmp_path / "s.sqlite3").execute("SELECT read_kind FROM propline_prices ORDER BY id")]
    assert kinds == ["sweep", "sweep", "close"]


@pytest.mark.parametrize("status,code,disabled,stopped", [
    (401, "invalid_api_key", "auth:invalid_api_key", None),
    (403, "upgrade_required", None, "upgrade_required:events"),
    (429, "daily_limit_exceeded", None, "daily_limit:daily_limit_exceeded"),
    (429, "burst_limit_exceeded", None, None),
    (503, None, None, None)])
def test_errors_never_retry_and_stop_or_disable_as_documented(tmp_path, capsys, status, code, disabled, stopped):
    csv = board_csv(tmp_path, ["Player A"])
    fake = FakePropLine(priced_events(["Player A"]), status=status, code=code)
    snaps = snapshots.Snapshots("nhl", tmp_path / "s.sqlite3")
    now = START_TS - 7200
    s = shadow(tmp_path, fake, now)
    asyncio.run(s.tick(csv, snaps, now=now))
    assert len(fake.calls) == 1                                             # the events call; nothing retried
    assert s.disabled == disabled
    assert s.budget.snapshot(now)["stopped"] == stopped
    out = capsys.readouterr().out
    assert KEY not in out and KEY not in repr(s)
    dump = "\n".join(str(r) for r in sqlite3.connect(tmp_path / "s.sqlite3").execute("SELECT * FROM propline_sweeps"))
    assert KEY not in dump


def test_a_network_failure_is_one_skipped_read_not_a_crash(tmp_path):
    csv = board_csv(tmp_path, ["Player A"])

    async def boom(url, params, headers):
        raise asyncio.TimeoutError()
    snaps = snapshots.Snapshots("nhl", tmp_path / "s.sqlite3")
    now = START_TS - 7200
    sw = asyncio.run(ps.Shadow(KEY, "nhl", get=boom, budget=ps.Budget(tmp_path / "u.json")).tick(csv, snaps, now=now))
    assert sw["errors"] == {"network:TimeoutError": 1} and sw["legs_priced"] == 0


def test_the_shadow_never_writes_the_odds_api_cache(tmp_path):
    cache = tmp_path / ".odds_cache.json"
    cache.write_text(json.dumps({"usage": {"date": "2026-11-06", "used": 7, "by_group": {"other": 7}, "cap": 80}}))
    before = (cache.read_bytes(), cache.stat().st_mtime_ns)
    assert ps.odds_api_usage(cache) == {"date": "2026-11-06", "used": 7, "by_group": {"other": 7}, "cap": 80}
    assert (cache.read_bytes(), cache.stat().st_mtime_ns) == before
    assert ps.odds_api_usage(tmp_path / "absent.json")["missing"].startswith("unreadable")
    import ast
    names = {n.attr if isinstance(n, ast.Attribute) else n.id for n in ast.walk(ast.parse(inspect.getsource(ps)))
             if isinstance(n, (ast.Attribute, ast.Name))}
    assert not names & {"OddsBook", "price", "reprice", "ensure", "refresh", "charge", "save", "publish_cap"}
    assert "api.the-odds-api.com" not in inspect.getsource(ps)


# --------------------------------------------------------------------------- live behaviour unchanged
def test_no_builder_or_live_function_reads_the_propline_shadow():
    for mod in (optimizer, ev_pool, correlation, integrity, scanner, fetch_odds, signals):
        assert "propline" not in inspect.getsource(mod), mod.__name__
    for fn in (sentinel.cycle, sentinel.flex_entries, sentinel.mixed_entries, sentinel.reevaluate, sentinel.stake_units,
               sentinel.would_send, sentinel.jit_refresh, sentinel._jit_refresh, sentinel.watch_legs, sentinel.watch_only):
        assert "propline" not in inspect.getsource(fn), fn.__name__
    assert pipeline_config.DEFAULTS["signals"]["propline_shadow"] is False   # off until a launch is approved


class SpyOddsGet:
    def __init__(self):
        self.calls = []

    async def __call__(self, url, params):
        self.calls.append(url)
        return 200, [], {"x-requests-remaining": "400", "x-requests-last": "0"}


def loop_state(tmp_path, monkeypatch, sub, on):
    d = tmp_path / sub
    d.mkdir()
    players = [f"Player {c}" for c in "ABCDEFG"]
    probs = [0.63 - i / 100 for i in range(7)]
    _, sends = kalshi_board(d, monkeypatch, probs)
    csv = board_csv(d, players)                                             # real NHL teams, one game per player
    spy = SpyOddsGet()
    odds = fetch_odds.OddsBook("x", cache=d / ".odds_cache.json", get=spy)
    a = cycle_args(d, csv)
    a.odds = odds
    fake = FakePropLine(priced_events(players, p=-300))                     # PropLine much higher: must change nothing
    a.propline = ps.Shadow(KEY, "nhl", get=fake, budget=ps.Budget(d / "u.json"), clock=lambda: START_TS - 7200) if on else None

    async def loop():
        for _ in range(2):
            await sentinel.cycle(a, [], chat_id="1")
            sentinel.propline_tick(a)                                       # exactly where main() calls it
            await asyncio.gather(*list(a.__dict__.get("background_tasks") or []))
    asyncio.run(loop())
    con = sqlite3.connect(d / "snapshots_nhl.sqlite3")
    live = [r[0] for r in con.execute("SELECT live_json FROM cycles ORDER BY id")]
    elig = [r[0] for r in con.execute("SELECT eligible_json FROM cycles ORDER BY id")]
    legs = con.execute("SELECT builder, player, side, p_entry, units, ev, size FROM slip_legs ORDER BY id").fetchall()
    prices = con.execute("SELECT kind, player, p_fair, source FROM prices ORDER BY id").fetchall()
    ledger = (d / "l.csv").read_text().splitlines() if (d / "l.csv").exists() else []
    ledger = [",".join(x.split(",")[1:]) for x in ledger]                    # drop the wall-clock timestamp column
    pl = con.execute("SELECT COUNT(*) FROM propline_prices").fetchone()[0]
    cache = (d / ".odds_cache.json").read_text() if (d / ".odds_cache.json").exists() else None
    return (live, elig, legs, prices, sends, len(ledger), spy.calls, cache and json.loads(cache)["usage"]["used"]), pl, fake


def test_live_slips_stakes_alerts_prices_and_odds_api_calls_are_identical_with_the_shadow_on_or_off(tmp_path, monkeypatch):
    on, pl_on, fake = loop_state(tmp_path, monkeypatch, "on", True)
    off, pl_off, _ = loop_state(tmp_path, monkeypatch, "off", False)
    assert on == off
    assert on[2] and on[4]                                                  # slips were built and alerted
    assert pl_on == 7 and pl_off == 0 and len(fake.calls) == 8              # the shadow did run (1 list + 7 events)


# --------------------------------------------------------------------------- harness
def pl_db(tmp_path, rows, sweeps=()):
    db = tmp_path / "snapshots_nhl.sqlite3"
    s = snapshots.Snapshots("nhl", db)
    for sw in sweeps:
        s.propline_sweep(sw)
    for r in rows:
        s.propline_prices(1, 1.0, [r])
    s.db.close()
    return db


def mk_row(player, ts, p, fetched=None, event_id="1000", team="BUF", start=START_TS, kind="sweep"):
    l = scanner.Leg(player, "points", 0.5, "CBJ v BUF", team=team, start_time=START)
    q = [book("pinnacle", "player_points", ou(player, 0.5, p, -p if p < 0 else -100))]
    ev = {**event(0, q), "id": event_id}
    r = ps.leg_row(l, "nhl", "player_points", ev, ev, ts if fetched is None else fetched, ts, kind)
    r["start_ts"] = start
    return r


def test_harness_reports_both_clv_entry_points_apart_with_close_reasons_and_the_warning(tmp_path):
    rows = [mk_row("Ok Guy", START_TS - 7200, -120), mk_row("Ok Guy", START_TS - 300, -140),
            mk_row("Stale Guy", START_TS - 7200, -120),
            mk_row("Same Guy", START_TS - 600, -120, fetched=111.0), mk_row("Same Guy", START_TS - 300, -120, fetched=111.0),
            mk_row("Moved Guy", START_TS - 7200, -120), mk_row("Moved Guy", START_TS - 300, -130, event_id="2000"),
            mk_row("Open Guy", START_TS + 3600, -120, start=START_TS + 20 * 3600)]
    db = pl_db(tmp_path, rows, [{"ts": 1.0, "sport": "nhl", "utc_day": "2098-12-31", "requests": 9, "x_daily_used": 9,
                                 "x_daily_limit": 1000, "x_daily_reset": 0, "budget_used": 9, "events_requested": 8,
                                 "legs_in_window": 5, "legs_priced": 5, "stopped": None, "calc_version": ps.CALC_VERSION,
                                 "odds_api_usage": {"date": "2098-12-31", "used": 12, "by_group": {"other": 12}}}])
    grader = FakeGrader({"Ok Guy": "hit", "Stale Guy": "miss", "Same Guy": "hit", "Moved Guy": "miss"})
    rep = backtest.run([db], outcomes=backtest.Outcomes(tmp_path / "o.sqlite3", grader=grader, fetch=True),
                       now=START_TS + 10 * 3600)
    sec = rep[rep.index("PROPLINE SHADOW"):]
    assert "eligible 4, usable 1, missing 0, stale 1, same fetch as entry 1, market mismatch 1" in sec
    assert "[4a] CLV, entry (a)" in sec and "[4b] CLV, entry (b)" in sec and "Never mixed" in sec
    assert sec.count(snapshots.CLOSING_WARNING) == 3                        # completeness, (b), (a)
    assert "NHL  PropLine live method  n=1 " in sec
    assert "PropLine 9 requests / 5 priced legs" in sec
    assert "NHL  PropLine live method" in sec and "Brier" in sec
    assert "pinnacle 100%" in sec and "novig   0%" in sec


def test_old_snapshot_files_without_propline_tables_still_load_and_say_so(tmp_path):
    db = tmp_path / "snapshots_nhl.sqlite3"
    con = sqlite3.connect(db)
    con.executescript("CREATE TABLE cycles (id INTEGER PRIMARY KEY, ts REAL, board_ts REAL, legs INTEGER, priced INTEGER, "
                      "good INTEGER, whole_dropped INTEGER, live_json TEXT, paper_json TEXT);"
                      "CREATE TABLE prices (id INTEGER PRIMARY KEY, cycle_id INTEGER, ts REAL, kind TEXT, player TEXT, "
                      "stat TEXT, line REAL, side TEXT, team TEXT, game TEXT, start_ts REAL, p_fair REAL, source TEXT, "
                      "method TEXT, pricing_json TEXT, steam_json TEXT);")
    con.close()
    h = backtest.History(db, "nhl")
    assert h.propline == [] and h.propline_sweeps == []
    rep = backtest.run([db], outcomes=backtest.Outcomes(tmp_path / "o.sqlite3", grader=FakeGrader({}), fetch=False),
                       now=START_TS)
    assert "no PropLine shadow rows logged yet" in rep
