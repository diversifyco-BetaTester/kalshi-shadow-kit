"""
cross_exchange_arb.py — watch the same event on Kalshi and Polymarket, alert on a locked arb.

For each pair, two hedges cover every outcome:
    A: buy Kalshi YES + buy the Polymarket side that pays when Kalshi resolves NO
    B: buy Kalshi NO  + buy the Polymarket side that pays when Kalshi resolves YES
Exactly one leg of a hedge pays $1, so N contracts lock in N - cost - fees. The script walks
both ask ladders level by level and keeps adding size only while the NEXT contract still costs
less than $1.00 including fees, then re-prices the whole fill with exact (rounded-up) fees.

FEES   Kalshi taker: ceil(0.07 * C * P * (1 - P)) to the cent, per order.
       Polymarket taker: read per market from Gamma's `feeSchedule` {rate, exponent}, charged as
       rate * C * (P * (1 - P)) ** exponent. Some guides put an extra factor of P in front, which
       only makes the fee smaller, so this is the conservative reading. `takerBaseFee` is not used.

INPUT  pairs.csv:  kalshi_ticker,poly_slug,same_direction,note
       same_direction = 1 when Kalshi YES is Polymarket's FIRST outcome ("Yes", or the away team
       in an NFL game market), 0 when it is the second.

  python3 -m local.cross_exchange_arb --nfl-games 2026-10-11              # writes pairs.csv
  python3 -m local.cross_exchange_arb pairs.csv                           # one scan, full table
  python3 -m local.cross_exchange_arb pairs.csv --interval 30 --telegram
  python3 -m local.cross_exchange_arb --suggest --kalshi-series KXFEDDECISION --poly-tag fed-rates

Read-only: places no orders. An arb only exists if both venues resolve the event the same way;
`--nfl-games` checks the tie rule text on both sides and skips a pair that doesn't match.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import re
import time
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

import aiohttp

from kalshi.client import KalshiClient
from kalshi.config import load_settings
from local.prize_picks_ev_scraper import gamma_market, gamma_markets, norm
from ops.telegram import send_async

CLOB = "https://clob.polymarket.com"
KALSHI_FEE = 0.07
POLY_MIN_SHARES = 5  # Polymarket's orderMinSize on these markets

Ladder = list[tuple[float, float]]  # (price, contracts), cheapest first


@dataclass
class Pair:
    kalshi: str
    poly: str
    same_direction: bool
    note: str = ""


@dataclass(frozen=True)
class PolyFee:
    rate: float
    exponent: float = 1.0

    def per_share(self, p: float) -> float:
        return self.rate * (p * (1 - p)) ** self.exponent

    def total(self, levels: Ladder) -> float:
        return math.ceil(sum(q * self.per_share(p) for p, q in levels) * 10_000 - 1e-9) / 10_000

    @classmethod
    def from_market(cls, m: dict) -> PolyFee | None:
        if m.get("feesEnabled") is False:
            return cls(0.0)
        fs = m.get("feeSchedule") or {}
        if "rate" in fs:
            return cls(float(fs["rate"]), float(fs.get("exponent", 1)))
        return None  # unknown: caller must supply --poly-fee


@dataclass
class Fill:
    hedge: str
    kalshi_side: str
    poly_side: str
    contracts: float
    kalshi_levels: Ladder
    poly_levels: Ladder
    kalshi_fee: float
    poly_fee: float

    @property
    def kalshi_cost(self) -> float:
        return sum(p * q for p, q in self.kalshi_levels)

    @property
    def poly_cost(self) -> float:
        return sum(p * q for p, q in self.poly_levels)

    @property
    def total(self) -> float:
        return self.kalshi_cost + self.poly_cost + self.kalshi_fee + self.poly_fee

    @property
    def profit(self) -> float:
        return self.contracts - self.total


@dataclass
class Top:
    """Best ask on each side of one hedge, and what one contract nets after fees."""
    hedge: str
    kalshi_side: str
    poly_side: str
    k_ask: float | None
    k_size: float
    p_ask: float | None
    p_size: float
    k_fee: float
    p_fee: float

    @property
    def net(self) -> float | None:
        """$1 payout minus cost and fees for one contract. Positive = arb at the top of book."""
        if self.k_ask is None or self.p_ask is None:
            return None
        return 1 - self.k_ask - self.p_ask - self.k_fee - self.p_fee


def kalshi_fee(contracts: float, price: float) -> float:
    return math.ceil(KALSHI_FEE * contracts * price * (1 - price) * 100 - 1e-9) / 100


def walk(k_asks: Ladder, p_asks: Ladder, poly_fee: PolyFee) -> tuple[float, Ladder, Ladder]:
    """Take size from both ladders while the marginal pair costs < $1 with fees."""
    k, p = [list(x) for x in k_asks], [list(x) for x in p_asks]
    i = j = 0
    n, k_used, p_used = 0.0, [], []
    while i < len(k) and j < len(p):
        kp, pp = k[i][0], p[j][0]
        marginal = kp + pp + KALSHI_FEE * kp * (1 - kp) + poly_fee.per_share(pp)
        if marginal >= 1.0:
            break
        q = min(k[i][1], p[j][1])
        k_used.append((kp, q))
        p_used.append((pp, q))
        n += q
        k[i][1] -= q
        p[j][1] -= q
        i += k[i][1] <= 1e-9
        j += p[j][1] <= 1e-9
    return n, merge(k_used), merge(p_used)


def merge(levels: Ladder) -> Ladder:
    out: dict[float, float] = {}
    for price, q in levels:
        out[price] = out.get(price, 0.0) + q
    return sorted(out.items())


def trim(levels: Ladder, n: float) -> Ladder:
    out, left = [], n
    for price, q in levels:
        take = min(q, left)
        if take > 0:
            out.append((price, take))
        left -= take
    return out


def price_fill(hedge: str, k_side: str, p_side: str, k_asks: Ladder, p_asks: Ladder, poly_fee: PolyFee) -> Fill | None:
    n, k_lv, p_lv = walk(k_asks, p_asks, poly_fee)
    n = math.floor(n)  # whole contracts only
    if n < POLY_MIN_SHARES:
        return None
    k_lv, p_lv = trim(k_lv, n), trim(p_lv, n)
    fill = Fill(hedge, k_side, p_side, n, k_lv, p_lv,
                kalshi_fee=sum(kalshi_fee(q, pr) for pr, q in k_lv),  # per level, rounded up: conservative
                poly_fee=poly_fee.total(p_lv))
    return fill if fill.profit > 0 else None


def top(hedge: str, k_side: str, p_side: str, k_asks: Ladder, p_asks: Ladder, poly_fee: PolyFee) -> Top:
    kp, kq = k_asks[0] if k_asks else (None, 0.0)
    pp, pq = p_asks[0] if p_asks else (None, 0.0)
    return Top(hedge, k_side, p_side, kp, kq, pp, pq,
               KALSHI_FEE * kp * (1 - kp) if kp is not None else 0.0,
               poly_fee.per_share(pp) if pp is not None else 0.0)


# --------------------------------------------------------------------------- data
def two_outcomes(m: dict) -> bool:
    """Yes/No markets and two-team game markets both map onto one Kalshi YES/NO."""
    return len(json.loads(m.get("outcomes") or "[]")) == 2


async def clob_asks(s: aiohttp.ClientSession, token_id: str) -> Ladder:
    async with s.get(f"{CLOB}/book", params={"token_id": token_id}) as r:
        r.raise_for_status()
        book = await r.json()
    levels = [(float(a["price"]), float(a["size"])) for a in book.get("asks", [])]
    return sorted((p, q) for p, q in levels if 0 < p < 1 and q > 0)


async def scan_pair(k: KalshiClient, s: aiohttp.ClientSession, pair: Pair,
                    fee_override: PolyFee | None) -> tuple[list[Fill], list[Top], dict, dict, PolyFee]:
    km = await k.get_market(pair.kalshi)
    book = await k.get_orderbook(pair.kalshi)
    pm = await gamma_market(s, pair.poly)
    if pm is None or not two_outcomes(pm):
        raise ValueError(f"{pair.poly}: not found or not a two-outcome market")
    fee = fee_override or PolyFee.from_market(pm)
    if fee is None:
        raise ValueError(f"{pair.poly}: no feeSchedule in Gamma; pass --poly-fee RATE")
    first_tok, second_tok = json.loads(pm["clobTokenIds"])
    poly_first, poly_second = await clob_asks(s, first_tok), await clob_asks(s, second_tok)
    names = json.loads(pm["outcomes"])
    # Polymarket side that pays when Kalshi resolves NO / YES
    on_k_no, on_k_yes = ((names[1], poly_second), (names[0], poly_first)) if pair.same_direction \
        else ((names[0], poly_first), (names[1], poly_second))
    legs = [("A", "yes", on_k_no[0], book.asks("yes"), on_k_no[1]),
            ("B", "no", on_k_yes[0], book.asks("no"), on_k_yes[1])]
    fills = [price_fill(*leg, fee) for leg in legs]
    tops = [top(*leg, fee) for leg in legs]
    return [f for f in fills if f], tops, km, pm, fee


def report(pair: Pair, f: Fill, km: dict, pm: dict) -> str:
    lv = lambda levels: ", ".join(f"{q:.0f}@{p*100:.1f}c" for p, q in levels)
    return (f"ARB {pair.note or pair.kalshi}\n"
            f"  buy {f.contracts:.0f} Kalshi {f.kalshi_side.upper()} {pair.kalshi}: {lv(f.kalshi_levels)}"
            f"  = ${f.kalshi_cost:.2f} + fee ${f.kalshi_fee:.2f}\n"
            f"  buy {f.contracts:.0f} Poly {f.poly_side} {pair.poly}: {lv(f.poly_levels)}"
            f"  = ${f.poly_cost:.2f} + fee ${f.poly_fee:.4f}\n"
            f"  total ${f.total:.2f} -> pays ${f.contracts:.0f}  locked ${f.profit:+.2f} ({f.profit / f.total:+.1%})")


# --------------------------------------------------------------------------- pairing
KALSHI_EVENT = re.compile(r"^KXNFLGAME-(\d{2})([A-Z]{3})(\d{2})([A-Z]+)$")
POLY_GAME = re.compile(r"^nfl-([a-z]+)-([a-z]+)-\d{4}-\d{2}-\d{2}$")
POLY_TO_KALSHI = {"JAX": "JAC", "LA": "LAR"}  # team codes the two venues spell differently
ET = ZoneInfo("America/New_York")


def kalshi_event_date(event_ticker: str) -> tuple[date, str] | None:
    m = KALSHI_EVENT.match(event_ticker)
    if not m:
        return None
    yy, mon, dd, teams = m.groups()
    return datetime.strptime(f"20{yy}{mon}{dd}", "%Y%b%d").date(), teams


def poly_game_key(m: dict) -> tuple[date, str, str] | None:
    """(ET kickoff date, away code, home code) in Kalshi's spelling. Slug dates are UTC."""
    g = POLY_GAME.match(m.get("slug") or "")
    if not g or m.get("sportsMarketType") != "moneyline" or not m.get("gameStartTime"):
        return None
    kickoff = datetime.fromisoformat(m["gameStartTime"].replace(" ", "T").replace("+00", "+00:00"))
    away, home = (POLY_TO_KALSHI.get(c.upper(), c.upper()) for c in g.groups())
    return kickoff.astimezone(ET).date(), away, home


def same_tie_rule(kalshi_rules: str, poly_rules: str) -> bool:
    """Both must split a tie 50/50, or a 'hedge' pays $0.50 instead of $1."""
    k, p = kalshi_rules.lower(), poly_rules.lower()
    return "tie" in k and "0.50" in k and "tie" in p and "50-50" in p


async def nfl_games(day: date, out: str) -> None:
    async with KalshiClient(load_settings()) as k, \
            aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
        km = [m async for m in k.paginate("/markets", "markets", {"series_ticker": "KXNFLGAME", "status": "open"})]
        poly = {}
        async for _, m in gamma_markets(s, "nfl"):
            key = poly_game_key(m)
            if key and key[0] == day:
                poly[key[1] + key[2]] = (key[1], key[2], m)
        rows, used, skipped = [], set(), []
        for m in sorted(km, key=lambda m: m["ticker"]):
            parsed = kalshi_event_date(m["event_ticker"])
            if not parsed or parsed[0] != day:
                continue
            team = m["ticker"].rsplit("-", 1)[1]
            if parsed[1] not in poly:
                skipped.append(f"{m['ticker']}: no Polymarket moneyline for {parsed[1]} on {day}")
                continue
            away, home, pm = poly[parsed[1]]
            if team not in (away, home):
                skipped.append(f"{m['ticker']}: team {team} is neither {away} nor {home}")
                continue
            rules = f"{m.get('rules_primary', '')} {m.get('rules_secondary', '')}"
            if "tie" not in rules.lower():
                full = await k.get_market(m["ticker"])
                rules = f"{full.get('rules_primary', '')} {full.get('rules_secondary', '')}"
            if not same_tie_rule(rules, pm.get("description", "")):
                skipped.append(f"{m['ticker']}: tie rules differ or missing, not a hedge")
                continue
            used.add(parsed[1])
            rows.append({"kalshi_ticker": m["ticker"], "poly_slug": pm["slug"],
                         "same_direction": int(team == away), "note": f"{away}@{home} {team}"})

    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["kalshi_ticker", "poly_slug", "same_direction", "note"])
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {len(rows)} pairs ({len(used)} games) for {day} to {out}")
    for r in rows:
        print(f"  {r['note']:16} {r['kalshi_ticker']:34} {r['poly_slug']:28} same_direction={r['same_direction']}")
    for line in skipped:
        print("  SKIP", line)
    for code in sorted(set(poly) - used):
        print(f"  SKIP Polymarket {poly[code][2]['slug']}: no matching Kalshi market")


async def suggest(series: list[str], tag: str, min_score: float) -> None:
    """Rank Polymarket markets by title-word overlap with each Kalshi market. Candidates only."""
    async with KalshiClient(load_settings()) as k, \
            aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
        km = [m for sr in series async for m in k.paginate("/markets", "markets", {"series_ticker": sr, "status": "open"})]
        pm = [(ev, m) async for ev, m in gamma_markets(s, tag) if two_outcomes(m)]
    words = lambda t: set(norm(t).split()) - {"the", "a", "of", "in", "on", "will", "be", "by", "to"}
    for m in km:
        kw = words(f"{m.get('title', '')} {m.get('yes_sub_title', '')}")
        scored = sorted(((len(kw & (pw := words(f"{ev.get('title', '')} {p.get('question', '')}"))) / len(kw | pw), p)
                         for ev, p in pm), key=lambda x: -x[0])[:3]
        for score, p in scored:
            if score >= min_score:
                print(f"{score:.2f}  {m['ticker']},{p.get('slug')},?  | K: {m.get('title', '')} {m.get('yes_sub_title', '')}"
                      f" | P: {p.get('question', '')} {p.get('outcomes', '')}")


def load_pairs(path: str) -> list[Pair]:
    with open(path, newline="") as f:
        return [Pair(r["kalshi_ticker"].strip(), r["poly_slug"].strip(), r["same_direction"].strip() == "1",
                     (r.get("note") or "").strip()) for r in csv.DictReader(f)]


def print_table(rows: list[tuple[Pair, Top, PolyFee]]) -> None:
    c = lambda x: f"{x*100:5.1f}" if x is not None else "    -"
    print(f"\n{'game / Kalshi side':24} {'hedge':34} {'K ask':>5} {'K qty':>7} {'P ask':>5} {'P qty':>7}"
          f" {'K fee':>5} {'P fee':>5} {'net/ct':>6}  (cents; net = 100 - asks - fees)")
    for pair, t, fee in sorted(rows, key=lambda r: -(r[1].net if r[1].net is not None else -9)):
        hedge = f"K {t.kalshi_side.upper()} + P {t.poly_side}"
        net = f"{t.net*100:+6.2f}" if t.net is not None else "     -"
        print(f"{pair.note:24} {hedge:34} {c(t.k_ask)} {t.k_size:7.0f} {c(t.p_ask)} {t.p_size:7.0f}"
              f" {t.k_fee*100:5.2f} {t.p_fee*100:5.2f} {net}")


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pairs", nargs="?", help="CSV of Kalshi/Polymarket pairs")
    ap.add_argument("--nfl-games", metavar="YYYY-MM-DD", help="pair that day's NFL games (ET), write --out, exit")
    ap.add_argument("--out", default="local/pairs.csv")
    ap.add_argument("--poly-fee", type=float, help="override Polymarket fee rate (default: each market's feeSchedule)")
    ap.add_argument("--min-profit", type=float, default=0.01, help="ignore arbs locking less than this many $")
    ap.add_argument("--interval", type=float, default=0, help="seconds between scans; 0 = scan once")
    ap.add_argument("--telegram", action="store_true", help="also send alerts to the TELEGRAM_CHAT_ID in .env")
    ap.add_argument("--suggest", action="store_true", help="print candidate pairs, then exit")
    ap.add_argument("--kalshi-series", nargs="*", default=[])
    ap.add_argument("--poly-tag", default="")
    ap.add_argument("--min-score", type=float, default=0.3)
    a = ap.parse_args()

    if a.nfl_games:
        return await nfl_games(date.fromisoformat(a.nfl_games), a.out)
    if a.suggest:
        if not (a.kalshi_series and a.poly_tag):
            ap.error("--suggest needs --kalshi-series and --poly-tag")
        return await suggest(a.kalshi_series, a.poly_tag, a.min_score)
    if not a.pairs:
        ap.error("give a pairs CSV, --nfl-games, or --suggest")

    pairs = load_pairs(a.pairs)
    override = PolyFee(a.poly_fee) if a.poly_fee is not None else None
    print(f"{len(pairs)} pairs | Kalshi fee ceil(0.07*C*P*(1-P)) | Polymarket fee "
          + (f"{a.poly_fee} * C*P*(1-P) (override)" if override else "from each market's feeSchedule"))
    last_alert: dict[tuple[str, str], float] = {}
    async with KalshiClient(load_settings()) as k, \
            aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
        while True:
            found, table, fees = 0, [], set()
            for pair in pairs:
                try:
                    fills, tops, km, pm, fee = await scan_pair(k, s, pair, override)
                except Exception as e:  # one bad pair shouldn't stop the scan
                    print(f"  skip {pair.kalshi} / {pair.poly}: {e}")
                    continue
                fees.add(fee)
                table += [(pair, t, fee) for t in tops]
                for f in fills:
                    if f.profit < a.min_profit:
                        continue
                    found += 1
                    key = (pair.kalshi, f.hedge)
                    if abs(last_alert.get(key, -1) - f.profit) < 0.005:
                        continue  # same arb as last scan, don't repeat the alert
                    last_alert[key] = f.profit
                    msg = report(pair, f, km, pm)
                    print(time.strftime("%H:%M:%S"), msg)
                    if a.telegram:
                        await send_async(msg)
            if not a.interval:
                print_table(table)
                print("Polymarket fee schedules seen:", ", ".join(f"rate {f.rate} exp {f.exponent}" for f in fees))
            print(f"{time.strftime('%H:%M:%S')} scan done: {found} arb(s) of >= {POLY_MIN_SHARES} contracts")
            if not a.interval:
                return
            await asyncio.sleep(a.interval)


if __name__ == "__main__":
    asyncio.run(main())
