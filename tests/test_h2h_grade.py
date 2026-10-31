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


# --------------------------------------------------------------------------- model versions
def test_only_the_current_version_is_graded_by_default(tmp_path, log, monkeypatch):
    monkeypatch.setattr(h2h_context, "MODEL_VERSION", "v1")
    log_pred(log, "A", "More", 0.95, ts=START - 600)                          # the old model, logged later
    monkeypatch.setattr(h2h_context, "MODEL_VERSION", "v2")
    log_pred(log, "A", "Less", 0.40, ts=START - 7200)                         # the current model, earlier
    log_pred(log, "B", "More", 0.60)
    path = tmp_path / "s.sqlite3"
    current = h2h_grade.predictions(path, version="v2")
    assert [(r["player"], r["side"], r["version"]) for r in current] == [("A", "Less", "v2"), ("B", "More", "v2")]
    assert [(r["player"], r["side"]) for r in h2h_grade.predictions(path, version="v1")] == [("A", "More")]
    both = h2h_grade.predictions(path, version=None)                         # each version's own last row
    assert sorted((r["player"], r["version"]) for r in both) == [("A", "v1"), ("A", "v2"), ("B", "v2")]


def test_all_versions_adds_a_by_version_slice(tmp_path, log, monkeypatch):
    for version, players in (("v1", ("A", "B")), ("v2", ("C",))):
        monkeypatch.setattr(h2h_context, "MODEL_VERSION", version)
        for p in players:
            log_pred(log, p, "More", 0.6)
    rows = h2h_grade.predictions(tmp_path / "s.sqlite3", version=None)
    outcomes = backtest.Outcomes(tmp_path / "o.sqlite3", grader=FakeGrader({"A": "hit", "B": "miss", "C": "hit"}), fetch=True)
    text = h2h_grade.report(h2h_grade.attach_outcomes(rows, outcomes, NOW), version=None)
    assert "all model versions: v1, v2" in text and "by model version:" in text
    assert "  v1                     n=2    hit 50.0%" in text and "  v2                     n=1    hit 100.0%" in text
    monkeypatch.setattr(h2h_context, "MODEL_VERSION", "v2")
    one = h2h_grade.report(h2h_grade.attach_outcomes(h2h_grade.predictions(tmp_path / "s.sqlite3"), outcomes, NOW))
    assert "model v2 (current)" in one and "by model version" not in one


def test_a_log_never_upgraded_still_grades_as_v1(tmp_path):
    import sqlite3
    path = tmp_path / "s.sqlite3"
    db = sqlite3.connect(path)
    db.executescript(h2h_shadow.SCHEMA)                                       # no model_version column
    db.execute("INSERT INTO h2h_predictions (ts, sport, player, stat, line, odds_type, team, start_ts, side, p_over, n_eff) "
               "VALUES (?, 'nba', 'Old', 'points', 24.5, 'standard', 'LAL', ?, 'More', 0.6, 3)", (START - 60, START))
    db.commit()
    db.close()
    assert h2h_grade.predictions(path) == []                                  # the current version has none
    [old] = h2h_grade.predictions(path, version="v1")
    assert old["player"] == "Old" and old["version"] == "v1"


# --------------------------------------------------------------------------- the one-week arms test
def arm_rows(n_games, slates, v3, line, anchor, over_rate=0.5, odds_type="standard"):
    """Graded rows: n_games per slate, 4 props each; each arm's P(over) a function of the outcome y."""
    rows = []
    for s in range(slates):
        for gi in range(n_games):
            for k in range(4):
                y = (gi * 4 + k) % 100 < over_rate * 100
                start = START + s * 86400 + gi * 60
                rows.append({"sport": "nba", "player": f"P{s}-{gi}-{k}", "team": f"T{gi}", "opp": f"O{gi}",
                             "start_ts": start, "odds_type": odds_type, "over": "hit" if y else "miss",
                             "p_over": v3(y), "p_over_line": line(y), "p_over_anchor": anchor(y)})
    return rows


def test_arms_need_all_three_probabilities_and_standard_props():
    rows = arm_rows(3, 1, lambda y: .6, lambda y: .5, lambda y: .5)
    rows[0]["p_over_line"] = None                                                 # logged before the arms existed
    for r in rows[1:3]:
        r["odds_type"] = "goblin"
    text = "\n".join(h2h_grade.arms_section(rows))
    assert f"{len(rows) - 3} graded standard props" in text
    assert "no graded props with all three arms" in "\n".join(h2h_grade.arms_section(rows[:1]))


def test_paired_brier_and_its_game_bootstrap():
    rows = arm_rows(10, 1, lambda y: .7 if y else .3, lambda y: .5, lambda y: .5)  # v3 always right
    d, lo, hi = h2h_grade.paired_brier(rows, "p_over", "p_over_line")
    assert d == pytest.approx(0.09 - 0.25) and lo == pytest.approx(d) and hi == pytest.approx(d)  # no spread
    again = h2h_grade.paired_brier(rows, "p_over", "p_over_line")
    assert again == (d, lo, hi)                                                   # seeded: reproducible


def test_the_bootstrap_resamples_games_not_props():
    # 2 games of 40 props: one where v3 is right, one where it is wrong. By prop the evidence looks huge (80
    # props); by game it is 2 observations and the interval must straddle 0.
    rows = []
    for gi, right in ((0, True), (1, False)):
        for k in range(40):
            y = k % 2 == 0
            p = (.8 if y else .2) if right else (.2 if y else .8)
            rows.append({"sport": "nba", "player": f"P{gi}{k}", "team": f"T{gi}", "opp": f"O{gi}", "start_ts": START,
                         "odds_type": "standard", "over": "hit" if y else "miss", "p_over": p, "p_over_line": .5,
                         "p_over_anchor": .5})
    d, lo, hi = h2h_grade.paired_brier(rows, "p_over", "p_over_line")
    assert lo < 0 < hi


def test_the_decision_waits_for_seven_slates_and_300_props():
    worse = arm_rows(5, 3, lambda y: .3 if y else .7, lambda y: .5, lambda y: .5)  # 60 props, 3 slates
    text = "\n".join(h2h_grade.arms_section(worse))
    assert "DECISION: not yet: 3/7 slates, 60/300 props" in text and "DROP H2H" in text


@pytest.mark.parametrize("v3, anchor, decision", [
    (lambda y: .65 if y else .35, lambda y: .5, "KEEP H2H as the primary factor"),
    (lambda y: .5, lambda y: .6 if y else .4, "DROP H2H as the primary factor; use the anchored arm"),
    (lambda y: .35 if y else .65, lambda y: .5, "DROP H2H as the primary factor; use the line alone"),
])
def test_the_pre_registered_rule(v3, anchor, decision):
    rows = arm_rows(11, 7, v3, lambda y: .5, anchor)                              # 7 slates, 308 props
    text = "\n".join(h2h_grade.arms_section(rows))
    assert f"DECISION (rule fixed 2026-10-31): {decision}" in text


def test_slates_are_us_eastern_dates_not_utc():
    late = START - START % 86400 + 86400 + 2 * 3600                              # 02:00 UTC = the evening before ET
    early = late - 6 * 3600                                                      # 20:00 UTC the same ET day
    rows = [{"start_ts": t} for t in (early, late)]
    assert h2h_grade.slate(rows[0]) == h2h_grade.slate(rows[1])


def test_the_engine_logs_both_baseline_arms(tmp_path):
    from tests.test_h2h_context import RECENT, box, line as gl, ME
    games = RECENT + [gl(f"h{i}", f"2026-10-1{i}T23:00Z", 30) for i in range(5)]
    p = h2h_context.predict(ME, "nba", "points", 20.5, "LAL", "OKC", games, {f"h{i}": box(f"h{i}", {ME: 30}) for i in range(5)})
    raw = p.rate * p.usage * p.exp_minutes
    assert p.p_over_line == pytest.approx(h2h_context.arm_p("nba", 20.5, 20.5))
    assert p.p_over_anchor == pytest.approx(h2h_context.arm_p("nba", 20.5 + h2h_context.ANCHOR_K * (raw - 20.5), 20.5))
    assert p.p_over_line < p.p_over_anchor < p.p_over                            # H2H leans over: anchored in between
    log = h2h_shadow.ShadowLog(tmp_path / "s.sqlite3")
    log.record("nba", {"player": ME, "stat": "points", "line": 20.5, "team": "LAL", "opp": "OKC", "start_ts": START,
                       "odds_type": "standard"}, p, 1.0, START - 60)
    assert log.db.execute("SELECT p_over_line, p_over_anchor FROM h2h_predictions").fetchone() == \
        (pytest.approx(p.p_over_line), pytest.approx(p.p_over_anchor))
