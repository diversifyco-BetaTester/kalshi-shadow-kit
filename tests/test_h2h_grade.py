"""local/h2h_grade.py: which logged prediction is graded, the join to outcomes (injected grader, no ESPN), the
call record against break-even, and calibration."""
import pytest

from local import backtest, h2h_context, h2h_grade, h2h_shadow

START = 1_000_000.0   # every game starts here
NOW = START + 10 * 3600  # 10 h later: settled (grade_slips waits 4 h)


def log_pred(log, player, side, p_over, ts=START - 3600, stat="points", line=24.5, sport="nba", odds_type="standard",
             n_eff=4.0, start=START):
    prop = {"player": player, "stat": stat, "line": line, "team": "LAL", "opp": "OKC", "start_ts": start,
            "odds_type": odds_type}
    pred = h2h_context.Prediction(player, stat, line, "OKC", side=side, p_over=p_over, mu=20.0, n_eff=n_eff)
    log.record(sport, prop, pred, 1.0, ts)


class FakeGrader:
    """The More-side result per player, as grade_slips.Grader.leg returns it."""

    def __init__(self, results):
        self.results, self.calls = results, []

    def leg(self, leg):
        self.calls.append(leg["player"])
        return {"result": self.results[leg["player"]], "actual": None, "note": ""}


def graded(tmp_path, results, first=False, fetch=True):
    rows = h2h_grade.predictions(tmp_path / "s.sqlite3", first=first)
    grader = FakeGrader(results)
    outcomes = backtest.Outcomes(tmp_path / "o.sqlite3", grader=grader, fetch=fetch)
    return h2h_grade.attach_outcomes(rows, outcomes, NOW), grader


@pytest.fixture
def log(tmp_path):
    return h2h_shadow.ShadowLog(tmp_path / "s.sqlite3")


# --------------------------------------------------------------------------- which prediction
def test_the_last_prediction_before_the_start_is_graded(tmp_path, log):
    log_pred(log, "A", "More", 0.60, ts=START - 7200)      # overnight
    log_pred(log, "A", "Less", 0.40, ts=START - 600)       # 5 pm: a teammate came back
    log_pred(log, "A", "More", 0.90, ts=START + 60)        # after the start: never graded
    [last], _ = graded(tmp_path, {"A": "hit"})
    [first], _ = graded(tmp_path, {"A": "hit"}, first=True)
    assert (last["side"], last["p_over"]) == ("Less", 0.40) and (first["side"], first["p_over"]) == ("More", 0.60)


# --------------------------------------------------------------------------- outcomes
def test_calls_are_scored_by_side_and_pushes_voids_leave_the_record(tmp_path, log):
    for name, side, p in (("over hit", "More", 0.6), ("under hit", "Less", 0.3), ("over miss", "More", 0.6),
                          ("pushed", "More", 0.6), ("dnp", "Less", 0.3), ("no call", None, 0.52)):
        log_pred(log, name, side, p)
    rows, _ = graded(tmp_path, {"over hit": "hit", "under hit": "miss", "over miss": "miss", "pushed": "push",
                                "dnp": "void", "no call": "hit"})
    by = {r["player"]: r["result"] for r in rows}
    assert by == {"over hit": "hit", "under hit": "hit", "over miss": "miss", "pushed": "push", "dnp": "void",
                  "no call": "hit"}                          # a no-call keeps the over's result, for calibration
    text = h2h_grade.report(rows)
    assert "all standard           n=3    hit 66.7%" in text and "push 1, void 1" in text


def test_recent_games_wait_and_nothing_is_fetched_without_grade(tmp_path, log):
    log_pred(log, "Late", "More", 0.6, start=NOW - 3600)    # started 1 h ago
    log_pred(log, "Done", "More", 0.6)
    rows, grader = graded(tmp_path, {"Late": "hit", "Done": "hit"}, fetch=False)
    assert {r["player"]: r["over"] for r in rows} == {"Late": "pending", "Done": "ungraded"} and grader.calls == []
    assert "run with --grade" in h2h_grade.report(rows)


def test_outcomes_are_cached_and_shared_with_the_backtest(tmp_path, log):
    log_pred(log, "A", "More", 0.6)
    _, first = graded(tmp_path, {"A": "hit"})
    rows, second = graded(tmp_path, {"A": "miss"})          # a second run: the cached result, no fetch
    assert first.calls == ["A"] and second.calls == [] and rows[0]["result"] == "hit"


# --------------------------------------------------------------------------- the call record
def test_wilson_interval():
    lo, hi = h2h_grade.wilson(60, 100)
    assert lo == pytest.approx(0.502, abs=1e-3) and hi == pytest.approx(0.691, abs=1e-3)
    assert h2h_grade.wilson(0, 0) == (0.0, 1.0)


def test_break_even_is_judged_on_the_interval_and_small_slices_are_flagged(tmp_path, log):
    for i in range(80):
        log_pred(log, f"P{i}", "More", 0.62)
    rows, _ = graded(tmp_path, {f"P{i}": "hit" if i < 60 else "miss" for i in range(80)})
    text = h2h_grade.report(rows)
    assert "n=80   hit 75.0%" in text and "above break-even" in text           # 95% CI 64%-83% > 54.2%
    few = [r for r in rows[:10]]
    assert "(insufficient data)" in h2h_grade.line_for("few", few)


def test_slices_by_sport_stat_confidence_sample_and_odds_type(tmp_path, log):
    log_pred(log, "A", "More", 0.58, n_eff=2.5)
    log_pred(log, "B", "Less", 0.20, stat="steals", line=0.5, sport="nhl", n_eff=6)
    log_pred(log, "G", "More", 0.70, odds_type="goblin")
    rows, _ = graded(tmp_path, {"A": "hit", "B": "miss", "G": "hit"})
    text = h2h_grade.report(rows)
    for want in ("NBA", "NHL", "56-60%", "75%+", "n_eff 2-3", "n_eff 5+", "NHL steals", "goblin / demon", "  goblin"):
        assert want in text
    assert "all standard           n=2 " in text                               # the goblin stays out of it


# --------------------------------------------------------------------------- calibration
def test_calibration_uses_every_prediction_and_scores_brier(tmp_path, log):
    log_pred(log, "A", "More", 0.8)
    log_pred(log, "B", None, 0.5)
    log_pred(log, "C", "Less", 0.2)
    rows, _ = graded(tmp_path, {"A": "hit", "B": "miss", "C": "miss"})
    text = h2h_grade.report(rows)
    brier = ((0.8 - 1) ** 2 + 0.5 ** 2 + 0.2 ** 2) / 3                         # 0.11
    assert f"Brier {brier:.4f} vs 0.2500" in text and "(better)" in text
    cal = text.split("CALIBRATION")[1]
    for row in ("20-35%                 1           20.0%      0.0%", "50-65%                 1           50.0%      0.0%",
                "80-100%                1           80.0%    100.0%"):
        assert row in cal
    assert "35-50%" not in cal and "0-20%" not in cal                         # empty buckets aren't printed


def test_no_shadow_log_yet(tmp_path, monkeypatch):
    import sys
    monkeypatch.setattr(sys, "argv", ["h2h_grade", "--db", str(tmp_path / "missing.sqlite3")])
    with pytest.raises(SystemExit, match="no shadow log yet"):
        h2h_grade.main()
