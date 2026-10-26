"""No 4-pick slips (live builder or paper optimizer); NHL shots on goal is priced from player_shots_on_goal."""
import asyncio

from local import fetch_odds, optimizer as op, sentinel
from tests import test_fetch_odds as tfo
from tests.test_optimizer import pool


def test_no_four_pick_slips_anywhere_by_default():
    assert sentinel.FLEX_SIZES == (5, 6)
    legs = pool([0.62, 0.61, 0.60, 0.59, 0.58, 0.57])
    assert all(len(e[2]) in (5, 6) for e in sentinel.flex_entries(legs))        # live builder
    r = op.build(legs)                                                            # paper optimizer
    assert set(r.by_size) == {5, 6} and r.chosen.size in (5, 6)
    assert op.build(pool([0.62, 0.61, 0.60, 0.59])).chosen is None              # four strong legs: no slip at all


def test_five_picks_keep_the_filler_guardrail_without_four_pick_candidates():
    r = op.build(pool([0.60, 0.60, 0.60, 0.60, 0.53]))                          # +EV 5-pick, but a 53% filler
    assert r.by_size[5].stats is None and r.chosen is None and "no leg >= 54%" in r.by_size[5].note
    assert op.build(pool([0.60, 0.60, 0.60, 0.60, 0.545])).chosen.size == 5


def test_nhl_shots_on_goal_requests_the_player_shots_on_goal_market(tmp_path):
    assert fetch_odds.MARKETS["nhl"]["shots on goal"] == "player_shots_on_goal"
    seen = []

    class API(tfo.FakeAPI):
        async def __call__(self, url, params):
            if not url.endswith("/events"):
                seen.append(params["markets"])
            return await super().__call__(url, params)
    l = tfo.leg()                                   # Ovechkin 2.5 shots on goal, WSH
    assert asyncio.run(tfo.book(tmp_path, API()).price("nhl", [l], tfo.NOW)) == 1
    assert seen == ["player_shots_on_goal"] and l.quotes[0].venue == "books"
