"""Per-sport slices of the shared daily Odds API cap (pipeline_config odds_budget.shares)."""
import asyncio
import json
from datetime import date

from local import fetch_odds as fo, pipeline_config
from tests.test_fetch_odds import NOW, FakeAPI, book, leg


def spend(b, sport, n):
    """Another sentinel's paid call, saved to the shared cache as a real pricing pass does."""
    b.charge({"x-requests-last": str(n), "x-requests-remaining": "400"}, sport)
    b.save()


def test_default_split_is_nfl_half_and_every_other_sport_half():
    assert pipeline_config.CONFIG["odds_budget"]["shares"] == {"nfl": 0.5, "other": 0.5}
    b = fo.OddsBook("k", cache=fo.Path("/nonexistent/c.json"), daily_cap=80, get=FakeAPI())
    assert b.group("nfl") == "nfl" and b.group("nhl") == b.group("mlb") == b.group("cfb") == "other"
    assert b.group_limit("nfl") == 40 and b.group_limit("nhl") == 40


def test_nfl_cannot_drain_the_credits_later_nhl_games_need(tmp_path):
    api = FakeAPI()
    b = book(tmp_path, api, daily_cap=80)
    asyncio.run(b.events("nhl", NOW))                       # free: learns the balance
    spend(b, "nfl", 39)
    assert b.affordable(1, "nfl") and not b.affordable(2, "nfl")      # NFL stops at 40...
    spend(b, "nfl", 1)
    assert not b.affordable(1, "nfl")
    assert b.affordable(40, "nhl") and not b.affordable(41, "nhl")    # ...NHL keeps its 40
    assert asyncio.run(b.price("nhl", [leg()], NOW)) == 1             # and actually buys its shots on goal
    u = b.usage()
    assert u["used"] == 41 and u["by_group"] == {"nfl": 40, "other": 1}


def test_a_full_share_is_skipped_with_the_reason_in_the_log_note(tmp_path):
    api = FakeAPI()
    b = book(tmp_path, api, daily_cap=80)
    asyncio.run(b.events("nhl", NOW))
    spend(b, "mlb", 40)                                     # MLB and NHL spend from the same "other" slice
    assert asyncio.run(b.price("nhl", [leg()], NOW)) == 0 and not any("/odds" in c for c in api.calls)
    assert any("other share 40/40" in n and "budget 40/80" in n for n in b.notes)
    assert b.affordable(40, "nfl")                          # NFL's slice is untouched


def test_shares_follow_odds_budget_and_the_split_is_shared_by_every_sentinel(tmp_path):
    api = FakeAPI()
    nhl = book(tmp_path, api, daily_cap=15)
    asyncio.run(nhl.events("nhl", NOW))
    assert nhl.group_limit("nhl") == 7                      # default 15 -> 7 / 7
    nhl.save()
    nfl = book(tmp_path, api, daily_cap=80)                 # a sentinel launched with --odds-budget 80
    nfl.publish_cap()
    nhl.load()
    assert nhl.cap() == 80 and nhl.group_limit("nhl") == 40 and nhl.group_limit("nfl") == 40
    spend(nfl, "nfl", 40)
    nfl.save()
    nhl.load()                                              # the other process sees NFL's spend
    assert nhl.usage()["by_group"]["nfl"] == 40 and not nhl.affordable(1, "nfl") and nhl.affordable(1, "nhl")


def test_split_resets_daily_and_old_usage_without_groups_still_loads(tmp_path):
    (tmp_path / "c.json").write_text(json.dumps({"usage": {"date": "2026-01-01", "used": 15, "cap": 15, "remaining": 300,
                                                           "by_group": {"nfl": 15}}}))
    b = book(tmp_path, FakeAPI(), daily_cap=80)
    assert b.usage()["used"] == 0 and b.usage()["by_group"] == {}       # a new day
    (tmp_path / "c.json").write_text(json.dumps({"usage": {"date": date.today().isoformat(), "used": 30, "cap": 80,
                                                           "remaining": 300}}))
    b = book(tmp_path, FakeAPI(), daily_cap=80)              # written before the split: no by_group yet
    assert b.usage()["by_group"] == {} and b.affordable(40, "nfl") and not b.affordable(51, "nhl")  # cap still binds


def test_no_shares_means_no_split(tmp_path):
    b = book(tmp_path, FakeAPI(), daily_cap=80, shares={})
    b.cache["usage"].update(date=date.today().isoformat(), used=0, cap=80, remaining=400)
    assert b.group("nfl") is None and b.group_limit("nfl") is None and b.affordable(80, "nfl")


def test_budget_line_for_the_sentinel_startup_log():
    u = {"used": 12, "cap": 80, "by_group": {"nfl": 10, "other": 2}}
    assert fo.budget_line(u, {"nfl": 0.5, "other": 0.5}) == "Odds API today: 12/80 · nfl 10/40 · other 2/40"
