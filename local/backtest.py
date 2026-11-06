"""
backtest.py — Task 4: the live builder vs the paper optimizer, replayed from the snapshot logs.

Reads every data/snapshots_<sport>.sqlite3 (read-only) and reports, per builder:

  slips     chosen slips. LIVE = the slips the sentinel sent (slip_legs, builder "live"). PAPER = the
            optimizer REPLAYED on every logged cycle: the cycle's eligible legs (cycles.eligible_json,
            after every live filter, pooled legs included) at their logged fair probabilities, with steam
            RECOMPUTED from the full price history (never the stored steam_json), deduped like the live
            logger (the same leg set once per snapshots.DEDUPE_S). The replay is checked against the
            optimizer pick the sentinel logged in the same cycle. Cycles logged before eligible_json
            existed are rebuilt from the price rows alive at that time (approximate: no injury
            guard/cooldown; flagged in the report).
  results   legs joined to settled outcomes (local/grade_slips.Grader on ESPN box scores, cached in
            data/backtest_outcomes.sqlite3; --grade fetches, otherwise the cache only). Pushes/voids drop
            the leg as on PrizePicks (grade_slips.payout). A slip with any leg pending/review is unsettled.
            ROI per $1 (flat $1 a slip), max drawdown ($ and units), profit frequency (payout > 1x), model
            EV vs realized, and the same at the quarter-Kelly stake (recorded for live slips, recomputed
            for paper; a 0u stake = the slip would be skipped under staking.below_min "skip").
  CLV       every leg of every chosen slip: entry fair prob vs the last same-source read at or before
            the start (from the leg's own sport file), side taken (snapshots.close_fields).
  coverage  priced legs / board legs per cycle, eligible legs, whole-number lines without a push price,
            and which stats go unpriced (cycles.coverage_json).

  python3 -m local.backtest                    # all snapshot files, cached outcomes only (no network)
  python3 -m local.backtest --grade            # grade settled legs from ESPN first
  python3 -m local.backtest --db PATH --slips-csv out.csv --config variant.json
"""
from __future__ import annotations

import argparse
import bisect
import csv
import json
import math
import re
import sqlite3
import statistics
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from local import grade_slips, integrity, optimizer, pipeline_config, sentinel, signals, snapshots, steam
from local import prize_picks_ev_scraper as scanner

OUTCOMES = snapshots.ROOT / "data" / "backtest_outcomes.sqlite3"
FINAL = ("hit", "miss", "push", "void")
FALLBACK_ALIVE_S = snapshots.HEARTBEAT_S + 180  # a leg with no row this long is gone (pre-eligible_json cycles)
MIN_N = 30  # a slice with fewer distinct legs is marked "(insufficient data)"
AGE_BUCKETS = [(60, "0-60s"), (300, "1-5m"), (900, "5-15m"), (3600, "15-60m"), (float("inf"), ">60m")]
P_BUCKETS = [(0.54, "50-54%"), (0.56, "54-56%"), (0.60, "56-60%"), (float("inf"), "60%+")]


# --------------------------------------------------------------------------- history
@dataclass
class Read:
    ts: float
    kind: str
    side: str
    p: float          # fair probability of `side` at that read
    team: str
    game: str
    pricing: str | None = None    # the pricing record (JSON) behind this read
    age_s: float | None = None    # price age when the row was written

    @property
    def p_over(self) -> float:
        return self.p if self.side == "More" else 1 - self.p


def series_key(player, stat, line, source, start_ts) -> tuple:
    return player, stat, float(line), source, round(start_ts or 0)


class History:
    """One sport's snapshot file in memory: cycles, per-(leg, source) price series, logged slips."""

    def __init__(self, path: Path, sport: str):
        self.path, self.sport = path, sport
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        cols = lambda t: {r[1] for r in con.execute(f"PRAGMA table_info({t})")}
        ccols = cols("cycles")
        extra = [c if c in ccols else f"NULL AS {c}" for c in ("eligible_json", "coverage_json")]
        self.cycles = [dict(zip(("id", "ts", "board_ts", "legs", "priced", "good", "whole_dropped", "live_json",
                                 "paper_json", "eligible_json", "coverage_json"), r))
                       for r in con.execute("SELECT id, ts, board_ts, legs, priced, good, whole_dropped, live_json, "
                                            f"paper_json, {', '.join(extra)} FROM cycles ORDER BY ts")]
        self.series: dict[tuple, list[Read]] = defaultdict(list)   # reads only (tick/watch)
        self.board_series: dict[tuple, list[Read]] = defaultdict(list)  # tick rows (board legs)
        self.closed: dict[tuple, list[float]] = defaultdict(list)    # 'close' marker times
        age_col = "price_age_s" if "price_age_s" in cols("prices") else "NULL"
        for ts, kind, player, stat, line, side, team, game, start, p, source, pricing, age in con.execute(
                "SELECT ts, kind, player, stat, line, side, team, game, start_ts, p_fair, source, pricing_json, "
                f"{age_col} FROM prices ORDER BY ts"):
            key = series_key(player, stat, line, source, start)
            r = Read(ts, kind, side, p, team, game, pricing, age)
            if kind == "close":
                self.closed[key].append(ts)
                continue
            self.series[key].append(r)
            if kind == "tick":
                self.board_series[key].append(r)
        self._ts = {k: [r.ts for r in v] for k, v in self.series.items()}
        self.slip_legs: list[dict] = []
        if "slip_legs" in {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
            scols = cols("slip_legs")
            names = ["slip_id", "builder", "entry_ts", "ev", "player", "stat", "line", "side", "team", "game",
                     "start_ts", "source", "p_entry", "sport", "units", "price_age_s", "stats_json", "leg_json"]
            sel = [c if c in scols else f"NULL AS {c}" for c in names]
            self.slip_legs = [dict(zip(names, r)) for r in con.execute(f"SELECT {', '.join(sel)} FROM slip_legs ORDER BY id")]
        # the stats-vs-market gap logged on chosen legs (stat_research), by leg: the first one logged
        self.stats: dict[tuple, dict] = {}
        for r in self.slip_legs:
            if r["stats_json"]:
                ident = (r["sport"] or sport, r["player"], r["stat"], float(r["line"]), r["side"], r["source"],
                         round(r["start_ts"] or 0))
                self.stats.setdefault(ident, json.loads(r["stats_json"]))
        # logged-only signals (local/signals.py): per-leg meta (first logged), JIT events, shadow promo prices,
        # line gaps and watchlist coverage per cycle. Absent in older files: every lookup then reports not_logged.
        self.leg_meta: dict[tuple, dict] = {}
        for r in self.slip_legs:
            if r.get("leg_json"):
                ident = (r["sport"] or sport, r["player"], r["stat"], float(r["line"]), r["side"], r["source"],
                         round(r["start_ts"] or 0))
                self.leg_meta.setdefault(ident, json.loads(r["leg_json"]))
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.jit: dict[tuple, list[dict]] = defaultdict(list)
        if "jit_events" in tables:
            for cyc, sp, player, stat, line, source, start, refreshed, delta in con.execute(
                    "SELECT cycle_ts, sport, player, stat, line, source, start_ts, refreshed, delta_json FROM jit_events "
                    "ORDER BY ts"):
                self.jit[(sp or sport, player, stat, float(line), source, round(start or 0), cyc)].append(
                    {"refreshed": bool(refreshed), "delta": json.loads(delta) if delta else None})
        self.shadow: list[dict] = []
        if "shadow_prices" in tables:
            cols_s = ("ts", "sport", "player", "stat", "line", "odds_type", "side", "team", "game", "start_ts", "p_fair",
                      "pricing_json", "calc_version")
            self.shadow = [dict(zip(cols_s, r)) for r in con.execute(f"SELECT {', '.join(cols_s)} FROM shadow_prices ORDER BY ts")]
        # PropLine shadow (local/propline_shadow.py): every read and no-read row, and each sweep. Absent in older files.
        self.propline: list[dict] = []
        self.propline_sweeps: list[dict] = []
        if "propline_prices" in tables:
            cols_p = ("ts", "sport", "player", "stat", "line", "team", "game", "start_ts", "event_id", "read_kind",
                      "fetched_ts", "p_live", "p_shin", "p_sharp", "n_books", "unpriced_reason", "presence_json", "ages_json")
            self.propline = [dict(zip(cols_p, r)) for r in
                             con.execute(f"SELECT {', '.join(cols_p)} FROM propline_prices ORDER BY ts, id")]
        if "propline_sweeps" in tables:
            self.propline_sweeps = [{"ts": ts, "sport": sp, "requests": n, **json.loads(js or "{}")} for ts, sp, n, js in
                                    con.execute("SELECT ts, sport, requests, sweep_json FROM propline_sweeps ORDER BY ts")]
        self.line_gaps, self.watch_cov = [], []
        if "line_gap_json" in ccols:
            for ts, lg, wc in con.execute("SELECT ts, line_gap_json, watchlist_json FROM cycles ORDER BY ts"):
                if lg:
                    self.line_gaps += [{**g, "cycle_ts": ts} for g in json.loads(lg)]
                if wc:
                    self.watch_cov.append({**json.loads(wc), "cycle_ts": ts})
        con.close()

    def read_at(self, key: tuple, t: float) -> Read | None:
        """The last same-source read at or before t (prices are piecewise constant between rows)."""
        ts = self._ts.get(key)
        if not ts:
            return None
        i = bisect.bisect_right(ts, t)
        return self.series[key][i - 1] if i else None

    def integrity_at(self, key: tuple, t: float, side: str, cfg: dict) -> tuple[dict, list[str]] | None:
        """local/integrity.py's features and flags for the price in force at t, from its logged pricing
        record; the age is exact from fetched_ts (else the logged age carried forward). None: no read."""
        r = self.read_at(key, t)
        if r is None:
            return None
        try:
            detail = json.loads(r.pricing) if r.pricing else {}
        except ValueError:
            detail = {}
        age = (t - detail["fetched_ts"] if detail.get("fetched_ts")
               else r.age_s + (t - r.ts) if r.age_s is not None else None)
        f = integrity.detail_features(detail, key[3], side, None if age is None else max(age, 0.0))
        return f, integrity.flags(f, cfg)

    def steam_at(self, key: tuple, t: float) -> steam.SteamInfo | None:
        """steam.py's feature recomputed from the full history: the picked-side move from the value AT the
        window start (or the first read, if the history is shorter) to the read at t."""
        now = self.read_at(key, t)
        if now is None:
            return None
        src = key[3]
        window = steam.WINDOW_S.get(src, steam.WINDOW_S["kalshi"])
        start = self.read_at(key, t - window)
        if start is None:
            start, span = self.series[key][0], t - self.series[key][0].ts
        else:
            span = window
        sign = 1 if now.p_over >= 0.5 else -1
        score = sign * (now.p_over - start.p_over) if span >= steam.MIN_SPAN_S.get(src, steam.MIN_SPAN_S["kalshi"]) else None
        return steam.SteamInfo(src, score, span, start.p_over if sign > 0 else 1 - start.p_over,
                               now.p_over if sign > 0 else 1 - now.p_over,
                               score is not None and abs(score) >= steam.DETECT, None, None)

    def alive_at(self, t: float) -> list[tuple[tuple, Read]]:
        """Board legs priced around t, for cycles logged before eligible_json: the newest source per leg."""
        best: dict[tuple, tuple[tuple, Read]] = {}
        for key, rows in self.board_series.items():
            i = bisect.bisect_right([r.ts for r in rows], t)
            if not i or t - rows[i - 1].ts > FALLBACK_ALIVE_S:
                continue
            r = rows[i - 1]
            if any(r.ts < c <= t for c in self.closed.get(key, ())):
                continue
            prop = key[:3] + key[4:]
            if prop not in best or best[prop][1].ts < r.ts:
                best[prop] = (key, r)
        return list(best.values())


def sport_of(path: Path, default: str = "") -> str:
    m = re.match(r"snapshots_(\w+)\.sqlite3$", path.name)
    return m.group(1) if m else default


# --------------------------------------------------------------------------- slips
@dataclass
class LegEntry:
    sport: str
    player: str
    stat: str
    line: float
    side: str
    p_entry: float
    source: str
    start_ts: float | None
    team: str = ""
    game: str = ""
    age_s: float | None = None              # price age at slip build (None: not logged before 2026-10-19)
    steam_detected: bool | None = None      # recomputed steam at entry (None: history too short)
    flags: list | None = None               # integrity flags at entry (None: no price record)
    price_kind: str | None = None           # integrity.kind at entry
    stats: dict | None = None               # stats-vs-market gap record (None: not logged)
    p_push: float = 0.0                     # whole line: P(push); p_entry is then P(win | no push)

    @property
    def key(self) -> tuple:
        return self.sport, self.player, self.stat, self.line, self.side

    @property
    def identity(self) -> tuple:
        """One leg: a leg chosen in several slips (or by both builders) is the same observation."""
        return self.sport, self.player, self.stat, float(self.line), self.side, self.source, round(self.start_ts or 0)


@dataclass
class Slip:
    builder: str
    db_sport: str
    entry_ts: float
    legs: list[LegEntry]
    ev_net: float
    units: float | None
    results: list[str] = field(default_factory=list)
    payout: float | None = None   # None: unsettled
    unsettled: str = ""


def to_leg(e: LegEntry, steam_info=None) -> scanner.Leg:
    l = scanner.Leg(e.player, e.stat, e.line, e.game, team=e.team, sport=e.sport,
                    start_time=datetime.fromtimestamp(e.start_ts, timezone.utc).isoformat() if e.start_ts else "")
    win = e.p_entry * (1 - e.p_push)
    l.quotes = [scanner.Quote(e.source or "replay", "replay", "replay", win if e.side == "More" else 1 - e.p_push - win, 0.0,
                              p_push=e.p_push if float(e.line) % 1 == 0 else None)]
    l.source_venue = e.source
    l.steam = steam_info
    l.price_age_s = e.age_s
    l.integrity_flags, l.price_kind = e.flags, e.price_kind
    return l


def from_leg(l: scanner.Leg) -> LegEntry:
    st = getattr(l, "steam", None)
    return LegEntry(l.sport, l.player, l.stat, l.line, l.pick[0], l.pick[1], snapshots.leg_source(l), l.start_ts,
                    l.team, l.game, getattr(l, "price_age_s", None),
                    None if st is None or st.score is None else st.detected,
                    getattr(l, "integrity_flags", None), getattr(l, "price_kind", None), p_push=l.p_push or 0.0)


def live_slips(h: History) -> list[Slip]:
    by_id: dict[str, list[dict]] = {}
    for r in h.slip_legs:
        if r["builder"] == "live":
            by_id.setdefault(r["slip_id"], []).append(r)
    out = []
    for rows in by_id.values():
        r0 = rows[0]
        legs = [LegEntry(r["sport"] or h.sport, r["player"], r["stat"], r["line"], r["side"], r["p_entry"], r["source"],
                         r["start_ts"], r["team"] or "", r["game"] or "", r["price_age_s"],
                         stats=json.loads(r["stats_json"]) if r["stats_json"] else None) for r in rows]
        out.append(Slip("live", h.sport, r0["entry_ts"], legs, r0["ev"], r0["units"]))
    return out


@dataclass
class Replay:
    slips: list[Slip]
    cycles: int = 0
    exact: int = 0            # cycles replayed from eligible_json
    approximate: int = 0      # cycles rebuilt from alive price rows
    agree: int = 0            # replayed pick == logged pick (legs and size, or both none)
    disagree: int = 0
    no_history: int = 0       # eligible legs with no price series (steam unavailable)
    steam_scored: int = 0
    steam_detected: int = 0
    flagged: int = 0          # eligible leg-cycles with an integrity flag
    vetoed: int = 0           # ... removed because the config enforces integrity
    notes: list[str] = field(default_factory=list)


def floor_for(cfg: dict, sport: str, min_ev: float) -> float:
    """The live sentinel's leg floor (sentinel.leg_floor) under this config."""
    return max(min_ev, cfg.get("min_fair_by_sport", {}).get(sport, 0.0))


def screen_entry(e: LegEntry, hist: History | None, key: tuple, t: float, cfg: dict, rep: Replay) -> bool:
    """Set the leg's integrity flags at t; False if the config enforces integrity and it is flagged."""
    got = hist.integrity_at(key, t, e.side, cfg) if hist else None
    if got is not None:
        e.price_kind, e.flags = got[0]["kind"], got[1]
        rep.flagged += bool(e.flags)
        if e.flags and cfg["integrity"]["enforce"]:
            rep.vetoed += 1
            return False
    return True


def replay_paper(h: History, histories: dict[str, History], cfg: dict, min_ev: float,
                 since: float = 0.0) -> Replay:
    rep = Replay([])
    recent: list[tuple[float, frozenset]] = []
    for c in h.cycles:
        if c["ts"] < since:
            continue
        rep.cycles += 1
        legs = []
        if c["eligible_json"] is not None:
            rep.exact += 1
            for ref in json.loads(c["eligible_json"]):
                sport, player, stat, line, side, p, source, start, team, game = ref[:10]
                e = LegEntry(sport or h.sport, player, stat, line, side, p, source, start, team or "", game or "",
                             ref[10] if len(ref) > 10 else None, p_push=ref[11] if len(ref) > 11 else 0.0)
                if e.p_push is None or (float(line) % 1 == 0 and len(ref) <= 11):
                    continue  # a whole line logged before push pricing: it never entered a slip
                if p < floor_for(cfg, e.sport, min_ev):  # a variant's stricter per-sport floor
                    continue
                hist = histories.get(e.sport)
                key = series_key(player, stat, line, source, start)
                st = hist.steam_at(key, c["ts"]) if hist else None
                rep.no_history += st is None
                if not screen_entry(e, hist, key, c["ts"], cfg, rep):
                    continue
                legs.append(to_leg(e, st))
        else:
            rep.approximate += 1
            for key, r in h.alive_at(c["ts"]):
                player, stat, line, source, start = key
                side = "More" if r.p_over >= 0.5 else "Less"
                e = LegEntry(h.sport, player, stat, line, side, max(r.p_over, 1 - r.p_over), source, start, r.team, r.game)
                if not (e.p_entry >= floor_for(cfg, h.sport, min_ev) and float(line) % 1 and (not start or start - scanner.START_BUFFER_MIN * 60 > c["ts"])):
                    continue
                if screen_entry(e, h, key, c["ts"], cfg, rep):
                    legs.append(to_leg(e, h.steam_at(key, c["ts"])))
        for l in legs:
            if l.steam is not None and l.steam.score is not None:
                rep.steam_scored += 1
                rep.steam_detected += l.steam.detected
        report = optimizer.build(legs, cfg=cfg)
        chosen = report.chosen
        mine = None if chosen is None else frozenset((l.player, l.stat, float(l.line), l.pick[0]) for l in chosen.legs)
        logged = json.loads(c["paper_json"]) if c["paper_json"] else None
        if logged is not None:
            pick = next((b for b in logged["by_size"] if b["size"] == logged["chosen"]), None)
            theirs = None if pick is None else frozenset((x[1], x[2], float(x[3]), x[4]) for x in pick["legs"])
            if theirs == mine:
                rep.agree += 1
            else:
                rep.disagree += 1
                if len(rep.notes) < 5:
                    rep.notes.append(f"cycle {c['id']}: replay {sorted(mine or [])} vs logged {sorted(theirs or [])}")
        if chosen is None:
            continue
        keys = frozenset((l.sport, l.player, l.stat, l.line, l.pick[0]) for l in chosen.legs)
        recent = [(t, k) for t, k in recent if c["ts"] - t < snapshots.DEDUPE_S]
        if any(k == keys for _, k in recent):
            continue
        recent.append((c["ts"], keys))
        entry = (chosen.stats.ev - 1, "flex", chosen.legs, [])
        units = sentinel.stake_units(entry, cfg["staking"]["kelly"], cfg["staking"]["unit_pct"], cfg["staking"]["below_min"])
        rep.slips.append(Slip("paper", h.sport, c["ts"], [from_leg(l) for l in chosen.legs], chosen.stats.ev - 1, units))
    return rep


# --------------------------------------------------------------------------- outcomes
class Outcomes:
    """Leg results keyed (sport, player, stat, line, start): the More-side result from grade_slips.Grader,
    flipped for Less. Only final results (hit/miss/push/void) are cached."""

    def __init__(self, path: Path = OUTCOMES, grader=None, fetch: bool = False):
        self.path, self.fetch = path, fetch
        self.grader = grader
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS outcomes (sport TEXT, player TEXT, stat TEXT, line REAL, start_ts INTEGER, "
                        "team TEXT, result_more TEXT, actual REAL, note TEXT, graded_at REAL, "
                        "PRIMARY KEY (sport, player, stat, line, start_ts))")
        self.fetched = 0

    def result(self, e: LegEntry, now: float) -> tuple[str, str]:
        """(result for the side taken, note)."""
        if not e.start_ts:
            return "review", "no start time"
        k = (e.sport, e.player, e.stat, float(e.line), round(e.start_ts))
        row = self.db.execute("SELECT result_more, note FROM outcomes WHERE sport=? AND player=? AND stat=? AND line=? "
                              "AND start_ts=?", k).fetchone()
        if row is None:
            if e.start_ts > now - grade_slips.SETTLE_AFTER_H * 3600:
                return "pending", "not settled yet"
            if not self.fetch:
                return "ungraded", "not graded (run with --grade)"
            if self.grader is None:
                self.grader = grade_slips.Grader(get=grade_slips.get_json)  # looked up now: tests block it
            try:
                g = self.grader.leg({"sport": e.sport, "player": e.player, "stat": e.stat, "line": e.line, "side": "More",
                                     "team": e.team, "start_time": datetime.fromtimestamp(e.start_ts, timezone.utc).isoformat()})
            except (OSError, ValueError, KeyError) as err:
                return "review", f"grader error {err!r}"
            self.fetched += 1
            if g["result"] not in FINAL:
                return g["result"], g.get("note", "")
            self.db.execute("INSERT OR REPLACE INTO outcomes VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (*k, e.team, g["result"], g.get("actual"), g.get("note", ""), now))
            self.db.commit()
            row = (g["result"], g.get("note", ""))
        res = row[0]
        if e.side == "Less" and res in ("hit", "miss"):
            res = "miss" if res == "hit" else "hit"
        return res, row[1] or ""


def settle(slips: list[Slip], outcomes: Outcomes, now: float) -> None:
    for s in slips:
        got = [outcomes.result(e, now) for e in s.legs]
        s.results = [r for r, _ in got]
        open_ = [r for r in s.results if r not in FINAL]
        if open_:
            s.unsettled = Counter(open_).most_common(1)[0][0]
        else:
            s.payout = grade_slips.payout(s.results)


# --------------------------------------------------------------------------- report
def drawdown(pnl: list[float]) -> float:
    peak = cum = dd = 0.0
    for x in pnl:
        cum += x
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return dd


def builder_lines(name: str, slips: list[Slip]) -> list[str]:
    out = [f"\n{name.upper()} builder: {len(slips)} slips chosen"]
    if not slips:
        return out
    sizes = Counter(len(s.legs) for s in slips)
    out.append("  sizes " + ", ".join(f"{n}-pick {c}" for n, c in sorted(sizes.items()))
               + f" · mean model EV {sum(s.ev_net for s in slips) / len(slips):+.1%}")
    done = sorted((s for s in slips if s.payout is not None), key=lambda s: s.entry_ts)
    why = Counter(s.unsettled for s in slips if s.payout is None)
    out.append(f"  settled {len(done)}" + (f", unsettled {sum(why.values())} (" + ", ".join(f"{k} {v}" for k, v in why.most_common()) + ")" if why else ""))
    if not done:
        return out
    flat = [s.payout - 1 for s in done]
    out.append(f"  flat $1: ROI per $1 {sum(flat) / len(done):+.1%} (net {sum(flat):+.2f} on {len(done)}), "
               f"max drawdown ${drawdown(flat):.2f}, profit frequency {sum(s.payout > 1 + 1e-9 for s in done) / len(done):.0%}, "
               f"model EV of these {sum(s.ev_net for s in done) / len(done):+.1%}")
    staked = [s for s in done if s.units]
    skipped = sum(1 for s in done if s.units == 0)
    if staked:
        u = [s.units * (s.payout - 1) for s in staked]
        out.append(f"  quarter Kelly: {sum(s.units for s in staked):.1f}u staked on {len(staked)}, net {sum(u):+.2f}u, "
                   f"ROI {sum(u) / sum(s.units for s in staked):+.1%}, max drawdown {drawdown(u):.2f}u"
                   + (f" ({skipped} would be skipped: Kelly stake under the minimum)" if skipped else ""))
    legs = [(e, r) for s in done for e, r in zip(s.legs, s.results) if r in ("hit", "miss")]
    if legs:
        out.append(f"  legs: hit {sum(r == 'hit' for _, r in legs) / len(legs):.1%} of {len(legs)} vs mean fair "
                   f"{sum(e.p_entry for e, _ in legs) / len(legs):.1%} (calibration)")
    return out


def clv_records(slips: list[Slip], histories: dict[str, History], now: float) -> list[dict]:
    recs = []
    for s in slips:
        for e in s.legs:
            rec = {"builder": s.builder, "entry_ts": s.entry_ts, "source": e.source, "clv": None, "note": None}
            hist = histories.get(e.sport)
            if not e.start_ts or e.start_ts > now:
                recs.append(rec)   # open: the event hasn't started
                continue
            row = hist.read_at(series_key(e.player, e.stat, e.line, e.source, e.start_ts), e.start_ts) if hist else None
            if row is None:
                rec["note"] = f"no {e.source} price before start" + ("" if hist else f" ({e.sport} file not loaded)")
            else:
                close, _, _, clv, note = snapshots.close_fields(e.side, e.p_entry, e.start_ts, (row.ts, row.side, row.p))
                rec.update(clv=clv, note=note)
            recs.append(rec)
    return recs


def leg_records(slips: list[Slip], histories: dict[str, History], now: float, cfg: dict | None = None) -> list[dict]:
    """One record per leg of every chosen slip: builder, CLV (clv_records), price age, steam at entry
    (recomputed from the price history), stat, entry fair probability and the settled result."""
    out = []
    clv = iter(clv_records(slips, histories, now))
    for s in slips:
        results = s.results or [""] * len(s.legs)
        for e, res in zip(s.legs, results):
            rec = next(clv)
            detected = e.steam_detected
            if detected is None and e.source and e.sport in histories:
                st = histories[e.sport].steam_at(series_key(e.player, e.stat, e.line, e.source, e.start_ts), s.entry_ts)
                detected = None if st is None or st.score is None else st.detected
            stats = e.stats if e.stats is not None else histories.get(e.sport, None) and histories[e.sport].stats.get(e.identity)
            flags, kind = e.flags, e.price_kind
            if flags is None and e.source and e.sport in histories:
                got = histories[e.sport].integrity_at(series_key(e.player, e.stat, e.line, e.source, e.start_ts),
                                                      s.entry_ts, e.side, cfg or pipeline_config.CONFIG)
                flags, kind = (got[1], got[0]["kind"]) if got else (None, None)
            out.append({**rec, "identity": e.identity, "age_s": e.age_s, "steam": detected, "stat": e.stat,
                        "p": e.p_entry, "result": res, "flags": flags, "kind": kind,
                        "gap": (stats or {}).get("gap"), "stats_note": None if stats is None else stats.get("note"),
                        # logged-only signal fields (signal_lines); the slices above never read them
                        "player": e.player, "game": e.game, "start": e.start_ts, "sport": e.sport,
                        "close_reason": close_reason(e, s.entry_ts, histories.get(e.sport), now),
                        "meta": histories[e.sport].leg_meta.get(e.identity) if e.sport in histories else None,
                        "jit": jit_delta(e, s.entry_ts, histories.get(e.sport))})
    return out


def close_reason(e: LegEntry, entry_ts: float, hist: History | None, now: float) -> str:
    """Closing-data quality of one leg: open (not started) | missing | stale | mismatch (the close read is for
    another team/game) | same_fetch (a books close read from the very cache fetch the entry was priced from:
    CLV 0 by construction) | ok. Only ok closes enter a signal's CLV; the reasons are reported per slice."""
    if not e.start_ts or e.start_ts > now:
        return "open"
    key = series_key(e.player, e.stat, e.line, e.source, e.start_ts)
    row = hist.read_at(key, e.start_ts) if hist else None
    if row is None:
        return "missing"
    if e.start_ts - row.ts > snapshots.STALE_CLOSE_S:
        return "stale"
    if (e.team and row.team and e.team != row.team) or (e.game and row.game and e.game != row.game):
        return "mismatch"
    if e.source == "books":
        ent = hist.read_at(key, entry_ts)

        def fetched(r):
            try:
                return (json.loads(r.pricing) if r is not None and r.pricing else {}).get("fetched_ts")
            except ValueError:
                return None
        if ent is not None and fetched(ent) is not None and fetched(ent) == fetched(row):
            return "same_fetch"
    return "ok"


def jit_delta(e: LegEntry, entry_ts: float, hist: History | None) -> dict | None:
    """The prob_delta record of this leg's JIT refresh in the cycle that chose it (None: not refreshed)."""
    if hist is None:
        return None
    evs = hist.jit.get((e.sport, e.player, e.stat, float(e.line), e.source, round(e.start_ts or 0), entry_ts)) or []
    got = [x["delta"] for x in evs if x["refreshed"] and x["delta"]]
    return got[-1] if got else None


def distinct(recs: list[dict]) -> list[dict]:
    """First entry of each leg (by entry time): repeats of one leg are one observation, not several."""
    seen, out = set(), []
    for r in sorted(recs, key=lambda r: r["entry_ts"]):
        if r["identity"] not in seen:
            seen.add(r["identity"])
            out.append(r)
    return out


GAP_BUCKETS = [(-0.15, "gap < -15 pts"), (-0.05, "-15 to -5 pts"), (0.05, "-5 to +5 pts"), (0.15, "+5 to +15 pts"),
               (float("inf"), "gap > +15 pts")]


def gap_bucket(gap: float | None, note: str | None) -> str:
    if gap is None:
        return "not logged" if note is None else "gap n/a"
    return next(label for top, label in GAP_BUCKETS if gap < top)


def age_bucket(age: float | None) -> str:
    if age is None:
        return "age not logged"
    return next(label for top, label in AGE_BUCKETS if age < top)


def p_bucket(p: float) -> str:
    return next(label for top, label in P_BUCKETS if p < top)


def flag(n: int) -> str:
    return "  (insufficient data)" if n < MIN_N else ""


def clv_cell(recs: list[dict]) -> str:
    if not recs:
        return "n=0"
    c = [r["clv"] for r in recs]
    return f"n={len(c):<4} mean {sum(c) / len(c) * 100:+6.2f} pts  beat {sum(x > 1e-9 for x in c) / len(c):4.0%}" + flag(len(c))


def slice_lines(recs: list[dict]) -> list[str]:
    """CLV slices (clean closes only) by source, price age, steam at entry and stat, each for all builders
    and per builder, plus a calibration table on settled legs. n = distinct legs."""
    clean = [r for r in recs if r["clv"] is not None and not r["note"]]
    out = [f"\nCLV SLICES (clean closes only; n = distinct legs, a leg in several slips or both builders counts once; "
           f"n < {MIN_N} = insufficient data)", " " + snapshots.CLOSING_WARNING]

    def block(title, key, order=None, top=None):
        groups: dict[str, list[dict]] = defaultdict(list)
        for r in clean:
            k = key(r)
            for label in (k if isinstance(k, list) else [k]):  # a leg with two flags is in both slices
                groups[label].append(r)
        labels = order or sorted(groups, key=lambda g: (-len(distinct(groups[g])), g))
        labels = [g for g in labels if g in groups][:top] if top else [g for g in labels if g in groups]
        out.append(f" by {title}" + ("" if labels else ": no clean closes yet"))
        for g in labels:
            mine = groups[g]
            out.append(f"   {g:<22} all   {clv_cell(distinct(mine))}")
            for b in ("live", "paper"):
                out.append(f"   {'':<22} {b:<5} {clv_cell(distinct([r for r in mine if r['builder'] == b]))}")

    block("source", lambda r: r["source"] or "unknown")
    block("price age at entry", lambda r: age_bucket(r["age_s"]), [l for _, l in AGE_BUCKETS] + ["age not logged"])
    block("steam at entry (recomputed)",
          lambda r: {True: "steam detected", False: "no steam"}.get(r["steam"], "steam not scorable"),
          ["steam detected", "no steam", "steam not scorable"])
    block("stat (top 10 by legs)", lambda r: r["stat"], top=10)
    block("market integrity at entry (a leg with several flags is in each)",
          lambda r: "no price record" if r.get("flags") is None else (r["flags"] or ["clean"]),
          ["clean", *integrity.FLAGS, "no price record"])
    block("stats-vs-market gap at entry (recent hit rate of our side - fair; a feature, never a veto)",
          lambda r: gap_bucket(r.get("gap"), r.get("stats_note")), [l for _, l in GAP_BUCKETS] + ["gap n/a", "not logged"])
    block("price kind", lambda r: r.get("kind") or "no price record",
          ["kalshi", "kalshi interp", "kalshi whole", "books main", "books alt", "books interp", "books whole",
           "no price record"])

    graded = [r for r in recs if r["result"] in ("hit", "miss")]
    out.append(" calibration by entry fair probability (settled legs, pushes/voids excluded)")
    if not graded:
        out.append("   no settled legs yet")
    for _, label in P_BUCKETS:
        for b, sel in (("all", graded), ("live", [r for r in graded if r["builder"] == "live"]),
                       ("paper", [r for r in graded if r["builder"] == "paper"])):
            legs = distinct([r for r in sel if p_bucket(r["p"]) == label])
            if not graded:
                break
            cell = (f"n={len(legs):<4} mean fair {sum(r['p'] for r in legs) / len(legs):5.1%}  hit "
                    f"{sum(r['result'] == 'hit' for r in legs) / len(legs):5.1%}" + flag(len(legs))) if legs else "n=0"
            out.append(f"   {label if b == 'all' else '':<22} {b:<5} {cell}")
    return out


# --------------------------------------------------------------------------- logged-only signals (2026-11-04)
DELTA_ORDER = ["toward", "against", "none"]
GAP_UNIT_BUCKETS = [(1e-9, "0 (same line)"), (0.5 + 1e-9, "0.5"), (1 + 1e-9, "1"), (2 + 1e-9, "1.5-2"),
                    (float("inf"), "> 2")]
LEG_DISTINCT_NOTE = ("n = distinct legs: identity (sport, player, stat, line, side, source, start); a leg re-chosen "
                     "in several slips (or by both builders) counts once, a moved line is a new leg")


def _missing_label(rec: dict | None, name: str) -> str:
    return f"n/a: {signals.reason(rec, name)}"


def signal_block(out: list[str], title: str, recs: list[dict], key, order: list[str] | None = None) -> None:
    """One signal slice: per bucket, closing-data quality (distinct legs) then CLV on the usable closes, with
    distinct games/players, mean, median, positive share and a 95% CI."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in recs:
        groups[key(r)].append(r)
    labels = [g for g in (order or []) if g in groups] + sorted(g for g in groups if g not in (order or []))
    out.append(f" by {title}" + ("" if labels else ": nothing logged yet"))
    for g in labels:
        mine = distinct(groups[g])
        out.append(f"   {g:<28} {signals.quality_line(mine)}")
        out.append(f"   {'':<28} CLV {signals.signal_cell([r for r in mine if r['close_reason'] == 'ok'])}")


def signal_lines(recs: list[dict], histories: dict[str, History], outcomes: "Outcomes", now: float) -> list[str]:
    """Builds 2-5 (local/signals.py): logged-only signals, reported apart from everything above. Never used by a
    builder; 30 distinct legs is a preliminary-look threshold only, 300 an evaluation target, neither a licence
    for a production change (that needs replay + out-of-sample validation + explicit approval)."""
    out = ["\nLOGGED-ONLY SIGNALS (Builds 2-5; never used by a builder; 30 distinct legs = preliminary look only, "
           "300 = evaluation target, neither authorises a production change)", " " + snapshots.CLOSING_WARNING,
           " " + LEG_DISTINCT_NOTE]
    out.append("\n BUILD 2 · prob_delta at the JIT refresh (current fair prob - last DISTINCT earlier price, side taken)")
    out.append(" " + snapshots.CLOSING_WARNING)
    out.append(f" floor: when prev_from is price_log, moves under {snapshots.CHANGE * 100:g} pts since the last logged row are "
               "unobservable (never written); when prev_from is pre_refresh_quote there is no such floor")
    jd = lambda r, name: (signals.value(r["jit"], name) if r.get("jit") else None)
    signal_block(out, "direction", recs,
                 lambda r: "no JIT refresh logged" if not r.get("jit") else
                 (jd(r, "direction") or _missing_label(r["jit"], "prob_delta")), DELTA_ORDER)
    signal_block(out, "book composition changed", recs,
                 lambda r: "no JIT refresh logged" if not r.get("jit") else
                 {True: "composition changed", False: "composition unchanged"}.get(
                     jd(r, "composition_changed"), _missing_label(r["jit"], "composition_changed")),
                 ["composition changed", "composition unchanged"])

    out.append("\n BUILD 4 · Demon/Goblin, ENTRY CLV (the builders take standard legs only: demon/goblin stay n=0)")
    out.append(" " + snapshots.CLOSING_WARNING)
    designation = lambda r: (signals.value(r["meta"], "odds_type") or _missing_label(r["meta"], "odds_type"))
    signal_block(out, "designation", recs, designation, ["standard", "demon", "goblin"])
    for g in ("standard", "demon", "goblin"):
        if not any(designation(r) == g for r in recs):
            out.append(f"   {g:<28} n=0")
    out.append("  calibration by designation (settled legs, pushes/voids excluded)")
    graded = distinct([r for r in recs if r["result"] in ("hit", "miss")])
    for g in ("standard", "demon", "goblin"):
        legs = [r for r in graded if designation(r) == g]
        out.append(f"   {g:<28} " + (f"n={len(legs):<4} mean fair {statistics.fmean(r['p'] for r in legs):5.1%}  hit "
                                     f"{sum(r['result'] == 'hit' for r in legs) / len(legs):5.1%}"
                                     + flag(len(legs)) if legs else "n=0"))
    out += shadow_lines(histories, outcomes, now)

    out.append("\n BUILD 5 · watchlist tag (manual list; tags only, no extra refresh). A CLV difference here is "
               "SELECTION BIAS of a hand-picked list, not a win")
    out.append(" " + snapshots.CLOSING_WARNING)
    signal_block(out, "watchlist", recs,
                 lambda r: {True: "watchlist", False: "not on watchlist"}.get(
                     signals.value(r["meta"], "watchlist"), _missing_label(r["meta"], "watchlist")),
                 ["watchlist", "not on watchlist"])
    cov = [c for h in histories.values() for c in h.watch_cov]
    if cov:
        tot = lambda k: sum(c.get(k) or 0 for c in cov)
        out.append(f"  coverage over {len(cov)} cycles (leg-cycles, listed legs): on board {tot('on_board')}, priced "
                   f"{tot('priced')}, >= floor {tot('good')}, eligible {tot('eligible')}, JIT events {tot('jit_events')}, "
                   f"chosen {tot('chosen')}")
    else:
        out.append("  coverage: no cycle logged a watchlist (local/targeted_props.json absent or not logged yet)")
    out += line_gap_lines(histories)
    return out


def shadow_lines(histories: dict[str, History], outcomes: "Outcomes", now: float) -> list[str]:
    """Build 4 shadow: demon/goblin board legs priced from Kalshi only. Entry = the first shadow read of a
    distinct (sport, player, stat, line, odds_type, start); close = the last shadow read at or before the
    start (> 30 min before: stale; the entry read itself: same_read). Labelled and kept apart from entry CLV."""
    out = [f"\n BUILD 4 · SHADOW PROMO CLV ({signals.SHADOW_LABEL}): never a slip, never the pool; More side only",
           " " + snapshots.CLOSING_WARNING,
           "  a small demon slice proves nothing: demon legs alone take a long time to reach evaluation size"]
    series: dict[tuple, list[dict]] = defaultdict(list)
    for h in histories.values():
        for r in h.shadow:
            series[(r["sport"] or h.sport, r["player"], r["stat"], float(r["line"]), r["odds_type"],
                    round(r["start_ts"] or 0))].append(r)
    if not series:
        return out + ["  no shadow promo prices logged yet"]
    by_type: dict[str, list[dict]] = defaultdict(list)
    for k, rows in series.items():
        rows = sorted(rows, key=lambda r: r["ts"])
        first, start = rows[0], rows[0]["start_ts"]
        rec = {"player": k[1], "game": first["game"], "start": start, "p": first["p_fair"], "clv": None}
        if not start or start > now:
            rec["close_reason"] = "open"
        else:
            before = [r for r in rows if r["ts"] <= start]
            close = before[-1] if before else None
            if close is None:
                rec["close_reason"] = "missing"
            elif start - close["ts"] > snapshots.STALE_CLOSE_S:
                rec["close_reason"] = "stale"
            elif close is first:
                rec["close_reason"] = "same_read"
            else:
                rec["close_reason"], rec["clv"] = "ok", close["p_fair"] - first["p_fair"]
        e = LegEntry(k[0], k[1], k[2], k[3], "More", first["p_fair"], "kalshi", start, first["team"] or "", first["game"] or "")
        rec["result"] = outcomes.result(e, now)[0] if start else "review"
        by_type[k[4]].append(rec)
    for g in sorted(by_type, key=lambda g: (["goblin", "demon"].index(g) if g in ("goblin", "demon") else 9, g)):
        mine = by_type[g]
        out.append(f"   {g:<28} {signals.quality_line(mine)}")
        out.append(f"   {'':<28} CLV ({signals.SHADOW_LABEL}) {signals.signal_cell([r for r in mine if r['close_reason'] == 'ok'])}")
        legs = [r for r in mine if r["result"] in ("hit", "miss")]
        out.append(f"   {'':<28} calibration " + (f"n={len(legs):<4} mean fair {statistics.fmean(r['p'] for r in legs):5.1%}  "
                                                 f"hit {sum(r['result'] == 'hit' for r in legs) / len(legs):5.1%}"
                                                 + flag(len(legs)) if legs else "n=0 settled"))
    return out


# --------------------------------------------------------------------------- PropLine shadow (logged only)
PL_FAIRS = (("p_live", "PropLine live method"), ("p_shin", "PropLine Shin"), ("p_sharp", "PropLine sharp-weighted"))


def pl_close(rows: list[dict], entry: dict | None, start: float | None, now: float, field: str = "p_live") -> tuple[str, dict | None]:
    """Close of one distinct PropLine leg: the last READ (with a value in `field`) at or before the start.
    open | missing | stale (> 30 min before start) | same_fetch (the entry read itself) | mismatch (another event
    or team) | ok."""
    if not start or start > now:
        return "open", None
    reads = [r for r in rows if r["ts"] <= start and r[field] is not None]
    if not reads or entry is None:
        return "missing", None
    close = reads[-1]
    if start - close["ts"] > snapshots.STALE_CLOSE_S:
        return "stale", close
    if (close["event_id"] and entry["event_id"] and close["event_id"] != entry["event_id"]) or \
            (close["team"] and entry["team"] and close["team"] != entry["team"]):
        return "mismatch", close
    if close is entry or (close["fetched_ts"] is not None and close["fetched_ts"] == entry["fetched_ts"]):
        return "same_fetch", close
    return "ok", close


def pl_series(histories: dict[str, History]) -> dict[tuple, list[dict]]:
    """Distinct PropLine legs: (sport, player, stat, line, round(start)) -> rows by time."""
    series: dict[tuple, list[dict]] = defaultdict(list)
    for h in histories.values():
        for r in h.propline:
            series[(r["sport"] or h.sport, r["player"], r["stat"], float(r["line"]), round(r["start_ts"] or 0))].append(r)
    return series


def brier_line(pairs: list[tuple[float, int, str, float]]) -> str:
    """pairs: (p_over, 1 if More hit else 0, player, start/game key). Brier and log-loss with a bootstrap CI."""
    if not pairs:
        return "n=0 settled"
    b = [(p - y) ** 2 for p, y, _, _ in pairs]
    ll = [-(y * math.log(min(max(p, 1e-6), 1 - 1e-6)) + (1 - y) * math.log(1 - min(max(p, 1e-6), 1 - 1e-6)))
          for p, y, _, _ in pairs]
    ci = signals.bootstrap_ci(b)
    return (f"n={len(pairs):<4} games {len({g for *_, g in pairs}):<3} players {len({pl for _, _, pl, _ in pairs}):<3} "
            f"Brier {statistics.fmean(b):.4f}" + (f" [{ci[0]:.4f}, {ci[1]:.4f}]" if ci else "")
            + f"  log-loss {statistics.fmean(ll):.4f}  mean p {statistics.fmean(p for p, *_ in pairs):5.1%}  "
            f"hit {statistics.fmean(y for _, y, *_ in pairs):5.1%}" + ("  (insufficient data)" if len(pairs) < signals.MIN_N else ""))


def propline_lines(recs: list[dict], histories: dict[str, History], outcomes: "Outcomes", now: float) -> list[str]:
    """PropLine shadow vs the live Odds API path, split by sport, stat and source, on distinct legs. Logged only:
    nothing here is a reason to switch source (a separate decision after the 4-week trial, with replay and
    out-of-sample validation)."""
    from local import propline_shadow as ps
    out = [f"\nPROPLINE SHADOW ({ps.LABEL})",
           " rules: 30 distinct legs = preliminary look only; 300 = evaluation target, not proof; no production change or "
           "source switch without historical replay + out-of-sample validation + separate approval",
           " " + LEG_DISTINCT_NOTE]
    series = pl_series(histories)
    if not series:
        return out + ["  no PropLine shadow rows logged yet"]
    # ---------------------------------------------------------------- coverage
    out.append("\n [1] coverage: distinct in-window board legs (game 10 min to 3 h away, the live books window); a leg "
               "counts as priced if ANY read priced it")
    out.append("     note: the Odds API is asked only for legs Kalshi can't price (sentinel.kalshi_liquid) and only within "
               "the credit budget: its column is what live priced, not what it could have")
    cov: dict[tuple, list[int]] = defaultdict(lambda: [0, 0, 0, 0, 0])
    for k, rows in series.items():
        sport, player, stat, line, start = k
        h = histories.get(sport)
        books = bool(h and h.series.get(series_key(player, stat, line, "books", start)))
        kalshi = bool(h and h.series.get(series_key(player, stat, line, "kalshi", start)))
        pl = any(r["p_live"] is not None for r in rows)
        anyb = any((r["n_books"] or 0) > 0 for r in rows)
        for key in ((sport, "ALL"), (sport, stat)):
            c = cov[key]
            c[0] += 1; c[1] += pl; c[2] += anyb; c[3] += books; c[4] += kalshi
    pct = lambda a, n: f"{a / n:5.0%}" if n else "  n/a"
    for (sport, stat), c in sorted(cov.items(), key=lambda kv: (kv[0][0], kv[0][1] != "ALL", -kv[1][0])):
        out.append(f"   {sport.upper():<4} {stat:<30} n={c[0]:<5} PropLine live-method {pct(c[1], c[0])}  PropLine any book "
                   f"{pct(c[2], c[0])}  Odds API {pct(c[3], c[0])}  Kalshi (live) {pct(c[4], c[0])}")
    why = Counter(r["unpriced_reason"] for rows in series.values() for r in rows[-1:] if r["unpriced_reason"])
    out.append("   PropLine unpriced, by the reason on each leg's LAST row: "
               + (", ".join(f"{k} {v}" for k, v in why.most_common()) if why else "none"))
    # ---------------------------------------------------------------- freshness
    out.append("\n [2] price age at the first priced read, per source (distinct legs)")
    pl_age, ob_age = defaultdict(list), defaultdict(list)
    for k, rows in series.items():
        first = next((r for r in rows if r["p_live"] is not None), None)
        if first:
            a = (json.loads(first["ages_json"] or "{}").get("data_age_s") or {}).get("value")
            if a is not None:
                pl_age[k[0]].append(a)
        h = histories.get(k[0])
        bs = h.board_series.get(series_key(k[1], k[2], k[3], "books", k[4])) if h else None
        if bs and bs[0].age_s is not None:
            ob_age[k[0]].append(bs[0].age_s)
    for sport in sorted(set(pl_age) | set(ob_age)):
        for name, xs in (("PropLine (now - oldest contributing market last_update)", pl_age[sport]),
                         ("Odds API (logged price_age_s)", ob_age[sport])):
            out.append(f"   {sport.upper():<4} {name:<58} " + (f"n={len(xs):<5} median {statistics.median(xs) / 60:6.1f} min  "
                                                              f"p90 {ps.p_quantile(xs, 0.9) / 60:6.1f} min" if xs else "n=0"))
    # ---------------------------------------------------------------- closing completeness + CLV (b): first read
    out.append("\n [3] closing completeness per source (entry = first read; close = last read at or before the start)")
    out.append(" " + snapshots.CLOSING_WARNING)
    board_recs: dict[str, list[dict]] = defaultdict(list)
    ob_recs: dict[str, list[dict]] = defaultdict(list)
    for k, rows in series.items():
        first = next((r for r in rows if r["p_live"] is not None), None)
        reason, close = pl_close(rows, first, k[4], now)
        board_recs[k[0]].append({"player": k[1], "game": rows[0]["game"], "start": k[4], "stat": k[2], "close_reason": reason,
                                 "clv": (close["p_live"] - first["p_live"]) if reason == "ok" else None,
                                 "p": first["p_live"] if first else None, "key": k})
        h = histories.get(k[0])
        bs = h.board_series.get(series_key(k[1], k[2], k[3], "books", k[4])) if h else None
        if bs:
            if not k[4] or k[4] > now:
                r2, clv2 = "open", None
            else:
                before = [x for x in h.series.get(series_key(k[1], k[2], k[3], "books", k[4]), []) if x.ts <= k[4]]
                c = before[-1] if before else None
                fetched = lambda x: (json.loads(x.pricing) if x is not None and x.pricing else {}).get("fetched_ts")
                if c is None:
                    r2, clv2 = "missing", None
                elif k[4] - c.ts > snapshots.STALE_CLOSE_S:
                    r2, clv2 = "stale", None
                elif c is bs[0] or (fetched(c) is not None and fetched(c) == fetched(bs[0])):
                    r2, clv2 = "same_fetch", None
                else:
                    r2, clv2 = "ok", c.p_over - bs[0].p_over
            ob_recs[k[0]].append({"player": k[1], "game": bs[0].game, "start": k[4], "close_reason": r2, "clv": clv2})
    for sport in sorted(board_recs):
        out.append(f"   {sport.upper():<4} PropLine  {signals.quality_line(board_recs[sport])}")
        out.append(f"   {sport.upper():<4} Odds API  {signals.quality_line(ob_recs.get(sport, []))}")
    out.append("\n [4b] CLV, entry (b): FIRST PropLine read, all in-window board legs, More side (P(over) close - entry). "
               "Never mixed with (a)")
    out.append(" " + snapshots.CLOSING_WARNING)
    for sport in sorted(board_recs):
        out.append(f"   {sport.upper():<4} PropLine live method  {signals.signal_cell([r for r in board_recs[sport] if r['close_reason'] == 'ok'])}")
        out.append(f"   {sport.upper():<4} Odds API (same rule)  {signals.signal_cell([r for r in ob_recs.get(sport, []) if r['close_reason'] == 'ok'])}")
    # ---------------------------------------------------------------- CLV (a): chosen legs at entry_ts
    out.append("\n [4a] CLV, entry (a): PropLine read at or before the slip's entry_ts, legs the live/paper builder CHOSE, "
               "side taken. Never mixed with (b)")
    out.append(" " + snapshots.CLOSING_WARNING)
    chosen: dict[tuple, dict] = {}
    for r in sorted(recs, key=lambda r: r["entry_ts"]):
        sport, player, stat, line, side, source, start = r["identity"]
        k = (sport, player, stat, float(line), round(start or 0), side)
        if k in chosen:
            continue  # one observation per distinct leg (and side)
        rows = series.get(k[:5])
        if not rows:
            chosen[k] = {"player": player, "game": r.get("game"), "start": start, "close_reason": "missing", "clv": None,
                         "builder": r["builder"]}
            continue
        entry = next((x for x in reversed(rows) if x["ts"] <= r["entry_ts"] and x["p_live"] is not None), None)
        reason, close = pl_close(rows, entry, start, now)
        if entry is None and reason != "open":
            reason = "missing"
        sgn = 1 if side == "More" else -1
        chosen[k] = {"player": player, "game": r.get("game"), "start": start, "close_reason": reason, "builder": r["builder"],
                     "clv": sgn * (close["p_live"] - entry["p_live"]) if reason == "ok" else None}
    by_b = defaultdict(list)
    for v in chosen.values():
        by_b[v["builder"]].append(v)
    for b in ("live", "paper"):
        mine = by_b.get(b, [])
        out.append(f"   {b:<6} {signals.quality_line(mine)}")
        out.append(f"   {'':<6} CLV {signals.signal_cell([v for v in mine if v['close_reason'] == 'ok'])}")
    # ---------------------------------------------------------------- calibration
    out.append("\n [5] calibration vs ESPN-graded outcomes (More side, first priced read per distinct leg; cached results only "
               "unless --grade)")
    cal: dict[tuple, list] = defaultdict(list)
    for k, rows in series.items():
        sport, player, stat, line, start = k
        if not start or start > now - grade_slips.SETTLE_AFTER_H * 3600:
            continue
        res = outcomes.result(LegEntry(sport, player, stat, line, "More", 0.5, ps.SOURCE, start, rows[0]["team"] or "",
                                       rows[0]["game"] or ""), now)[0]
        if res not in ("hit", "miss"):
            continue
        y = 1 if res == "hit" else 0
        g = (rows[0]["game"], start)
        for f, name in PL_FAIRS:
            first = next((r for r in rows if r[f] is not None), None)
            if first:
                cal[(sport, name)].append((first[f], y, player, g))
        h = histories.get(sport)
        bs = h.board_series.get(series_key(player, stat, line, "books", start)) if h else None
        if bs:
            cal[(sport, "Odds API books")].append((bs[0].p_over, y, player, g))
    if not cal:
        out.append("   no settled, graded PropLine legs yet")
    for (sport, name), pairs in sorted(cal.items()):
        out.append(f"   {sport.upper():<4} {name:<26} {brier_line(pairs)}")
    # ---------------------------------------------------------------- cost
    out.append("\n [6] cost: PropLine requests per distinct priced leg vs Odds API credits per distinct books-priced leg")
    for sport in sorted({s["sport"] for h in histories.values() for s in h.propline_sweeps}):
        reqs = sum(s["requests"] or 0 for h in histories.values() for s in h.propline_sweeps if s["sport"] == sport)
        priced = sum(1 for k, rows in series.items() if k[0] == sport and any(r["p_live"] is not None for r in rows))
        out.append(f"   {sport.upper():<4} PropLine {reqs} requests / {priced} priced legs = "
                   + (f"{reqs / priced:.2f} per leg" if priced else "n/a"))
    days: dict[str, int] = {}
    groups: dict[str, dict] = {}
    for h in histories.values():
        for s in h.propline_sweeps:
            u = s.get("odds_api_usage") or {}
            if u.get("date") and isinstance(u.get("used"), int):
                days[u["date"]] = max(days.get(u["date"], 0), u["used"])
                groups[u["date"]] = u.get("by_group") or {}
    ob_priced = sum(1 for k in series if histories.get(k[0]) and
                    histories[k[0]].series.get(series_key(k[1], k[2], k[3], "books", k[4])))
    credits = sum(days.values())
    out.append(f"   Odds API {credits} credits on {len(days)} day(s) (ALL sports sharing the cap, as the live cache recorded "
               f"them while the shadow ran) / {ob_priced} books-priced shadow-window legs = "
               + (f"{credits / ob_priced:.2f} per leg (upper bound: the credits include sports and legs outside this table)"
                  if ob_priced else "n/a"))
    # ---------------------------------------------------------------- Novig / ProphetX, sharp consensus
    out.append("\n [7] book presence (first PropLine read with any two-sided book, distinct legs) and the sharp-weighted "
               "consensus vs the live method")
    for sport in sorted({k[0] for k in series}):
        firsts = [next((r for r in rows if (r["n_books"] or 0) > 0), None) for k, rows in series.items() if k[0] == sport]
        firsts = [r for r in firsts if r]
        if not firsts:
            out.append(f"   {sport.upper():<4} no PropLine read with a two-sided book yet")
            continue
        pres = Counter()
        for r in firsts:
            for b, v in json.loads(r["presence_json"] or "{}").items():
                pres[b] += bool(v.get("value"))
        out.append(f"   {sport.upper():<4} n={len(firsts):<5} " + "  ".join(f"{b} {pres[b] / len(firsts):4.0%}" for b in ps.PRESENCE))
        sharp = [r for r in firsts if r["p_sharp"] is not None]
        d = [r["p_sharp"] - r["p_live"] for r in sharp if r["p_live"] is not None]
        out.append(f"   {'':<4} sharp consensus available {len(sharp) / len(firsts):4.0%}; vs PropLine live method: "
                   + (f"n={len(d)} mean {statistics.fmean(d) * 100:+.2f} median {statistics.median(d) * 100:+.2f} "
                      f"mean |d| {statistics.fmean(abs(x) for x in d) * 100:.2f} pts" if d else "n=0"))
        h = histories.get(sport)
        dl = []
        for k, rows in series.items():
            if k[0] != sport or not h:
                continue
            first = next((r for r in rows if r["p_live"] is not None), None)
            ob = h.read_at(series_key(k[1], k[2], k[3], "books", k[4]), first["ts"]) if first else None
            if ob is not None:
                dl.append(first["p_live"] - ob.p_over)
        out.append(f"   {'':<4} PropLine live method - Odds API live price (last books read at or before the PropLine read): "
                   + (f"n={len(dl)} mean {statistics.fmean(dl) * 100:+.2f} median {statistics.median(dl) * 100:+.2f} pts"
                      if dl else "n=0"))
    return out


def gap_unit_bucket(gap: float) -> str:
    return next(label for top, label in GAP_UNIT_BUCKETS if gap < top)


def line_gap_lines(histories: dict[str, History]) -> list[str]:
    """Build 3: coverage only, no CLV (a leg off the books' main line has no books entry price to judge). A
    PrizePicks-vs-book line gap is NOT a middle: that needs both sides placeable with overlapping outcomes."""
    out = ["\n BUILD 3 · pp_book_line_gap_units (EXPERIMENTAL, coverage only, no CLV; a line gap is NOT a middle)",
           "  legs the books left unpriced whose market was already cached; distinct (sport, player, stat, PP line, start)"]
    first: dict[tuple, dict] = {}
    for h in histories.values():
        for g in h.line_gaps:
            k = (g.get("sport") or h.sport, g.get("player"), g.get("stat"), signals.value(g, "pp_line"),
                 round(g.get("start_ts") or 0))
            first.setdefault(k, g)
    if not first:
        return out + ["  nothing logged yet"]
    recs = list(first.values())
    have = [g for g in recs if signals.value(g, "pp_book_line_gap_units") is not None]
    miss = Counter(signals.reason(g, "pp_book_line_gap_units") for g in recs if g not in have)
    out.append(f"  {len(recs)} distinct legs: gap measured {len(have)}"
               + (", not measured " + ", ".join(f"{k} {v}" for k, v in miss.most_common()) if miss else ""))
    by = Counter(gap_unit_bucket(signals.value(g, "pp_book_line_gap_units")) for g in have)
    out.append("  by gap (stat units): " + (", ".join(f"{label} {by[label]}" for _, label in GAP_UNIT_BUCKETS if by[label]) or "none"))
    sides = Counter(str(signals.value(g, "both_sides_available")) for g in have)
    out.append("  both_sides_available: " + ", ".join(f"{k} {v}" for k, v in sides.most_common()))
    stats = Counter(g.get("stat") for g in have)
    out.append("  by stat: " + ", ".join(f"{k} {v}" for k, v in stats.most_common(8)))
    return out


def coverage_lines(histories: list[History], since: float) -> list[str]:
    out = ["\nCOVERAGE (priced legs / board legs per cycle)"]
    for h in histories:
        cyc = [c for c in h.cycles if c["ts"] >= since and c["legs"]]
        if not cyc:
            out.append(f"  {h.sport.upper()}: no cycles")
            continue
        cov = [c["priced"] / c["legs"] for c in cyc]
        boards = {c["board_ts"]: c["whole_dropped"] or 0 for c in cyc}
        elig = [len(json.loads(c["eligible_json"])) for c in cyc if c["eligible_json"] is not None]
        out.append(f"  {h.sport.upper()}: {len(cyc)} cycles, priced {sum(c['priced'] for c in cyc) / len(cyc):.0f} of "
                   f"{sum(c['legs'] for c in cyc) / len(cyc):.0f} legs a cycle ({sum(cov) / len(cov):.0%}; "
                   f"min {min(cov):.0%}, max {max(cov):.0%}), >= min fair {sum(c['good'] for c in cyc) / len(cyc):.1f}"
                   + (f", eligible {sum(elig) / len(elig):.1f}" if elig else "")
                   + f" · whole-number lines without a push price {sum(c['whole_dropped'] or 0 for c in cyc) / len(cyc):.0f} a cycle "
                   f"({sum(boards.values()) / len(boards):.0f} a board over {len(boards)} boards)")
        by_day: dict[str, list[float]] = defaultdict(list)
        for c, x in zip(cyc, cov):
            by_day[time.strftime("%Y-%m-%d", time.localtime(c["ts"]))].append(x)
        if len(by_day) > 1:
            out.append("    by day: " + " | ".join(f"{d} {sum(v) / len(v):.0%}" for d, v in sorted(by_day.items())))
        stats: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        logged = [c for c in cyc if c["coverage_json"]]
        for c in logged:
            for stat, (n, p) in json.loads(c["coverage_json"]).items():
                stats[stat][0] += n
                stats[stat][1] += p
        if logged:
            gaps = [(s, (n, p)) for s, (n, p) in sorted(stats.items(), key=lambda kv: -(kv[1][0] - kv[1][1])) if n > p][:8]
            out.append("    most unpriced (avg unpriced legs a cycle, priced share): " + (", ".join(
                f"{s} {(n - p) / len(logged):.0f} ({p / n:.0%})" for s, (n, p) in gaps) or "none"))
        else:
            out.append("    per-stat coverage: not logged in these cycles (cycles.coverage_json, logged from 2026-10-17)")
    return out


def run(paths: list[Path], cfg: dict | None = None, outcomes: Outcomes | None = None, now: float | None = None,
        min_ev: float = sentinel.FLEX_MIN_EV, since: float = 0.0, slips_csv: Path | None = None) -> str:
    cfg = cfg or pipeline_config.CONFIG
    now = time.time() if now is None else now
    histories = {}
    for p in paths:
        h = History(p, sport_of(p, p.stem))
        if h.sport in histories:  # cross-sport closes are looked up by sport: one file per sport per run
            raise SystemExit(f"two snapshot files for {h.sport}: {histories[h.sport].path} and {p}; run them separately")
        histories[h.sport] = h
    outcomes = outcomes or Outcomes()
    live, paper, replays = [], [], {}
    for h in histories.values():
        live += [s for s in live_slips(h) if s.entry_ts >= since]
        replays[h.sport] = replay_paper(h, histories, cfg, min_ev, since)
        paper += replays[h.sport].slips
    settle(live + paper, outcomes, now)
    first = min((c["ts"] for h in histories.values() for c in h.cycles), default=None)
    last = max((c["ts"] for h in histories.values() for c in h.cycles), default=None)
    span = (f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(first))} to {time.strftime('%Y-%m-%d %H:%M', time.localtime(last))}"
            if first else "no cycles")
    out = [f"BACKTEST · {', '.join(f'{h.sport}: {h.path.name}' for h in histories.values())} · {span}",
           f"optimizer: selection {cfg['optimizer']['selection']}, sizes {cfg['optimizer']['sizes']}, "
           f"marginal {cfg['optimizer']['marginal_threshold']:.0%}, stacks {'on' if cfg['optimizer']['allow_stacks'] else 'off'}; "
           f"staking below {cfg['staking']['min_units']}u: {cfg['staking']['below_min']}"]
    out.append("\nREPLAY (paper optimizer re-run per cycle, steam recomputed from the price history)")
    for sport, r in replays.items():
        line = (f"  {sport.upper()}: {r.cycles} cycles ({r.exact} from logged eligible legs, {r.approximate} rebuilt from "
                f"price rows: approximate), replay vs logged pick: {r.agree} agree, {r.disagree} differ; steam scored on "
                f"{r.steam_scored} leg-cycles ({r.steam_detected} detected)")
        if r.no_history:
            line += f"; {r.no_history} eligible leg-cycles without a price series"
        line += (f"; integrity flags on {r.flagged} leg-cycles"
                 + (f", {r.vetoed} removed (integrity.enforce)" if cfg["integrity"]["enforce"] else " (shadow)"))
        out.append(line)
        out += [f"    {n}" for n in r.notes]
    out += builder_lines("live", live) + builder_lines("paper", paper)
    out.append("")
    out += snapshots.clv_lines(clv_records(live + paper, histories, now))
    recs = leg_records(live + paper, histories, now, cfg)
    out += slice_lines(recs)
    out += coverage_lines(list(histories.values()), since)
    out += signal_lines(recs, histories, outcomes, now)
    out += propline_lines(recs, histories, outcomes, now)
    if outcomes.fetched:
        out.append(f"\n{outcomes.fetched} leg results graded this run (ESPN box scores, now cached)")
    if slips_csv:
        with open(slips_csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["builder", "db_sport", "entry_ts", "size", "model_ev", "units", "payout", "unsettled", "legs"])
            for s in sorted(live + paper, key=lambda s: s.entry_ts):
                w.writerow([s.builder, s.db_sport, datetime.fromtimestamp(s.entry_ts, timezone.utc).isoformat(timespec="seconds"),
                            len(s.legs), round(s.ev_net, 5), s.units, s.payout, s.unsettled,
                            json.dumps([[e.sport, e.player, e.stat, e.line, e.side, round(e.p_entry, 4), e.source, r]
                                        for e, r in zip(s.legs, s.results)])])
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", action="append", type=Path, help="snapshot file(s) (default: every data/snapshots_*.sqlite3)")
    ap.add_argument("--grade", action="store_true", help="fetch settled leg results from ESPN box scores (network)")
    ap.add_argument("--outcomes", type=Path, default=OUTCOMES, help="leg results cache")
    ap.add_argument("--config", type=Path, help="pipeline_config JSON overrides for the replay (variants)")
    ap.add_argument("--min-ev", type=float, default=sentinel.FLEX_MIN_EV,
                    help="fair-probability floor for cycles logged before eligible legs were")
    ap.add_argument("--since", help="only cycles/slips from this date on (YYYY-MM-DD, local)")
    ap.add_argument("--slips-csv", type=Path, help="write every chosen slip with its leg results here")
    a = ap.parse_args()
    paths = a.db or sorted((snapshots.ROOT / "data").glob("snapshots_*.sqlite3"))
    if not paths:
        raise SystemExit("no snapshot files yet (data/snapshots_<sport>.sqlite3)")
    cfg = pipeline_config.load(a.config) if a.config else None
    since = time.mktime(time.strptime(a.since, "%Y-%m-%d")) if a.since else 0.0
    print(run(paths, cfg, Outcomes(a.outcomes, fetch=a.grade), min_ev=a.min_ev, since=since, slips_csv=a.slips_csv))


if __name__ == "__main__":
    main()
