"""
pick6_hedge.py — size a Kalshi hedge for a DraftKings Pick6 entry, from live order books.

You type in the Pick6 side (payout, and what the entry cost you in cash). The script reads
Kalshi order books (read-only) and prices the hedge that pays when the entry LOSES.

Two hedge shapes:
  combo  one Kalshi market that pays if the entry loses, e.g. NO on a 2-leg combo.
         Buy N = payout contracts. Locked profit = payout - cost - fees - cash stake.
  legs   one Kalshi market per Pick6 leg that pays if THAT leg fails.
         Buy `payout` contracts on each. Usually far worse; shown so you can compare.

Lines must match exactly (same player, same stat, same threshold, e.g. Pick6 "More 64.5
rec yds" == Kalshi "65+ rec yds"), and check both venues' rules for a player who doesn't play.

Run from the repo root:
  python -m local.pick6_hedge --payout 30 --combo KXTICKER:no
  python -m local.pick6_hedge --payout 30 --legs KXLEG_A:no KXLEG_B:no
  python -m local.pick6_hedge --payout 15 --stake 5 --combo KXTICKER:no    # $5 cash qualifier
"""
from __future__ import annotations

import argparse
import asyncio
import math

from kalshi.client import KalshiClient
from kalshi.config import load_settings

FEE_RATE = 0.07  # Kalshi taker fee estimate: ceil(0.07 * C * P * (1 - P)) per order. Check the current schedule.


def taker_fee(contracts: float, price: float) -> float:
    return math.ceil(FEE_RATE * contracts * price * (1 - price) * 100) / 100


def fill_cost(asks: list[tuple[float, float]], contracts: float) -> tuple[float, float, float] | None:
    """Walk the ask ladder. Returns (cost, fees, worst price), or None if the book is too thin."""
    left, cost, fees, worst = contracts, 0.0, 0.0, 0.0
    for price, qty in asks:
        take = min(left, qty)
        cost += take * price
        fees += taker_fee(take, price)
        worst, left = price, left - take
        if left <= 1e-9:
            return cost, fees, worst
    return None


def parse_leg(s: str) -> tuple[str, str]:
    ticker, _, side = s.partition(":")
    if side not in ("yes", "no"):
        raise argparse.ArgumentTypeError(f"{s!r}: use TICKER:yes or TICKER:no (the side that pays when the entry loses)")
    return ticker, side


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--payout", type=float, required=True, help="Pick6 payout in $ if the entry wins (e.g. $10 x 3 = 30)")
    ap.add_argument("--stake", type=float, default=0.0, help="cash you paid for the entry (0 for a bonus pick)")
    ap.add_argument("--bankroll", type=float, default=45.0, help="cash available for the hedge")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--combo", type=parse_leg, help="one market that pays if the entry loses")
    g.add_argument("--legs", type=parse_leg, nargs="+", help="one market per leg that pays if that leg fails")
    a = ap.parse_args()

    hedges = [a.combo] if a.combo else a.legs
    n = math.ceil(a.payout)  # contracts per hedge market; each pays $1
    total_cost = total_fees = 0.0
    async with KalshiClient(load_settings()) as k:
        for ticker, side in hedges:
            m = await k.get_market(ticker)
            book = await k.get_orderbook(ticker)
            filled = fill_cost(book.asks(side), n)
            print(f"\n{ticker}  [{side.upper()}]  {m.get('title', '')}")
            if filled is None:
                print(f"  book too thin: {book.depth_at_or_better(side, 0.99):.0f} contracts available, need {n}")
                return
            cost, fees, worst = filled
            print(f"  buy {n} {side.upper()} up to {worst*100:.0f}c   cost ${cost:.2f}   est. fees ${fees:.2f}")
            total_cost, total_fees = total_cost + cost, total_fees + fees

    # Entry wins -> Pick6 pays `payout`. Entry loses -> at least one hedge pays `n`. Take the worse.
    worst_case = min(a.payout, n) - total_cost - total_fees - a.stake
    print(f"\nhedge cash needed  ${total_cost + total_fees:.2f}  (bankroll ${a.bankroll:.2f})")
    print(f"locked profit      ${worst_case:+.2f}  in the worst case")
    if total_cost + total_fees > a.bankroll:
        print("SKIP: hedge costs more than your bankroll")
    elif worst_case <= 0:
        print("SKIP: no locked profit at these prices")
    else:
        print("GO: hedge on Kalshi first, then submit the Pick6 entry with the identical lines")


if __name__ == "__main__":
    asyncio.run(main())
