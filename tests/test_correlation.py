import math

import pytest

pytestmark = pytest.mark.usefixtures("stacks_allowed")  # these tests exercise the stack model

from local import correlation as cor
from local import prize_picks_ev_scraper as scanner
from local import sentinel

KICK = "2099-01-01T17:00:00Z"


def leg(player, stat, team, game, p, side="More", sport="nfl"):
    l = scanner.Leg(player, stat, 50.5, game, team=team, start_time=KICK, sport=sport)
    l.quotes = [scanner.Quote("kalshi", player, player, p if side == "More" else 1 - p, 0.02)]
    return l


ALLEN = lambda p=0.58: leg("Josh Allen", "passing yards", "BUF", "BUF v MIA", p)
SHAKIR = lambda p=0.57: leg("Khalil Shakir", "receiving yards", "BUF", "BUF v MIA", p)


def test_passing_stack_is_recognized_and_labelled():
    kind, st = cor.classify(ALLEN(), SHAKIR())
    assert kind == "stack" and st.rho == cor.RHO["passing"]
    assert st.label == "BUF Allen Pass + Shakir Rec"
    assert cor.classify(SHAKIR(), ALLEN())[0] == "stack"                                   # either order
    other_team = leg("Tyreek Hill", "receiving yards", "MIA", "BUF v MIA", 0.57)
    assert cor.classify(ALLEN(), other_team)[0] == "unmodeled"                            # opponent's WR
    less = leg("Khalil Shakir", "receiving yards", "BUF", "BUF v MIA", 0.57, side="Less")
    assert cor.classify(ALLEN(), less)[0] == "unmodeled"                                  # More + Less: no stack


def test_opposing_goalie_saves_stack():
    a = leg("Jake Allen", "saves", "NJ", "NJ v NYR", 0.56, sport="nhl")
    b = leg("Igor Shesterkin", "saves", "NYR", "NJ v NYR", 0.56, sport="nhl")
    kind, st = cor.classify(a, b)
    assert kind == "stack" and st.rho == cor.RHO["goalies"] and "opposing goalies" in st.label


@pytest.mark.parametrize("x,y,why", [
    (("James Cook", "rushing yards", "BUF"), ("Ray Davis", "rushing attempts", "BUF"), "rushing volume"),
    (("Jalen Brunson", "points", "NYK"), ("Mikal Bridges", "points + assists", "NYK"), "points volume"),
    (("James Cook", "rushing attempts", "BUF"), ("Josh Allen", "passing attempts", "BUF"), "opposite scripts"),
])
def test_conflicts_are_vetoed(x, y, why):
    sport = "nba" if x[1] == "points" else "nfl"
    a = leg(*x, "G v H", 0.58, sport=sport)
    b = leg(*y, "G v H", 0.58, sport=sport)
    kind, reason = cor.classify(a, b)
    assert kind == "veto" and why in reason


def test_check_allows_stacks_and_independent_games_only():
    others = [leg(f"P{i}", "receptions", f"T{i}", f"T{i} v U{i}", 0.56) for i in range(4)]
    ok, stacks, _ = cor.check([ALLEN(), SHAKIR(), *others])
    assert ok and [s.label for s in stacks] == ["BUF Allen Pass + Shakir Rec"]
    ok, _, why = cor.check([ALLEN(), SHAKIR(), leg("Dalton Kincaid", "receptions", "BUF", "BUF v MIA", 0.56), *others[:2]])
    assert not ok and "3 legs from one game" in why
    ok, _, why = cor.check([ALLEN(), leg("Tua Tagovailoa", "passing yards", "MIA", "BUF v MIA", 0.56), *others])
    assert not ok and "correlation not modeled" in why
    assert cor.check(others) == (True, [], "")


def test_joint_probability_matches_the_closed_form():
    for rho in (0.0, 0.15, 0.35, -0.3):
        assert abs(cor.both_win(0.5, 0.5, rho) - (0.25 + math.asin(rho) / (2 * math.pi))) < 1e-6
    assert cor.both_win(0.58, 0.57, 0.35) > 0.58 * 0.57
    dist = cor.hit_distribution([ALLEN(), SHAKIR()], [cor.check([ALLEN(), SHAKIR()])[1][0]])
    assert abs(sum(dist) - 1) < 1e-9 and dist[2] > 0.58 * 0.57


def test_a_stack_raises_slip_ev_versus_treating_the_legs_as_independent():
    combo = [ALLEN(), SHAKIR(), *[leg(f"P{i}", "receptions", f"T{i}", f"T{i} v U{i}", 0.56) for i in range(3)]]
    ok, stacks, _ = cor.check(combo)
    table = scanner.PAYOUTS["flex"][5]
    indep = scanner.entry_ev([l.pick[1] for l in combo], table)
    assert ok and cor.slip_ev(combo, table, stacks) > indep > 0
    assert cor.slip_ev(combo, table, []) == pytest.approx(indep)


def test_payout_shading_rejects_the_stack_and_falls_back_to_independent_legs():
    stack = [ALLEN(0.60), SHAKIR(0.60)]
    indep = [leg(f"P{i}", "receptions", f"T{i}", f"T{i} v U{i}", 0.565) for i in range(5)]
    has_stack = lambda combo: {"Josh Allen", "Khalil Shakir"} <= {l.player for l in combo}
    unshaded = {len(e[2]): e for e in sentinel.flex_entries(stack + indep, payout_factor=1.0)}
    assert unshaded[5][3] and has_stack(unshaded[5][2])                                  # unshaded: the stack wins
    stacked5, stacks5 = unshaded[5][2], unshaded[5][3]
    for f in (1.0, 0.9, 0.8, 0.75, 0.6, 0.5):
        picked = sentinel.flex_entries(stack + indep, payout_factor=f)
        assert all(e[0] > 0 for e in picked)                                            # nothing at or below break-even
        stack_ev = cor.slip_ev(stacked5, scanner.PAYOUTS["flex"][5], stacks5, f)
        five = next(e for e in picked if len(e[2]) == 5)
        if stack_ev <= 0:                                                                # break-even rule: rejected...
            assert not five[3] and not has_stack(five[2])                                # ...independent legs instead
    assert cor.slip_ev(stacked5, scanner.PAYOUTS["flex"][5], stacks5, 0.5) <= 0
    assert all(not e[3] for e in sentinel.flex_entries(stack + indep, payout_factor=0.5))


def test_alert_header_tags_the_stack():
    combo = [ALLEN(), SHAKIR(), *[leg(f"P{i}", "receptions", f"T{i}", f"T{i} v U{i}", 0.56) for i in range(3)]]
    ok, stacks, _ = cor.check(combo)
    msg = sentinel.alert_text((0.2, "flex", tuple(combo), stacks), "nfl", 0, 0, 1.0)
    lines = msg.splitlines()
    assert lines[0].startswith("🎯") and lines[2] == "<b>[STACK: BUF Allen Pass + Shakir Rec (+Correlated)]</b>"
    assert sentinel.stake_units((0.2, "flex", tuple(combo), stacks)) >= 0.5
    plain = sentinel.alert_text((0.2, "flex", tuple(combo[2:]), []), "nfl", 0, 0, 1.0)
    assert "STACK" not in plain


def test_at_most_one_stack_per_slip():
    pair = lambda g: [leg(f"QB{g}", "passing yards", f"T{g}", f"T{g} v U{g}", 0.58),
                      leg(f"WR{g}", "receiving yards", f"T{g}", f"T{g} v U{g}", 0.57)]
    singles = [leg(f"P{i}", "receptions", f"S{i}", f"S{i} v R{i}", 0.56) for i in range(3)]
    ok, stacks, _ = cor.check(pair(1) + singles)
    assert ok and len(stacks) == 1
    ok, _, why = cor.check(pair(1) + pair(2) + singles[:1])
    assert not ok and "2 correlated pairs (max 1 per slip)" in why
    best = sentinel.flex_entries(pair(1) + pair(2) + pair(3) + singles)
    assert best and all(len(e[3]) <= 1 for e in best)
    assert all(len({cor.game_id(l) for l in e[2]}) >= len(e[2]) - 1 for e in best)   # the rest: one leg per game


def test_passing_rho_is_moderated():
    assert cor.RHO["passing"] == 0.22 and cor.MAX_STACKS == 1


# ================================================================ NBA and MLB rules
def p(player, stat, team, game, prob=0.57, side="More", sport="nba", position="", bat_order=None):
    l = leg(player, stat, team, game, prob, side=side, sport=sport)
    l.position, l.bat_order = position, bat_order
    return l


LAL = "LAL v DAL"


def test_nba_playmaker_stack_guard_assists_with_teammate_scoring():
    doncic = p("Luka Doncic", "assists", "LAL", LAL, position="G")
    for mate in (p("Daniel Gafford", "points", "LAL", LAL, position="C"), p("Austin Reaves", "threes", "LAL", LAL, position="G")):
        for order in ((doncic, mate), (mate, doncic)):
            kind, st = cor.classify(*order)
            assert kind == "stack" and st.rho == cor.RHO["playmaker"] and st.a is doncic
    _, st = cor.classify(doncic, p("Daniel Gafford", "points", "LAL", LAL, position="C"))
    assert st.label == "LAL Doncic Ast + Gafford Pts"


@pytest.mark.parametrize("assister,mate", [
    (dict(position="F"), dict(team="LAL")),                     # a forward's assists: not a playmaker stack
    (dict(position=""), dict(team="LAL")),                      # position unknown: can't tell
    (dict(position="G"), dict(team="DAL")),                     # opponent's points
    (dict(position="G", side="Less"), dict(team="LAL")),        # Less assists
])
def test_nba_non_stacks_fall_back_to_unmodeled(assister, mate):
    a = p("Luka Doncic", "assists", "LAL", LAL, **{"position": "G", **assister})
    b = p("Daniel Gafford", "points", mate["team"], LAL, position="C")
    assert cor.classify(a, b)[0] == "unmodeled"


def test_nba_frontcourt_rebounds_and_turnover_steal_conflicts_are_vetoed():
    c1, c2 = p("Daniel Gafford", "rebounds", "LAL", LAL, position="C"), p("Jaxson Hayes", "rebounds", "LAL", LAL, position="F-C")
    kind, why = cor.classify(c1, c2)
    assert kind == "veto" and "frontcourt" in why
    guard = p("Austin Reaves", "rebounds", "LAL", LAL, position="G")
    assert cor.classify(c1, guard)[0] == "unmodeled"                                   # guard + center: not this veto
    tov = p("Luka Doncic", "turnovers", "LAL", LAL, position="G")
    stl = p("Kyrie Irving", "steals", "DAL", LAL, side="Less", position="G")
    for order in ((tov, stl), (stl, tov)):
        kind, why = cor.classify(*order)
        assert kind == "veto" and "same possessions" in why
    assert cor.classify(tov, p("Anthony Davis", "steals", "LAL", LAL, side="Less"))[0] == "unmodeled"   # teammate
    assert cor.classify(p("A B", "points", "LAL", LAL), p("C D", "points + assists", "LAL", LAL))[0] == "veto"  # still capped


CLE = "CLE v TB"


def test_mlb_table_setter_needs_lineup_spots_1_to_3_then_2_to_5():
    runs = p("Steven Kwan", "runs", "CLE", CLE, sport="mlb", bat_order=1)
    rbi = p("José Ramírez", "rbis", "CLE", CLE, sport="mlb", bat_order=3)
    kind, st = cor.classify(rbi, runs)
    assert kind == "stack" and st.a is runs and st.rho == cor.RHO["table_setter"] and st.label == "CLE Kwan Runs + Ramírez RBI"
    for r_spot, b_spot, kind in ((3, 5, "stack"), (2, 4, "stack"), (4, 3, "unmodeled"), (1, 6, "unmodeled"),
                                 (3, 1, "unmodeled"), (None, 3, "unmodeled"), (1, None, "unmodeled")):
        a = p("Steven Kwan", "runs", "CLE", CLE, sport="mlb", bat_order=r_spot)
        b = p("José Ramírez", "rbis", "CLE", CLE, sport="mlb", bat_order=b_spot)
        assert cor.classify(a, b)[0] == kind, (r_spot, b_spot)


def test_mlb_pitcher_hits_allowed_with_opposing_batter():
    sp = p("Nick Martinez", "hits allowed", "TB", CLE, sport="mlb", position="SP")
    for stat in ("hits", "total bases"):
        kind, st = cor.classify(p("Steven Kwan", stat, "CLE", CLE, sport="mlb"), sp)
        assert kind == "stack" and st.a is sp and st.rho == cor.RHO["hits_allowed"]
    assert st.label == "TB Martinez H Allowed + CLE Kwan TB"
    assert cor.classify(sp, p("Yandy Diaz", "hits", "TB", CLE, sport="mlb"))[0] == "unmodeled"         # own team


@pytest.mark.parametrize("pitcher,batter,why", [
    (("strikeouts", "More"), ("hitter strikeouts", "Less"), "More strikeouts"),
    (("strikeouts", "More"), ("hits", "More"), "More strikeouts"),
    (("earned runs", "Less"), ("rbis", "More"), "Less earned runs"),
])
def test_mlb_pitcher_batter_conflicts_are_vetoed(pitcher, batter, why):
    sp = p("Nick Martinez", pitcher[0], "TB", CLE, sport="mlb", side=pitcher[1], position="SP")
    bat = p("Steven Kwan", batter[0], "CLE", CLE, sport="mlb", side=batter[1])
    for order in ((sp, bat), (bat, sp)):
        kind, reason = cor.classify(*order)
        assert kind == "veto" and why in reason


def test_every_assumed_correlation_respects_the_cap():
    assert cor.RHO_CAP == 0.22 and all(v <= cor.RHO_CAP for v in cor.RHO.values()) and cor.MAX_STACKS == 1


def test_at_most_one_stack_per_slip_across_sports():
    nba = [p("Luka Doncic", "assists", "LAL", LAL, position="G"), p("Daniel Gafford", "points", "LAL", LAL, position="C")]
    mlb = [p("Steven Kwan", "runs", "CLE", CLE, sport="mlb", bat_order=1),
           p("José Ramírez", "rbis", "CLE", CLE, sport="mlb", bat_order=2)]
    singles = [p(f"S{i}", "receptions", f"T{i}", f"T{i} v U{i}", sport="nfl") for i in range(3)]
    ok, stacks, _ = cor.check(nba + singles)
    assert ok and len(stacks) == 1
    ok, _, why = cor.check(nba + mlb + singles[:1])
    assert not ok and "2 correlated pairs (max 1 per slip)" in why


def test_mixed_sport_slips_keep_one_stack_and_one_leg_per_other_game():
    own = [p("Luka Doncic", "assists", "LAL", LAL, 0.58, position="G"), p("Daniel Gafford", "points", "LAL", LAL, 0.58, position="C"),
           p("Jalen Brunson", "points", "NYK", "NYK v BOS", 0.57, position="G")]
    pooled = [p("Steven Kwan", "runs", "CLE", CLE, 0.58, sport="mlb", bat_order=1),
              p("José Ramírez", "rbis", "CLE", CLE, 0.58, sport="mlb", bat_order=2),
              *[p(f"Q{i}", "receptions", f"T{i}", f"T{i} v U{i}", 0.565, sport="nfl") for i in range(3)]]
    entries = sentinel.mixed_entries(own, pooled, "nba")
    assert entries
    for ev, kind, combo, stacks in entries:
        assert ev > 0 and len(stacks) <= 1 and {"nba"} < {l.sport for l in combo}
        ok, again, _ = cor.check(combo)
        assert ok and len(again) == len(stacks)
        games = [cor.game_id(l) for l in combo]
        assert len(set(games)) == len(games) - len(stacks)                                # stack shares one game, rest unique


def test_lineup_spots_come_from_espn_once_posted():
    import asyncio
    from local import injury_guard as ig
    board = {"events": [{"id": "g1"}]}
    summary = {"rosters": [{"team": {"abbreviation": "CLE"}, "roster": [
        {"athlete": {"displayName": "Steven Kwan"}, "batOrder": 1}, {"athlete": {"displayName": "José Ramírez"}, "batOrder": 3},
        {"athlete": {"displayName": "Tanner Bibee"}}]}]}

    async def get(url):
        return board if "scoreboard" in url else summary
    legs = [p("Steven Kwan", "runs", "CLE", CLE, sport="mlb"), p("José Ramírez", "rbis", "CLE", CLE, sport="mlb"),
            p("Tanner Bibee", "strikeouts", "CLE", CLE, sport="mlb")]
    asyncio.run(ig.annotate_lineups(legs, get))
    assert [l.bat_order for l in legs] == [1, 3, None]
    assert cor.classify(legs[0], legs[1])[0] == "stack"


def test_position_survives_parser_csv_and_pool(tmp_path):
    from local import ev_pool
    from local.parse_board import parse_json, write_csv
    rows, _ = parse_json({"props": [{"player": "Luka Doncic", "team": "LAL", "position": "G", "league": "NBA",
                                     "stat": "Assists", "line": 8.5, "opponent": "DAL", "start_time": KICK,
                                     "odds_type": "standard"}]}, "nba")
    write_csv(rows, tmp_path / "lines.csv")
    l = scanner.load_legs(str(tmp_path / "lines.csv"))[0]
    assert l.position == "G"
    l.sport, l.bat_order = "nba", None
    l.quotes = [scanner.Quote("kalshi", "x", "x", 0.58, 0.02)]
    pool = ev_pool.EvPool(tmp_path / "pool.json")
    pool.publish("nba", [(l, "Kalshi")], 0)
    back = pool.others("mlb", 0)[0]
    assert back.position == "G" and back.bat_order is None



def test_stacks_are_excluded_by_default(monkeypatch):
    from local import pipeline_config
    monkeypatch.setitem(pipeline_config.CONFIG["optimizer"], "allow_stacks", False)
    combo = [ALLEN(), SHAKIR(), *[leg(f"P{i}", "receptions", f"T{i}", f"T{i} v U{i}", 0.56) for i in range(3)]]
    ok, stacks, why = cor.check(combo)
    assert not ok and stacks == [] and "correlated pair excluded until joint outcomes are modeled" in why
    best = sentinel.flex_entries(combo + [leg("P9", "receptions", "T9", "T9 v U9", 0.56)])
    assert best and all(not e[3] and not {"Josh Allen", "Khalil Shakir"} <= {l.player for l in e[2]} for e in best)
