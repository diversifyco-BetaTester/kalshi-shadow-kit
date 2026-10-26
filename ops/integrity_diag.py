"""
integrity_diag.py — did the shadow-mode market-integrity flags (local/integrity.py) predict CLV loss?

Reads data/snapshots_<sport>.sqlite3 (read-only; the sentinels' console output isn't saved, the snapshot
files are the log). For every distinct leg priced in the window (sport, player, stat, line, source, start):
  entry  its first read in the window; side = the side picked then; flags = integrity flags at that read
         (recomputed from the logged pricing record, price age exact from fetched_ts, as the backtest does)
  close  the last same-source read at or before the start; legs not started yet are "open", a close read
         more than snapshots.STALE_CLOSE_S (30 min) before the start is counted but not used
  CLV    close - entry for the side taken, in points
Two populations: every priced leg, and the eligible legs (cleared the floors and filters: what both builders
chose from). Every CLV line prints n, and n < 30 is marked "(insufficient data)". The "unchanged" share
matters for books legs: a close read from the SAME cache fetch as the entry shows 0 CLV by construction.

  python3 -m ops.integrity_diag                       # today (local midnight to now)
  python3 -m ops.integrity_diag --since 2026-10-23    # from that date on
  python3 -m ops.integrity_diag --since 2026-10-20 --until 2026-10-23
"""
from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from local import backtest, integrity, pipeline_config, snapshots

DATA = Path(__file__).resolve().parents[1] / "data"


def _side_p(read, side: str) -> float:
    return read.p if read.side == side else 1 - read.p


def leg_records(paths: list[Path], since: float, until: float, now: float, cfg: dict) -> list[dict]:
    out = []
    for path in paths:
        sport = backtest.sport_of(path, path.stem)
        h = backtest.History(path, sport)
        eligible = set()
        for c in h.cycles:
            if since <= c["ts"] < until and c["eligible_json"]:
                for r in json.loads(c["eligible_json"]):
                    if (r[0] or sport) == sport:
                        eligible.add(backtest.series_key(r[1], r[2], r[3], r[6], r[7]))
        for key, reads in h.series.items():
            window = [r for r in reads if since <= r.ts < until]
            if not window:
                continue
            start = key[4]
            first = window[0]
            side = "More" if first.p_over >= 0.5 else "Less"
            got = h.integrity_at(key, first.ts, side, cfg)
            feat, flags = got if got else ({"kind": "?"}, [])
            rec = {"sport": sport, "source": key[3], "kind": feat["kind"], "flags": flags, "eligible": key in eligible,
                   "clv": None, "status": "open", "same_fetch": False}
            if start and start <= now:
                close = h.read_at(key, start)
                if close is None or close.ts < first.ts:
                    rec["status"] = "no close"
                elif start - close.ts > snapshots.STALE_CLOSE_S:
                    rec["status"] = "stale close"
                else:
                    rec["status"] = "closed"
                    rec["clv"] = _side_p(close, side) - _side_p(first, side)
                    f0 = json.loads(first.pricing or "{}").get("fetched_ts")
                    rec["same_fetch"] = bool(f0) and json.loads(close.pricing or "{}").get("fetched_ts") == f0
            out.append(rec)
    return out


def cell(rs: list[dict]) -> str:
    c = [r["clv"] for r in rs if r["clv"] is not None]
    if not c:
        return "n=0"
    pct = lambda k: sum(k(x) for x in c) / len(c)
    s = (f"n={len(c):<4} mean CLV {sum(c) / len(c) * 100:+6.2f} pts · beat {pct(lambda x: x > 1e-9):4.0%} · "
         f"unchanged {pct(lambda x: abs(x) <= 1e-9):4.0%} · lost {pct(lambda x: x < -1e-9):4.0%}")
    return s + ("  (insufficient data)" if len(c) < backtest.MIN_N else "")


def report(records: list[dict], since: float, until: float, cfg: dict) -> str:
    c, s = cfg["integrity"], cfg["shading"]
    out = [f"INTEGRITY DIAGNOSTIC · {datetime.fromtimestamp(since):%Y-%m-%d %H:%M} to "
           f"{datetime.fromtimestamp(until):%Y-%m-%d %H:%M} · {'ENFORCED' if c['enforce'] else 'shadow mode'}",
           f"thresholds: stale > {c['max_age_s'] / 60:.0f} min (books), min books {c['min_books']}, books disagree > "
           f"{c['max_spread'] * 100:.0f} pts, PIN against > {c['pin_against'] * 100:.0f} pts, flag ladder interp "
           f"{c['flag_interp']} · books interpolation {'on' if cfg['books']['ladder_interp'] else 'off'}, books whole "
           f"lines {'on' if cfg['books']['whole_lines'] else 'off'}",
           f"kalshi: wide > {c['kalshi_max_spread'] * 100:.0f}c, thin < {c['kalshi_min_volume']:g} contracts, flag kalshi "
           f"interp {c['flag_kalshi_interp']} · shading "
           + (f"on ({s['spread_mult']:g} x spread, +{s['interp_pts'] * 100:g} interp, +{s['thin_pts'] * 100:g} thin, "
              f"max {s['max_pts'] * 100:g} pts)" if s["enabled"] else "off")
           + " · CLV is on the raw (unshaded) price"]
    if not records:
        return "\n".join(out + ["\nNo legs priced in this window."])
    by = Counter((r["sport"], r["source"]) for r in records)
    out.append("\nDistinct legs priced: " + ", ".join(f"{s.upper()} {src} {n}" for (s, src), n in sorted(by.items())))
    for label, pop in (("ALL PRICED LEGS", records), ("ELIGIBLE LEGS (cleared floors + filters)",
                                                       [r for r in records if r["eligible"]])):
        flagged = [r for r in pop if r["flags"]]
        books = [r for r in pop if r["source"] == "books"]
        kalshi = [r for r in pop if r["source"] == "kalshi"]
        out.append(f"\n=== {label}: {len(pop)} legs ({len(books)} books-priced, {len(kalshi)} Kalshi-priced) ===")
        share = lambda rs, name: (f"{sum(bool(r['flags']) for r in rs)} of {len(rs)} {name} legs "
                                  f"({sum(bool(r['flags']) for r in rs) / len(rs):.0%})" if rs else f"no {name} legs")
        out.append(f"Flagged at entry: {share(books, 'books')} · {share(kalshi, 'Kalshi')}")  # each over its own source
        reasons = Counter(f for r in flagged for f in r["flags"])
        out.append("Flag reasons (a leg can carry several): " + (", ".join(f"{f} {n}" for f, n in reasons.most_common()) or "none"))
        combos = Counter(" + ".join(sorted(r["flags"])) for r in flagged)
        if combos:
            out.append("  combinations: " + ", ".join(f"{k} {n}" for k, n in combos.most_common()))
        out.append("Close status: " + ", ".join(f"{s} {n}" for s, n in Counter(r["status"] for r in pop).most_common()))
        out.append("CLV (closed legs, side taken at first read):")
        for name, rs in (("books", books), ("kalshi", kalshi)):  # each source's flagged legs against its own clean ones
            out.append(f"  unflagged {name:<7} {cell([r for r in rs if not r['flags']])}")
            out.append(f"  flagged {name:<9} {cell([r for r in rs if r['flags']])}")
            for f in integrity.FLAGS:
                got = [r for r in rs if f in r["flags"]]
                if got:
                    out.append(f"    {f:<15} {cell(got)}")
        closed = [r for r in books if r["clv"] is not None]
        if closed:
            out.append(f"  note: {sum(r['same_fetch'] for r in closed)}/{len(closed)} closed books legs closed on the SAME "
                       "cache fetch as their entry (CLV 0 by construction)")
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", help="first local date (YYYY-MM-DD; default: today)")
    ap.add_argument("--until", help="last local date, inclusive (YYYY-MM-DD; default: now)")
    ap.add_argument("--db", action="append", type=Path, help="snapshot file(s) (default: every data/snapshots_*.sqlite3)")
    a = ap.parse_args()
    now = time.time()
    midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    since = datetime.strptime(a.since, "%Y-%m-%d") if a.since else midnight
    until = (datetime.strptime(a.until, "%Y-%m-%d") + timedelta(days=1)).timestamp() if a.until else now
    paths = a.db or sorted(DATA.glob("snapshots_*.sqlite3"))
    if not paths:
        raise SystemExit("no snapshot files yet (data/snapshots_<sport>.sqlite3)")
    cfg = pipeline_config.CONFIG
    print(report(leg_records(paths, since.timestamp(), until, now, cfg), since.timestamp(), until, cfg))


if __name__ == "__main__":
    main()
