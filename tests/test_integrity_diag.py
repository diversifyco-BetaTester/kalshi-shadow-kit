import json

from local import pipeline_config, snapshots
from ops import integrity_diag as diag
from tests.test_integrity import books_leg
from tests.test_steam_snapshots import KICK_TS, NOW


def test_flagged_vs_unflagged_clv_with_window_and_open_legs(tmp_path):
    db = snapshots.Snapshots("nhl", tmp_path / "snapshots_nhl.sqlite3")
    clean = books_leg({"pinnacle": 0.57, "draftkings": 0.57}, fetched=NOW, player="Clean Leg")
    single = books_leg({"draftkings": 0.58}, fetched=NOW, player="One Book")          # single_book
    db.prices(1, NOW, [clean, single])
    later_clean = books_leg({"pinnacle": 0.59, "draftkings": 0.59}, fetched=KICK_TS - 900, player="Clean Leg")
    later_single = books_leg({"draftkings": 0.55}, fetched=KICK_TS - 900, player="One Book")
    db.prices(2, KICK_TS - 600, [later_clean, later_single])
    future = books_leg({"pinnacle": 0.6, "draftkings": 0.6}, fetched=NOW, player="Not Started")
    from datetime import datetime, timezone
    future.start_time = datetime.fromtimestamp(KICK_TS + 86400, timezone.utc).isoformat()
    db.prices(3, NOW, [future])
    recs = diag.leg_records([tmp_path / "snapshots_nhl.sqlite3"], NOW - 60, KICK_TS, KICK_TS + 60, pipeline_config.CONFIG)
    by = {r["flags"] and r["flags"][0] or ("open" if r["status"] == "open" else "clean"): r for r in recs}
    assert abs(by["single_book"]["clv"] + 0.03) < 1e-9
    assert abs(by["clean"]["clv"] - 0.02) < 1e-9 and by["open"]["status"] == "open"
    text = diag.report(recs, NOW - 60, KICK_TS, pipeline_config.CONFIG)
    assert "Flagged at entry: 1 of 3 books legs (33%) · no Kalshi legs" in text and "single_book 1" in text
    assert "unflagged kalshi  n=0" in text and "flagged kalshi    n=0" in text
    assert "unflagged books   n=1    mean CLV  +2.00 pts" in text and "(insufficient data)" in text
    assert "single_book     n=1    mean CLV  -3.00 pts" in text
    outside = diag.leg_records([tmp_path / "snapshots_nhl.sqlite3"], KICK_TS + 3600, KICK_TS + 7200, KICK_TS + 7200,
                               pipeline_config.CONFIG)
    assert outside == [] and "No legs priced" in diag.report(outside, 0, 1, pipeline_config.CONFIG)


def test_each_source_is_reported_against_its_own_legs():
    rec = lambda src, flags, clv: {"sport": "nfl", "source": src, "kind": src, "flags": flags, "eligible": False,
                                   "clv": clv, "status": "closed", "same_fetch": False}
    recs = [rec("books", [], 0.01), rec("books", ["single_book"], -0.02),
            rec("kalshi", [], -0.01), rec("kalshi", [], -0.01), rec("kalshi", ["kalshi_wide"], 0.03)]
    text = diag.report(recs, NOW - 60, KICK_TS, pipeline_config.CONFIG)
    assert "Flagged at entry: 1 of 2 books legs (50%) · 1 of 3 Kalshi legs (33%)" in text   # never over 100%
    assert "flagged books     n=1    mean CLV  -2.00 pts" in text                            # books only
    assert "unflagged kalshi  n=2    mean CLV  -1.00 pts" in text                            # clean Kalshi only
    assert "flagged kalshi    n=1    mean CLV  +3.00 pts" in text
    assert "kalshi_wide     n=1    mean CLV  +3.00 pts" in text
