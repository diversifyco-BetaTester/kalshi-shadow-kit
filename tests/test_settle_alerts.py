"""Telegram settlement alerts: one per newly graded slip, bulk-collapsed, never for old slips."""
import json
from datetime import datetime, timezone

import pytest

from local import grade_slips as gs
from local import ledger

START = "2026-10-13T23:00:00+00:00"
LATER = datetime.fromisoformat(START).timestamp() + 6 * 3600


class FakeGrader:
    def __init__(self, results):
        self.results = results

    def leg(self, l):
        return {"result": self.results[l["player"]], "actual": None, "note": ""}


def leg(player):
    return {"sport": "nhl", "player": player, "stat": "points", "line": 0.5, "side": "More", "probability": 0.57,
            "source": "Kalshi", "team": "WSH", "start_time": START}


def slip(players, kind, stake=1.0):
    return {"timestamp": "2026-10-13T20:00:00+00:00", "sports": "NHL", "slip_type": kind, "net_ev_pct": 9.0,
            "stake_units": stake, "earliest_start": START, "telegram": "sent", "legs": json.dumps([leg(p) for p in players])}


class Sender:
    def __init__(self, ok=True):
        self.sent, self.ok = [], ok

    def __call__(self, text, chat_id=None):
        assert chat_id == "42"                                    # only ever the alert chat
        self.sent.append(text)
        return self.ok


def graded_ledger(tmp_path, slips, results):
    path = tmp_path / "slips.csv"
    for s in slips:
        ledger.append(s, path)
    gs.grade(path, LATER, FakeGrader(results))
    return path


SIX = [f"Player {c}" for c in "ABCDEF"]


def test_one_alert_per_settled_slip_and_never_twice(tmp_path):
    path = graded_ledger(tmp_path, [slip(SIX, "6-Pick Flex")], {p: "hit" for p in SIX})
    send = Sender()
    out = gs.notify_settled(path, LATER, send, chat="42")
    assert out == ["settlement alert sent: 1 slip(s)"] and len(send.sent) == 1
    card = send.sent[0]
    assert card.startswith("🎉 <b>Slip Settled: NHL 6-Pick Flex Hit!</b> (6/6) | <b>+24.00u</b>")
    assert "✅ A" in card and "📅 Today:" in card and "📒 All-time: 1-0-0 · +24.00u" in card
    assert gs.notify_settled(path, LATER + 60, send, chat="42") == [] and len(send.sent) == 1   # once only
    assert ledger.read(path)[0]["settle_alert"].startswith("sent ")


def test_loss_partial_and_downgraded_slips_read_correctly(tmp_path):
    results = {**{p: "hit" for p in SIX[:4]}, "Player E": "miss", "Player F": "miss"}       # 4/6: 0.4x
    path = graded_ledger(tmp_path, [slip(SIX, "6-Pick Flex", 0.5)], results)
    send = Sender()
    gs.notify_settled(path, LATER, send, chat="42")
    assert send.sent[0].startswith("⚠️ <b>Slip Settled: NHL 6-Pick Flex Loss</b> (4/6) | <b>-0.30u</b>")
    pushed = {**{p: "hit" for p in SIX[:5]}, "Player F": "push"}                              # 5/5 after a push
    path2 = graded_ledger(tmp_path / "b", [slip(SIX, "6-Pick Flex")], pushed)
    send2 = Sender()
    gs.notify_settled(path2, LATER, send2, chat="42")
    assert "6-Pick Flex → 5 Hit!</b> (5/5) | <b>+9.00u</b>" in send2.sent[0] and "➖ F" in send2.sent[0]


def test_bulk_grading_collapses_into_one_message(tmp_path):
    slips = [slip([f"P{i}{c}" for c in "ABCDE"], "5-Pick Flex") for i in range(5)]
    results = {f"P{i}{c}": ("hit" if i % 2 == 0 else "miss") for i in range(5) for c in "ABCDE"}
    path = graded_ledger(tmp_path, slips, results)
    send = Sender()
    out = gs.notify_settled(path, LATER, send, chat="42")
    assert len(send.sent) == 1 and out == ["settlement alert sent: 5 slip(s)"]
    assert send.sent[0].startswith("🧾 <b>5 slips settled</b> · 3-2-0") and send.sent[0].count("• ") == 5
    assert all(r["settle_alert"].startswith("sent ") for r in ledger.read(path))


def test_a_failed_send_is_retried_then_given_up_after_24h(tmp_path):
    path = graded_ledger(tmp_path, [slip(SIX, "6-Pick Flex")], {p: "hit" for p in SIX})
    assert gs.notify_settled(path, LATER, Sender(ok=False), chat="42") == ["settlement alert FAILED: 1 slip(s)"]
    retry = Sender()
    gs.notify_settled(path, LATER + 1800, retry, chat="42")
    assert len(retry.sent) == 1                                       # retried, then marked sent
    path2 = graded_ledger(tmp_path / "b", [slip(SIX, "6-Pick Flex")], {p: "hit" for p in SIX})
    gs.notify_settled(path2, LATER, Sender(ok=False), chat="42")
    late = Sender()
    assert gs.notify_settled(path2, LATER + 25 * 3600, late, chat="42") == [] and late.sent == []


def test_slips_graded_before_alerts_existed_are_never_announced(tmp_path):
    path = graded_ledger(tmp_path, [slip(SIX, "6-Pick Flex")], {p: "hit" for p in SIX})
    rows = ledger.read(path)
    rows[0]["settle_alert"] = ""                                      # an old row: graded, no "due" mark
    with ledger.locked(path):
        ledger.rewrite(rows, path)
    send = Sender()
    assert gs.notify_settled(path, LATER, send, chat="42") == [] and send.sent == []


def test_the_digest_run_reports_them_instead_of_alerting_twice(tmp_path):
    path = graded_ledger(tmp_path, [slip(SIX, "6-Pick Flex")], {p: "hit" for p in SIX})
    send = Sender()
    assert gs.notify_settled(path, LATER, send, chat="42", in_digest=True) == ["1 settled slip(s) reported in the digest"]
    assert send.sent == [] and ledger.read(path)[0]["settle_alert"] == "in digest"


def test_no_alert_chat_means_nothing_sent_and_still_due(tmp_path):
    path = graded_ledger(tmp_path, [slip(SIX, "6-Pick Flex")], {p: "hit" for p in SIX})
    send = Sender()
    assert gs.notify_settled(path, LATER, send, chat="") == ["1 settlement alert(s) not sent: no TELEGRAM_ALERT_CHAT_ID"]
    assert send.sent == [] and ledger.read(path)[0]["settle_alert"] == "due"   # sent once a chat is configured


def test_the_daily_line_counts_today_only(tmp_path):
    path = graded_ledger(tmp_path, [slip(SIX, "6-Pick Flex")], {p: "hit" for p in SIX})
    rows = ledger.read(path)
    line = gs.day_line(rows, LATER)
    assert "📅 Today: <b>1-0-0</b> · <b>+24.00u</b> on 1.0u · ROI <b>+2400.0%</b>" in line
    assert "📅 Today: <b>0-0-0</b> · <b>+0.00u</b> on 0.0u · ROI <b>n/a</b>" in gs.day_line(rows, LATER + 3 * 86400)


def test_test_alert_is_marked_and_touches_no_ledger():
    card = gs.test_alert_card()
    assert card.startswith("🧪 <b>TEST</b>") and "Slip Settled" in card


def test_a_quiet_notify_run_writes_no_log_but_a_failure_is_logged(tmp_path, monkeypatch, capsys):
    path = tmp_path / "slips.csv"
    future = slip(SIX, "6-Pick Flex")
    future["legs"] = json.dumps([{**leg(p), "start_time": "2098-12-31T17:00:00+00:00"} for p in SIX])
    ledger.append(future, path)                                       # not settled yet: nothing to grade
    log = tmp_path / "grader.log"
    monkeypatch.setattr("sys.argv", ["grade_slips", "--ledger", str(path), "--log", str(log), "--notify"])
    monkeypatch.setattr(gs, "alert_chat", lambda: "42")
    monkeypatch.setattr(gs, "Grader", lambda: FakeGrader({}))
    gs.main()
    assert not log.exists() and capsys.readouterr().out == ""
    ledger.append(slip(SIX, "6-Pick Flex"), path)                     # settled, but ESPN parsing blows up
    gs.main()
    assert "grading failed" in log.read_text()
