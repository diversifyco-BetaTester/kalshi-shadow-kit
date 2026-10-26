import json
import threading
import time
import urllib.error

import pytest

from ops import telegram_listener as tl

ME, STRANGER = "1000001", "999"  # dummy chat ids: the real one lives only in .env
NOW = 1_800_000_000.0


def update(text, chat=ME, date=NOW, uid=1):
    return {"update_id": uid, "message": {"chat": {"id": int(chat)}, "text": text, "date": date}}


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(tl, "POOL", tmp_path / "ev_pool.json")
    monkeypatch.setattr(tl, "ODDS_CACHE", tmp_path / "odds.json")
    monkeypatch.setattr(tl, "HEARTBEAT", tmp_path / ".heartbeat_{sport}.json")
    monkeypatch.setattr(tl, "LINES", tmp_path / "lines_{sport}.csv")
    monkeypatch.setattr(tl, "DOWNLOADS", tmp_path)
    monkeypatch.setattr(tl, "OFFSET", tmp_path / "offset")
    monkeypatch.setattr(tl, "sentinel_pids", lambda: {"nhl": [11], "mlb": [], "nfl": [12, 13], "nba": [14], "cfb": []})
    return tmp_path


class NoRefresh:
    def start(self):
        raise AssertionError("refresh must not run")


def test_only_the_whitelisted_chat_is_obeyed(state, capsys):
    assert tl.handle(update("/status", chat=STRANGER), ME, NoRefresh(), NOW) is None
    assert tl.handle(update("/refresh", chat=STRANGER), ME, NoRefresh(), NOW) is None
    assert "ignored a message from chat 999" in capsys.readouterr().out
    assert tl.handle(update("/status"), ME, NoRefresh(), NOW).startswith("<b>Pipeline status</b>")


def test_stale_and_plain_messages_are_dropped_and_bot_suffix_is_accepted(state):
    assert tl.handle(update("/refresh", date=NOW - tl.STALE_S - 1), ME, NoRefresh(), NOW) is None
    assert tl.handle(update("hello"), ME, NoRefresh(), NOW) is None
    assert tl.handle(update("/pool@my_alert_bot"), ME, NoRefresh(), NOW).startswith("<b>Pool</b>")
    assert "Unknown command /rm" in tl.handle(update("/rm -rf"), ME, NoRefresh(), NOW)


def test_status_reports_sentinels_boards_and_credits(state):
    (state / ".heartbeat_nhl.json").write_text(json.dumps({"ts": NOW - 30, "state": "ok", "legs": 59, "priced": 21,
                                                            "good": 3, "slips": 1}))
    (state / ".heartbeat_nba.json").write_text(json.dumps({"ts": NOW - 900, "state": "ok"}))
    for sport, age in (("nhl", 120), ("nfl", 600)):
        p = state / f"lines_{sport}.csv"
        p.write_text("x")
        import os
        os.utime(p, (NOW - age, NOW - age))
    board = state / "nhl_board.json"
    board.write_text("{}")
    import os
    os.utime(board, (NOW - 300, NOW - 300))
    (state / "odds.json").write_text(json.dumps({"usage": {"date": "2026-10-16", "used": 48, "cap": 55, "remaining": 367}}))
    s = tl.status(NOW)
    assert "• <b>NHL</b>: ✅ 21/59 priced, 3 ≥ min, 1 slip(s) · board 2 min ago" in s
    assert "• <b>MLB</b>: ⛔ not running · no board yet" in s
    assert "• <b>NFL</b> (2 copies!): ⚠️ running, last cycle never · board 10 min ago" in s
    assert "• <b>NBA</b>: ⚠️ running, last cycle 15 min ago" in s
    assert "Latest board dump:" in s and "(5 min ago)" in s
    assert "Odds API: 367 credits left this month · today 48/55" in s


def test_pool_lists_live_legs_only(state):
    leg = lambda p, player, **kw: {"sport": "nhl", "player": player, "stat": "shots on goal", "line": 1.5, "side": "More",
                                   "p": p, "tag": "Consensus: PIN, DK", "updated_ts": NOW - 30, "expires_ts": NOW + 3600, **kw}
    (state / "ev_pool.json").write_text(json.dumps({"legs": [
        leg(0.56, "B <Two>"), leg(0.58, "A One"), leg(0.60, "Expired", expires_ts=NOW - 1),
        leg(0.61, "Stale Writer", updated_ts=NOW - 400)], "sent_legs": {"k": NOW - 60}}))
    p = tl.pool(NOW)
    lines = p.splitlines()
    assert lines[0] == "<b>Pool</b>: 2 live legs (NHL 2) · 1 cooling down"
    assert "A One" in lines[1] and "B &lt;Two&gt;" in lines[2] and "Expired" not in p and "Stale Writer" not in p
    assert "<b>More</b> 58.0% (Consensus: PIN, DK)" in lines[1]
    (state / "ev_pool.json").write_text(json.dumps({"legs": []}))
    assert tl.pool(NOW).startswith("<b>Pool</b>: empty")


def test_refresh_runs_one_at_a_time_and_reports_back():
    replies, gate = [], threading.Event()

    def slow():
        gate.wait(2)
        return "10846 props {'NFL': 6116} | Boards synced for NFL."
    r = tl.Refresher(replies.append, ["NFL", "NHL"], runner=slow)
    assert r.start().startswith("🔄 Refreshing NFL NHL")
    assert "already running" in r.start()
    gate.set()
    r.thread.join(2)
    assert replies == ["🔄 Refresh done: 10846 props {&#x27;NFL&#x27;: 6116} | Boards synced for NFL."]


def test_poll_loop_advances_and_saves_the_offset(state):
    sent = []
    batches = [[update("/help", uid=41), update("/status", chat=STRANGER, uid=42)]]
    seen_offsets = []

    def fetch(token, offset):
        seen_offsets.append(offset)
        return batches.pop(0) if batches else []
    tl.run("t", ME, ["NFL"], fetch=fetch, reply=sent.append, once=True)
    assert sent == [tl.HELP] and (state / "offset").read_text() == "43"
    tl.run("t", ME, ["NFL"], fetch=fetch, reply=sent.append, once=True)
    assert seen_offsets == [None, 43] and len(sent) == 1


def test_conflict_with_another_poller_backs_off_instead_of_crashing(state, monkeypatch, capsys):
    monkeypatch.setattr(tl.time, "sleep", lambda s: None)

    def fetch(token, offset):
        raise urllib.error.HTTPError("u", 409, "Conflict", {}, None)
    tl.run("t", ME, ["NFL"], fetch=fetch, reply=lambda t: None, once=True)
    assert "another listener is polling this bot" in capsys.readouterr().out


# ---------------------------------------------------------------- /pnl, /summary
def graded_ledger(path):
    from local import ledger
    legs = [{"sport": s, "player": p, "stat": "points", "line": 0.5, "side": "More", "probability": 0.56, "team": "X",
             "start_time": "2026-10-14T23:00:00+00:00"} for s, p in (("nhl", "A"), ("nhl", "B"), ("mlb", "C"), ("nfl", "D"), ("nfl", "E"))]
    res = ["hit", "hit", "hit", "hit", "miss"]
    rows = [{"timestamp": "2026-10-14T20:00:00+00:00", "sports": "MLB+NFL+NHL", "slip_type": "5-Pick Flex", "net_ev_pct": 9,
             "stake_units": 1.5, "earliest_start": "", "telegram": "sent", "legs": legs, "status": "graded", "hits": 4,
             "misses": 1, "pushes": 0, "payout_multiplier": 2.0, "net_units": 1.5, "graded_at": "2026-10-15T13:00:00+00:00",
             "leg_results": [{**l, "result": r} for l, r in zip(legs, res)]},
            {"timestamp": "2026-10-15T20:00:00+00:00", "sports": "NHL", "slip_type": "6-Pick Flex", "net_ev_pct": 9,
             "stake_units": 0.5, "earliest_start": "", "telegram": "sent", "legs": legs, "status": "", "leg_results": []}]
    with ledger.locked(path):
        ledger.rewrite(rows, path)


@pytest.mark.parametrize("cmd", ["/pnl", "/summary", "/PnL@my_alert_bot"])
def test_pnl_and_summary_reply_with_the_card(state, monkeypatch, cmd):
    monkeypatch.setattr(tl, "LEDGER", state / "slips.csv")
    graded_ledger(state / "slips.csv")
    card = tl.handle(update(cmd), ME, NoRefresh(), NOW)
    lines = card.splitlines()
    assert lines[0] == "📒 <b>P&amp;L, all time</b>"
    assert lines[1] == "🟢 <b>+1.50u</b> on 1.5u · ROI <b>+100.0%</b>"
    assert lines[2] == "🧾 Record <b>1-0-0</b> (1 slip, 100% won)"
    assert "  NHL 2/2 · 100%" in lines and "  MLB 1/1 · 100%" in lines and "  NFL 1/2 · 50%" in lines
    assert lines[-1] == "⏳ 1 open · 0 pending · 0 need review" and len(card) < 600


def test_pnl_is_whitelisted_and_survives_an_empty_or_missing_ledger(state, monkeypatch):
    monkeypatch.setattr(tl, "LEDGER", state / "none.csv")
    assert tl.handle(update("/pnl", chat=STRANGER), ME, NoRefresh(), NOW) is None
    assert tl.handle(update("/pnl"), ME, NoRefresh(), NOW).startswith("📒 <b>P&amp;L</b>\nNo graded slips yet (0 logged)")
    import builtins
    real_import = builtins.__import__

    def no_local(name, *a, **k):
        if name == "local" and a and a[2] and "grade_slips" in a[2]:
            raise ImportError("No module named 'local'")
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", no_local)
    assert tl.pnl().startswith("P&amp;L unavailable: local/grade_slips.py not found")


def test_help_lists_the_new_commands(state):
    assert "/pnl (or /summary)" in tl.handle(update("/help"), ME, NoRefresh(), NOW)
