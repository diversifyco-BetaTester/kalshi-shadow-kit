"""
snapshots.py — what each sentinel saw and chose, every cycle, for the Task 4 backtest.

One SQLite file per sport (data/snapshots_<sport>.sqlite3; data/ is gitignored), so the five sentinels
never contend for a write lock. Tables:

  boards  every new board for this league: its board time, capture time and the league's RAW
          projections (all odds types, as the bookmarklet saved them), gzip-compressed JSON
  prices  one row per priced leg when its price changes, and at least every HEARTBEAT_S while it
          doesn't: the leg (player, stat, line, side, team, game, start), the fair probability and source,
          the full pricing record (raw two-way prices, implied, fair, de-vig method; for Kalshi both
          methods' fair values), and the steam feature (local/steam.py). kind="tick" for board legs,
          kind="watch" for chosen legs re-priced off the board (CLV; never part of a replayed board).
          Rows are keyed per source, so a leg read from Kalshi and from the books keeps two series.
          When a leg's game starts (it leaves the pregame set), its last price is written again as
          kind="close" (a marker: CLV closes use the last tick/watch READ at or before start).
  cycles  every cycle: counts (legs, priced, >= min, whole-number lines the parser dropped), the live
          builder's +EV slips (which were sent, their stake), the paper optimizer's report (chosen slip and
          the best slip per size with EV, variance, EV/sd, log-growth, P(profit)), the ELIGIBLE legs both
          builders chose from (after injury guard/cooldown, plus pooled legs) and per-stat coverage

  slip_legs  CLV: every leg of every CHOSEN slip, by builder: "live" (the slips the sentinel sent) and
          "paper" (the optimizer's pick; the same leg set is recorded once per DEDUPE_S, not every
          cycle it's re-chosen). Each row keeps the leg's sport, the side taken and the ENTRY fair
          probability with its source (a pooled leg's real venue, not "pool"), then, once the event starts, the CLOSE: the last price row for the same leg from the
          SAME source (Kalshi entry -> Kalshi close) at or before start time, read from the leg's own sport
          file. Until then the leg is on its sport sentinel's watch list (open_legs, across every snapshot
          file) and is re-priced every cycle from its source even if it left the PrizePicks board, so a moved/pulled line still gets a real close. A close older than
          STALE_CLOSE_S at start is kept but flagged. clv = close - entry for the side taken (> 0: we beat
          the close).
  watch_legs  every ELIGIBLE Kalshi-priced leg of this sport (cleared the floors, chosen or not), so it is
          watched like a chosen leg until its start: a line PrizePicks moves (each line is its own series)
          or a prop it pulls otherwise stops being priced hours before the start and the integrity report
          and calibration get a stale close (2026-10-25: 454 NFL legs, 306 of them moved lines). Kalshi
          only: it is read every cycle anyway, so watching costs no Odds API credit.

Prices are piecewise constant between rows, so change-only rows lose nothing and keep a full NFL
day near 10^5 rows instead of ~10^6.

  python3 -m local.snapshots --clv           # CLV report over every data/snapshots_<sport>.sqlite3
"""
from __future__ import annotations

import gzip
import json
import sqlite3
import time
from pathlib import Path

from local import prize_picks_ev_scraper as scanner

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "snapshots_{sport}.sqlite3"
HEARTBEAT_S = 600
DEDUPE_S = 120 * 60       # the paper optimizer re-choosing the same leg set: one CLV record per this window
STALE_CLOSE_S = 30 * 60   # a close read this long before start is flagged (the leg stopped being priced)
CHANGE = 0.002  # a fair probability move smaller than this isn't a new row (until HEARTBEAT_S)

SCHEMA = """
CREATE TABLE IF NOT EXISTS boards (board_ts REAL PRIMARY KEY, captured_at TEXT, n_props INTEGER, props_gz BLOB);
CREATE TABLE IF NOT EXISTS cycles (id INTEGER PRIMARY KEY, ts REAL, board_ts REAL, legs INTEGER, priced INTEGER,
    good INTEGER, whole_dropped INTEGER, live_json TEXT, paper_json TEXT, eligible_json TEXT, coverage_json TEXT);
CREATE TABLE IF NOT EXISTS prices (id INTEGER PRIMARY KEY, cycle_id INTEGER, ts REAL, kind TEXT, player TEXT, stat TEXT,
    line REAL, side TEXT, team TEXT, game TEXT, start_ts REAL, p_fair REAL, source TEXT, method TEXT,
    pricing_json TEXT, steam_json TEXT, price_age_s REAL);
CREATE INDEX IF NOT EXISTS prices_prop ON prices (player, stat, line, ts);
CREATE TABLE IF NOT EXISTS slip_legs (id INTEGER PRIMARY KEY, slip_id TEXT, builder TEXT, cycle_id INTEGER, entry_ts REAL,
    size INTEGER, ev REAL, sent INTEGER, player TEXT, stat TEXT, line REAL, side TEXT, team TEXT, game TEXT, start_ts REAL,
    source TEXT, p_entry REAL, close_p REAL, close_ts REAL, close_age_s REAL, clv REAL, close_note TEXT,
    sport TEXT, units REAL, price_age_s REAL, stats_json TEXT);
CREATE INDEX IF NOT EXISTS slip_legs_open ON slip_legs (close_p, start_ts);
CREATE TABLE IF NOT EXISTS watch_legs (player TEXT, stat TEXT, line REAL, team TEXT, game TEXT, start_ts REAL,
    source TEXT, first_ts REAL, PRIMARY KEY (player, stat, line, start_ts, source));
"""
# columns added after the first databases were written: (table, column, type)
MIGRATIONS = [("cycles", "eligible_json", "TEXT"), ("cycles", "coverage_json", "TEXT"),
              ("slip_legs", "sport", "TEXT"), ("slip_legs", "units", "REAL"),
              ("prices", "price_age_s", "REAL"), ("slip_legs", "price_age_s", "REAL"), ("slip_legs", "stats_json", "TEXT")]
CLOSE_KINDS = ("tick", "watch")  # a 'close' row repeats the last read's values at a later time: not a read


def leg_source(l: scanner.Leg) -> str:
    """The sharp source a leg's fair price came from. A pooled leg (another sport's sentinel) keeps its
    real venue in source_venue; its quote's venue is just "pool"."""
    return getattr(l, "source_venue", "") or (l.quotes[0].venue if l.quotes else "")


def fetched_ts(l: scanner.Leg) -> float | None:
    """When the price behind a leg was read: the Kalshi read of that cycle, the books cache entry's fetch,
    the older rung of an interpolated Kalshi price, or (pooled legs) what the other sentinel logged.
    With several quotes, the oldest."""
    t = getattr(l, "fetched_ts", None)
    if t:
        return t
    ts = []
    for q in l.quotes:
        d = q.detail or {}
        if d.get("fetched_ts"):
            ts.append(d["fetched_ts"])
        else:
            ts += [r["fetched_ts"] for r in d.get("rungs") or [] if isinstance(r, dict) and r.get("fetched_ts")]
    return min(ts) if ts else None


def price_age(l: scanner.Leg, at: float) -> float | None:
    """Seconds between the read behind the leg's price and `at` (slip build / logging time)."""
    t = fetched_ts(l)
    return None if t is None else round(max(0.0, at - t), 1)


def leg_ref(l: scanner.Leg, at: float | None = None) -> list:
    """A leg as logged in cycles.eligible_json: enough to find its price history again, plus its price age
    and (12th) its push probability (0 for a half line; p is then P(win | no push) for a whole line).
    p is the market's own (raw_pick); the 13th field is the shade the live builder took off it."""
    side, p = l.raw_pick
    return [l.sport, l.player, l.stat, l.line, side, round(p, 5), leg_source(l), l.start_ts,
            l.team, l.game, None if at is None else price_age(l, at), round(l.p_push or 0.0, 5), round(l.shade, 5)]


def slip_summary(entry: tuple, sent: bool = False, units: float | None = None) -> dict:
    ev, kind, combo, stacks = entry
    return {"ev": round(ev, 5), "size": len(combo), "kind": kind, "sent": sent, "units": units,
            "legs": [[l.sport, l.player, l.stat, l.line, l.pick[0], round(l.pick[1], 5)] for l in combo],
            "stacks": [s.label for s in stacks or []]}


def paper_summary(report) -> dict:
    def cand(c):
        if c.stats is None:
            return {"size": c.size, "none": c.note}
        s = c.stats
        return {"size": c.size, "ev": round(s.ev, 5), "variance": round(s.variance, 4), "ev_per_sd": round(s.metric("sharpe"), 5),
                "log_growth": round(s.growth, 6), "p_profit": round(s.p_profit, 5),
                "legs": [[l.sport, l.player, l.stat, l.line, l.pick[0], round(l.pick[1], 5)] for l in c.legs]}
    return {"chosen": None if report.chosen is None else report.chosen.size,
            "by_size": [cand(c) for _, c in sorted(report.by_size.items())],
            "excluded": report.excluded[:20], "warnings": report.warnings[:20]}


def migrate(db: sqlite3.Connection) -> None:
    for table, col, typ in MIGRATIONS:
        if col not in {r[1] for r in db.execute(f"PRAGMA table_info({table})")}:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    db.commit()


def last_read(db: sqlite3.Connection, player: str, stat: str, line: float, source: str, at: float):
    """(ts, side, p_fair) of the last same-source read of a leg at or before `at`, or None."""
    return db.execute("SELECT ts, side, p_fair FROM prices WHERE player=? AND stat=? AND line=? AND source=? "
                      f"AND kind IN {CLOSE_KINDS} AND ts <= ? ORDER BY ts DESC LIMIT 1",
                      (player, stat, line, source, at)).fetchone()


def close_fields(side: str, p_entry: float, start: float, row) -> tuple:
    """(close_p, close_ts, close_age_s, clv, note) from a last_read row; the same rule everywhere."""
    if row is None:
        return None, None, None, None, None
    close = _side_p(side, row[1], row[2])
    age = start - row[0]
    note = f"stale close ({age / 60:.0f} min before start)" if age > STALE_CLOSE_S else ""
    return close, row[0], age, close - p_entry, note


class Snapshots:
    def __init__(self, sport: str, path: Path | str | None = None):
        self.sport = sport
        self.path = Path(path or str(DB).format(sport=sport))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript(SCHEMA)
        migrate(self.db)
        self.last: dict[tuple, tuple] = {}  # price key -> (ts, p, source, row values) of its last written row

    def sibling(self, sport: str) -> Path:
        """Another sport's snapshot file next to this one (a cross-sport slip's legs are priced there)."""
        return self.path.with_name(f"snapshots_{sport}.sqlite3")

    def _prices_db(self, sport: str | None):
        if not sport or sport == self.sport:
            return self.db, False
        path = self.sibling(sport)
        if not path.exists():
            return None, False
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        con.execute("PRAGMA busy_timeout=5000")
        return con, True

    # ------------------------------------------------------------------ CLV
    def record_slip(self, builder: str, cycle_id: int, now: float, legs, ev: float, sent: bool,
                    units: float | None = None, age_at: float | None = None) -> str | None:
        """One row per leg of a chosen slip, at its entry fair price (raw_pick: unshaded, so CLV compares
        the market with itself). Paper slips re-chosen within
        DEDUPE_S are not recorded again. Returns the slip id, or None when it wasn't recorded."""
        keys = sorted((l.sport, l.player, l.stat, l.line, l.pick[0]) for l in legs)
        slip_key = json.dumps(keys)
        if builder == "paper":
            recent = self.db.execute("SELECT slip_id FROM slip_legs WHERE builder='paper' AND entry_ts > ? "
                                     "GROUP BY slip_id", (now - DEDUPE_S,)).fetchall()
            if any(r[0].split("|", 1)[1] == slip_key for r in recent):
                return None
        slip_id = f"{builder}:{now:.0f}|{slip_key}"
        at = now if age_at is None else age_at
        for l in legs:
            self.db.execute("INSERT INTO slip_legs (slip_id, builder, cycle_id, entry_ts, size, ev, sent, player, stat, line, "
                            "side, team, game, start_ts, source, p_entry, sport, units, price_age_s) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (slip_id, builder, cycle_id, now, len(legs), ev, int(sent), l.player, l.stat, l.line,
                             l.pick[0], l.team, l.game, l.start_ts, leg_source(l), l.raw_pick[1], l.sport or self.sport, units,
                             price_age(l, at)))
        self.db.commit()
        return slip_id

    def record_stats(self, slip_id: str, l: scanner.Leg, rec: dict) -> int:
        """Store a leg's stats-vs-market gap (local/stat_research.py) on its slip_legs row."""
        n = self.db.execute("UPDATE slip_legs SET stats_json=? WHERE slip_id=? AND player=? AND stat=? AND line=?",
                            (json.dumps(rec), slip_id, l.player, l.stat, l.line)).rowcount
        self.db.commit()
        return n

    def open_legs(self, now: float) -> list[dict]:
        """THIS sport's chosen legs whose event hasn't started, from every sentinel's snapshot file (a
        mixed slip logged by another sport's sentinel included): this sentinel re-prices them each cycle."""
        cols = ("player", "stat", "line", "team", "game", "start_ts", "source")
        q = (f"SELECT DISTINCT {', '.join(cols)} FROM slip_legs WHERE close_p IS NULL AND close_note IS NULL "
             "AND start_ts > ? AND (sport = ? OR sport IS NULL)")
        rows = set(self.db.execute(q, (now, self.sport)).fetchall())
        for path in self.path.parent.glob("snapshots_*.sqlite3"):
            if path == self.path:
                continue
            try:
                con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
                rows |= set(con.execute(q.replace(" OR sport IS NULL", ""), (now, self.sport)).fetchall())
                con.close()
            except sqlite3.Error:  # a sibling without slip_legs yet, or busy: next cycle
                pass
        rows |= set(self.db.execute(f"SELECT {', '.join(cols)} FROM watch_legs WHERE start_ts > ?", (now,)).fetchall())
        return [dict(zip(cols, r)) for r in sorted(rows, key=str)]

    def watch_eligible(self, legs, now: float) -> int:
        """Put this sport's eligible Kalshi-priced legs on the watch list (open_legs) until their start;
        drops entries whose game started over a day ago. Returns the legs newly added."""
        rows = [(l.player, l.stat, l.line, l.team, l.game, l.start_ts, "kalshi", now) for l in legs
                if (l.sport or self.sport) == self.sport and leg_source(l) == "kalshi" and l.start_ts and l.start_ts > now]
        before = self.db.total_changes
        self.db.executemany("INSERT OR IGNORE INTO watch_legs VALUES (?,?,?,?,?,?,?,?)", rows)
        added = self.db.total_changes - before
        self.db.execute("DELETE FROM watch_legs WHERE start_ts < ?", (now - 86400,))
        self.db.commit()
        return added

    def resolve_closes(self, now: float) -> int:
        """Fill the close (same source, last read at or before start, from the leg's own sport file) for
        chosen legs whose event started."""
        done = 0
        for rid, sport, player, stat, line, side, start, source, p_entry in self.db.execute(
                "SELECT id, sport, player, stat, line, side, start_ts, source, p_entry FROM slip_legs "
                "WHERE close_p IS NULL AND close_note IS NULL AND start_ts <= ?", (now,)).fetchall():
            con, opened = self._prices_db(sport)
            row = last_read(con, player, stat, line, source, start) if con is not None else None
            if opened:
                con.close()
            if row is None:
                self.db.execute("UPDATE slip_legs SET close_note=? WHERE id=?", (f"no {source} price before start", rid))
                continue
            self.db.execute("UPDATE slip_legs SET close_p=?, close_ts=?, close_age_s=?, clv=?, close_note=? WHERE id=?",
                            (*close_fields(side, p_entry, start, row), rid))
            done += 1
        self.db.commit()
        return done

    def has_board(self, board_ts: float) -> bool:
        return self.db.execute("SELECT 1 FROM boards WHERE board_ts=?", (board_ts,)).fetchone() is not None

    def board(self, board_ts: float, captured_at: str, props: list[dict]) -> None:
        blob = gzip.compress(json.dumps(props, separators=(",", ":")).encode())
        self.db.execute("INSERT OR IGNORE INTO boards VALUES (?,?,?,?)", (board_ts, captured_at, len(props), blob))
        self.db.commit()

    def cycle(self, now: float, board_ts: float, stats: dict, live: list[dict], paper: dict | None,
              eligible: list | None = None, coverage: dict | None = None) -> int:
        """`eligible`: the legs (leg_ref) both builders chose from, after every filter (min fair prob,
        injury guard, cooldown, whole-number lines) plus pooled legs: the backtest replays from exactly
        these. `coverage`: {stat: [legs, priced]} this cycle."""
        cur = self.db.execute("INSERT INTO cycles (ts, board_ts, legs, priced, good, whole_dropped, live_json, paper_json, "
                              "eligible_json, coverage_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
                              (now, board_ts, stats.get("legs"), stats.get("priced"), stats.get("good"),
                               stats.get("whole_dropped"), json.dumps(live), json.dumps(paper),
                               None if eligible is None else json.dumps(eligible),
                               None if coverage is None else json.dumps(coverage)))
        return cur.lastrowid

    def prices(self, cycle_id: int, now: float, legs: list[scanner.Leg], age_at: float | None = None) -> int:
        """Write changed/heartbeat price rows for priced legs, and close rows for legs whose game started."""
        written, seen = 0, set()
        for l in legs:
            if not l.quotes or not l.pick:
                continue
            q = l.quotes[0]
            key = (l.player, l.stat, l.line, l.game_date, q.venue)  # per source: CLV closes are same-source
            seen.add(key)
            age = price_age(l, now if age_at is None else age_at)
            detail = {**(q.detail or {}), "price_age_s": age, **({"shade": l.shade} if l.shade else {})}
            steam = getattr(l, "steam", None)
            side, p = l.raw_pick  # the market's price, unshaded: entries and closes on one scale
            values = (l.player, l.stat, l.line, side, l.team, l.game, l.start_ts, p, q.venue,
                      detail.get("method") or "unknown",
                      json.dumps(detail), json.dumps(steam.record() if steam else None), age)
            last = self.last.get(key)
            if last and last[2] == q.venue and abs(last[1] - p) < CHANGE and now - last[0] < HEARTBEAT_S:
                self.last[key] = (last[0], last[1], last[2], values)  # keep the newest values for the close row
                continue
            self.db.execute("INSERT INTO prices (cycle_id, ts, kind, player, stat, line, side, team, game, start_ts, p_fair, "
                            "source, method, pricing_json, steam_json, price_age_s) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (cycle_id, now, "watch" if getattr(l, "watch", False) else "tick", *values))
            self.last[key] = (now, p, q.venue, values)
            written += 1
        cutoff = now + scanner.START_BUFFER_MIN * 60
        for key in [k for k in self.last if k not in seen]:
            values = self.last[key][3]
            if values[6] and values[6] <= cutoff:  # started (or about to): the last price is the close
                self.db.execute("INSERT INTO prices (cycle_id, ts, kind, player, stat, line, side, team, game, start_ts, "
                                "p_fair, source, method, pricing_json, steam_json, price_age_s) VALUES (?,?,'close',?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                (cycle_id, now, *values))
                written += 1
            del self.last[key]
        self.db.commit()
        return written


def _side_p(side_taken: str, row_side: str, row_p: float) -> float:
    """A price row's probability for the side we took (the row may be for the other side by then)."""
    return row_p if row_side == side_taken else 1 - row_p


def load_board_props(feed: Path, sport: str) -> tuple[str, list[dict]]:
    """(captured_at, this league's raw props) from a bookmarklet board.json; ("", []) for pasted text."""
    if feed.suffix != ".json":
        return "", []
    try:
        d = json.loads(feed.read_text())
    except (OSError, ValueError):
        return "", []
    return d.get("captured_at", ""), [p for p in d.get("props") or [] if str(p.get("league", "")).upper() == sport.upper()]


# --------------------------------------------------------------------------- CLV report
def clv_lines(records: list[dict], include_stale: bool = False, builders=("live", "paper")) -> list[str]:
    """CLV summary per builder. Each record: builder, entry_ts, source, clv (None: no close yet/ever) and
    note (None: open, "": clean close, "stale close ...", "no ... price before start")."""
    out = ["CLV per leg = closing fair prob - entry fair prob, side taken, same source (points)"]
    for builder in builders:
        mine = [r for r in records if r["builder"] == builder]
        settled = [r for r in mine if r["clv"] is not None and (include_stale or not r["note"])]
        stale = sum(1 for r in mine if r["clv"] is not None and r["note"])
        open_ = sum(1 for r in mine if r["clv"] is None and not r["note"])
        missing = sum(1 for r in mine if r["clv"] is None and r["note"])
        out.append(f"\n{builder.upper()} builder: {len(mine)} legs logged, {len(settled)} with a clean close "
                   f"({stale} stale{' included' if include_stale else ' excluded'}, {open_} open, {missing} no close)")
        if not settled:
            continue
        clv = [r["clv"] for r in settled]
        beat, flat = sum(c > 1e-9 for c in clv), sum(abs(c) <= 1e-9 for c in clv)
        out.append(f"  mean CLV {sum(clv) / len(clv) * 100:+.2f} pts · beat the close {beat / len(clv):.0%} "
                   f"({beat}/{len(clv)}, {flat} unchanged)")
        for src in sorted({r["source"] for r in settled}):
            c = [r["clv"] for r in settled if r["source"] == src]
            out.append(f"  {src}: {len(c)} legs, mean {sum(c) / len(c) * 100:+.2f} pts, beat {sum(x > 1e-9 for x in c) / len(c):.0%}")
        running, total, n = [], 0.0, 0
        by_day: dict[str, list[float]] = {}
        for r in sorted(settled, key=lambda r: r["entry_ts"]):
            by_day.setdefault(time.strftime("%Y-%m-%d", time.localtime(r["entry_ts"])), []).append(r["clv"])
        for day, vals in by_day.items():
            total += sum(vals)
            n += len(vals)
            running.append(f"{day}: {len(vals)} legs, day {sum(vals) / len(vals) * 100:+.2f}, running {total / n * 100:+.2f}")
        out.append("  running mean by entry day: " + " | ".join(running))
    return out


def clv_report(paths: list[Path] | None = None, include_stale: bool = False) -> str:
    """Per-leg CLV of the chosen slips the sentinels logged (slip_legs), by builder."""
    paths = paths if paths is not None else sorted((ROOT / "data").glob("snapshots_*.sqlite3"))
    records = []
    for p in paths:
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        try:
            records += [dict(zip(("builder", "entry_ts", "source", "clv", "note"), r)) for r in
                        con.execute("SELECT builder, entry_ts, source, clv, close_note FROM slip_legs").fetchall()]
        except sqlite3.OperationalError:  # an older database without the table
            pass
        con.close()
    if not records:
        return "CLV: no chosen slips logged yet"
    return "\n".join(clv_lines(records, include_stale))


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--clv", action="store_true", help="print the CLV report (default action)")
    ap.add_argument("--include-stale", action="store_true", help="count closes read long before start")
    a = ap.parse_args()
    print(clv_report(include_stale=a.include_stale))


if __name__ == "__main__":
    main()
