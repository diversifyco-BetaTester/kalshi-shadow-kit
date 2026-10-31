"""
h2h_grade.py — grade the H2H shadow model: logged predictions (data/h2h_shadow.sqlite3) joined to how each prop
actually finished.

Which prediction counts: a prop is logged every time its prediction changes, so it can have several rows. The
graded one is the LAST row logged before the game started (what you would have acted on at lock); --first grades
the earliest instead (an overnight call), to see whether late news helped.

Model versions: only the CURRENT model version (h2h_context.MODEL_VERSION) is graded by default, so a fixed
model is never judged on its predecessor's calls. --model-version v1 grades an older one; --all-versions grades
each version's own predictions and adds a by-version slice.

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
  python3 -m local.h2h_grade --all-versions          # every model version, side by side

THE ONE-WEEK TEST (v3 vs line-anchored baselines; fixed 2026-10-31, BEFORE any v3 prediction existed)
  Every v3 prediction logs two baseline arms on the same prop (h2h_context ANCHOR_K): "line" (the PrizePicks
  line as the projection, H2H plays no part) and "anchored" (the line moved 25% toward the H2H projection).
  The arms differ only in their projection: same distribution, same clamp, same props, same outcomes.
  Compared on standard props only (goblin/demon lines aren't the market's line), by Brier score, paired per
  prop, with a 95% interval from a bootstrap over GAMES (props in one game move together: resampling props
  would overstate the evidence).
  Decision, once there are DECIDE_MIN_SLATES slates and DECIDE_MIN_PROPS graded props:
    KEEP H2H as the primary factor  only if v3 beats the line arm: the whole interval of
                                    Brier(v3) - Brier(line) is below 0
    otherwise DROP it, and          use the anchored arm if it beats the line the same way (H2H as a small
                                    adjustment), else the line alone (H2H adds nothing measurable)
  The rule is applied as written: no peeking-and-moving the thresholds.
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

import random

from local import backtest, h2h_context, h2h_shadow, optimizer
from local import prize_picks_ev_scraper as scanner

BREAKEVEN = optimizer.breakeven(5)  # ~54.25%: the 5-pick Flex per-leg break-even (6-pick is ~54.21%)
CONF_BUCKETS = [(0.60, "56-60%"), (0.65, "60-65%"), (0.75, "65-75%"), (1.01, "75%+")]
NEFF_BUCKETS = [(3, "n_eff 2-3"), (5, "n_eff 3-5"), (float("inf"), "n_eff 5+")]
ARMS = (("v3 (H2H primary)", "p_over"), ("line", "p_over_line"), ("anchored", "p_over_anchor"))
DECIDE_MIN_SLATES, DECIDE_MIN_PROPS = 7, 300
BOOT_N, BOOT_SEED = 2000, 20261031
CAL_BUCKETS = [(0.2, "0-20%"), (0.35, "20-35%"), (0.5, "35-50%"), (0.65, "50-65%"), (0.8, "65-80%"), (1.01, "80-100%")]


def bucket(x: float, buckets: list[tuple[float, str]]) -> str:
    return next(label for top, label in buckets if x < top)


def predictions(path: Path | str = h2h_shadow.DB, since: float = 0.0, until: float = math.inf, sport: str | None = None,
                first: bool = False, version: str | None = "current") -> list[dict]:
    """One row per prop (sport, player, stat, line, start) and model version: its last (or first) prediction
    logged before the start. `version` None: every version, each graded on its own predictions; "current":
    h2h_context.MODEL_VERSION as it is NOW (not when this module was imported)."""
    version = h2h_context.MODEL_VERSION if version == "current" else version
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    pick, v = ("MIN(id)" if first else "MAX(id)"), h2h_shadow.version_sql(db)
    q = (f"SELECT p.*, {v.replace('model_version', 'p.model_version')} AS version FROM h2h_predictions p JOIN "
         f"(SELECT {pick} AS id FROM h2h_predictions WHERE ts < start_ts AND start_ts >= ? AND start_ts < ? "
         f"AND (? IS NULL OR sport = ?) AND (? IS NULL OR {v} = ?) "
         f"GROUP BY {v}, sport, player, stat, line, start_ts) k ON p.id = k.id ORDER BY p.start_ts, p.player")
    rows = [dict(r) for r in db.execute(q, (since, until if math.isfinite(until) else 1e18, sport, sport,
                                            version, version))]
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
    versions = sorted({r["version"] for r in std})
    for title, key in (*([("model version", lambda r: r["version"])] if len(versions) > 1 else []),
                       ("sport", lambda r: r["sport"].upper()), ("side", lambda r: r["side"]),
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


def game_key(r: dict) -> tuple:
    return r["sport"], round(r["start_ts"]), tuple(sorted((r["team"] or "", r["opp"] or "")))


def slate(r: dict) -> str:
    return datetime.fromtimestamp(r["start_ts"], scanner.EASTERN).strftime("%Y-%m-%d")


def paired_brier(rows: list[dict], a: str, b: str, n: int = BOOT_N, seed: int = BOOT_SEED) -> tuple[float, float, float]:
    """Mean of Brier(a) - Brier(b) per prop (< 0: `a` better), with a 95% interval from a bootstrap over games."""
    games: dict[tuple, list[float]] = {}
    for r in rows:
        y = 1.0 if r["over"] == "hit" else 0.0
        games.setdefault(game_key(r), []).append((r[a] - y) ** 2 - (r[b] - y) ** 2)
    keys = sorted(games)
    point = sum(sum(v) for v in games.values()) / sum(len(v) for v in games.values())
    rng, stats = random.Random(seed), []
    for _ in range(n):
        pick = [games[keys[rng.randrange(len(keys))]] for _ in keys]
        stats.append(sum(sum(v) for v in pick) / sum(len(v) for v in pick))
    stats.sort()
    return point, stats[int(0.025 * n)], stats[int(0.975 * n) - 1]


def arms_section(rows: list[dict]) -> list[str]:
    """The one-week test (module docstring): v3 against the line-anchored baselines, and the decision."""
    cmp = [r for r in rows if r["over"] in ("hit", "miss") and (r["odds_type"] or "standard") == "standard"
           and all(r.get(k) is not None for _, k in ARMS)]
    if not cmp:
        return ["\nARMS (v3 vs line-anchored baselines): no graded props with all three arms yet"]
    slates, games = sorted({slate(r) for r in cmp}), {game_key(r) for r in cmp}
    out = [f"\nARMS: v3 vs line-anchored baselines · {len(cmp)} graded standard props · {len(games)} games · "
           f"{len(slates)} slate(s) {slates[0]}..{slates[-1]}"]
    for name, k in ARMS:
        brier = sum((r[k] - (r["over"] == "hit")) ** 2 for r in cmp) / len(cmp)
        calls = [r for r in cmp if max(r[k], 1 - r[k]) >= h2h_context.THRESHOLD]
        ok = sum((r["over"] == "hit") == (r[k] >= 0.5) for r in calls)
        more = sum(r[k] >= 0.5 for r in calls)
        rec = (f"calls n={len(calls):<4} hit {ok / len(calls):5.1%} [95% {ci_text(ok, len(calls))}] "
               f"({more} More / {len(calls) - more} Less)" if calls else "no calls")
        out.append(f"  {name:18} Brier {brier:.4f}   {rec}")
    out.append(f"  {'always 50%':18} Brier 0.2500")
    out.append(" paired Brier difference, per prop (< 0: the first is better), 95% bootstrap over games:")
    diffs = {}
    for a, b in (("p_over", "p_over_line"), ("p_over_anchor", "p_over_line"), ("p_over", "p_over_anchor")):
        d = diffs[(a, b)] = paired_brier(cmp, a, b)
        name = lambda k: next(n for n, kk in ARMS if kk == k)
        verdict = "better" if d[2] < 0 else "worse" if d[1] > 0 else "no clear difference"
        out.append(f"  {name(a):>18} - {name(b):<18} {d[0]:+.4f}  [{d[1]:+.4f}, {d[2]:+.4f}]  {verdict}")
    v3_wins, anchor_wins = diffs[("p_over", "p_over_line")][2] < 0, diffs[("p_over_anchor", "p_over_line")][2] < 0
    decision = ("KEEP H2H as the primary factor (v3 beats the line)" if v3_wins else
                "DROP H2H as the primary factor; use the anchored arm (H2H as a small adjustment beats the line)"
                if anchor_wins else "DROP H2H as the primary factor; use the line alone (H2H adds nothing measurable)")
    if len(slates) < DECIDE_MIN_SLATES or len(cmp) < DECIDE_MIN_PROPS:
        out.append(f" DECISION: not yet: {len(slates)}/{DECIDE_MIN_SLATES} slates, {len(cmp)}/{DECIDE_MIN_PROPS} "
                   f"props. On the data so far the rule would say: {decision}")
    else:
        out.append(f" DECISION (rule fixed 2026-10-31): {decision}")
    return out


def ci_text(k: int, n: int) -> str:
    lo, hi = wilson(k, n)
    return f"{lo:.0%}-{hi:.0%}"


def report(rows: list[dict], first: bool = False, version: str | None = "current") -> str:
    version = h2h_context.MODEL_VERSION if version == "current" else version
    status = Counter(r["result"] if r["side"] else r["over"] for r in rows)
    which = (f"model {version}" + (" (current)" if version == h2h_context.MODEL_VERSION else "") if version
             else "all model versions: " + ", ".join(sorted({r["version"] for r in rows})))
    out = [f"H2H SHADOW GRADE · {which} · {len(rows)} props · graded prediction: "
           f"{'first' if first else 'last before start'}",
           "outcomes: " + (", ".join(f"{k} {v}" for k, v in status.most_common()) or "none")]
    if status.get("ungraded"):
        out.append("  (ungraded props: run with --grade to fetch their results)")
    return "\n".join(out + calls_section(rows) + calibration_section(rows) + arms_section(rows))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--grade", action="store_true", help="fetch outcomes from ESPN for finished props first")
    ap.add_argument("--since", help="first game date (YYYY-MM-DD)")
    ap.add_argument("--until", help="last game date, inclusive (YYYY-MM-DD)")
    ap.add_argument("--sport", choices=("nba", "nhl"))
    ap.add_argument("--first", action="store_true", help="grade each prop's first prediction, not its last")
    ap.add_argument("--model-version", default=h2h_context.MODEL_VERSION,
                    help=f"grade this model version (default: the current one, {h2h_context.MODEL_VERSION})")
    ap.add_argument("--all-versions", action="store_true", help="grade every model version, side by side")
    ap.add_argument("--db", type=Path, default=h2h_shadow.DB)
    ap.add_argument("--outcomes", type=Path, default=backtest.OUTCOMES)
    a = ap.parse_args()
    if not a.db.exists():
        raise SystemExit(f"no shadow log yet at {a.db}: it fills as the NBA/NHL sentinels read new boards")
    day = lambda d: datetime.strptime(d, "%Y-%m-%d").timestamp()
    version = None if a.all_versions else a.model_version
    rows = predictions(a.db, day(a.since) if a.since else 0.0, day(a.until) + 86400 if a.until else math.inf, a.sport,
                       a.first, version)
    outcomes = backtest.Outcomes(a.outcomes, fetch=a.grade)
    attach_outcomes(rows, outcomes, time.time())
    if a.grade:
        print(f"fetched {outcomes.fetched} outcome(s) from ESPN")
    print(report(rows, a.first, version))


if __name__ == "__main__":
    main()
