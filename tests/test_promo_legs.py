from local import prize_picks_ev_scraper as scanner
from local.parse_board import parse_json
from local.sentinel import flex_entries


def leg(name, team, p, odds_type="standard"):
    l = scanner.Leg(name, "points", 0.5, "A v B", team=team, odds_type=odds_type)
    l.quotes = [scanner.Quote("kalshi", name, name, p, 0.02)]
    return l


def test_promo_legs_are_more_only():
    assert leg("D", "A", 0.20, "demon").pick == ("More", 0.20)       # never ("Less", 0.80)
    assert leg("G", "A", 0.80, "goblin").pick == ("More", 0.80)
    assert leg("S", "A", 0.20).pick == ("Less", 0.80)


def test_promo_legs_never_enter_slip_ev():
    std = [leg(f"S{i}", "AB"[i % 2], 0.60) for i in range(6)]
    gob = [leg(f"G{i}", "AB"[i % 2], 0.90, "goblin") for i in range(6)]
    assert all(not l.promo for _, _, combo, _ in scanner.rank_entries(std + gob) for l in combo)
    assert all(not l.promo for _, _, combo, _ in flex_entries(std + gob) for l in combo)
    assert flex_entries(gob) == []


def prop(line=0.5, **kw):
    return {"player": "P One", "team": "BOS", "league": "NHL", "stat": "Points", "line": line, "opponent": "NYR",
            "start_time": "2026-10-14T19:00:00-04:00", "odds_type": "standard", **kw}


def test_parse_json_keeps_only_standard_two_way_lines():
    props = [prop(), prop(1.5, odds_type="demon", stat="Assists"), prop(0.5, odds_type="goblin", stat="Goals"),
             prop(stat="Saves", odds_type=None), prop(stat="Hits", is_promo=True),
             prop(2.5, stat="Shots On Goal", flash_sale_line_score=1.5), prop(stat="Blocked Shots", adjusted_odds=True),
             prop(stat="Faceoffs Won", status="in_progress"), prop(stat="Time On Ice", is_live=True),
             prop(stat="Goals Allowed", projection_type="Combo"), prop(stat="Assists", odds_type="flash")]
    rows, bad = parse_json({"props": props}, "nhl")
    assert [(r["stat"], r["odds_type"]) for r in rows] == [("points", "standard"), ("blocked shots", "standard")]
    assert not any("adjusted" in b for b in bad)                                    # adjusted_odds standard: kept
    for why in ("demon", "goblin", "unknown odds_type", "promo", "flash-sale", "in_progress",
                "live", "Combo", "flash line"):
        assert any(why in b for b in bad), why


def test_old_boards_without_the_new_fields_still_parse():
    old = {k: v for k, v in prop().items()}
    assert len(parse_json({"props": [old]}, "nhl")[0]) == 1


def test_one_active_line_per_player_stat_game():
    moved = [prop(28.5, stat="Receiving Yards", league="NFL", updated_at="2026-10-14T18:00:00Z"),
             prop(22.5, stat="Receiving Yards", league="NFL", updated_at="2026-10-14T19:20:00Z")]
    rows, _ = parse_json({"props": moved}, "nfl")
    assert [r["line"] for r in rows] == ["22.5"]                                   # newest wins
    same = [prop(6.5, stat="Hitter Fantasy Score", league="MLB"), prop(6.5, stat="Hitter Fantasy Score", league="MLB")]
    assert len(parse_json({"props": same}, "mlb")[0]) == 1                          # duplicates agree: keep one
    clash = [prop(28.5, stat="Receiving Yards", league="NFL"), prop(22.5, stat="Receiving Yards", league="NFL")]
    rows, bad = parse_json({"props": clash}, "nfl")
    assert rows == [] and any("disagree (28.5, 22.5)" in b for b in bad)          # can't tell: drop both
    other_game = [prop(28.5, stat="Receiving Yards", league="NFL"),
                  prop(30.5, stat="Receiving Yards", league="NFL", start_time="2026-10-21T13:00:00-04:00")]
    assert len(parse_json({"props": other_game}, "nfl")[0]) == 2                    # different games: both


def test_whole_number_lines_are_kept_for_push_pricing_and_adjusted_half_lines_kept():
    rows, bad = parse_json({"props": [
        prop(2.0, stat="Shots On Goal", adjusted_odds=False, player="Jesper Bratt"),     # whole: priced with its push
        prop(2.5, stat="Shots On Goal", adjusted_odds=True, player="Jack Hughes"),       # adjusted standard: kept
        prop(1.5, stat="Shots On Goal", odds_type="goblin", adjusted_odds=True, player="Nico Hischier")]}, "nhl")
    assert sorted((r["player"], r["line"]) for r in rows) == [("Jack Hughes", "2.5"), ("Jesper Bratt", "2")]
    assert not any("whole-number" in b for b in bad) and any("goblin" in b for b in bad)


def test_whole_number_check_runs_on_the_active_line_only():
    moved_to_whole = [prop(2.5, stat="Shots On Goal", updated_at="2026-10-15T12:00:00Z"),
                      prop(2.0, stat="Shots On Goal", updated_at="2026-10-15T15:00:00Z")]
    assert [r["line"] for r in parse_json({"props": moved_to_whole}, "nhl")[0]] == ["2"]   # the stale 2.5 is not a fallback
    moved_to_half = [prop(2.0, stat="Shots On Goal", updated_at="2026-10-15T12:00:00Z"),
                     prop(2.5, stat="Shots On Goal", updated_at="2026-10-15T15:00:00Z")]
    assert [r["line"] for r in parse_json({"props": moved_to_half}, "nhl")[0]] == ["2.5"]
