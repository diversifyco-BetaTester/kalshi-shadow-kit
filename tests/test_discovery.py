import json
from datetime import date

import pytest

from local import fetch_odds as fo
from local import snapshots
from ops import discovery
from tests.test_fetch_odds import EVENTS, NOW

START = "2026-10-14T19:40:00-04:00"


def prop(player, stat, line=2.5, team="WSH", opp="MTL", odds_type="standard"):
    return {"player": player, "team": team, "position": "F", "league": "NHL", "stat": stat, "line": line, "opponent": opp,
            "start_time": START, "odds_type": odds_type, "projection_id": f"{player}{stat}", "status": "pre_game",
            "projection_type": "Single Stat", "is_promo": False, "adjusted_odds": False, "is_live": False,
            "in_game": False, "updated_at": "2026-10-14T11:00:00-04:00", "allowed_wager_types": None,
            "discount_name": None, "event_type": "player"}


def snapshot_db(tmp_path):
    db = snapshots.Snapshots("nhl", tmp_path / "snapshots_nhl.sqlite3")
    db.board(NOW - 600, "x", [prop("A", "Shots On Goal"), prop("B", "Shots On Goal", team="BOS", opp="NYR"),
                              prop("C", "Time On Ice", line=17.5), prop("D", "Plus/Minus", line=0.5),
                              prop("E", "Points", line=0.5)])
    for i in range(4):
        db.cycle(NOW + 60 * i, NOW - 600, {"legs": 10}, [], None,
                 coverage={"shots on goal": [6, 0], "time on ice": [2, 0], "plus/minus": [1, 0], "points": [1, 1]})
    from tests.test_steam_snapshots import leg
    l = leg(0.55)
    l.stat = "points"
    db.prices(1, NOW, [l])                                   # Kalshi-priced in the logs
    return tmp_path / "snapshots_nhl.sqlite3"


class CatalogAPI:
    def __init__(self, keys):
        self.keys, self.calls = keys, []

    async def __call__(self, url, params):
        self.calls.append(url)
        if url.endswith("/events"):
            return 200, EVENTS, {"x-requests-last": "0", "x-requests-remaining": "170"}
        assert url.endswith("/events/ev1/markets") and set(params["bookmakers"].split(",")) == set(fo.BOOKS)
        return 200, {"id": "ev1", "bookmakers": [{"key": "draftkings", "markets": [{"key": k} for k in self.keys]}]}, \
            {"x-requests-last": "1", "x-requests-remaining": "169"}


def book(tmp_path, api, cap=80):
    return fo.OddsBook("k", cache=tmp_path / "c.json", get=api, daily_cap=cap)


def run(tmp_path, api, monkeypatch, refresh=False, cap=80):
    monkeypatch.setattr(discovery, "CANDIDATES", {"nhl": {"plus/minus": "player_plus_minus"}})
    monkeypatch.setattr(discovery.time, "time", lambda: NOW)
    b = book(tmp_path, api, cap)
    text = discovery.run([snapshot_db(tmp_path)], b, refresh, catalog=tmp_path / "cat.json", out=tmp_path / "d.csv",
                         reset=date(2026, 11, 1), today=date(2026, 10, 19))
    return text, b


def test_buckets_volume_and_cost_from_logs_and_one_cached_catalog_call(tmp_path, monkeypatch):
    api = CatalogAPI(["player_shots_on_goal", "player_plus_minus"])
    text, b = run(tmp_path, api, monkeypatch)
    rows = {r["stat"]: r for r in __import__("csv").DictReader(open(tmp_path / "d.csv"))}
    assert rows["shots on goal"]["bucket"] == "MAPPED" and rows["shots on goal"]["in_catalog"] == "yes"
    assert rows["time on ice"]["bucket"] == "MAPPED" and "didn't post it" in rows["time on ice"]["note"]
    assert rows["points"]["bucket"] == "MAPPED" and "Kalshi" in rows["points"]["note"]
    pm = rows["plus/minus"]
    assert pm["bucket"] == "SUPPORTED-UNMAPPED" and pm["odds_key"] == "player_plus_minus"
    assert float(pm["legs_per_cycle"]) == 1.0 and float(pm["board_share"]) == 0.1 and float(pm["games_per_day"]) == 1
    assert float(pm["est_added_credits_per_day"]) == discovery.FETCHES_PER_GAME == 3
    assert float(pm["est_added_credits_per_day_with_alt"]) == 6
    assert float(rows["shots on goal"]["legs_per_cycle"]) == 6.0 and float(rows["shots on goal"]["games_per_day"]) == 2
    assert [r["stat"] for r in __import__("csv").DictReader(open(tmp_path / "d.csv"))][0] == "shots on goal"  # by volume
    assert sum("/markets" in c for c in api.calls) == 1 and b.usage()["used"] == 1 and b.usage()["by_group"] == {"other": 1}
    assert "Sustainable spend to the reset: 11.1 credits/day" in text            # (169 - 25) / 13 days
    # cached: a second run never re-checks
    text2, _ = run(tmp_path, api, monkeypatch)
    assert sum("/markets" in c for c in api.calls) == 1
    run(tmp_path, api, monkeypatch, refresh=True)
    assert sum("/markets" in c for c in api.calls) == 2


def test_a_stat_with_no_kalshi_market_and_no_posted_key_is_unsupported(tmp_path, monkeypatch):
    text, _ = run(tmp_path, CatalogAPI(["player_shots_on_goal"]), monkeypatch)
    rows = {r["stat"]: r for r in __import__("csv").DictReader(open(tmp_path / "d.csv"))}
    assert rows["plus/minus"]["bucket"] == "UNSUPPORTED" and rows["plus/minus"]["est_added_credits_per_day"] == ""
    assert all(r["bucket"] in discovery.BUCKETS for r in rows.values())


def test_budget_refusal_spends_nothing_and_leaves_buckets_provisional(tmp_path, monkeypatch):
    api = CatalogAPI(["player_plus_minus"])
    (tmp_path / "c.json").write_text(json.dumps({"usage": {"date": date.fromtimestamp(NOW).isoformat(), "used": 40, "cap": 80,
                                                           "remaining": 170, "by_group": {"other": 40}}}))
    text, b = run(tmp_path, api, monkeypatch)
    assert not any("/markets" in c for c in api.calls) and "budget refused" in text
    rows = {r["stat"]: r for r in __import__("csv").DictReader(open(tmp_path / "d.csv"))}
    assert rows["plus/minus"]["bucket"] == "UNSUPPORTED" and "provisional" in rows["plus/minus"]["note"]


def test_an_alias_of_a_mapped_market_costs_nothing_extra(tmp_path, monkeypatch):
    v = discovery.StatVolume(legs=10, priced=0)
    v.games_by_day["2026-10-18"] = {"A@B", "C@D"}
    monkeypatch.setitem(discovery.CANDIDATES, "nfl", {"pass completions": "player_pass_completions"})
    rows = discovery.build_rows("nfl", 10, 100, {"pass completions": v}, {"player_pass_completions"})
    assert rows[0].bucket == "SUPPORTED-UNMAPPED" and rows[0].added_credits_day == 0.0 and "alias" in rows[0].note
