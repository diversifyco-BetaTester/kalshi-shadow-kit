"""Soccer: PrizePicks' combined SOCCER league, priced by Kalshi only (titles as Kalshi lists them, 2026-10-23)."""
import asyncio
from types import SimpleNamespace

from local import fetch_odds, grade_slips, parse_board, sentinel
from local import prize_picks_ev_scraper as scanner

START = "2098-12-31T15:00:00.000-04:00"


def prop(player, stat, line, team, opp, odds_type="standard", pid=None):
    return {"player": player, "team": team, "opponent": opp, "position": "Forward", "league": "SOCCER", "stat": stat,
            "line": line, "start_time": START, "odds_type": odds_type, "projection_id": pid or f"{player}{stat}",
            "status": "pre_game", "projection_type": "Single Stat", "is_promo": False, "adjusted_odds": False,
            "is_live": False, "in_game": False, "updated_at": "2098-12-31T10:00:00Z", "allowed_wager_types": None,
            "discount_name": None, "event_type": "player"}


def test_soccer_is_a_sentinel_sport_and_parses_standard_lines_with_team_names():
    assert "soccer" in parse_board.SPORTS and "soccer" not in fetch_odds.SPORT_KEYS   # Kalshi only: no credits
    rows, bad = parse_board.parse_json({"props": [
        prop("Chris Wood", "Goals", 0.5, "Nottingham Forest", "Ipswich"),
        prop("Neco Williams", "Goal + Assist", 0.5, "Nottingham Forest", "Ipswich"),
        prop("Felix Keidel", "Goals", 0.5, "Elversberg", "Leipzig", odds_type="demon"),       # dropped
        prop("Matz Sels", "Goalie Saves", 3.5, "Nottingham Forest", "Ipswich")]}, "soccer")
    assert sorted((r["player"], r["stat"], r["game"]) for r in rows) == [
        ("Chris Wood", "goals", "Ipswich v Nottingham Forest"), ("Matz Sels", "saves", "Ipswich v Nottingham Forest"),
        ("Neco Williams", "goal+assist", "Ipswich v Nottingham Forest")]
    assert any("demon" in b for b in bad)


def q(title, p, ticker):
    return scanner.Quote("kalshi", ticker, title, p, 0.02, title=title)


def test_kalshi_soccer_titles_match_goals_assists_and_score_or_assist():
    goal = scanner.Leg("Chris Wood", "goals", 0.5, "Nottingham Forest@Ipswich", team="Nottingham Forest", start_time=START)
    ast = scanner.Leg("Xaver Schlager", "assists", 0.5, "Nottingham Forest@Ipswich", team="Nottingham Forest", start_time=START)
    soa = scanner.Leg("Neco Williams", "goal+assist", 0.5, "Nottingham Forest@Ipswich", team="Nottingham Forest", start_time=START)
    two = scanner.Leg("Neco Williams", "goal+assist", 1.5, "Nottingham Forest@Ipswich", team="Nottingham Forest", start_time=START)
    quotes = [q("Chris Wood: 1+ goals", 0.38, "KXEPLGOAL-98DEC31IPSNFO-NFOCWOOD9-1"),
              q("Xaver Schlager: 1+ assists?", 0.17, "KXEPLAST-98DEC31IPSNFO-NFOXSCHLA21-1"),
              q("Neco Williams: score or assist?", 0.31, "KXEPLSOA-98DEC31IPSNFO-NFONWILLI3")]
    scanner.attach([goal, ast, soa, two], quotes)
    assert goal.p_over == 0.38 and goal.pick == ("Less", 0.62)
    assert ast.p_over == 0.17 and soa.p_over == 0.31
    assert two.quotes == []                       # "score or assist" is 1+ only: never a 1.5 line


def test_a_soccer_cycle_prices_from_kalshi_and_alerts_with_the_soccer_icon(tmp_path, monkeypatch):
    players = [("Chris Wood", "Nottingham Forest", "Ipswich"), ("Harry Kane", "Bayern", "Dortmund"),
               ("Lionel Messi", "Inter Miami", "Atlanta United"), ("Henry Martin", "America", "Toluca"),
               ("Erling Haaland", "Man City", "Arsenal")]

    async def quotes(series):
        assert "KXEPLGOAL" in series and "KXMLSGOAL" in series
        return [q(f"{p}: 1+ goals", 0.38, f"KXEPLGOAL-98DEC31G{i}-X{i}-1") for i, (p, _, _) in enumerate(players)]
    sends = []

    async def fake_send(text, chat_id=None):
        sends.append(text)
        return True
    monkeypatch.setattr(sentinel.scanner, "kalshi_quotes", quotes)
    monkeypatch.setattr(sentinel, "send_async", fake_send)
    csv = tmp_path / "lines_soccer.csv"
    csv.write_text("player,stat,line,game,team,odds_type,start_time,position\n" + "".join(
        f"{p},goals,0.5,{t}@{o},{t},standard,2098-12-31T19:00:00+00:00,Forward\n" for p, t, o in players))
    a = SimpleNamespace(sport="soccer", csv=str(csv), board="none", feed="none", seen=(), nudge={}, stale_min=45,
                        max_board_age=0, min_ev=0.545, flex=True, odds=None, pool=None, dedupe_min=30,
                        leg_cooldown_min=120, kelly=0.25, unit_pct=0.01, ledger=tmp_path / "l.csv", research=False)
    asyncio.run(sentinel.cycle(a, [], chat_id="1"))
    assert len(sends) == 1 and "⚽ <b>SOCCER</b>" in sends[0] and "SOCCER 5-Pick Flex" in sends[0]
    assert "LESS" in sends[0] and "62.0%" in sends[0]


def test_soccer_legs_are_left_for_a_human_to_grade():
    g = grade_slips.Grader(get=lambda url: (_ for _ in ()).throw(AssertionError("no ESPN call for soccer")))
    r = g.leg({"sport": "soccer", "player": "Chris Wood", "stat": "goals", "line": 0.5, "side": "Less", "team": "Nottingham Forest",
               "start_time": START})
    assert r["result"] == "review" and "no SOCCER grader yet" in r["note"]
