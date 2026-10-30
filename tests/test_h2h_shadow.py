"""local/h2h_shadow.py and its sentinel hook: every new NBA/NHL board's props projected in the background and logged
on change. Never alerted. No network: the predictor is injected."""
import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest

from local import h2h_context, h2h_shadow, sentinel

NOW = 4_000_000_000.0  # 2096: every board start below is in the future unless set otherwise
START = "2098-12-31T19:00:00-05:00"


def row(player="Star Guy", stat="points", line=24.5, game="LAL v OKC", team="LAL", start=START, odds_type="standard"):
    return {"player": player, "stat": stat, "line": str(line), "game": game, "team": team, "start_time": start,
            "odds_type": odds_type}


def pred(prop_player="Star Guy", side="More", p=0.6, mu=26.0, notes=()):
    return h2h_context.Prediction(prop_player, "points", 24.5, "OKC", side=side, p_over=p, mu=mu, rate=0.7,
                                  exp_minutes=36.0, n_h2h=5, n_eff=4.0, notes=list(notes),
                                  games=[{"date": "2026-01-01", "minutes": 36, "stat": 27, "weight": 1.0, "context": []}])


def predictor(table):
    """A fake h2h_context.run: {player: Prediction or Exception}; records the calls."""
    calls = []

    async def run(player, sport, stat, line, team, opp):
        calls.append((player, sport, stat, line, team, opp))
        got = table[player]
        if isinstance(got, Exception):
            raise got
        return got
    run.calls = calls
    return run


def rows_in(log):
    return log.db.execute("SELECT player, side, p_over, error FROM h2h_predictions ORDER BY id").fetchall()


# --------------------------------------------------------------------------- the board's props
@pytest.mark.parametrize("game, team, opp", [("LAL v OKC", "LAL", "OKC"), ("BOS @ CAR", "CAR", "BOS"),
                                             ("NYK vs DET", "NY", "DET"), ("LAL v OKC", "XXX", None), ("LAL", "LAL", None)])
def test_the_opponent_comes_from_the_game(game, team, opp):
    assert h2h_shadow.opponent_of(game, team) == opp


def test_board_props_keep_upcoming_nba_nhl_games_once_each():
    rows = [row(), row(),                                                   # a duplicate
            row(player="Started", start="2020-01-01T19:00:00-05:00"),      # already played
            row(player="No Time", start=""), row(player="Bad Game", game="LAL"),
            row(player="Goblin", odds_type="goblin")]
    props = h2h_shadow.board_props(rows, "nba", NOW)
    assert [(p["player"], p["opp"], p["odds_type"]) for p in props] == [("Star Guy", "OKC", "standard"),
                                                                         ("Goblin", "OKC", "goblin")]
    assert h2h_shadow.board_props(rows, "nfl", NOW) == []                   # no minutes data: never projected


# --------------------------------------------------------------------------- logging on change
def test_every_prop_is_projected_and_logged_with_its_context(tmp_path):
    log = h2h_shadow.ShadowLog(tmp_path / "s.sqlite3")
    run = predictor({"Star Guy": pred(), "Role Guy": pred("Role Guy", side=None, p=0.52)})
    counts = asyncio.run(h2h_shadow.run_board("nba", [row(), row(player="Role Guy", team="OKC")], 111.0, log, NOW,
                                              predictor=run))
    assert counts == {"props": 2, "calls": 1, "More": 1, "Less": 0, "logged": 2, "errors": 0}
    assert sorted(run.calls) == [("Role Guy", "nba", "points", 24.5, "OKC", "LAL"), ("Star Guy", "nba", "points", 24.5, "LAL", "OKC")]
    games, board_ts, opp = log.db.execute("SELECT games_json, board_ts, opp FROM h2h_predictions WHERE player='Star Guy'").fetchone()
    assert json.loads(games)[0]["weight"] == 1.0 and board_ts == 111.0 and opp == "OKC"


def test_an_unchanged_prediction_on_the_next_dump_is_not_logged_again(tmp_path):
    log = h2h_shadow.ShadowLog(tmp_path / "s.sqlite3")
    for board_ts, p in ((1.0, 0.600), (2.0, 0.603), (3.0, 0.62)):          # +0.3 pt: same; +2 pt: new row
        asyncio.run(h2h_shadow.run_board("nba", [row()], board_ts, log, NOW, predictor=predictor({"Star Guy": pred(p=p)})))
    assert [r[2] for r in rows_in(log)] == [0.6, 0.62]


def test_a_context_change_is_logged_even_at_the_same_probability(tmp_path):
    log = h2h_shadow.ShadowLog(tmp_path / "s.sqlite3")
    asyncio.run(h2h_shadow.run_board("nba", [row()], 1.0, log, NOW, predictor=predictor({"Star Guy": pred()})))
    later = pred(notes=["usage without co star: x1.050"])                   # 5 pm: a teammate ruled out
    asyncio.run(h2h_shadow.run_board("nba", [row()], 2.0, log, NOW, predictor=predictor({"Star Guy": later})))
    assert len(rows_in(log)) == 2


def test_one_failing_prop_is_logged_as_an_error_and_the_rest_still_run(tmp_path):
    log = h2h_shadow.ShadowLog(tmp_path / "s.sqlite3")
    run = predictor({"Star Guy": TimeoutError("espn"), "Role Guy": pred("Role Guy")})
    counts = asyncio.run(h2h_shadow.run_board("nba", [row(), row(player="Role Guy")], 1.0, log, NOW, predictor=run))
    assert counts["errors"] == 1 and counts["logged"] == 2
    by = {r[0]: r for r in rows_in(log)}
    assert by["Star Guy"][1] is None and by["Star Guy"][3].startswith("TimeoutError") and by["Role Guy"][1] == "More"


def test_the_nba_and_nhl_sentinels_can_share_the_log(tmp_path):
    path = tmp_path / "s.sqlite3"
    nba, nhl = h2h_shadow.ShadowLog(path), h2h_shadow.ShadowLog(path)
    asyncio.run(h2h_shadow.run_board("nba", [row()], 1.0, nba, NOW, predictor=predictor({"Star Guy": pred()})))
    asyncio.run(h2h_shadow.run_board("nhl", [row(game="BOS @ CAR", team="CAR")], 1.0, nhl, NOW,
                                     predictor=predictor({"Star Guy": pred()})))
    assert sqlite3.connect(path).execute("SELECT sport FROM h2h_predictions ORDER BY id").fetchall() == [("nba",), ("nhl",)]
    assert "NBA  More" in h2h_shadow.report(path) and "NHL  More" in h2h_shadow.report(path)


# --------------------------------------------------------------------------- one board at a time
def test_a_board_landing_mid_run_waits_and_only_the_newest_runs(tmp_path):
    seen, gate = [], asyncio.Event

    async def go():
        release = gate()

        async def slow_run(sport, rows, board_ts, log):
            seen.append(board_ts)
            if board_ts == 1.0:
                await release.wait()
            return {"props": 0, "calls": 0, "More": 0, "Less": 0, "logged": 0, "errors": 0}
        runner = h2h_shadow.BoardRunner("nba", h2h_shadow.ShadowLog(tmp_path / "s.sqlite3"), run=slow_run)
        assert runner.submit([], 1.0) is True
        await asyncio.sleep(0)
        assert runner.submit([], 2.0) is False and runner.submit([], 3.0) is False   # 2.0 replaced by 3.0
        release.set()
        await runner.task
    asyncio.run(go())
    assert seen == [1.0, 3.0]


def test_a_crashing_run_never_escapes(tmp_path, capsys):
    async def boom(sport, rows, board_ts, log):
        raise RuntimeError("bad board")

    async def go():
        runner = h2h_shadow.BoardRunner("nhl", h2h_shadow.ShadowLog(tmp_path / "s.sqlite3"), run=boom)
        runner.submit([], 1.0)
        await runner.task
    asyncio.run(go())
    assert "H2H shadow (NHL) failed: RuntimeError" in capsys.readouterr().out


# --------------------------------------------------------------------------- the sentinel hook
def test_the_sentinel_hook_submits_new_boards_and_tolerates_failure(capsys):
    submitted = []
    ok = SimpleNamespace(h2h=SimpleNamespace(submit=lambda rows, ts: submitted.append(ts) or True))
    assert sentinel.h2h_on_board(ok, [row()], 5.0) is True and submitted == [5.0]
    assert sentinel.h2h_on_board(SimpleNamespace(), [row()], 6.0) is False         # NFL / --no-h2h-shadow: no runner

    def broken(rows, ts):
        raise RuntimeError("no loop")
    assert sentinel.h2h_on_board(SimpleNamespace(h2h=SimpleNamespace(submit=broken)), [row()], 7.0) is False
    assert "H2H shadow not started" in capsys.readouterr().out


def test_the_shadow_model_never_alerts(tmp_path, monkeypatch):
    """A whole board through the runner: rows in the log, nothing sent anywhere."""
    sent = []
    monkeypatch.setattr(sentinel, "send_async", lambda *a, **k: sent.append(a))
    log = h2h_shadow.ShadowLog(tmp_path / "s.sqlite3")
    asyncio.run(h2h_shadow.run_board("nba", [row()], 1.0, log, NOW, predictor=predictor({"Star Guy": pred()})))
    assert len(rows_in(log)) == 1 and sent == []


# --------------------------------------------------------------------------- model versions
OLD_SCHEMA = h2h_shadow.SCHEMA  # the same table, created before model_version existed (no such column)


def old_log(path):
    db = sqlite3.connect(path)
    db.executescript(OLD_SCHEMA)
    db.execute("INSERT INTO h2h_predictions (ts, sport, player, stat, line, start_ts, side, p_over) "
               "VALUES (1, 'nba', 'Old Row', 'points', 24.5, 9e9, 'Less', 0.2)")
    db.commit()
    db.close()


def test_an_old_log_gains_the_column_and_its_rows_count_as_v1(tmp_path):
    path = tmp_path / "s.sqlite3"
    old_log(path)
    assert h2h_shadow.version_sql(sqlite3.connect(path)) == "'v1'"              # readable before any upgrade
    log = h2h_shadow.ShadowLog(path)                                           # a sentinel on the new code opens it
    asyncio.run(h2h_shadow.run_board("nba", [row()], 1.0, log, NOW, predictor=predictor({"Star Guy": pred()})))
    versions = dict(sqlite3.connect(path).execute(
        f"SELECT player, {h2h_shadow.version_sql(log.db)} FROM h2h_predictions").fetchall())
    assert versions == {"Old Row": "v1", "Star Guy": h2h_context.MODEL_VERSION}
    text = h2h_shadow.report(path)
    assert "v1   NBA  Less" in text and f"{h2h_context.MODEL_VERSION:4} NBA  More" in text and "(current)" in text


def test_a_new_model_version_always_logs_a_fresh_row(tmp_path, monkeypatch):
    log = h2h_shadow.ShadowLog(tmp_path / "s.sqlite3")
    same = predictor({"Star Guy": pred()})
    monkeypatch.setattr(h2h_context, "MODEL_VERSION", "v1")
    asyncio.run(h2h_shadow.run_board("nba", [row()], 1.0, log, NOW, predictor=same))
    monkeypatch.setattr(h2h_context, "MODEL_VERSION", "v2")
    asyncio.run(h2h_shadow.run_board("nba", [row()], 2.0, log, NOW, predictor=same))   # identical prediction
    asyncio.run(h2h_shadow.run_board("nba", [row()], 3.0, log, NOW, predictor=same))   # same version: no row
    assert log.db.execute("SELECT model_version FROM h2h_predictions ORDER BY id").fetchall() == [("v1",), ("v2",)]
