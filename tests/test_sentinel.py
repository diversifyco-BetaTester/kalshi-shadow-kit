import json
from local import prize_picks_ev_scraper as scanner
from local.parse_board import parse_json
from local.sentinel import alert_text, kelly_fraction, repeat_of, leg_key, stake_units

NOW = 1_760_000_000.0


def leg(name, team, p, start="", game="A v B"):
    l = scanner.Leg(name, "points", 0.5, game, team=team, start_time=start)
    l.quotes = [scanner.Quote("kalshi", name, name, p, 0.02)]
    return l


def iso(ts):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def entry(legs, ev=0.10):
    return (ev, "flex", tuple(legs), False)


def test_pregame_drops_started_and_imminent_games_keeps_unknown():
    legs = [leg("Live", "A", .99, iso(NOW - 600)), leg("Soon", "A", .6, iso(NOW + 300)),
            leg("Later", "A", .6, iso(NOW + 3600)), leg("Pasted", "A", .6)]
    keep, dropped = scanner.pregame(legs, NOW)
    assert [l.player for l in keep] == ["Later", "Pasted"] and dropped == 2


def test_repeat_needs_four_shared_legs_and_less_than_five_points_of_ev():
    six = [leg(f"P{i}", "AB"[i % 2], .56) for i in range(8)]
    sent = [(NOW - 60, frozenset(leg_key(l) for l in six[:6]), 0.10)]
    win = 30 * 60
    assert repeat_of(entry(six[:5], 0.08), sent, NOW, win)               # 5-leg subset of the 6 just sent
    assert repeat_of(entry(six[2:7] + six[7:8], 0.14), sent, NOW, win)   # shares 4, only +4 pts
    assert not repeat_of(entry(six[2:8], 0.15), sent, NOW, win)          # shares 4, +5 pts: resend
    assert not repeat_of(entry([six[0], six[1], six[2], six[6], six[7]], 0.10), sent, NOW, win)  # shares 3
    assert not repeat_of(entry(six[:5], 0.08), sent, NOW + win, win)     # cooldown over


def test_kelly_units_are_clamped_and_zero_edge_bets_nothing():
    table = scanner.PAYOUTS["flex"][6]
    assert kelly_fraction([0.5] * 6, table) == 0.0
    f = kelly_fraction([0.6] * 6, table)
    assert 0 < f < 1
    assert stake_units(entry([leg(f"P{i}", "A", .545) for i in range(6)])) == 0.0   # under 0.5u: skipped
    assert stake_units(entry([leg(f"P{i}", "A", .80) for i in range(6)])) == 2.0    # huge edge -> cap


def test_alert_card_is_laid_out_for_hand_entry():
    def l(name, sport, team, game, start_h, p, side="More", stat="points", line=0.5):
        x = scanner.Leg(name, stat, line, game, team=team, sport=sport, start_time=iso(NOW + start_h * 3600))
        x.quotes = [scanner.Quote("books" if sport == "nhl" else "kalshi", "e", "PIN,DK de-vigged", p if side == "More" else 1 - p, 0.02)]
        return x
    legs = [l("Late Skater", "nhl", "COL", "COL v PIT", 6, 0.56), l("Early Skater", "nhl", "WSH", "MTL v WSH", 3, 0.58),
            l("Bills Wideout", "nfl", "BUF", "BUF v MIA", 48, 0.57, stat="receiving yards", line=49.5),
            l("Pats Tight End", "nfl", "NE", "NE v NYJ", 48, 0.555, side="Less", stat="receptions", line=3.5),
            l("Saturday Back", "cfb", "OSU", "OSU v TEX", 30, 0.56, stat="rushing yards", line=60.5)]
    status = {id(legs[1]): "Active", id(legs[0]): "Active / Confirmed Starter", id(legs[3]): "Q (Knee): Less only",
              id(legs[4]): "no CFB injury feed"}
    research = {id(legs[1]): "L5 avg 2.8 · 3/5 over 2.5"}
    msg = alert_text((0.123, "flex", tuple(legs), []), "nhl", 0, NOW, 1.4, research, status)
    lines = msg.splitlines()
    assert lines[0] == "🎯 <b>NFL+CFB+NHL 5-Pick Flex</b> · 💵 <b>STAKE 1.4u</b>"            # stake on top
    assert lines[-1] == "💵 <b>STAKE 1.4u</b> · 5-Pick Flex pays 10x (5/5) · 2x (4/5) · 0.4x (3/5)"   # and bottom
    names = [x[6:-7] for x in lines if x.startswith("<code>")]
    assert names == ["Bills Wideout", "Pats Tight End", "Saturday Back", "Early Skater", "Late Skater"]  # board order, then time
    i = lines.index("<code>Pats Tight End</code>")
    assert lines[i + 1] == "   Receptions | 3.5 | <b>LESS ⬇️</b>" and lines[i + 2] == "   ⚠️ Q (Knee): Less only"
    assert lines[i + 3] == "   📈 55.5% · Kalshi"
    j = lines.index("<code>Early Skater</code>")
    assert lines[j + 2] == "   ✅ No injury · L5 avg 2.8 · 3/5 over 2.5" and lines[j + 3] == "   📈 58.0% · Consensus: PIN, DK"
    assert lines[lines.index("<code>Late Skater</code>") + 2] == "   ✅ Confirmed Starter"
    assert lines[lines.index("<code>Saturday Back</code>") + 2] == "   • no CFB injury feed"
    headers = [x for x in lines if x.startswith(("🏈", "🏒"))]
    assert len(headers) == 5 and headers[0].startswith("🏈 <b>NFL</b>") and "BUF vs MIA" in headers[0]
    assert all(msg.count(f"<code>{n}</code>") == 1 for n in names)


def test_parse_json_nhl_stats_combo_skip_and_start_time():
    props = [{"player": "P One", "team": "BOS", "league": "NHL", "stat": s, "line": 2.5, "opponent": "NYR",
              "odds_type": "standard", "start_time": "2026-10-13T19:00:00.000-04:00"}
             for s in ("Shots On Goal", "Hits", "Faceoffs Won", "Blocked Shots", "Goalie Saves")]
    props.append({**props[0], "player": "A + B", "stat": "Shots On Goal (Combo)"})
    rows, bad = parse_json({"props": props}, "nhl")
    assert [r["stat"] for r in rows] == ["shots on goal", "hits", "faceoffs won", "blocked shots", "saves"]
    assert rows[0]["start_time"] == "2026-10-13T19:00:00.000-04:00"
    assert len(bad) == 1 and "combo" in bad[0]


def test_kalshi_market_from_another_game_day_never_prices_a_leg():
    tomorrow = scanner.Leg("Miro Heiskanen", "assists", 0.5, "COL v DAL", team="DAL",
                           start_time="2026-10-14T21:38:00-04:00")
    tonight = scanner.Quote("kalshi", "KXNHLAST-26OCT13COLDAL-DALMHEISKANEN4-1", "Miro Heiskanen: 1+ assists",
                            0.985, 0.03, title="Miro Heiskanen: 1+ assists")
    right_day = scanner.Quote("kalshi", "KXNHLAST-26OCT14COLDAL-DALMHEISKANEN4-1", "Miro Heiskanen: 1+ assists",
                              0.40, 0.02, title="Miro Heiskanen: 1+ assists")
    assert tomorrow.game_date == "26OCT14" and tonight.game_date == "26OCT13"
    scanner.attach([tomorrow], [tonight])
    assert tomorrow.quotes == []
    scanner.attach([tomorrow], [tonight, right_day])
    assert tomorrow.quotes == [right_day]


def test_stale_board_notices_stay_on_stdout_never_telegram(tmp_path, monkeypatch, capsys):
    import asyncio
    import os
    import time
    from types import SimpleNamespace
    from datetime import datetime, timezone

    from local import sentinel

    sent = []

    async def fake_send(text, chat_id=None):
        sent.append(text)
        return True

    async def no_quotes(series):
        return []

    monkeypatch.setattr(sentinel, "send_async", fake_send)
    monkeypatch.setattr(sentinel.scanner, "kalshi_quotes", no_quotes)
    csv = tmp_path / "lines_nhl.csv"
    later = datetime.fromtimestamp(time.time() + 6 * 3600, timezone.utc).isoformat()
    csv.write_text("player,stat,line,game,team,odds_type,start_time\n"
                   f"A B,points,0.5,BOS v NYR,BOS,standard,{later}\n")
    for age_min in (50, 90):  # past the notice (45); then past the pricing cutoff (60)
        os.utime(csv, (time.time() - age_min * 60,) * 2)
        a = SimpleNamespace(sport="nhl", csv=str(csv), board="none", seen=(), nudge={}, stale_min=45,
                            max_board_age=60, min_ev=0.545, flex=True, odds=None, pool=None, dedupe_min=30,
                            kelly=0.25, unit_pct=0.01, ledger=tmp_path / "slips.csv")
        asyncio.run(sentinel.cycle(a, [], chat_id="123"))
        out = capsys.readouterr().out
        assert f"board is {age_min} min old: click the bookmarklet" in out and "<b>" not in out
    assert sent == []


def test_missing_board_waits_quietly_then_runs_once_it_lands(tmp_path, monkeypatch, capsys):
    import asyncio
    import time as _time
    from types import SimpleNamespace

    from local import sentinel

    async def no_quotes(series):
        return []
    monkeypatch.setattr(sentinel.scanner, "kalshi_quotes", no_quotes)
    feed, csv = tmp_path / "cfb_board.json", tmp_path / "lines_cfb.csv"
    a = SimpleNamespace(sport="cfb", feed=str(feed), board=str(tmp_path / "board_cfb.txt"), csv=str(csv), seen=(),
                        nudge={}, stale_min=45, max_board_age=60, min_ev=0.545, flex=True, odds=None, pool=None,
                        dedupe_min=30, kelly=0.25, unit_pct=0.01, ledger=tmp_path / "slips.csv", research=False)
    t0 = _time.time()
    for k in range(3):                                   # three 60 s cycles: no exception, one note
        a.seen = sentinel.refresh_csv(sentinel.newest_feed(a.feed, a.board), csv, "cfb", a.seen)
        asyncio.run(sentinel.cycle(a, [], chat_id=""))
    out = capsys.readouterr().out
    assert out.count("waiting for initial cfb board to land") == 1 and "cycle failed" not in out
    a.waiting_note_at = t0 - 11 * 60                     # ten minutes later: reminded once more
    asyncio.run(sentinel.cycle(a, [], chat_id=""))
    assert "waiting for initial cfb board" in capsys.readouterr().out
    feed.write_text(json.dumps({"props": [{"player": "Jeremiah Smith", "team": "OSU", "league": "CFB",
                                           "stat": "Receiving Yards", "line": 84.5, "opponent": "TEX",
                                           "start_time": "2099-01-01T00:00:00Z", "odds_type": "standard"}]}))
    a.seen = sentinel.refresh_csv(sentinel.newest_feed(a.feed, a.board), csv, "cfb", a.seen)
    asyncio.run(sentinel.cycle(a, [], chat_id=""))       # the board landed: a normal cycle
    out = capsys.readouterr().out
    assert csv.exists() and "waiting" not in out and "standard legs priced" in out


def test_a_barely_plus_ev_slip_is_never_staked_above_its_quarter_kelly_amount():
    import math
    from local import sentinel
    table = scanner.PAYOUTS["flex"][6]
    for p in (0.545, 0.55, 0.555, 0.56, 0.57, 0.58, 0.60, 0.65, 0.80):
        e = entry([leg(f"P{i}", "A", p) for i in range(6)])
        kelly_units = 0.25 * kelly_fraction([p] * 6, table) / 0.01
        for mode in ("skip", "stake"):
            u = stake_units(e, below_min=mode)
            assert u <= kelly_units + 1e-9 and u <= sentinel.MAX_UNITS             # never above Kelly, never above cap
            if kelly_units < sentinel.MIN_UNITS:
                assert u == (0.0 if mode == "skip" else math.floor(kelly_units * 10 + 1e-9) / 10)
            else:
                assert u == math.floor(min(kelly_units, 2.0) * 10 + 1e-9) / 10
    thin = entry([leg(f"P{i}", "A", .55) for i in range(6)])
    assert 0 < 0.25 * kelly_fraction([.55] * 6, table) / 0.01 < 0.5 and stake_units(thin) == 0.0   # default: skip


def test_slips_under_the_minimum_stake_are_not_sent_or_claimed(tmp_path, monkeypatch, capsys):
    import asyncio
    from types import SimpleNamespace
    from local import sentinel
    players = [f"Player {c}" for c in "ABCDEF"]

    async def quotes(series):
        return [scanner.Quote("kalshi", f"KXNHLPTS-98DEC31G{i}-{p}", f"{p}: 1+ points", 0.55, 0.02, title=f"{p}: 1+ points")
                for i, p in enumerate(players)]
    sends = []

    async def fake_send(text, chat_id=None):
        sends.append(text)
        return True
    monkeypatch.setattr(sentinel.scanner, "kalshi_quotes", quotes)
    monkeypatch.setattr(sentinel, "send_async", fake_send)
    csv = tmp_path / "lines_nhl.csv"
    csv.write_text("player,stat,line,game,team,odds_type,start_time,position\n" + "".join(
        f"{p},points,0.5,H{i} v A{i},H{i},standard,2098-12-31T17:00:00+00:00,F\n" for i, p in enumerate(players)))
    a = SimpleNamespace(sport="nhl", csv=str(csv), board="none", feed="none", seen=(), nudge={}, stale_min=45,
                        max_board_age=0, min_ev=0.545, flex=True, odds=None, pool=None, dedupe_min=30,
                        leg_cooldown_min=120, kelly=0.25, unit_pct=0.01, ledger=tmp_path / "l.csv", research=False)
    asyncio.run(sentinel.cycle(a, [], chat_id="1"))
    assert sends == [] and not a.sent_props_cache and not (tmp_path / "l.csv").exists()   # 6 x 55%: +EV, Kelly < 0.5u
    out = capsys.readouterr().out
    assert "skipped (Kelly stake under 0.5u)" in out and ", 0 new" in out
