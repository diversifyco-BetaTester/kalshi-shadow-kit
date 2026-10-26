"""
ledger.py — append every slip a sentinel alerts to local/slips_history.csv, for grading later.

One row per alerted slip. Several sentinels (one per sport) append at once, so each append holds
an flock on local/slips_history.lock and writes the header only when the file is empty. Rows are
written whole and flushed before the lock is released, so a reader never sees half a row.

Columns: timestamp (ISO, UTC), sports ('MLB+NFL+NHL'), slip_type ('6-Pick Flex'), net_ev_pct,
stake_units, earliest_start (ISO, UTC; empty for a pasted board), telegram ('sent' / 'failed' /
'terminal' when no chat is set), legs (JSON list: sport, player, stat, line, side, probability,
source, team, start_time). team and start_time are there so each leg can be graded against the box
score. local/grade_slips.py fills the GRADE_FIELDS later (status, hits, misses, pushes,
payout_multiplier, net_units, graded_at, leg_results), rewriting the file under the same lock.
"""
from __future__ import annotations

import csv
import fcntl
import io
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

LEDGER = Path("local/slips_history.csv")
FIELDS = ["timestamp", "sports", "slip_type", "net_ev_pct", "stake_units", "earliest_start", "telegram", "legs"]
GRADE_FIELDS = ["status", "hits", "misses", "pushes", "payout_multiplier", "net_units", "graded_at", "leg_results",
                "settle_alert"]  # "due" when graded, then "sent <iso>" / "failed <iso>" / "in digest" (grade_slips)
ALL_FIELDS = FIELDS + GRADE_FIELDS


def iso(ts: float | None) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds") if ts else ""


def row(entry: tuple, units: float, now: float, telegram: str, source) -> dict:
    """The ledger row for one alerted slip. `source(leg)` names where its price came from."""
    ev, kind, combo, _ = entry
    starts = [l.start_ts for l in combo if l.start_ts]
    legs = [{"sport": l.sport, "player": l.player, "stat": l.stat, "line": l.line, "side": l.pick[0],
             "probability": round(l.pick[1], 4), "source": source(l) or "Kalshi", "team": l.team,
             "start_time": l.start_time, "pricing": next((q.detail for q in l.quotes if q.detail), None),
             "push": round(l.p_push or 0.0, 4)}  # whole line: probability is P(win | no push)
            for l in combo]
    return {"timestamp": iso(now), "sports": "+".join(sorted({l.sport.upper() for l in combo if l.sport})),
            "slip_type": f"{len(combo)}-Pick {kind.title()}", "net_ev_pct": round(ev * 100, 2),
            "stake_units": units, "earliest_start": iso(min(starts)) if starts else "", "telegram": telegram,
            "legs": json.dumps(legs, ensure_ascii=False)}


@contextmanager
def locked(path: Path = LEDGER):
    """Exclusive lock shared by every writer of `path` (sentinels appending, the grader rewriting)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def header(path: Path) -> list[str] | None:
    try:
        with open(path, newline="") as f:
            return next(csv.reader(f), None)
    except FileNotFoundError:
        return None


def append(r: dict, path: Path = LEDGER) -> None:
    buf = io.StringIO()
    with locked(path):
        cols = header(path)
        w = csv.DictWriter(buf, fieldnames=cols or ALL_FIELDS, restval="", extrasaction="ignore")
        if not cols:
            w.writeheader()
        w.writerow(r)
        with open(path, "a", newline="") as f:
            f.write(buf.getvalue())
            f.flush()
            os.fsync(f.fileno())


def rewrite(rows: list[dict], path: Path = LEDGER) -> None:
    """Replace the file with `rows` (legs as decoded lists or JSON strings). Caller holds locked()."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=ALL_FIELDS, restval="", extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({**r, **{k: json.dumps(r[k], ensure_ascii=False) for k in ("legs", "leg_results")
                                if isinstance(r.get(k), list)}})
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read(path: Path = LEDGER) -> list[dict]:
    """Rows with legs (and leg_results, once graded) decoded, for grading/reporting."""
    try:
        with open(path, newline="") as f:
            rows = list(csv.DictReader(f))
    except FileNotFoundError:
        return []
    for r in rows:
        r["legs"] = json.loads(r["legs"])
        r["leg_results"] = json.loads(r["leg_results"]) if r.get("leg_results") else []
    return rows
