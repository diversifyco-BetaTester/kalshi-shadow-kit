"""
shadow.py — live maker shadow for Kalshi's 15-minute crypto windows. PLACES NO ORDERS.

The question it answers: if we rested non-crossing limit orders around fair value instead of
crossing the spread, would they fill, and would the fills be worth having? A resting order fills
first exactly when someone better-informed wants the other side (adverse selection), so "it filled
at a 2c edge" means nothing until you see where fair value went next.

Every POLL_S, for each series' current window:
  1. fair value from Coinbase spot (1 Hz), minus the spot-vs-strike basis measured over the 60 s
     before the window opened (the strike is the RTI average over that minute)
  2. quote.make_quotes -> a hypothetical YES bid and YES ask, SIZE contracts each, never crossing
     and (unless --improve) never better than the touch. A changed price is cancel/replace: the new
     order joins the BACK of its queue. A side is only quoted if a full fill keeps the realistic
     position within MAX_POSITION.
     If spot moved more than FAST_SIGMAS of its own volatility over the last FAST_WINDOW_S, both
     quotes are pulled for COOLDOWN_S. The check runs once a poll, so trades in the POLL_S before
     it can still hit the quotes, as they would for a live bot polling at the same rate.
  3. replay every Kalshi trade print through shadow.queue_sim (conservative + realistic queue)
  4. on each fill, journal fair value then, and again 30 s and 60 s later (the markout)
  5. after close, read the result; each fill is held to settlement (no exit in a 15-minute binary)
  6. once a MARK_EVERY_S, journal our fair value and Kalshi's mid whether or not we quote, so the
     report can check which one is calibrated against the settlements
Quoting stops STOP_S before close (the live bot's decision point); fair value keeps being
recorded until the 60 s settlement average starts, so the 60 s markouts are always defined.

Maker fees come from each series' fee_type: "quadratic" charges takers only (KXBTC15M is this
today); "quadratic_with_maker_fees" charges makers MAKER_RATE·mult·P·(1−P) too.

Run from the repo root (KALSHI_ENV=prod in .env: the order books you shadow must be real):
    python -m examples.crypto_maker.shadow run
    python -m examples.crypto_maker.shadow run --series KXBTC15M KXETH15M --edge 0.03 --improve
    python -m examples.crypto_maker.shadow report
"""
from __future__ import annotations

import argparse
import asyncio
import bisect
import logging
import math
import sqlite3
import statistics
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import aiohttp

from kalshi.client import KalshiAPIError, KalshiClient
from kalshi.config import load_settings
from kalshi.orderbook import OrderBook
from kalshi.trades import Trade
from shadow.queue_sim import QueuePosition

from .quote import SETTLEMENT_WINDOW_S, make_quotes, p_yes, realized_vol

log = logging.getLogger("maker")

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "data" / "maker_shadow.sqlite3"
LOG = ROOT / "maker_shadow.log"
SERIES = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M")
COINBASE = "https://api.exchange.coinbase.com/products/{}-USD/ticker"
POLL_S = 3
SIZE = 5               # contracts per hypothetical quote
EDGE = 0.04            # quote no closer than this to fair value (2c was picked off: -2c at +60 s)
MAX_POSITION = 10      # |net YES contracts| per window (realistic fills); a full fill never goes past it
FAST_WINDOW_S = 5      # spot move measured over this many seconds...
FAST_SIGMAS = 3.0      # ...pulls both quotes when it exceeds this many sigmas of realized vol
COOLDOWN_S = 10        # and keeps them pulled this long after the last fast move
MARK_EVERY_S = 60      # calibration sample interval; marks within one window share its outcome
STOP_S = 120           # stop quoting this long before close
MAX_BASIS_BPS = 25.0   # Coinbase vs the stamped strike; wider means the oracle doesn't match RTI
MAX_SPOT_AGE_S = 5.0
MAKER_RATE = 0.0175
MARKOUTS = (30, 60)
ESTIMATES = ("cons", "real")

SCHEMA = """
CREATE TABLE IF NOT EXISTS windows (
    ticker TEXT PRIMARY KEY, series TEXT, open_ts REAL, close_ts REAL, strike REAL, strike_type TEXT,
    basis_bps REAL, fee_type TEXT, maker_rate REAL, result TEXT
);
CREATE TABLE IF NOT EXISTS quotes (
    id INTEGER PRIMARY KEY, ticker TEXT, side TEXT, price REAL, size REAL, placed_ts REAL, cancel_ts REAL,
    fair REAL, best_bid REAL, best_ask REAL, q_ahead0 REAL, edge REAL, improve INTEGER
);
CREATE TABLE IF NOT EXISTS pulls (ts REAL, ticker TEXT, move_bps REAL, threshold_bps REAL);
CREATE TABLE IF NOT EXISTS marks (ticker TEXT, ts REAL, tau REAL, fair REAL, mid REAL, sigma REAL);
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY, quote_id INTEGER, ticker TEXT, est TEXT, side TEXT, price REAL, count REAL,
    ts REAL, fair REAL, mid REAL, fair_30 REAL, fair_60 REAL, mid_60 REAL
);
"""


def connect(readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    else:
        DB.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(DB)
        db.executescript(SCHEMA)
    db.row_factory = sqlite3.Row
    return db


def ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


# --------------------------------------------------------------------------- spot
class Spot:
    """1 Hz Coinbase mid for one asset. Stand-in for the RTI; the basis check corrects the level."""

    def __init__(self, base: str):
        self.base = base
        self.hist: deque[tuple[float, float]] = deque(maxlen=3600)

    async def run(self, session: aiohttp.ClientSession) -> None:
        while True:
            try:
                async with session.get(COINBASE.format(self.base), timeout=aiohttp.ClientTimeout(total=3)) as r:
                    d = await r.json()
                self.hist.append((time.time(), (float(d["bid"]) + float(d["ask"])) / 2))
            except Exception as e:  # noqa: BLE001 — a missed second is fine; staleness is checked at use
                log.debug("%s spot: %r", self.base, e)
            await asyncio.sleep(1)

    def latest(self) -> float | None:
        if self.hist and time.time() - self.hist[-1][0] <= MAX_SPOT_AGE_S:
            return self.hist[-1][1]
        return None

    def ago(self, seconds: float) -> float | None:
        """The last sample at least `seconds` old."""
        cutoff = time.time() - seconds
        return next((p for t, p in reversed(self.hist) if t <= cutoff), None)

    def between(self, t0: float, t1: float) -> list[float]:
        return [p for t, p in self.hist if t0 <= t <= t1]


# --------------------------------------------------------------------------- state
@dataclass
class Order:
    id: int
    side: str  # "bid" (we buy YES) | "ask" (we sell YES = buy NO)
    price: float
    placed_ts: float
    q: dict[str, QueuePosition]  # per estimate, in queue_sim's "resting ask" terms


@dataclass
class Window:
    ticker: str
    series: str
    open_ts: float
    close_ts: float
    strike: float
    above: bool
    basis: float  # Coinbase − RTI, dollars
    seen: set[str] = field(default_factory=set)
    last_trade_ts: float = 0.0
    orders: dict[str, Order | None] = field(default_factory=lambda: {"bid": None, "ask": None})
    position: dict[str, float] = field(default_factory=lambda: dict.fromkeys(ESTIMATES, 0.0))
    hist_t: list[float] = field(default_factory=list)  # fair/mid history, one point per poll
    hist: list[tuple[float, float | None]] = field(default_factory=list)
    pending: list[tuple[int, float]] = field(default_factory=list)  # (fill id, fill ts) awaiting markouts
    pulled_until: float = 0.0
    last_mark: float = 0.0

    def at(self, t: float) -> tuple[float, float | None] | None:
        i = bisect.bisect_right(self.hist_t, t) - 1
        return self.hist[i] if i >= 0 else None


def queue_terms(side: str, price: float) -> float:
    """queue_sim models a resting ask that takers buy. Our YES ask is one; our YES bid at b is
    a NO ask at 1 − b, filled by takers buying NO."""
    return round(price if side == "ask" else 1.0 - price, 4)


def queue_ahead(book: OrderBook, side: str, price: float) -> float:
    return book.size_at_ask("yes" if side == "ask" else "no", queue_terms(side, price))


class Shadow:
    def __init__(self, k: KalshiClient, db: sqlite3.Connection, spots: dict[str, Spot], series: list[str],
                 edge: float, size: float, improve: bool):
        self.k, self.db, self.spots, self.series, self.edge, self.size = k, db, spots, series, edge, size
        self.improve = improve
        self.windows: dict[str, Window | None] = dict.fromkeys(series)
        self.skipped: set[str] = set()
        self.fees: dict[str, tuple[str, float]] = {}

    # ------------------------------------------------------------------ windows
    async def fee(self, series: str) -> tuple[str, float]:
        if series not in self.fees:
            s = (await self.k.get(f"/series/{series}")).get("series", {})
            fee_type, mult = s.get("fee_type") or "?", float(s.get("fee_multiplier") or 1)
            self.fees[series] = (fee_type, MAKER_RATE * mult if "maker" in fee_type else 0.0)
            log.info("%s fee_type=%s -> maker fee rate %.4f", series, fee_type, self.fees[series][1])
        return self.fees[series]

    async def find_window(self, series: str, now: float) -> Window | None:
        for m in await self.k.list_markets(series, status="open", limit=20):
            if m["ticker"] in self.skipped or m.get("floor_strike") in (None, ""):
                continue
            open_ts, close_ts = ts(m["open_time"]), ts(m["close_time"])
            if not open_ts <= now < close_ts - STOP_S:
                continue
            strike, st = float(m["floor_strike"]), (m.get("strike_type") or "").lower()
            spot = self.spots[base(series)]
            at_open = spot.between(open_ts - SETTLEMENT_WINDOW_S, open_ts)
            why = ("strike_type " + st if st not in ("greater", "greater_or_equal", "less", "less_or_equal")
                   else f"only {len(at_open)} spot samples in the minute before open" if len(at_open) < 30 else "")
            basis = statistics.fmean(at_open) - strike if at_open else 0.0
            if not why and abs(basis / strike * 1e4) > MAX_BASIS_BPS:
                why = f"basis {basis / strike * 1e4:+.1f} bps"
            if why:
                log.info("%s skipped: %s", m["ticker"], why)
                self.skipped.add(m["ticker"])
                continue
            fee_type, rate = await self.fee(series)
            self.db.execute("INSERT OR IGNORE INTO windows VALUES (?,?,?,?,?,?,?,?,?,NULL)",
                            (m["ticker"], series, open_ts, close_ts, strike, st, basis / strike * 1e4, fee_type, rate))
            self.db.commit()
            log.info("%s window: strike %.2f, basis %+.1f bps, %.0fs to close", m["ticker"], strike,
                     basis / strike * 1e4, close_ts - now)
            return Window(m["ticker"], series, open_ts, close_ts, strike, not st.startswith("less"), basis,
                          last_trade_ts=now)
        return None

    # ------------------------------------------------------------------ orders
    def cancel(self, w: Window, side: str, now: float) -> None:
        o = w.orders[side]
        if o:
            self.db.execute("UPDATE quotes SET cancel_ts=? WHERE id=?", (now, o.id))
            w.orders[side] = None

    def place(self, w: Window, side: str, price: float, book: OrderBook, fair: float, now: float) -> None:
        ahead = queue_ahead(book, side, price)
        cur = self.db.execute("INSERT INTO quotes (ticker, side, price, size, placed_ts, fair, best_bid, best_ask, "
                              "q_ahead0, edge, improve) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                              (w.ticker, side, price, self.size, now, fair, book.best_yes_bid, book.best_yes_ask,
                               ahead, self.edge, self.improve))
        w.orders[side] = Order(cur.lastrowid, side, price, now,
                               {e: QueuePosition(self.size, queue_terms(side, price), ahead) for e in ESTIMATES})

    def replay(self, w: Window, trades: list[Trade]) -> None:
        for t in trades:
            if t.trade_id in w.seen:
                continue
            w.seen.add(t.trade_id)
            w.last_trade_ts = max(w.last_trade_ts, t.ts)
            side = "ask" if t.taker_side == "yes" else "bid"  # taker buying YES lifts asks
            o = w.orders[side]
            if not o or t.ts < o.placed_ts:
                continue
            px = t.yes_price if side == "ask" else 1.0 - t.yes_price
            for est, q in o.q.items():
                got = q.on_trade(px, t.count, t.ts)
                if got <= 0:
                    continue
                w.position[est] += got if side == "bid" else -got
                fair, mid = w.at(t.ts) or (None, None)
                cur = self.db.execute("INSERT INTO fills (quote_id, ticker, est, side, price, count, ts, fair, mid) "
                                      "VALUES (?,?,?,?,?,?,?,?,?)",
                                      (o.id, w.ticker, est, side, o.price, got, t.ts, fair, mid))
                w.pending.append((cur.lastrowid, t.ts))
                if est == "real":
                    log.info("FILL %s %s %.0f @ %.2f (fair %s) pos %+.0f", w.ticker, side, got, o.price,
                             f"{fair:.3f}" if fair is not None else "?", w.position[est])

    def markouts(self, w: Window, now: float, final: bool = False) -> None:
        left = []
        for fid, t in w.pending:
            done = True
            for h in MARKOUTS:
                if now >= t + h:
                    fair, mid = w.at(t + h) or (None, None)
                    cols = f"fair_{h}=?" + (", mid_60=?" if h == 60 else "")
                    self.db.execute(f"UPDATE fills SET {cols} WHERE id=? AND fair_{h} IS NULL",
                                    (fair, mid, fid) if h == 60 else (fair, fid))
                else:
                    done = False
            if not done and not final:
                left.append((fid, t))
        w.pending = left

    # ------------------------------------------------------------------ one poll
    async def poll_series(self, series: str, now: float) -> None:
        w = self.windows[series]
        if w and now >= w.close_ts - SETTLEMENT_WINDOW_S:
            self.markouts(w, now, final=True)
            self.db.commit()
            self.windows[series] = w = None
        if w is None:
            self.windows[series] = w = await self.find_window(series, now)
            if w is None:
                return

        trades = [Trade.from_api(t) async for t in
                  self.k.paginate("/markets/trades", "trades", {"ticker": w.ticker, "min_ts": int(w.last_trade_ts)})]
        self.replay(w, sorted(trades, key=lambda t: t.ts))
        book = await self.k.get_orderbook(w.ticker)
        for o in filter(None, w.orders.values()):
            o.q["real"].on_book(queue_ahead(book, o.side, o.price))

        spot = self.spots[base(series)]
        px = spot.latest()
        fair = None
        if px is not None:
            sigma = realized_vol([p for _, p in list(spot.hist)[-900:]])
            fair = p_yes(px - w.basis, w.strike, sigma, w.close_ts - now)
            fair = fair if w.above else 1.0 - fair
            self.check_fast_move(w, spot, px, sigma, now)
        bb, ba = book.best_yes_bid, book.best_yes_ask
        mid = (bb + ba) / 2 if bb is not None and ba is not None else None
        if fair is not None:
            w.hist_t.append(now)
            w.hist.append((fair, mid))
            if mid is not None and now - w.last_mark >= MARK_EVERY_S:
                w.last_mark = now
                self.db.execute("INSERT INTO marks VALUES (?,?,?,?,?,?)", (w.ticker, now, w.close_ts - now, fair, mid,
                                                                         sigma))
        self.markouts(w, now)

        quoting = fair is not None and now < w.close_ts - STOP_S and now >= w.pulled_until
        want = make_quotes(fair, bb, ba, self.edge, self.improve) if quoting else None
        pos = w.position["real"]
        for side in ("bid", "ask"):
            price = getattr(want, side) if want else None
            o = w.orders[side]
            if o and not o.q["real"].done and price is not None and abs(o.price - price) < 1e-9:
                continue  # unchanged and still working: its worst case was within the cap when placed
            self.cancel(w, side, now)
            if price is not None and abs(pos + (self.size if side == "bid" else -self.size)) <= MAX_POSITION:
                self.place(w, side, price, book, fair, now)
        self.db.commit()

    def check_fast_move(self, w: Window, spot: Spot, px: float, sigma: float, now: float) -> None:
        old = spot.ago(FAST_WINDOW_S)
        if old is None:
            return
        move, limit = abs(math.log(px / old)) * 1e4, FAST_SIGMAS * sigma * math.sqrt(FAST_WINDOW_S) * 1e4
        if move > limit:
            if now >= w.pulled_until:
                log.info("PULL %s: spot moved %.1f bps in %ds (limit %.1f)", w.ticker, move, FAST_WINDOW_S, limit)
            w.pulled_until = now + COOLDOWN_S
            self.db.execute("INSERT INTO pulls VALUES (?,?,?,?)", (now, w.ticker, move, limit))

    async def settle(self) -> int:
        n = 0
        for r in self.db.execute("SELECT ticker FROM windows WHERE result IS NULL AND close_ts < ?",
                                 (time.time() - 120,)).fetchall():
            try:
                res = ((await self.k.get_market(r["ticker"])).get("result") or "").lower()
            except KalshiAPIError:
                continue
            if res in ("yes", "no"):
                self.db.execute("UPDATE windows SET result=? WHERE ticker=?", (res, r["ticker"]))
                n += 1
        self.db.commit()
        return n


def base(series: str) -> str:
    return series.removeprefix("KX").removesuffix("15M")


async def run_loop(series: list[str], edge: float, size: float, improve: bool, once: bool) -> None:
    db = connect()
    db.execute("UPDATE quotes SET cancel_ts=? WHERE cancel_ts IS NULL", (time.time(),))  # a restart drops them
    db.commit()
    spots = {base(s): Spot(base(s)) for s in series}
    async with aiohttp.ClientSession() as session, KalshiClient(load_settings()) as k:
        tasks = [asyncio.create_task(s.run(session)) for s in spots.values()]
        sh = Shadow(k, db, spots, series, edge, size, improve)
        log.info("MAKER SHADOW — no orders. %s | %d x %.0fc from fair, %s | max position %d | "
                 "pull on %.0f-sigma %ds moves | every %ds", ",".join(series), size, edge * 100,
                 "improving the touch" if improve else "joining the touch or behind", MAX_POSITION,
                 FAST_SIGMAS, FAST_WINDOW_S, POLL_S)
        last_settle = 0.0
        try:
            while True:
                now = time.time()
                for s in series:
                    try:
                        await sh.poll_series(s, now)
                    except Exception as e:  # noqa: BLE001 — one series failing must not stop the others
                        log.warning("%s poll failed: %r", s, e)
                if now - last_settle > 60:
                    last_settle = now
                    if n := await sh.settle():
                        log.info("%d window(s) settled", n)
                if once:
                    return
                await asyncio.sleep(max(0.0, POLL_S - (time.time() - now)))
        finally:
            for t in tasks:
                t.cancel()


# --------------------------------------------------------------------------- report
def signed(side: str, price: float, value: float | None) -> float | None:
    """Edge per contract in our favour: bought YES at price (bid) or sold YES at price (ask)."""
    if value is None:
        return None
    return value - price if side == "bid" else price - value


def cents(xs: list[tuple[float, float]]) -> str:
    """Contract-weighted mean of (value, count) pairs, in cents."""
    n = sum(c for _, c in xs)
    return f"{sum(v * c for v, c in xs) / n * 100:+6.2f}c" if n else "     — "


def calibration(db: sqlite3.Connection) -> None:
    """Settled marks bucketed by our fair value, then by Kalshi's mid. A calibrated column has its
    mean close to the YES rate in every bucket; Brier is the mean squared error (lower is better)."""
    rows = db.execute("SELECT m.ticker, m.fair, m.mid, w.series, w.result = 'yes' AS yes FROM marks m "
                      "JOIN windows w USING (ticker) WHERE w.result IN ('yes', 'no')").fetchall()
    print(f"\n CALIBRATION: {len(rows)} marks from {len({r['ticker'] for r in rows})} settled windows "
          f"(one mark a {MARK_EVERY_S}s; marks in a window share its outcome, so count windows, not marks)")
    if not rows:
        return
    for key, label in (("fair", "our fair value"), ("mid", "Kalshi mid")):
        print(f"\n by {label:<16}{'marks':>7}{'windows':>9}{'mean fair':>11}{'mean mid':>10}{'YES rate':>10}")
        for lo in range(0, 100, 10):
            g = [r for r in rows if lo <= r[key] * 100 < lo + 10 or (lo == 90 and r[key] >= 1)]
            if g:
                print(f"   {lo:>3}-{lo + 10:<3}c{'':<10}{len(g):>7}{len({r['ticker'] for r in g}):>9}"
                      f"{statistics.fmean(r['fair'] for r in g):>11.3f}{statistics.fmean(r['mid'] for r in g):>10.3f}"
                      f"{statistics.fmean(r['yes'] for r in g):>10.3f}")
    print()
    for series in sorted({r["series"] for r in rows}) + ["ALL"]:
        g = [r for r in rows if series in ("ALL", r["series"])]
        brier = {k: statistics.fmean((r[k] - r["yes"]) ** 2 for r in g) for k in ("fair", "mid")}
        print(f" Brier {series:<9} fair {brier['fair']:.4f}   Kalshi mid {brier['mid']:.4f}   "
              f"({len({r['ticker'] for r in g})} windows)")


def report() -> None:
    db = connect(readonly=True)
    windows = {r["ticker"]: r for r in db.execute("SELECT * FROM windows")}
    quotes = db.execute("SELECT COUNT(*), AVG(q_ahead0) FROM quotes").fetchone()
    print(f" {len(windows)} windows, {sum(1 for w in windows.values() if w['result'])} settled, "
          f"{quotes[0]} quotes placed (mean queue ahead at placement {quotes[1] or 0:,.0f})")
    fee_types = {(w["series"], w["fee_type"], w["maker_rate"]) for w in windows.values()}
    print(" maker fees: " + ", ".join(f"{s} {ft} ({r:.4f})" for s, ft, r in sorted(fee_types)))
    settings = db.execute("SELECT edge, improve, COUNT(*) FROM quotes GROUP BY edge, improve").fetchall()
    print(" quote settings: " + (", ".join(f"edge {e * 100:.0f}c {'improving' if i else 'joining'} ({n} quotes)"
                                          for e, i, n in settings) or "none"))
    pulls = db.execute("SELECT COUNT(*), COUNT(DISTINCT ticker) FROM pulls").fetchone()
    print(f" fast-move pulls: {pulls[0]} polls across {pulls[1]} windows")
    print(" Edges are per contract, in our favour. at fill: fair minus price when we filled. "
          "+30s/+60s: same, with fair\n value 30/60 s later (the drop is adverse selection). "
          "settled: what the contract paid, net of maker fee.\n")
    print(f" {'series':<11}{'est':<6}{'quotes':>7}{'filled':>8}{'contracts':>10}{'at fill':>9}{'+30s':>9}"
          f"{'+60s':>9}{'Kalshi mid+60s':>15}{'settled':>9}{'P&L':>9}")
    for series in sorted({w["series"] for w in windows.values()}) + ["ALL"]:
        for est in ESTIMATES:
            rows = db.execute("SELECT f.* FROM fills f JOIN windows w USING (ticker) WHERE f.est=? AND "
                              "(w.series=? OR ?='ALL')", (est, series, series)).fetchall()
            nq = db.execute("SELECT COUNT(*) FROM quotes q JOIN windows w USING (ticker) WHERE w.series=? OR ?='ALL'",
                            (series, series)).fetchone()[0]
            col = {k: [(signed(r["side"], r["price"], r[k]), r["count"]) for r in rows if r[k] is not None]
                   for k in ("fair", "fair_30", "fair_60", "mid_60")}
            settled = []
            for r in rows:
                w = windows[r["ticker"]]
                if w["result"] in ("yes", "no"):
                    fee = w["maker_rate"] * r["price"] * (1 - r["price"])
                    settled.append((signed(r["side"], r["price"], 1.0 if w["result"] == "yes" else 0.0) - fee,
                                    r["count"]))
            pnl = sum(v * c for v, c in settled)
            print(f" {series:<11}{est:<6}{nq:>7}{len({r['quote_id'] for r in rows}):>8}"
                  f"{sum(r['count'] for r in rows):>10,.0f}{cents(col['fair']):>9}{cents(col['fair_30']):>9}"
                  f"{cents(col['fair_60']):>9}{cents(col['mid_60']):>15}{cents(settled):>9}{pnl:>+9.2f}")
    print("\n cons = nobody ahead of us ever cancels; real = queue capped by the level's size at each poll.")
    calibration(db)


def main() -> None:
    ap = argparse.ArgumentParser(description="Kalshi 15-minute crypto maker shadow (no orders)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run")
    run.add_argument("--once", action="store_true")
    run.add_argument("--series", nargs="+", default=list(SERIES))
    run.add_argument("--edge", type=float, default=EDGE, help="min distance from fair value, dollars")
    run.add_argument("--size", type=float, default=SIZE, help="contracts per hypothetical quote")
    run.add_argument("--improve", action="store_true", help="quote 1c inside the touch (front of the queue)")
    sub.add_parser("report")
    a = ap.parse_args()
    if a.cmd == "run":
        logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)-7s %(message)s",
                            handlers=[logging.StreamHandler(), logging.FileHandler(LOG)])
        try:
            asyncio.run(run_loop([s.upper() for s in a.series], a.edge, a.size, a.improve, a.once))
        except KeyboardInterrupt:
            print("\nstopped.")
    else:
        report()


if __name__ == "__main__":
    main()
