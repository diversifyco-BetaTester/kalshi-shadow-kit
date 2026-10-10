"""
shadow.py — live shadow of the dead-bucket strategy. PLACES NO ORDERS (the kit has no order code).

Every POLL_S, for each city's climate day in progress:
  1. pull the station's latest METARs and apply rule.death_time
  2. when a bucket first dies, "place" a hypothetical HYPO_SIZE-contract 1c YES ask at the
     BACK of the queue (queue ahead = size resting at the 1c YES ask right now)
  3. replay every later trade through shadow.queue_sim (conservative + realistic estimates)
  4. flag divergence: a dead bucket bid or traded >= FLAG_PRICE (someone thinks it's alive)
  5. after close, read the result; a dead bucket settling YES is a BREAK
Everything lands in SQLite (data/deadbucket_shadow.sqlite3). If Telegram is configured, each
fresh death (within FRESH_S of the reading) is posted as a feed item, plus flags and breaks;
buckets that died before startup are tracked silently so a restart never floods the channel.

Run from the repo root:
    python -m examples.weather_deadbucket.shadow run
    python -m examples.weather_deadbucket.shadow report
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sqlite3
import statistics
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import aiohttp

from kalshi.client import KalshiAPIError, KalshiClient
from kalshi.config import load_settings
from kalshi.trades import fetch_trades
from ops.telegram import send_async
from shadow.queue_sim import QueuePosition

from .data import STATIONS, asos_obs, num, ts
from .rule import death_time

log = logging.getLogger("deadbucket")

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "data" / "deadbucket_shadow.sqlite3"
LOG = ROOT / "deadbucket_shadow.log"
POLL_S = 120
CONFIRM = 2
HYPO_SIZE = 5
PRICE = 0.01
FLAG_PRICE = 0.05
FRESH_S = 3600

SCHEMA = """
CREATE TABLE IF NOT EXISTS dead (
    ticker TEXT PRIMARY KEY, series TEXT, label TEXT, close_ts REAL, dead_obs_ts REAL, detected_ts REAL,
    obs_max INTEGER, q_ahead0 REAL,
    q_cons REAL, filled_cons REAL DEFAULT 0, fill_ts_cons REAL,
    q_real REAL, filled_real REAL DEFAULT 0, fill_ts_real REAL,
    taker_1c REAL DEFAULT 0, last_trade_ts REAL, result TEXT
);
CREATE TABLE IF NOT EXISTS seen_trades (trade_id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS flags (ts REAL, ticker TEXT, kind TEXT, detail TEXT, UNIQUE(ticker, kind));
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


async def flag(db: sqlite3.Connection, ticker: str, kind: str, detail: str) -> None:
    """Record once per (ticker, kind); alert on the first occurrence only."""
    if db.execute("INSERT OR IGNORE INTO flags VALUES (?,?,?,?)", (time.time(), ticker, kind, detail)).rowcount:
        db.commit()
        log.warning("FLAG %s %s: %s", kind, ticker, detail)
        await send_async(f"⚠️ <b>Dead-bucket {kind}</b>\n{ticker}\n{detail}")


def announcement(series: str, m: dict, mx: int, t_dead: float, close: float, now: float, q0: float) -> str:
    return (f"💀 <b>{series}</b> · {m.get('yes_sub_title')} is dead\n"
            f"Station {STATIONS[series]} confirmed {mx}°F at {datetime.fromtimestamp(t_dead, timezone.utc):%H:%M} UTC "
            f"({CONFIRM} readings)\n"
            f"1c queue: {q0:,.0f} contracts · {(close - now) / 3600:.1f} h to close\n"
            f"<code>{m['ticker']}</code>")


def positions(r: sqlite3.Row) -> tuple[QueuePosition, QueuePosition]:
    cons = QueuePosition(HYPO_SIZE, PRICE, r["q_cons"], r["filled_cons"], r["fill_ts_cons"])
    real = QueuePosition(HYPO_SIZE, PRICE, r["q_real"], r["filled_real"], r["fill_ts_real"])
    return cons, real


async def poll_once(k: KalshiClient, session: aiohttp.ClientSession, db: sqlite3.Connection) -> str:
    now = time.time()
    newly = 0
    today = date.today()
    for series in STATIONS:
        try:
            markets = await k.list_markets(series)
            obs = await asos_obs(session, series, today - timedelta(days=1), today + timedelta(days=1),
                                 use_cache=False)
        except Exception as e:  # noqa: BLE001 — one city failing must not stop the poll
            log.warning("%s: %r", series, e)
            continue
        for m in markets:
            close = ts(m["close_time"])
            if not (now < close <= now + 86400) or m.get("strike_type") not in ("between", "less"):
                continue
            if db.execute("SELECT 1 FROM dead WHERE ticker=?", (m["ticker"],)).fetchone():
                continue
            t_dead, mx = death_time(obs, close - 86400, close, m["strike_type"], num(m.get("floor_strike")),
                                    num(m.get("cap_strike")), confirm=CONFIRM)
            if t_dead is None:
                continue
            try:
                q0 = (await k.get_orderbook(m["ticker"])).size_at_ask("yes", PRICE)
            except KalshiAPIError as e:
                log.warning("%s orderbook: %s", m["ticker"], e)
                continue
            db.execute("""INSERT INTO dead (ticker, series, label, close_ts, dead_obs_ts, detected_ts, obs_max,
                          q_ahead0, q_cons, q_real, last_trade_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                       (m["ticker"], series, m.get("yes_sub_title"), close, t_dead, now, mx, q0, q0, q0, now))
            db.commit()
            newly += 1
            log.info("DEAD %s (%s) running max %s°F | %.1f h to close | 1c queue ahead %.0f",
                     m["ticker"], m.get("yes_sub_title"), mx, (close - now) / 3600, q0)
            if now - t_dead <= FRESH_S:
                await send_async(announcement(series, m, mx, t_dead, close, now, q0))

    live = db.execute("SELECT * FROM dead WHERE result IS NULL AND close_ts > ?", (now - 300,)).fetchall()
    for r in live:
        try:
            trades = await fetch_trades(k, r["ticker"], min_ts=r["last_trade_ts"])
        except KalshiAPIError as e:
            log.warning("%s trades: %s", r["ticker"], e)
            continue
        cons, real = positions(r)
        taker_1c, last = r["taker_1c"], r["last_trade_ts"]
        for t in trades:
            if not db.execute("INSERT OR IGNORE INTO seen_trades VALUES (?)", (t.trade_id,)).rowcount:
                continue
            last = max(last, t.ts)
            if not (r["detected_ts"] <= t.ts < r["close_ts"]):
                continue
            if t.yes_price >= FLAG_PRICE:
                await flag(db, r["ticker"], "divergence", f"YES traded {t.yes_price*100:.0f}c x{t.count:.0f} "
                                                          f"after running max {r['obs_max']}°F")
            if t.taker_side == "yes":
                taker_1c += t.count if t.yes_price <= PRICE + 1e-9 else 0
                cons.on_trade(t.yes_price, t.count, t.ts)
                real.on_trade(t.yes_price, t.count, t.ts)
        if r["close_ts"] > now:
            try:
                book = await k.get_orderbook(r["ticker"])
                real.on_book(book.size_at_ask("yes", PRICE))
                if (book.best_yes_bid or 0) >= FLAG_PRICE:
                    await flag(db, r["ticker"], "divergence", f"YES bid {book.best_yes_bid*100:.0f}c "
                                                              f"after running max {r['obs_max']}°F")
            except KalshiAPIError as e:
                log.warning("%s orderbook: %s", r["ticker"], e)
        db.execute("""UPDATE dead SET q_cons=?, filled_cons=?, fill_ts_cons=?, q_real=?, filled_real=?,
                      fill_ts_real=?, taker_1c=?, last_trade_ts=? WHERE ticker=?""",
                   (cons.ahead, cons.filled, cons.fill_ts, real.ahead, real.filled, real.fill_ts, taker_1c, last,
                    r["ticker"]))
        db.commit()

    settled = 0
    for r in db.execute("SELECT ticker, label, obs_max FROM dead WHERE result IS NULL AND close_ts < ?",
                        (now - 600,)).fetchall():
        try:
            res = ((await k.get_market(r["ticker"])).get("result") or "").lower()
        except KalshiAPIError:
            continue
        if res in ("yes", "no"):
            db.execute("UPDATE dead SET result=? WHERE ticker=?", (res, r["ticker"]))
            db.commit()
            settled += 1
            if res == "yes":
                await flag(db, r["ticker"], "BREAK", f"'{r['label']}' settled YES after running max {r['obs_max']}°F")
    return f"poll: {newly} newly dead, {len(live)} tracked, {settled} settled"


async def run_loop(once: bool) -> None:
    db = connect()
    async with aiohttp.ClientSession() as session, KalshiClient(load_settings()) as k:
        log.info("DEAD-BUCKET SHADOW — no orders. %d cities, confirm=%d, %d x %.0fc, every %ds",
                 len(STATIONS), CONFIRM, HYPO_SIZE, PRICE * 100, POLL_S)
        while True:
            try:
                log.info(await poll_once(k, session, db))
            except Exception as e:  # noqa: BLE001 — keep polling; the watchdog catches a dead process
                log.exception("poll failed: %r", e)
            if once:
                return
            await asyncio.sleep(POLL_S)


def report() -> None:
    db = connect(readonly=True)
    rows = db.execute("SELECT * FROM dead").fetchall()
    done = [r for r in rows if r["result"]]
    print(f" {len(rows)} dead buckets tracked, {len(done)} settled")
    if not rows:
        return
    print(f" 1c queue ahead at death: median {statistics.median(r['q_ahead0'] for r in rows):,.0f}")
    for est in ("cons", "real"):
        full = [r for r in done if r[f"filled_{est}"] >= HYPO_SIZE]
        pnl = sum(r[f"filled_{est}"] * (PRICE if r["result"] == "no" else PRICE - 1) for r in done)
        print(f"   {'conservative' if est == 'cons' else 'realistic':<13} filled {len(full)}/{len(done)}  "
              f"hypothetical P&L ${pnl:+.2f}")
    print(f"\n {'series':<12}{'dead':>6}{'median queue':>14}{'filled(real)':>14}{'taker 1c':>10}")
    for series in STATIONS:
        g = [r for r in rows if r["series"] == series]
        if g:
            print(f" {series:<12}{len(g):>6}{statistics.median(r['q_ahead0'] for r in g):>14,.0f}"
                  f"{sum(r['filled_real'] >= HYPO_SIZE for r in g):>14}{sum(r['taker_1c'] for r in g):>10,.0f}")
    for f in db.execute("SELECT * FROM flags ORDER BY ts"):
        print(f" FLAG {datetime.fromtimestamp(f['ts']):%m-%d %H:%M} {f['kind']} {f['ticker']}: {f['detail']}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Dead-bucket live shadow (no orders)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run").add_argument("--once", action="store_true")
    sub.add_parser("report")
    a = ap.parse_args()
    if a.cmd == "run":
        logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)-7s %(message)s",
                            handlers=[logging.StreamHandler(), logging.FileHandler(LOG)])
        try:
            asyncio.run(run_loop(a.once))
        except KeyboardInterrupt:
            print("\nstopped.")
    else:
        report()


if __name__ == "__main__":
    main()
