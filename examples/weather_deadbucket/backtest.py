"""
backtest.py — does the dead-bucket rule ever misfire, and how much taker flow is there?
READ-ONLY.

For each settled bucket of each city over --days:
  * find when the confirmed running max killed it (rule.death_time), tradable --lag-min later
  * BREAK = a dead bucket that settled YES (must be zero: one break = -99c vs +1c per win)
  * obs check = confirmed obs max above the NWS CLI high (the rule could misfire)
  * flow = taker YES buys at <= 2c after death — an UPPER bound on what a resting 1c ask
    could have sold; whether you'd actually fill depends on the queue (see shadow.py)

Run from the repo root:
    python -m examples.weather_deadbucket.backtest
    python -m examples.weather_deadbucket.backtest --series KXHIGHTATL,KXHIGHTSFO --days 60
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
from collections import defaultdict
from datetime import date, timedelta

import aiohttp

from kalshi.client import KalshiClient
from kalshi.config import load_settings
from kalshi.trades import fetch_trades
from shadow.stats import block_bootstrap_ci, rule_of_three

from .data import STATIONS, asos_obs, cached, cli_highs, num, settled_markets, ts
from .rule import death_time, event_day, in_bucket, rnd, safe_f

MAX_PRICE = 0.02


async def run(series_list: list[str], days: int, lag_min: float, margin: int, confirm: int) -> None:
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    dead, obs_checks, mismatches, all_days = [], [], [], set()
    sem = asyncio.Semaphore(8)
    async with aiohttp.ClientSession() as session, KalshiClient(load_settings()) as k:
        for series in series_list:
            markets = await settled_markets(k, series, start, end)
            obs = await asos_obs(session, series, start, end)
            cli = await cli_highs(session, series, {start.year, end.year})
            events = defaultdict(list)
            for m in markets:
                events[m["event_ticker"]].append(m)
            todo = []
            for ev, ms in events.items():
                day = event_day(ev).isoformat()
                close = ts(ms[0]["close_time"])
                vals = [safe_f(v) for t, v in obs if close - 86400 <= t < close]
                confirmed = [min(vals[i:i + confirm]) for i in range(len(vals) - confirm + 1)]
                high = cli.get(day)
                if confirmed and high is not None:
                    obs_checks.append((series, day, max(confirmed), high))
                win = [m for m in ms if m["result"].lower() == "yes"]
                if high is not None and win and not in_bucket(rnd(high), win[0]["strike_type"],
                                                              num(win[0]["floor_strike"]), num(win[0]["cap_strike"])):
                    mismatches.append((series, day, high, win[0]["yes_sub_title"]))
                for m in ms:
                    t_dead, mx = death_time(obs, close - 86400, close, m["strike_type"], num(m["floor_strike"]),
                                            num(m["cap_strike"]), margin, confirm)
                    if t_dead is not None and t_dead + lag_min * 60 < close:
                        todo.append(dict(series=series, day=day, ticker=m["ticker"], label=m["yes_sub_title"],
                                         result=m["result"].lower(), dead_ts=t_dead + lag_min * 60, close=close,
                                         obs_max=mx))
                all_days.add(day)

            async def pull(r):
                async with sem:
                    async def fetch():
                        return [[t.ts, t.yes_price, t.count, t.taker_side]
                                for t in await fetch_trades(k, r["ticker"], r["dead_ts"], r["close"])]
                    r["trades"] = await cached(f"trades_{r['ticker']}_{int(r['dead_ts'])}", fetch)
            await asyncio.gather(*(pull(r) for r in todo))
            dead += todo
            print(f"  {series:<12} events {len(events):<4} dead buckets {len(todo)}")

    print("=" * 78)
    print(f" DEAD-BUCKET BACKTEST {start} → {end} | confirm {confirm} | margin +{margin}°F | lag {lag_min:.0f} min")
    print("=" * 78)
    breaks = [r for r in dead if r["result"] == "yes"]
    print(f" breaks: {len(breaks)} of {len(dead)} dead buckets settled YES "
          f"(95% upper bound on break rate {rule_of_three(len(dead)):.2%}; 1c selling breaks even at 1%)")
    for r in breaks:
        print(f"   !! {r['ticker']} ({r['label']}) running max {r['obs_max']}")
    unsafe = [c for c in obs_checks if c[2] > c[3]]
    print(f" obs above CLI high: {len(unsafe)} of {len(obs_checks)} days")
    for c in unsafe:
        print(f"   !! {c[0]} {c[1]}: confirmed obs max {c[2]} > CLI {c[3]}")
    print(f" Kalshi winner vs CLI mismatches: {len(mismatches)}")
    for x in mismatches:
        print(f"   {x[0]} {x[1]}: CLI {x[2]} vs Kalshi winner '{x[3]}'")

    print(f"\n taker YES buys <= {MAX_PRICE*100:.0f}c after death (upper bound on resting-ask fills)")
    print(f"   {'city':<12}{'dead':>6}{'contracts':>12}{'per bucket':>12}")
    groups = []
    for series in series_list:
        rs = [r for r in dead if r["series"] == series]
        n = sum(c for r in rs for t, px, c, side in r["trades"]
                if side == "yes" and px <= MAX_PRICE + 1e-9 and r["dead_ts"] <= t < r["close"])
        print(f"   {series:<12}{len(rs):>6}{n:>12,.0f}{n / max(len(rs), 1):>12,.0f}")
    for r in dead:
        g = [(px if r["result"] == "no" else px - 1) * c for t, px, c, side in r["trades"]
             if side == "yes" and px <= MAX_PRICE + 1e-9 and r["dead_ts"] <= t < r["close"]]
        if g:
            groups.append([sum(g)])
    if groups:
        m, lo, hi = block_bootstrap_ci(groups)
        print(f"   gross a maker could have earned per traded dead bucket: ${m:.2f} (95% CI ${lo:.2f}..${hi:.2f})")


def main() -> None:
    ap = argparse.ArgumentParser(description="Dead-bucket backtest (read-only)")
    ap.add_argument("--series", default="all", help="'all' or a comma list of Kalshi series")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--lag-min", type=float, default=15, help="minutes after the obs before acting")
    ap.add_argument("--margin", type=int, default=0, help="extra °F the running max must clear")
    ap.add_argument("--confirm", type=int, default=2, help="consecutive readings that must clear the bucket")
    a = ap.parse_args()
    series = list(STATIONS) if a.series == "all" else a.series.split(",")
    asyncio.run(run(series, a.days, a.lag_min, a.margin, a.confirm))


if __name__ == "__main__":
    main()
