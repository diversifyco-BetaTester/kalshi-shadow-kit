"""local/h2h_context.py: box-score and game-log parsing (in ESPN's shapes, checked 2026-10-30), the per-minute H2H
baseline, absence context, minutes, the distribution, and the cached fetch layer. No network."""
import asyncio
import math

import pytest

from local import h2h_context as h

TEAM, OPP = "LAL", "OKC"
ME = "Star Guy"


def line(eid, date, value, minutes=30.0, opp=OPP, team=TEAM, season=2027):
    return h.GameLine(eid, date, team, opp, minutes, value, season)


def box(eid, ours=None, theirs=None, goalie=None):
    return h.Box(eid, {TEAM: {h.nk(n): m for n, m in (ours or {}).items()},
                       OPP: {h.nk(n): m for n, m in (theirs or {}).items()}}, goalie or {})


def injured(*players, status="Out"):
    return {(t, tuple(h.nk(n).split())): {"status": status} for t, n in players}


RECENT = [line(f"r{i}", f"2026-10-{28 - 2 * i:02d}T23:00Z", 20, 30.0, opp="XXX") for i in range(5)]


def predict(lines, boxes=None, injuries=None, **kw):
    return h.predict(ME, kw.pop("sport", "nba"), kw.pop("stat", "points"), kw.pop("line_", 20.5), TEAM, OPP,
                     lines, boxes or {}, injuries, **kw)


# --------------------------------------------------------------------------- parsing (ESPN shapes)
def summary(teams):
    return {"header": {"id": "401"}, "boxscore": {"players": teams}}


def test_nba_box_takes_minutes_and_drops_dnps():
    s = summary([{"team": {"abbreviation": "LAL"}, "statistics": [{"keys": ["minutes", "points"], "athletes": [
        {"athlete": {"displayName": "Star Guy"}, "stats": ["36", "28"], "didNotPlay": False},
        {"athlete": {"displayName": "Bench Guy"}, "stats": [], "didNotPlay": True, "reason": "COACH'S DECISION"},
        {"athlete": {"displayName": "Garbage Time Jr."}, "stats": ["0", "0"]}]}]}])
    b = h.parse_box(s, "nba")
    assert b.event_id == "401" and b.minutes == {"LAL": {"star guy": 36.0}}       # suffix "Jr." dropped by name_key


def test_nhl_box_reads_time_on_ice_and_keeps_goalies_apart():
    keys = ["blockedShots", "timeOnIce", "shotsTotal"]
    s = summary([{"team": {"abbreviation": "FLA"}, "statistics": [
        {"name": "forwards", "keys": keys, "athletes": [{"athlete": {"displayName": "Sam Bennett"}, "stats": ["0", "19:16", "3"]}]},
        {"name": "defenses", "keys": keys, "athletes": [{"athlete": {"displayName": "Aaron Ekblad"}, "stats": ["3", "22:31", "1"]}]},
        {"name": "goalies", "keys": ["saves", "timeOnIce"], "athletes": [
            {"athlete": {"displayName": "Akira Schmid"}, "stats": ["19", "59:55"]},
            {"athlete": {"displayName": "Backup Goalie"}, "stats": ["0", "0:05"]}]}]}])
    b = h.parse_box(s, "nhl", "9")
    assert b.minutes["FLA"] == {"sam bennett": pytest.approx(19 + 16 / 60), "aaron ekblad": pytest.approx(22.5 + 1 / 60)}
    assert b.goalie == {"FLA": "akira schmid"}                                     # most TOI; not a skater


def test_game_log_rows_carry_event_team_minutes_and_stat():
    log = {"names": ["minutes", "points"],
           "events": {"e1": {"gameDate": "2026-10-28T23:30:00.000+00:00", "team": {"abbreviation": "LAL"},
                             "opponent": {"abbreviation": "OKC"}},
                      "e0": {"gameDate": "2026-10-05T23:30:00.000+00:00", "team": {"abbreviation": "LAL"},
                             "opponent": {"abbreviation": "GS"}}},
           "seasonTypes": [{"displayName": "2026-27 Regular Season", "categories": [{"events": [{"eventId": "e1", "stats": ["34", "27"]}]}]},
                           {"displayName": "2026-27 Preseason", "categories": [{"events": [{"eventId": "e0", "stats": ["20", "9"]}]}]}]}
    [g] = h.lines_from_log(log, "nba", "points", 2027)
    assert (g.event_id, g.team, g.opp, g.minutes, g.value, g.season) == ("e1", "LAL", "OKC", 34.0, 27.0, 2027)
    assert h.lines_from_log(log, "nba", "not a stat") == []


def test_season_is_the_year_it_ends():
    assert h.season_of("2026-10-30T00:00Z") == 2027 and h.season_of("2026-05-12T00:00Z") == 2026


# --------------------------------------------------------------------------- distribution
def test_poisson_half_and_whole_lines():
    over, push = h.prob_over(2.0, 2.5)
    assert push == 0 and over == pytest.approx(1 - math.exp(-2) * (1 + 2 + 2))
    over, push = h.prob_over(2.0, 2.0)
    assert push == pytest.approx(math.exp(-2) * 2) and over == pytest.approx(1 - math.exp(-2) * 5)


def test_normal_for_high_projections():
    over, push = h.prob_over(25.0, 24.5)
    assert push == 0 and 0.5 < over < 0.6
    over, push = h.prob_over(25.0, 25.0)
    assert 0 < push < 0.1 and over < 0.5
    assert h.prob_over(0.0, 0.5) == (0.0, 0.0)


# --------------------------------------------------------------------------- the per-minute baseline
def test_rate_is_per_minute_and_scaled_to_expected_minutes():
    games = RECENT + [line("h1", "2026-10-10T23:00Z", 30, 40.0), line("h2", "2026-10-12T23:00Z", 10, 20.0)]
    p = predict(games, {"h1": box("h1", {ME: 40}), "h2": box("h2", {ME: 20})})
    assert p.n_h2h == 2 and p.rate == pytest.approx(40 / 60)                       # weighted sums, not mean of ratios
    assert p.exp_minutes == pytest.approx(30.0) and p.mu == pytest.approx(20.0)  # last 5 games: 30 min
    assert p.games[0]["weight"] == 1.0 and p.games[0]["context"] == []


def test_older_seasons_count_less():
    games = RECENT + [line("h1", "2026-10-10T23:00Z", 30, season=2027), line("h0", "2025-02-10T23:00Z", 10, season=2025)]
    p = predict(games, {"h1": box("h1", {ME: 30}), "h0": box("h0", {ME: 30})})
    assert [g["weight"] for g in p.games] == [1.0, pytest.approx(h.SEASON_DECAY ** 2, abs=1e-3)]
    assert p.h2h_rate == pytest.approx((30 + 10 * h.SEASON_DECAY ** 2) / (30 * (1 + h.SEASON_DECAY ** 2)))


def test_recent_form_is_one_extra_game_so_h2h_stays_primary():
    games = RECENT + [line(f"h{i}", f"2026-10-1{i}T23:00Z", 30) for i in range(5)]   # H2H 1.0/min, recent 0.67
    boxes = {f"h{i}": box(f"h{i}", {ME: 30}) for i in range(5)}
    p = predict(games, boxes)
    assert p.h2h_rate == pytest.approx(1.0) and p.recent_rate == pytest.approx(25 / 30)   # last 10 incl. H2H
    assert p.rate == pytest.approx((5 * 1.0 + h.PRIOR_GAMES * 25 / 30) / (5 + h.PRIOR_GAMES))


def test_never_recording_the_stat_in_h2h_is_not_certainty():
    games = [line(g.event_id, g.date, 1, 30.0, opp="XXX") for g in RECENT] + \
            [line(f"h{i}", f"2026-10-1{i}T23:00Z", 0) for i in range(9)]               # 0 steals in 9 meetings
    p = predict(games, {f"h{i}": box(f"h{i}", {ME: 30}) for i in range(9)}, stat="steals", line_=0.5)
    assert p.h2h_rate == 0 and p.mu > 0 and 0.01 < p.p_over < 0.5 and p.side == "Less"


# --------------------------------------------------------------------------- absence context
def mate_boxes(mate_minutes_recent=34.0):
    """This season's recent boxes: 'Co Star' is a key teammate (34 min)."""
    return {g.event_id: box(g.event_id, {ME: 30, "Co Star": mate_minutes_recent}) for g in RECENT}


def test_a_key_teammate_out_tonight_downweights_games_he_played():
    boxes = mate_boxes() | {"h1": box("h1", {ME: 30, "Co Star": 35}), "h2": box("h2", {ME: 30})}
    games = RECENT + [line("h1", "2026-10-10T23:00Z", 20), line("h2", "2026-10-12T23:00Z", 30)]
    p = predict(games, boxes, injured((TEAM, "Co Star")))
    by = {g["date"]: g for g in p.games}
    assert by["2026-10-10"]["weight"] == h.MISMATCH_W and "co star played then, out tonight" in by["2026-10-10"]["context"]
    assert by["2026-10-12"]["weight"] == 1.0                                       # he was out then too: same context


def test_a_teammate_back_tonight_downweights_games_he_missed():
    boxes = mate_boxes() | {"h1": box("h1", {ME: 30})}
    p = predict(RECENT + [line("h1", "2026-10-10T23:00Z", 20)], boxes)
    assert p.games[0]["context"] == ["co star out then, plays tonight"] and p.games[0]["weight"] == h.MISMATCH_W


def test_bench_players_and_goalies_are_not_key_teammates():
    boxes = {g.event_id: box(g.event_id, {ME: 30, "Bench Guy": 12}) for g in RECENT} | {"h1": box("h1", {ME: 30})}
    p = predict(RECENT + [line("h1", "2026-10-10T23:00Z", 20)], boxes, injured((TEAM, "Bench Guy")))
    assert p.games[0]["context"] == []


def test_an_opponent_starter_out_tonight_counts_only_if_he_played_then():
    boxes = {"h1": box("h1", {ME: 30}, {"Their Star": 36}), "h2": box("h2", {ME: 30}, {"Their Star": 36}),
             "h3": box("h3", {ME: 30}, {"Other Guy": 30})}
    games = RECENT + [line("h1", "2026-10-10T23:00Z", 20), line("h2", "2026-10-12T23:00Z", 20),
                      line("h3", "2026-10-14T23:00Z", 20)]
    p = predict(games, boxes, injured((OPP, "Their Star")))
    by = {g["date"]: g["context"] for g in p.games}
    assert by["2026-10-10"] == ["OKC their star played then, out tonight"]
    assert by["2026-10-14"] == []      # absent from an old box: injured or not on the roster then, can't tell


def test_mismatches_are_capped_so_a_changed_roster_cant_zero_a_game():
    mates = {f"Mate {i}": 30 for i in range(6)}
    boxes = {g.event_id: box(g.event_id, {ME: 30, **mates}) for g in RECENT} | {"h1": box("h1", {ME: 30})}
    p = predict(RECENT + [line("h1", "2026-10-10T23:00Z", 20)], boxes)
    assert len(p.games[0]["context"]) == 6 and p.games[0]["weight"] == h.MISMATCH_W ** h.MAX_MISMATCHES


def test_a_game_for_another_team_is_a_mismatch_and_a_missing_box_weighs_less():
    games = RECENT + [line("h1", "2025-02-10T23:00Z", 20, team="DAL", season=2025), line("h2", "2026-10-10T23:00Z", 20)]
    p = predict(games, {"h1": box("h1")})
    by = {g["date"]: g for g in p.games}
    assert by["2025-02-10"]["context"] == ["on DAL then"]
    assert by["2026-10-10"]["weight"] == h.MISSING_BOX_W and by["2026-10-10"]["context"] == ["no box score"]


def test_nhl_opposing_goalie_matters_for_points_not_shots():
    boxes = {"h1": box("h1", {ME: 18}, goalie={OPP: "old goalie"})}
    games = [line(g.event_id, g.date, 2, 18.0, opp="XXX") for g in RECENT] + [line("h1", "2026-10-10T23:00Z", 2, 18.0)]
    pts = predict(games, boxes, sport="nhl", stat="points", line_=0.5, opp_goalie="New Goalie")
    shots = predict(games, boxes, sport="nhl", stat="shots on goal", line_=2.5, opp_goalie="New Goalie")
    assert pts.games[0]["context"] == ["OKC goalie old goalie then, new goalie tonight"] and shots.games[0]["context"] == []


# --------------------------------------------------------------------------- usage
def usage_games(with_value, without_value, n_with=4, n_without=4):
    games, boxes = [], {}
    for i in range(n_with + n_without):
        eid, played = f"u{i}", i < n_with
        games.append(line(eid, f"2026-10-{28 - i:02d}T23:00Z", with_value if played else without_value, 30.0, opp="XXX"))
        boxes[eid] = box(eid, {ME: 30, **({"Co Star": 34} if played else {})})
    return games, boxes


def test_a_teammate_out_tonight_boosts_usage_from_this_seasons_split():
    games, boxes = usage_games(with_value=20, without_value=26)                    # +30% per minute without him
    games += [line("h1", "2026-10-01T23:00Z", 20)]
    boxes["h1"] = box("h1", {ME: 30, "Co Star": 34})
    p = predict(games, boxes, injured((TEAM, "Co Star")))
    expected = 1 + 4 / (4 + h.USAGE_K) * 0.3                                      # shrunk, all H2H weight differs
    assert p.usage == pytest.approx(expected) and any("usage without co star" in n for n in p.notes)


def test_a_thin_split_never_moves_usage():
    games, boxes = usage_games(with_value=20, without_value=40, n_with=1, n_without=7)
    games += [line("h1", "2026-10-01T23:00Z", 20)]                                # with the H2H game: 2 with him
    boxes["h1"] = box("h1", {ME: 30, "Co Star": 34})
    assert predict(games, boxes, injured((TEAM, "Co Star"))).usage == 1.0


# --------------------------------------------------------------------------- minutes and status
def test_a_minutes_restriction_caps_expected_minutes():
    back = [line("r0", "2026-10-28T23:00Z", 10, 18.0, opp="XXX")]                 # first game after 3 weeks out
    before = [line(f"b{i}", f"2026-10-0{7 - i}T23:00Z", 20, 34.0, opp="XXX") for i in range(4)]
    p = predict(back + before + [line("h1", "2026-10-01T23:00Z", 20, 34.0)])
    assert p.exp_minutes == 18.0 and any("minutes restriction" in n for n in p.notes)


def test_status_out_means_no_prediction():
    p = predict(RECENT + [line("h1", "2026-10-10T23:00Z", 20)], injuries=injured((TEAM, ME)))
    assert p.p_over is None and p.side is None and "no prediction" in p.notes[0]


def test_questionable_cuts_minutes_and_blocks_more():
    games = RECENT + [line(f"h{i}", f"2026-10-1{i}T23:00Z", 27) for i in range(4)]  # 27 a game: a More at 20.5
    healthy = predict(games)
    q = predict(games, injuries=injured((TEAM, ME), status="Questionable"))
    assert healthy.side == "More" and q.exp_minutes == pytest.approx(30 * 0.85)
    assert q.side is None and any("no More" in n for n in q.notes)


# --------------------------------------------------------------------------- calls
def test_sides_need_the_threshold_and_enough_h2h():
    boxes = {f"h{i}": box(f"h{i}", {ME: 30}) for i in range(5)}
    more = predict(RECENT + [line(f"h{i}", f"2026-10-1{i}T23:00Z", 27) for i in range(5)], boxes)
    less = predict(RECENT + [line(f"h{i}", f"2026-10-1{i}T23:00Z", 15) for i in range(5)], boxes)
    close = predict(RECENT + [line(f"h{i}", f"2026-10-1{i}T23:00Z", 21) for i in range(5)], boxes)
    assert (more.side, less.side, close.side) == ("More", "Less", None)
    thin = predict(RECENT + [line("h1", "2026-10-10T23:00Z", 35)])
    assert thin.side is None and thin.p_over > h.THRESHOLD and any("thin H2H" in n for n in thin.notes)


def test_a_stat_espn_doesnt_log_says_so():
    p = predict(RECENT, sport="nhl", stat="hits", line_=1.5)
    assert p.p_over is None and "hits isn't in ESPN game logs" in p.notes[0]


def test_no_h2h_and_unsupported_sports():
    assert "no games against OKC" in predict(RECENT).notes[0]
    assert "NFL not supported" in predict(RECENT, sport="nfl").notes[0]


# --------------------------------------------------------------------------- fetch layer
def test_fetch_context_requests_three_seasons_and_caches_finals(tmp_path, monkeypatch):
    calls = []

    def log_for(season):
        date = f"{season - 1}-12-01T00:00Z"
        return {"names": ["minutes", "points"],
                "events": {f"e{season}": {"gameDate": date, "team": {"abbreviation": TEAM}, "opponent": {"abbreviation": OPP}}},
                "seasonTypes": [{"displayName": "Regular Season", "categories": [{"events": [{"eventId": f"e{season}", "stats": ["30", "20"]}]}]}]}

    async def get(url, params):
        calls.append((url.rsplit("/", 1)[-1], dict(params)))
        if url.endswith("/search"):
            return {"items": []}
        if url.endswith("/gamelog"):
            return log_for(params.get("season", 2027))
        if url.endswith("/summary"):
            return summary([{"team": {"abbreviation": TEAM}, "statistics": [{"keys": ["minutes"], "athletes": [
                {"athlete": {"displayName": ME}, "stats": ["30"]}]}]}])
        return {}

    async def find(name, sport, team, get_):
        return "123", ""

    async def no_injuries(sport, get_):
        return {}
    monkeypatch.setattr(h.enricher, "find_athlete", find)
    monkeypatch.setattr(h.injury_guard, "injuries", no_injuries)
    cache = h.Cache(tmp_path / "c.sqlite3")
    now = h.datetime(2026, 10, 30, tzinfo=h.timezone.utc).timestamp()
    ctx = asyncio.run(h.fetch_context(ME, "nba", "points", TEAM, OPP, get=get, cache=cache, now=now))
    assert [c[1] for c in calls if c[0] == "gamelog"] == [{}, {"season": 2026}, {"season": 2025}]
    assert sorted(ctx["boxes"]) == ["e2025", "e2026", "e2027"] and len(ctx["lines"]) == 3
    calls.clear()
    asyncio.run(h.fetch_context(ME, "nba", "points", TEAM, OPP, get=get, cache=cache, now=now))
    assert calls == []                                    # current season within its TTL, the rest kept forever
    assert asyncio.run(h.fetch_context(ME, "nfl", "receptions", "PIT", "NO", get=get, cache=cache))["lines"] == []


def test_a_rookie_without_past_seasons_still_gets_this_seasons_games(tmp_path, monkeypatch):
    class NotFound(Exception):
        status = 404

    async def get(url, params):
        if url.endswith("/gamelog"):
            if params.get("season"):
                raise NotFound("no log for that season")
            return {"names": ["minutes", "points"],
                    "events": {"e1": {"gameDate": "2026-10-20T00:00Z", "team": {"abbreviation": TEAM}, "opponent": {"abbreviation": OPP}}},
                    "seasonTypes": [{"displayName": "Regular Season", "categories": [{"events": [{"eventId": "e1", "stats": ["30", "20"]}]}]}]}
        return {}

    async def find(name, sport, team, get_):
        return "9", ""

    async def no_injuries(sport, get_):
        return {}
    monkeypatch.setattr(h.enricher, "find_athlete", find)
    monkeypatch.setattr(h.injury_guard, "injuries", no_injuries)
    now = h.datetime(2026, 10, 30, tzinfo=h.timezone.utc).timestamp()
    ctx = asyncio.run(h.fetch_context(ME, "nba", "points", TEAM, OPP, get=get, cache=h.Cache(tmp_path / "c.sqlite3"), now=now))
    assert [g.event_id for g in ctx["lines"]] == ["e1"]


# --------------------------------------------------------------------------- guards (2026-10-30)
def goalie_games(saves=27, minutes=60.0):
    games = [line(f"r{i}", f"2026-10-{28 - 2 * i:02d}T23:00Z", saves, minutes, opp="XXX") for i in range(4)]
    games.append(line("relief", "2026-10-29T23:00Z", 12, 40.0, opp="XXX"))       # pulled / relief appearance
    games[1] = line("r1", "2026-10-10T23:00Z", saves, minutes, opp="XXX")         # 19-day gap before the 40 min
    return games + [line(f"h{i}", f"2026-10-0{i + 1}T23:00Z", saves, minutes) for i in range(4)]


def test_a_saves_prop_needs_the_probable_starter():
    games = goalie_games()
    none = predict(games, sport="nhl", stat="saves", line_=25.5)
    other = predict(games, sport="nhl", stat="saves", line_=25.5, starter="Backup Guy")
    assert none.p_over is None and "no probable starting goalie" in none.notes[0]
    assert other.p_over is None and "not LAL's probable starter (Backup Guy)" in other.notes[0]


def test_a_starting_goalie_expects_a_full_game_not_a_restriction():
    boxes = {f"h{i}": box(f"h{i}", {ME: 60}) for i in range(4)}
    p = predict(goalie_games(), boxes, sport="nhl", stat="saves", line_=25.5, starter=ME)
    assert p.exp_minutes == h.GOALIE_MINUTES and not any("restriction" in n for n in p.notes)
    assert p.usage == 1.0                                                         # skaters never move a goalie's saves


def test_only_the_strongest_usage_factor_applies_and_it_is_capped():
    games, boxes = usage_games(with_value=20, without_value=40)                    # x2 per minute without him: huge
    for i in range(4):                                                            # a second teammate, weaker effect
        boxes[f"u{i}"].minutes[TEAM]["side kick"] = 30
    games += [line("h1", "2026-10-01T23:00Z", 20)]
    boxes["h1"] = box("h1", {ME: 30, "Co Star": 34, "Side Kick": 30})
    p = predict(games, boxes, injured((TEAM, "Co Star"), (TEAM, "Side Kick")))
    [usage] = [n for n in p.notes if n.startswith("usage")]
    assert p.usage == pytest.approx(1 + h.USAGE_CAP) and "capped" in usage and "strongest of 2" in usage


def test_a_role_change_weighs_recent_form_equal_to_h2h():
    games = [line(g.event_id, g.date, 30, 30.0, opp="XXX") for g in RECENT]        # 1.0/min now
    games += [line(f"h{i}", f"2025-0{i + 1}-10T23:00Z", 12, 30.0, season=2025) for i in range(4)]  # 0.4/min then
    p = predict(games, {f"h{i}": box(f"h{i}", {ME: 30}) for i in range(4)}, line_=25.5)
    assert any(n.startswith("role change") for n in p.notes)
    assert p.rate == pytest.approx((p.h2h_rate + p.recent_rate) / 2)              # equal weight, not 1 extra game


def test_a_projection_far_from_the_line_gets_no_call():
    games = RECENT + [line(f"h{i}", f"2026-10-1{i}T23:00Z", 4, 30.0) for i in range(5)]   # projecting ~6 at 20.5
    p = predict(games, {f"h{i}": box(f"h{i}", {ME: 30}) for i in range(5)})
    assert p.side is None and any("from the line 20.5" in n for n in p.notes)
    small = predict(RECENT + [line(f"h{i}", f"2026-10-1{i}T23:00Z", 0, 30.0) for i in range(5)],
                    {f"h{i}": box(f"h{i}", {ME: 30}) for i in range(5)}, stat="steals", line_=0.5)
    assert not any("from the line" in n for n in small.notes)                     # a 0.5 line: guard doesn't apply


def test_probabilities_are_widened_and_capped():
    tight = h.prob_over(5.0, 8.5)[0]
    wide = h.prob_over(5.0, 8.5, var=5.0 * 3)[0]
    assert wide > tight                                                           # more variance, fatter tail
    over4, push4 = h.prob_over(5.0, 4.0, var=12.0)
    assert over4 + push4 == pytest.approx(h.prob_over(5.0, 3.5, var=12.0)[0])   # P(X>4) + P(X=4) = P(X>3.5)
    games = RECENT + [line(f"h{i}", f"2026-10-1{i}T23:00Z", 27) for i in range(9)]
    p = predict(games, {f"h{i}": box(f"h{i}", {ME: 30}) for i in range(9)}, line_=16.5)
    assert p.p_over <= h.MAX_CONF


def test_negative_binomial_matches_its_closed_form():
    mu, var = 4.0, 10.0
    r = mu * mu / (var - mu)
    q = mu / (r + mu)
    pmf0 = (1 - q) ** r
    over, push = h.prob_over(mu, 0.0, var)
    assert push == pytest.approx(pmf0) and over == pytest.approx(1 - pmf0)
