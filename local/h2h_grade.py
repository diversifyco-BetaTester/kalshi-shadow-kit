"""
h2h_grade.py — grade the H2H shadow model: logged predictions (data/h2h_shadow.sqlite3) joined to how each prop
actually finished.

Which prediction counts: a prop is logged every time its prediction changes, so it can have several rows. The
graded one is the LAST row logged before the game started (what you would have acted on at lock); --first grades
the earliest instead (an overnight call), to see whether late news helped.

Outcomes come from backtest.Outcomes: grade_slips.Grader on ESPN box scores, cached in
data/backtest_outcomes.sqlite3 (shared with the backtest, so a prop graded there isn't fetched again). Same rules as
the slip ledger: hit / miss; push (a whole line landed exactly) and void (didn't play, postponed) are counted but
leave the hit rate; review (the grader can't decide) and pending (not final, or started under 4 h ago) wait.

Report:
  calls        the model's More/Less calls: hit rate with a 95% Wilson interval, against the per-leg break-even
               of 5-pick Flex (standard props only: goblins and demons pay differently), by sport, stat, side,
               confidence and effective H2H sample. A slice under backtest.MIN_N (30) graded calls is marked
               "(insufficient data)": nobody should act on it.
  calibration  EVERY graded prediction with a probability, calls or not: predicted P(over) by bucket against
               how often the over actually hit, and the Brier score against always saying 50%. A model whose 70%
               predictions hit 55% of the time is overconfident, whatever its call record.

  python3 -m local.h2h_grade              # report from cached outcomes only (no network)
  python3 -m local.h2h_grade --grade      # first fetch outcomes for finished props from ESPN, then report
  python3 -m local.h2h_grade --grade --since 2026-11-01 --sport nba
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from local import backtest, h2h_shadow, optimizer

BREAKEVEN = optimizer.breakeven(5)  # ~54.25%: the 5-pick Flex per-leg break-even (6-pick is ~54.21%)
CONF_BUCKETS = [(0.60, "56-60%"), (0.65, "60-65%"), (0.75, "65-75%"), (1.01, "75%+")]
NEFF_BUCKETS = [(3, "n_eff 2-3"), (5, "n_eff 3-5"), (float("inf"), "n_eff 5+")]
CAL_BUCKETS = [(0.2, "0-20%"), (0.35, "20-35%"), (0.5, "35-50%"), (0.65, "50-65%"), (0.8, "65-80%"), (1.01, "80-100%")]


def bucket(x: float, buckets: list[tuple[float, str]]) -> str:
    return next(label for top, label in buckets if x < top)


def predictions(path: Path | str = h2h_shadow.DB, since: float = 0.0, until: float = math.inf, sport: str | None = None,
                first: bool = False) -> list[dict]:
    """One row per prop (sport, player, stat, line, start): its last (or first) prediction logged before the start."""
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    pick = "MIN(id)" if first else "MAX(id)"
    q = (f"SELECT p.* FROM h2h_predictions p JOIN (SELECT {pick} AS id FROM h2h_predictions WHERE ts < start_ts "
         "AND start_ts >= ? AND start_ts < ? AND (? IS NULL OR sport = ?) "
         "GROUP BY sport, player, stat, line, start_ts) k ON p.id = k.id ORDER BY p.start_ts, p.player")
    rows = [dict(r) for r in db.execute(q, (since, until if math.isfinite(until) else 1e18, sport, sport))]
    db.close()
    return rows


def attach_outcomes(preds: list[dict], outcomes: backtest.Outcomes, now: float) -> list[dict]:
    """Adds "over" (hit / miss / push / void / review / pending / ungraded: the More side's result) and "note"."""
    for p in preds:
        leg = SimpleNamespace(sport=p["sport"], player=p["player"], stat=p["stat"], line=p["line"],
                              start_ts=p["start_ts"], team=p["team"], side="More")
        p["over"], p["note"] = outcomes.result(leg, now)
        if p["side"] and p["over"] in ("hit", "miss"):
            won_over = p["over"] == "hit"
            p["result"] = "hit" if won_over == (p["side"] == "More") else "miss"
        else:
            p["result"] = p["over"]
    return preds


def wilson(hits: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if not n:
        return 0.0, 1.0
    phat, d = hits / n, 1 + z * z / n
    mid, half = (phat + z * z / (2 * n)) / d, z * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n)) / d
    return max(0.0, mid - half), min(1.0, mid + half)


def line_for(label: str, rows: list[dict], breakeven: bool = True) -> str:
    graded = [r for r in rows if r["result"] in ("hit", "miss")]
    n, hits = len(graded), sum(r["result"] == "hit" for r in graded)
    if not n:
        return f"  {label:<22} n=0"
    lo, hi = wilson(hits, n)
    extra = Counter(r["result"] for r in rows if r["result"] in ("push", "void"))
    s = f"  {label:<22} n={n:<4} hit {hits / n:5.1%}  [95% {lo:.0%}-{hi:.0%}]"
    if breakeven:
        s += "  above break-even" if lo > BREAKEVEN else "  below break-even" if hi < BREAKEVEN else ""
    if extra:
        s += "  (" + ", ".join(f"{k} {v}" for k, v in sorted(extra.items())) + ")"
    return s + ("  (insufficient data)" if n < backtest.MIN_N else "")


def calls_section(rows: list[dict]) -> list[str]:
    calls = [r for r in rows if r["side"]]
    std = [r for r in calls if (r["odds_type"] or "standard") == "standard"]
    out = [f"\nCALLS: {len(calls)} logged, {sum(r['result'] in ('hit', 'miss') for r in calls)} graded "
           f"(break-even {BREAKEVEN:.1%} per leg, standard props)", line_for("all standard", std)]
    for title, key in (("sport", lambda r: r["sport"].upper()), ("side", lambda r: r["side"]),
                       ("confidence", lambda r: bucket(max(r["p_over"], 1 - r["p_over"]), CONF_BUCKETS)),
                       ("H2H sample", lambda r: bucket(r["n_eff"] or 0, NEFF_BUCKETS)),
                       ("stat", lambda r: f"{r['sport'].upper()} {r['stat']}")):
        out.append(f" by {title}:")
        for k in sorted({key(r) for r in std}):
            out.append(line_for(k, [r for r in std if key(r) == k]))
    other = [r for r in calls if (r["odds_type"] or "standard") != "standard"]
    if other:
        out.append(" goblin / demon (other payouts, no break-even shown):")
        for k in sorted({r["odds_type"] for r in other}):
            out.append(line_for(k, [r for r in other if r["odds_type"] == k], breakeven=False))
    return out


def calibration_section(rows: list[dict]) -> list[str]:
    graded = [r for r in rows if r["p_over"] is not None and r["over"] in ("hit", "miss")]
    out = [f"\nCALIBRATION: {len(graded)} graded predictions with a probability (calls and no-calls)"]
    if not graded:
        return out
    y = [1.0 if r["over"] == "hit" else 0.0 for r in graded]
    brier = sum((r["p_over"] - t) ** 2 for r, t in zip(graded, y)) / len(graded)
    out.append(f"  Brier {brier:.4f} vs {0.25:.4f} for always 50% ({'better' if brier < 0.25 else 'not better'})"
               + ("  (insufficient data)" if len(graded) < backtest.MIN_N else ""))
    out.append(f"  {'predicted P(over)':<18} {'n':>5} {'mean predicted':>15} {'over hit':>9}")
    for label in [b for _, b in CAL_BUCKETS]:
        sel = [(r, t) for r, t in zip(graded, y) if bucket(r["p_over"], CAL_BUCKETS) == label]
        if sel:
            mp, hit = sum(r["p_over"] for r, _ in sel) / len(sel), sum(t for _, t in sel) / len(sel)
            out.append(f"  {label:<18} {len(sel):>5} {mp:>15.1%} {hit:>9.1%}" + ("  (insufficient data)" if len(sel) < backtest.MIN_N else ""))
    return out


def report(rows: list[dict], first: bool = False) -> str:
    status = Counter(r["result"] if r["side"] else r["over"] for r in rows)
    out = [f"H2H SHADOW GRADE · {len(rows)} props · graded prediction: {'first' if first else 'last before start'}",
           "outcomes: " + (", ".join(f"{k} {v}" for k, v in status.most_common()) or "none")]
    if status.get("ungraded"):
        out.append("  (ungraded props: run with --grade to fetch their results)")
    return "\n".join(out + calls_section(rows) + calibration_section(rows))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--grade", action="store_true", help="fetch outcomes from ESPN for finished props first")
    ap.add_argument("--since", help="first game date (YYYY-MM-DD)")
    ap.add_argument("--until", help="last game date, inclusive (YYYY-MM-DD)")
    ap.add_argument("--sport", choices=("nba", "nhl"))
    ap.add_argument("--first", action="store_true", help="grade each prop's first prediction, not its last")
    ap.add_argument("--db", type=Path, default=h2h_shadow.DB)
    ap.add_argument("--outcomes", type=Path, default=backtest.OUTCOMES)
    a = ap.parse_args()
    if not a.db.exists():
        raise SystemExit(f"no shadow log yet at {a.db}: it fills as the NBA/NHL sentinels read new boards")
    day = lambda d: datetime.strptime(d, "%Y-%m-%d").timestamp()
    rows = predictions(a.db, day(a.since) if a.since else 0.0, day(a.until) + 86400 if a.until else math.inf, a.sport, a.first)
    outcomes = backtest.Outcomes(a.outcomes, fetch=a.grade)
    attach_outcomes(rows, outcomes, time.time())
    if a.grade:
        print(f"fetched {outcomes.fetched} outcome(s) from ESPN")
    print(report(rows, a.first))


if __name__ == "__main__":
    main()
