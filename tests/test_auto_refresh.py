import json
import subprocess
from datetime import datetime

import pytest

from ops import auto_refresh as ar

# the bookmarklet's code (scripts/, never published) is injected into the page: a clone without it skips these
needs_bookmarklet = pytest.mark.skipif(not ar.DUMP_JS.exists(), reason="no bookmarklet (scripts/) on this machine")


def test_hour_window():
    d = lambda h, m=0: datetime(2026, 10, 15, h, m)
    assert ar.next_run(d(13, 5), 12, 21) == d(13, 5)                      # inside: now
    assert ar.next_run(d(9), 12, 21) == d(12)                             # morning: noon today
    assert ar.next_run(d(21, 30), 12, 21) == datetime(2026, 10, 16, 12)   # evening: noon tomorrow


class FakeBoard:
    """osa_raw stand-in for one PrizePicks tab. A reload shows the featured sport (NFL) only; clicking a
    sport tab adds that league's projections to the store, unless it is listed in `slow` (first click
    loads nothing) or absent from `tabs_shown`."""
    def __init__(self, selected="NHL", slow=(), tabs_shown=("NFL", "NHL", "MLB", "NBA"), props_ok=True):
        self.selected, self.loaded, self.slow, self.tabs_shown = selected, {selected}, set(slow), tabs_shown
        self.props_ok, self.calls = props_ok, []

    def __call__(self, *args):
        return subprocess.CompletedProcess(["osascript"], 0, self.reply(*args), "")

    def reply(self, cmd, *rest):
        self.calls.append((cmd,) + tuple(rest[2:3]) if cmd in ("click", "selected") else (cmd,))
        if cmd == "list":
            return json.dumps({"tabs": [{"w": 0, "t": 5, "title": "PrizePicks", "url": "https://app.prizepicks.com/board"}]})
        if cmd == "reload":
            self.selected, self.loaded = "NFL", {"NFL"}
            return "{}"
        if cmd == "loading":
            return json.dumps({"loading": False})
        if cmd in ("click", "selected"):
            want = rest[2] if len(rest) > 2 else ""
            found = want in self.tabs_shown
            did = cmd == "click" and found and self.selected != want
            if did:
                self.selected = want
                if want in self.slow:
                    self.slow.discard(want)
                else:
                    self.loaded.add(want)
            return json.dumps({"nav": True, "selected": self.selected, "found": found, "clicked": did})
        assert cmd == "dump"
        props = [{"projection_id": f"{lg}{i}", "league": lg} for lg in sorted(self.loaded) for i in range(3)]
        return json.dumps({"method": "redux", "captured_at": "t", "url": "u", "props": props if self.props_ok else []})


@pytest.fixture
def fast(monkeypatch, tmp_path):
    monkeypatch.setattr(ar, "DOWNLOADS", tmp_path)
    monkeypatch.setattr(ar.time, "sleep", lambda s: None)
    return tmp_path


@needs_bookmarklet
def test_one_tab_clicks_each_league_dumps_once_and_restores_the_view(fast, monkeypatch):
    tab = FakeBoard(selected="NHL")
    monkeypatch.setattr(ar, "osa_raw", tab)
    report = ar.refresh(reload=True, wanted=("NFL", "NHL", "MLB"))
    assert "9 props {'MLB': 3, 'NFL': 3, 'NHL': 3}" in report                   # all three, one board
    clicks = [c[1] for c in tab.calls if c[0] == "click"]
    assert clicks == ["NFL", "NHL", "MLB", "NHL"]                              # sequence, then back to NHL
    assert [c[0] for c in tab.calls].count("dump") == 1 and tab.selected == "NHL"
    board = json.loads((fast / "nhl_board.json").read_text())
    assert ar.leagues(board) == {"MLB": 3, "NFL": 3, "NHL": 3}


@needs_bookmarklet
def test_delay_between_clicks(monkeypatch, tmp_path):
    slept = []
    monkeypatch.setattr(ar, "DOWNLOADS", tmp_path)
    monkeypatch.setattr(ar.time, "sleep", slept.append)
    monkeypatch.setattr(ar, "osa_raw", FakeBoard(selected="NFL"))
    ar.refresh(reload=False, wanted=("NFL", "NHL", "MLB"))
    assert slept.count(ar.CLICK_DELAY_S) == 3 and 2 <= ar.CLICK_DELAY_S <= 3


@needs_bookmarklet
def test_slow_league_gets_one_retry(fast, monkeypatch):
    tab = FakeBoard(selected="NFL", slow={"MLB"})
    monkeypatch.setattr(ar, "osa_raw", tab)
    report = ar.refresh(reload=False, wanted=("NFL", "NHL", "MLB"))
    assert "'MLB': 3" in report and [c[0] for c in tab.calls].count("dump") == 2


@needs_bookmarklet
def test_missing_league_tab_is_reported_not_fatal(fast, monkeypatch):
    monkeypatch.setattr(ar, "osa_raw", FakeBoard(selected="NFL", tabs_shown=("NFL", "NHL")))
    report = ar.refresh(reload=False, wanted=("NFL", "NHL", "MLB"))
    assert "no MLB tab on the board" in report and "'NHL': 3" in report


@needs_bookmarklet
def test_nothing_written_when_the_tab_has_no_projections_or_chrome_is_closed(fast, monkeypatch):
    monkeypatch.setattr(ar, "osa_raw", FakeBoard(props_ok=False))
    report = ar.refresh(reload=False)
    assert "boards unchanged" in report and list(fast.iterdir()) == []
    monkeypatch.setattr(ar, "osa_raw", lambda *a: subprocess.CompletedProcess([], 0, json.dumps({"tabs": []}), ""))
    assert ar.refresh() == "no PrizePicks tab open in Chrome: nothing refreshed"


@needs_bookmarklet
def test_bookmarklet_returns_json_only_when_asked():
    src = ar.DUMP_JS.read_text()
    assert "window.__boardJsonOnly === true" in src and 'handoff.id = "__board_json__"' in src
    assert 'handoff.type = "application/json"' in src and "return json;" in src         # inert hand-off element
    assert "document.createElement('script')" in ar.JXA and "__board_json__" in ar.JXA   # injected, read back
    assert "nav.league-navigation button[role=tab]" in ar.JXA                            # the sport tabs


@needs_bookmarklet
def test_dump_failure_details_reach_the_report(fast, monkeypatch):
    tab = FakeBoard()

    def chrome(*args):
        if args[0] == "dump":
            return subprocess.CompletedProcess([], 1, "", "execution error: Executing JavaScript through AppleScript "
                                                          "is turned off. (12)")
        return tab(*args)
    monkeypatch.setattr(ar, "osa_raw", chrome)
    report = ar.refresh(reload=False)
    assert "osascript exit 1" in report and "turned off" in report and "boards unchanged" in report


def test_describe_reports_size_preview_and_parse_result():
    assert "<<undefined>>" in ar.describe("<<undefined>>") and "not JSON" in ar.describe("<<undefined>>")
    big = json.dumps({"method": "redux", "props": [{"projection_id": str(i)} for i in range(3)], "url": "u"})
    d = ar.describe(big)
    assert f"{len(big):,} chars" in d and "props=3" in d and "method='redux'" in d


@needs_bookmarklet
def test_diagnose_is_read_only(monkeypatch, capsys):
    calls = []

    def chrome(*args):
        calls.append(args[0])
        if args[0] == "list":
            return subprocess.CompletedProcess([], 0, json.dumps({"tabs": [{"w": 0, "t": 0, "title": "PrizePicks", "url": "u"}]}), "")
        return subprocess.CompletedProcess([], 0, json.dumps({"props": [], "method": "dom-text", "board_text": "x"}), "")
    monkeypatch.setattr(ar, "osa_raw", chrome)
    ar.diagnose()
    assert calls == ["list", "probe", "dump"] and "props=0" in capsys.readouterr().out    # never "reload"
