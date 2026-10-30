"""
h2h_shadow.py — run local/h2h_context.py on every prop of each new NBA/NHL board, in SHADOW mode: predictions are
logged, never alerted, never used by either slip builder.

The sentinel calls BoardRunner.submit() from its on_board hook (a new board file landed: every bookmarklet dump).
Each prop whose game hasn't started is projected in the background, CONCURRENCY at a time, so pricing and alerts
never wait on ESPN. A board that lands while one is still running waits and runs next; if several land, only the
newest is kept.

Logged to data/h2h_shadow.sqlite3 (gitignored), table h2h_predictions: one row per prop when its prediction
CHANGES (side, P(over) by 0.5 pt+, projection, or the context notes), plus the first sighting. Props repeat on
every 15-minute dump; a change (a teammate ruled out at 5 pm) is exactly what the log should catch. Each row has
the full context: weights and mismatches per H2H game (games_json), usage, expected minutes, notes.

NBA and NHL only: ESPN has no minutes for NFL (see h2h_context).

  python3 -m local.h2h_shadow --report      # what the shadow log holds, by sport and call
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import time
from pathlib import Path

from local import h2h_context
from local import prize_picks_ev_scraper as scanner
from local.grade_slips import canon

DB = Path(__file__).resolve().parents[1] / "data" / "h2h_shadow.sqlite3"
CONCURRENCY = 4
P_CHANGE = 0.005  # a P(over) move smaller than this isn't a new row

SCHEMA = """
CREATE TABLE IF NOT EXISTS h2h_predictions (id INTEGER PRIMARY KEY, ts REAL, board_ts REAL, sport TEXT, player TEXT,
    stat TEXT, line REAL, odds_type TEXT, team TEXT, opp TEXT, start_ts REAL, side TEXT, p_over REAL, p_push REAL,
    mu REAL, rate REAL, usage REAL, exp_minutes REAL, n_h2h INTEGER, n_eff REAL, notes_json TEXT, games_json TEXT,
    error TEXT);
CREATE INDEX IF NOT EXISTS h2h_prop ON h2h_predictions (sport, player, stat, line, start_ts, id);
"""


def opponent_of(game: str, team: str) -> str | None:
    """'DET v NYK' / 'BOS @ CAR' / 'A vs B' -> the side that isn't `team`."""
    for sep in (" @ ", " vs ", " v ", " VS "):
        if sep in game:
            sides = [canon(s.strip()) for s in game.split(sep, 1)]
            other = [s for s in sides if s != canon(team)]
            return other[0] if len(other) == 1 else None
    return None


def board_props(rows: list[dict], sport: str, now: float) -> list[dict]:
    """The board's props for an NBA/NHL game that starts more than START_BUFFER_MIN from now, one per
    (player, stat, line, start), with the opponent resolved."""
    if sport not in h2h_context.SUPPORTED:
        return []
    out, seen = {}, set()
    for r in rows:
        leg = scanner.Leg(r["player"], r["stat"], float(r["line"]), r.get("game", ""), team=r.get("team", ""),
                          start_time=r.get("start_time", ""))
        start, opp = leg.start_ts, opponent_of(leg.game, leg.team)
        if not (start and opp and leg.team) or start <= now + scanner.START_BUFFER_MIN * 60:
            continue
        key = (leg.player, leg.stat, leg.line, start)
        if key not in seen:
            seen.add(key)
            out[key] = {"player": leg.player, "stat": leg.stat, "line": leg.line, "team": canon(leg.team), "opp": opp,
                        "start_ts": start, "odds_type": (r.get("odds_type") or "standard").lower()}
    return list(out.values())


class ShadowLog:
    def __init__(self, path: Path | str = DB):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")   # the NBA and NHL sentinels share this file
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript(SCHEMA)

    def _last(self, sport: str, prop: dict):
        return self.db.execute("SELECT side, p_over, mu, notes_json, error FROM h2h_predictions WHERE sport=? AND "
                               "player=? AND stat=? AND line=? AND start_ts=? ORDER BY id DESC LIMIT 1",
                               (sport, prop["player"], prop["stat"], prop["line"], prop["start_ts"])).fetchone()

    def record(self, sport: str, prop: dict, pred: h2h_context.Prediction | None, board_ts: float, now: float,
               error: str = "") -> bool:
        """Write the prediction if it differs from the prop's last row; True when a row was written."""
        notes = json.dumps(pred.notes if pred else [])
        last = self._last(sport, prop)
        if last is not None:
            side, p, mu, last_notes, last_err = last
            same_p = (p is None and (pred is None or pred.p_over is None)) or (
                p is not None and pred is not None and pred.p_over is not None and abs(p - pred.p_over) < P_CHANGE)
            same_mu = (mu is None and (pred is None or pred.mu is None)) or (
                mu is not None and pred is not None and pred.mu is not None and abs(mu - pred.mu) < 0.01)
            if (side == (pred.side if pred else None) and same_p and same_mu and last_notes == notes
                    and (last_err or "") == error):
                return False
        g = (lambda k: getattr(pred, k) if pred else None)
        self.db.execute(
            "INSERT INTO h2h_predictions (ts, board_ts, sport, player, stat, line, odds_type, team, opp, start_ts, side, "
            "p_over, p_push, mu, rate, usage, exp_minutes, n_h2h, n_eff, notes_json, games_json, error) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (now, board_ts, sport, prop["player"], prop["stat"], prop["line"], prop["odds_type"], prop["team"],
             prop["opp"], prop["start_ts"], g("side"), g("p_over"), g("p_push"), g("mu"), g("rate"), g("usage"),
             g("exp_minutes"), g("n_h2h"), g("n_eff"), notes, json.dumps(pred.games if pred else []), error or None))
        self.db.commit()
        return True


async def run_board(sport: str, rows: list[dict], board_ts: float, log: ShadowLog, now: float | None = None,
                    predictor=None, concurrency: int = CONCURRENCY) -> dict:
    """Project every prop of a board and log the changes. Never raises; one prop's failure is logged as its row."""
    now = time.time() if now is None else now
    predictor = predictor or h2h_context.run
    props = board_props(rows, sport, now)
    sem, counts = asyncio.Semaphore(concurrency), {"props": len(props), "calls": 0, "More": 0, "Less": 0,
                                                    "logged": 0, "errors": 0}

    async def one(prop):
        async with sem:
            pred, error = None, ""
            try:
                pred = await predictor(prop["player"], sport, prop["stat"], prop["line"], prop["team"], prop["opp"])
            except Exception as e:  # noqa: BLE001 — one prop must not stop the board
                error = f"{type(e).__name__}: {e}"[:200]
                counts["errors"] += 1
            if pred is not None and pred.side:
                counts["calls"] += 1
                counts[pred.side] += 1
            try:
                counts["logged"] += log.record(sport, prop, pred, board_ts, time.time(), error)
            except sqlite3.Error as e:
                counts["errors"] += 1
                print(f"{time.strftime('%H:%M:%S')} H2H shadow log write failed: {e!r}")
    await asyncio.gather(*(one(p) for p in props))
    return counts


class BoardRunner:
    """One board at a time per sentinel; the newest board waiting behind a running one runs next."""

    def __init__(self, sport: str, log: ShadowLog | None = None, run=run_board):
        self.sport, self.log, self._run = sport, log or ShadowLog(), run
        self.task: asyncio.Task | None = None
        self.pending: tuple[list[dict], float] | None = None

    def submit(self, rows: list[dict], board_ts: float) -> bool:
        """Queue a board (call from inside the event loop). True when it started now, False when it waits."""
        if self.task is not None and not self.task.done():
            self.pending = (rows, board_ts)  # an older pending board is replaced: only the newest matters
            return False
        self.task = asyncio.ensure_future(self._loop(rows, board_ts))
        return True

    async def _loop(self, rows: list[dict], board_ts: float) -> None:
        while True:
            started = time.time()
            try:
                c = await self._run(self.sport, rows, board_ts, self.log)
                print(f"{time.strftime('%H:%M:%S')} H2H shadow ({self.sport.upper()}): {c['props']} props, "
                      f"{c['calls']} calls ({c['More']} More / {c['Less']} Less), {c['logged']} logged, "
                      f"{c['errors']} errors in {time.time() - started:.0f}s")
            except Exception as e:  # noqa: BLE001 — the shadow model must never take the sentinel down
                print(f"{time.strftime('%H:%M:%S')} H2H shadow ({self.sport.upper()}) failed: {e!r}")
            if self.pending is None:
                return
            (rows, board_ts), self.pending = self.pending, None


def report(path: Path | str = DB) -> str:
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    rows = db.execute("SELECT sport, COALESCE(side, 'no call'), COUNT(*), COUNT(DISTINCT player || stat || line || start_ts) "
                      "FROM h2h_predictions GROUP BY 1, 2 ORDER BY 1, 2").fetchall()
    return "\n".join(f"{s.upper():4} {side:8} {n:6} rows, {props:5} props" for s, side, n, props in rows) or "empty"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--report", action="store_true", help="summarize the shadow log")
    ap.add_argument("--db", type=Path, default=DB)
    a = ap.parse_args()
    print(report(a.db))


if __name__ == "__main__":
    main()
