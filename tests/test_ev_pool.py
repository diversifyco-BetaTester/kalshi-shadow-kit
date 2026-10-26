from datetime import datetime, timezone

from local import ev_pool
from local import prize_picks_ev_scraper as scanner
from local.sentinel import alert_text, leg_key, mixed_entries, repeat_keys

NOW = datetime(2026, 10, 14, 18, 0, tzinfo=timezone.utc).timestamp()


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def leg(sport, name, team, game, p, start_in_h=3.0):
    l = scanner.Leg(name, "points", 0.5, game, team=team, start_time=iso(NOW + start_in_h * 3600), sport=sport)
    l.quotes = [scanner.Quote("kalshi", name, name, p, 0.02)]
    return l


def test_publish_replaces_own_sport_and_others_sees_only_other_sports(tmp_path):
    pool = ev_pool.EvPool(tmp_path / "pool.json")
    pool.publish("nhl", [(leg("nhl", "A", "COL", "COL v DAL", .60), "")], NOW)
    pool.publish("mlb", [(leg("mlb", "B", "LAD", "LAD v MIL", .58), "DK+FD")], NOW)
    pool.publish("nhl", [(leg("nhl", "C", "BOS", "BOS v NYR", .40), "")], NOW + 60)   # A gone, C Less 60%
    seen = pool.others("mlb", NOW + 60)
    assert [(l.player, l.sport, l.pick[0], round(l.pick[1], 2)) for l in seen] == [("C", "nhl", "Less", 0.6)]
    b = pool.others("nhl", NOW + 60)[0]
    assert b.player == "B" and b.quotes[0].text == "DK+FD" and b.start_ts == NOW + 3 * 3600


def test_pool_drops_started_games_stale_writers_and_undated_legs(tmp_path):
    pool = ev_pool.EvPool(tmp_path / "pool.json")
    undated = leg("mlb", "U", "LAD", "LAD v MIL", .6)
    undated.start_time = ""
    pool.publish("mlb", [(leg("mlb", "Soon", "LAD", "LAD v MIL", .6, start_in_h=0.1), ""),
                         (leg("mlb", "Later", "SD", "SD v SF", .6), ""), (undated, "")], NOW)
    assert [l.player for l in pool.others("nhl", NOW)] == ["Later"]
    assert pool.others("nhl", NOW + ev_pool.STALE_S + 1) == []        # MLB sentinel stopped refreshing


def test_mixed_slips_need_both_sides_and_distinct_games():
    own = [leg("nhl", f"N{i}", "COL" if i % 2 else "DAL", "COL v DAL" if i < 2 else f"G{i} v H{i}", .58)
           for i in range(5)]
    others = [leg("mlb", f"M{i}", "LAD", f"LAD{i} v MIL{i}", .57) for i in range(4)]
    entries = mixed_entries(own, others, "nhl")
    assert entries
    for ev, kind, combo, _ in entries:
        sports = {l.sport for l in combo}
        assert sports == {"nhl", "mlb"} and kind == "flex" and len(combo) in (5, 6)
        assert not ({"N0", "N1"} <= {l.player for l in combo})           # N0, N1 share COL v DAL
    assert mixed_entries(own, [], "nhl") == [] and mixed_entries([], others, "nhl") == []


def test_mixed_slip_found_by_both_sentinels_is_sent_once(tmp_path):
    path = tmp_path / "pool.json"
    nhl_side, mlb_side = ev_pool.EvPool(path), ev_pool.EvPool(path)
    combo = [leg("nhl", "N1", "COL", "COL v DAL", .6), leg("mlb", "M1", "LAD", "LAD v MIL", .6)]
    keys = frozenset(leg_key(l) for l in combo)
    rule = lambda k, ev, s: repeat_keys(k, ev, s, NOW, 1800)
    assert nhl_side.claim(keys, 0.10, NOW, 1800, rule)
    assert not mlb_side.claim(keys, 0.12, NOW + 5, 1800, rule)                  # same combo, +2 pts: repeat
    assert mlb_side.claim(keys, 0.16, NOW + 10, 1800, rule)                     # +6 pts: worth resending
    assert ev_pool.EvPool(path).claim(keys, 0.10, NOW + 1811, 1800,
                                      lambda k, ev, s: repeat_keys(k, ev, s, NOW + 1811, 1800))  # window over


def test_leg_keys_tell_sports_apart():
    assert leg_key(leg("nhl", "Same Name", "A", "A v B", .6)) != leg_key(leg("mlb", "Same Name", "A", "A v B", .6))


def test_mixed_alert_title_and_league_tags():
    combo = tuple([leg("nhl", "N1", "COL", "COL v DAL", .60), leg("mlb", "M1", "LAD", "LAD v MIL", .58)]
                  + [leg("nhl", f"N{i}", "BOS", f"BOS{i} v NYR{i}", .56) for i in range(2, 5)])
    msg = alert_text((0.12, "flex", combo, False), "nhl", 0, NOW, 1.0)
    lines = msg.splitlines()
    assert lines[0] == "🎯 <b>MLB+NHL 5-Pick Flex</b> · 💵 <b>STAKE 1.0u</b>"               # board tab order
    mlb, nhl = next(i for i, x in enumerate(lines) if "<b>MLB</b>" in x), next(i for i, x in enumerate(lines) if "<b>NHL</b>" in x)
    assert mlb < lines.index("<code>M1</code>") < nhl < lines.index("<code>N1</code>")      # grouped by sport
    single = alert_text((0.12, "flex", tuple(l for l in combo if l.sport == "nhl"), False), "nhl", 0, NOW, 1.0)
    assert single.startswith("🎯 <b>NHL 4-Pick") and "<b>MLB</b>" not in single
