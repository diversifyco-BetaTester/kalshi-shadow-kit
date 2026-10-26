"""
discovery.py — which PrizePicks stat types we can't price, and whether a sharp reference exists for them.

A MEASUREMENT step only: it maps nothing, and it never prices a leg. A leg priced by a model of our own has
no independent closing market, so it can't produce CLV; only a market some venue actually posts is a
candidate.

Inputs (no PrizePicks request, no Odds API odds request):
  * data/snapshots_<sport>.sqlite3: cycles.coverage_json gives {stat: [legs, priced]} for every cycle (the
    standard half-line legs the sentinel actually sees); the stored raw boards give the games per day on
    which each stat is offered (re-parsed with local/parse_board, so stat names match the coverage).
  * The Odds API market CATALOG for one real upcoming event per sport, from the event-markets endpoint
    (/events/{id}/markets: the market keys each of our books has posted; quota cost 1 per call). It goes
    through the same budgeted OddsBook as every sentinel (shared daily cap, sport shares, reserve) and is
    cached in local/.odds_market_catalog.json: it is NEVER re-checked unless --refresh-catalog is given.

Buckets (exactly one per stat):
  MAPPED               requested from The Odds API (fetch_odds.MARKETS) or priced from Kalshi in the logs
  SUPPORTED-UNMAPPED   not requested, but its Odds API market key (CANDIDATES) is in the catalog
  UNSUPPORTED          no Kalshi price and no posted Odds API market: no sharp reference exists

Credit estimate for a SUPPORTED-UNMAPPED stat: the API charges one credit per market (x region) per event
request, so adding a market adds one credit to each event fetch that has a leg needing it. An event is
fetched once per TTL inside the pre-game window (window - START_BUFFER_MIN), i.e. FETCHES_PER_GAME times,
so added credits/day = games/day x FETCHES_PER_GAME x (1, or 2 when its alternate ladder is also needed).

  python3 -m ops.discovery                      # report from the logs + the cached catalog
  python3 -m ops.discovery --refresh-catalog    # re-check the catalog (about 1 credit per sport)
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import fcntl
import gzip
import json
import math
import re
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv

from local import fetch_odds, parse_board
from local import prize_picks_ev_scraper as scanner

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
CATALOG = ROOT / "local" / ".odds_market_catalog.json"
OUT = DATA / "discovery.csv"
BUCKETS = ("MAPPED", "SUPPORTED-UNMAPPED", "UNSUPPORTED")
FETCHES_PER_GAME = math.ceil((fetch_odds.LOOKAHEAD_H * 3600 - scanner.START_BUFFER_MIN * 60) / fetch_odds.TTL_S)

# PrizePicks stat (as parse_board names it) -> its documented Odds API market key, for stats we do NOT request.
# A lookup for this report only: nothing here is requested by the sentinels. Whether a key counts as
# SUPPORTED is decided by the live catalog, never by this table alone.
CANDIDATES = {
    "nfl": {"longest reception": "player_reception_longest", "longest rush": "player_rush_longest",
            "longest completion": "player_pass_longest_completion", "rush+rec yds": "player_rush_reception_yds",
            "pass+rush yds": "player_pass_rush_yds", "pass+rush+rec tds": "player_pass_rush_reception_tds",
            "tackles+ast": "player_tackles_assists", "kicking points": "player_kicking_points",
            "field goals made": "player_field_goals", "sacks": "player_sacks", "int": "player_pass_interceptions",
            "pass completions": "player_pass_completions", "touchdowns": "player_anytime_td"},
    "nhl": {},
    "mlb": {},
}
CANDIDATES["cfb"] = dict(CANDIDATES["nfl"])


# --------------------------------------------------------------------------- board volume
@dataclass
class StatVolume:
    legs: int = 0             # summed over cycles
    priced: int = 0
    kalshi_rows: int = 0      # price rows from Kalshi in the logs
    books_rows: int = 0
    games_by_day: dict = field(default_factory=lambda: defaultdict(set))


def volumes(path: Path, sport: str, days: int | None = None) -> tuple[int, int, dict[str, StatVolume]]:
    """(cycles, legs per cycle summed, {stat: StatVolume}) from one sport's snapshot file (read-only)."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    since = 0.0
    if days:
        last = con.execute("SELECT MAX(ts) FROM cycles").fetchone()[0] or 0
        since = last - days * 86400
    out: dict[str, StatVolume] = defaultdict(StatVolume)
    cycles = total = 0
    for (cov,) in con.execute("SELECT coverage_json FROM cycles WHERE coverage_json IS NOT NULL AND ts >= ?", (since,)):
        cycles += 1
        for stat, (n, p) in json.loads(cov).items():
            out[stat].legs += n
            out[stat].priced += p
            total += n
    for stat, source, n in con.execute("SELECT stat, source, COUNT(*) FROM prices WHERE ts >= ? GROUP BY 1, 2", (since,)):
        if stat in out:
            if source == "kalshi":
                out[stat].kalshi_rows += n
            elif source == "books":
                out[stat].books_rows += n
    for board_ts, blob in con.execute("SELECT board_ts, props_gz FROM boards WHERE board_ts >= ?", (since,)):
        rows, _ = parse_board.parse_json({"props": json.loads(gzip.decompress(blob))}, sport)
        day = datetime.fromtimestamp(board_ts, scanner.EASTERN).date().isoformat()
        for r in rows:
            if r["stat"] in out:
                out[r["stat"]].games_by_day[day].add(r["game"])
    con.close()
    return cycles, total, dict(out)


# --------------------------------------------------------------------------- Odds API catalog
def load_catalog(path: Path = CATALOG) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


async def fetch_catalog(book: fetch_odds.OddsBook, sport: str, now: float) -> dict:
    """The market keys our books have posted for the next upcoming event of `sport`: one event-markets
    call (quota cost 1), budgeted like every paid call. Returns {"error": ...} instead of spending when
    the budget refuses."""
    with open(book.cache_path.with_suffix(".lock"), "a") as lock:  # the same lock every pricing pass holds
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            book.load()
            events = [e for e in await book.events(sport, now)
                      if fetch_odds.ts(e["commence_time"]) > now + scanner.START_BUFFER_MIN * 60]
            if not events:
                return {"error": "no upcoming event listed"}
            e = min(events, key=lambda e: e["commence_time"])
            if not book.affordable(1, sport):
                u = book.usage()
                return {"error": f"budget refused (today {u['used']}/{u['cap']}, balance {u.get('remaining')})"}
            before = book.usage()["used"]
            body = await book.call(f"/sports/{e.get('sport_key') or fetch_odds.SPORT_KEYS[sport][0]}/events/{e['id']}/markets",
                                   {"regions": "us", "bookmakers": ",".join(fetch_odds.BOOKS)}, sport)
            cost = book.usage()["used"] - before
            book.save()
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    if not isinstance(body, dict):
        return {"error": "; ".join(book.notes) or "no response", "cost": cost}
    books = {b["key"]: sorted(m["key"] for m in b.get("markets", [])) for b in body.get("bookmakers", [])}
    return {"ts": now, "event_id": e["id"], "event": f"{e.get('away_team')} @ {e.get('home_team')}",
            "commence_time": e["commence_time"], "books": books, "cost": cost}


async def ensure_catalog(sports: list[str], refresh: bool, book: fetch_odds.OddsBook | None,
                         path: Path = CATALOG, now: float | None = None) -> dict:
    """The cached catalog, fetching only sports never checked (or all with refresh). Never re-checks
    on its own: a missing book (no ODDS_API_KEY) leaves those sports unchecked."""
    now = time.time() if now is None else now
    cat = load_catalog(path)
    for sport in sports:
        if sport in cat and "books" in cat[sport] and not refresh:
            continue
        if book is None:
            cat.setdefault(sport, {"error": "not checked (no ODDS_API_KEY)"})
            continue
        cat[sport] = await fetch_catalog(book, sport, now)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cat, indent=1))
    return cat


def posted(cat: dict, sport: str) -> set[str] | None:
    """Market keys any of our books posted for the checked event (base keys and alternates); None if unchecked."""
    c = cat.get(sport) or {}
    if "books" not in c:
        return None
    return {k for keys in c["books"].values() for k in keys}


# --------------------------------------------------------------------------- report
@dataclass
class Row:
    sport: str
    stat: str
    bucket: str
    legs_per_cycle: float
    board_share: float
    priced_pct: float
    odds_key: str
    in_catalog: str
    games_per_day: float
    added_credits_day: float | None
    added_credits_day_alt: float | None
    note: str


def classify(sport: str, stat: str, v: StatVolume, keys: set[str] | None) -> tuple[str, str, str, str]:
    """(bucket, odds_key, in_catalog, note)."""
    mapped_key = fetch_odds.MARKETS.get(sport, {}).get(stat)
    if mapped_key or v.kalshi_rows:
        via = " + ".join(x for x in (f"books {mapped_key}" if mapped_key else "", "Kalshi" if v.kalshi_rows else "") if x)
        note = f"mapped via {via}"
        cat = "" if keys is None or not mapped_key else ("yes" if mapped_key in keys else "no")
        if mapped_key and cat == "no":
            note += "; requested, but our books didn't post it for the checked event"
        if v.priced == 0:
            note += "; 0 legs priced in the logs"
        return "MAPPED", mapped_key or "", cat, note
    key = CANDIDATES.get(sport, {}).get(stat)
    if keys is None:
        return "UNSUPPORTED", key or "", "", "catalog not checked: bucket provisional"
    if key and key in keys:
        alias = next((s for s, k in fetch_odds.MARKETS.get(sport, {}).items() if k == key), None)
        return ("SUPPORTED-UNMAPPED", key, "yes",
                f"same market as mapped '{alias}': a stat-name alias, not a new market" if alias else "")
    hint = sorted(k for k in keys if set(re.findall(r"[a-z]+", stat)) & set(k.split("_")[1:]) - {"player", "alternate"})
    note = (f"documented key {key} not posted by our books for the checked event" if key
            else "no Kalshi market, no Odds API key")
    if hint and not key:
        note += f"; catalog keys sharing a word (check by hand): {', '.join(hint[:4])}"
    return "UNSUPPORTED", key or "", "no" if key else "", note


def build_rows(sport: str, cycles: int, total: int, vols: dict[str, StatVolume], keys: set[str] | None) -> list[Row]:
    rows = []
    for stat, v in vols.items():
        bucket, key, cat, note = classify(sport, stat, v, keys)
        days = v.games_by_day
        games = sum(len(g) for g in days.values()) / len(days) if days else 0.0
        cost = alt = None
        if bucket == "SUPPORTED-UNMAPPED":
            already = key in fetch_odds.MARKETS.get(sport, {}).values()
            cost = 0.0 if already else games * FETCHES_PER_GAME
            alt = 0.0 if already else games * FETCHES_PER_GAME * 2
        rows.append(Row(sport, stat, bucket, v.legs / cycles if cycles else 0.0, v.legs / total if total else 0.0,
                        v.priced / v.legs if v.legs else 0.0, key, cat, games, cost, alt, note))
    return rows


def budget_context(usage: dict, shares: dict, reset: date, today: date, reserve: int) -> tuple[list[str], float | None]:
    remaining = usage.get("remaining")
    days_left = max((reset - today).days, 1)
    cap = int(usage.get("cap") or 0)
    lines = [f"Budget: {remaining if remaining is not None else '?'} credits left on the plan (reserve {reserve} never spent), "
             f"{days_left} days to the reset on {reset.isoformat()}; today's shared cap {cap} "
             f"({', '.join(f'{g} {n}' for g, n in fetch_odds.group_limits(cap, shares).items())}), "
             f"{usage.get('used', 0)} used today."]
    sustainable = None
    if remaining is not None:
        sustainable = max(remaining - reserve, 0) / days_left
        lines.append(f"Sustainable spend to the reset: {sustainable:.1f} credits/day IN TOTAL across every sport. "
                     f"The {cap}/day cap is {'above' if cap > sustainable else 'within'} that: the cap is a ceiling, "
                     "not an allowance.")
    lines.append(f"Cadence: each event is fetched once per {fetch_odds.TTL_S // 60} min inside the "
                 f"{fetch_odds.LOOKAHEAD_H} h pre-game window, so {FETCHES_PER_GAME} fetches per game; an added market "
                 "costs 1 credit per fetch (2 if its alternate ladder is needed too).")
    return lines, sustainable


def summary(rows: list[Row], cycles: dict[str, int], budget: list[str], sustainable: float | None,
            cat: dict, top: int = 25) -> str:
    out = ["PROP DISCOVERY (measurement only: nothing mapped, nothing model-priced)", ""]
    for sport, n in cycles.items():
        c = cat.get(sport) or {}
        checked = (f"catalog: {c['event']} ({c['commence_time'][:16]}), {len(posted(cat, sport) or [])} market keys "
                   f"across {len(c['books'])} books, cost {c.get('cost', '?')}" if "books" in c
                   else f"catalog: {c.get('error', 'not checked')}")
        out.append(f"{sport.upper()}: {n} cycles · {checked}")
    out += [""] + budget + [""]
    out.append(f"{'stat':<34} {'sport':<5} {'bucket':<19} {'legs/cyc':>8} {'share':>6} {'priced':>7} {'+cr/day':>9}  note")
    for r in sorted(rows, key=lambda r: -r.legs_per_cycle)[:top]:
        cr = "" if r.added_credits_day is None else f"{r.added_credits_day:.0f}-{r.added_credits_day_alt:.0f}"
        out.append(f"{r.stat[:34]:<34} {r.sport:<5} {r.bucket:<19} {r.legs_per_cycle:8.1f} {r.board_share:6.1%} "
                   f"{r.priced_pct:7.0%} {cr:>9}  {r.note}")
    su = [r for r in rows if r.bucket == "SUPPORTED-UNMAPPED"]
    out.append("")
    for b in BUCKETS:
        mine = [r for r in rows if r.bucket == b]
        out.append(f"{b}: {len(mine)} stats, {sum(r.legs_per_cycle for r in mine):.1f} legs/cycle")
    if su:
        lo, hi = sum(r.added_credits_day for r in su), sum(r.added_credits_day_alt for r in su)
        out.append(f"Mapping every SUPPORTED-UNMAPPED stat would add ~{lo:.0f}-{hi:.0f} credits/day"
                   + (f" against {sustainable:.1f}/day sustainable in total" if sustainable is not None else ""))
    return "\n".join(out)


def write_csv(rows: list[Row], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["stat", "sport", "bucket", "legs_per_cycle", "board_share", "priced_pct", "odds_key", "in_catalog",
                    "games_per_day", "est_added_credits_per_day", "est_added_credits_per_day_with_alt", "note"])
        for r in sorted(rows, key=lambda r: -r.legs_per_cycle):
            w.writerow([r.stat, r.sport, r.bucket, round(r.legs_per_cycle, 2), round(r.board_share, 4), round(r.priced_pct, 4),
                        r.odds_key, r.in_catalog, round(r.games_per_day, 2),
                        "" if r.added_credits_day is None else round(r.added_credits_day, 1),
                        "" if r.added_credits_day_alt is None else round(r.added_credits_day_alt, 1), r.note])


def run(paths: list[Path], book: fetch_odds.OddsBook | None, refresh: bool = False, catalog: Path = CATALOG,
        out: Path = OUT, days: int | None = None, reset: date | None = None, today: date | None = None) -> str:
    today = today or date.today()
    reset = reset or (date(today.year + (today.month == 12), today.month % 12 + 1, 1))
    data = {}
    for p in paths:
        sport = re.match(r"snapshots_(\w+)\.sqlite3$", p.name).group(1)
        data[sport] = volumes(p, sport, days)
    cat = asyncio.run(ensure_catalog(list(data), refresh, book, catalog))
    rows = []
    for sport, (cycles, total, vols) in data.items():
        rows += build_rows(sport, cycles, total, vols, posted(cat, sport))
    write_csv(rows, out)
    usage = book.usage() if book else {}
    shares = book.shares if book else {}
    budget, sustainable = budget_context(usage, shares, reset, today, book.reserve if book else fetch_odds.RESERVE)
    return summary(rows, {s: d[0] for s, d in data.items()}, budget, sustainable, cat) + f"\n\nCSV: {out}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", action="append", type=Path, help="snapshot file(s) (default: every data/snapshots_*.sqlite3)")
    ap.add_argument("--refresh-catalog", action="store_true", help="re-check the Odds API market catalog (about 1 credit a sport)")
    ap.add_argument("--days", type=int, help="only the last N days of cycles and boards")
    ap.add_argument("--reset-date", help="when the Odds API plan's credits reset (YYYY-MM-DD; default: the 1st of next month)")
    ap.add_argument("--out", type=Path, default=OUT, help="CSV path")
    a = ap.parse_args()
    paths = a.db or sorted(DATA.glob("snapshots_*.sqlite3"))
    if not paths:
        raise SystemExit("no snapshot files yet (data/snapshots_<sport>.sqlite3)")
    load_dotenv(ROOT / ".env")
    book = fetch_odds.OddsBook.from_env()
    reset = date.fromisoformat(a.reset_date) if a.reset_date else None
    print(run(paths, book, a.refresh_catalog, out=a.out, days=a.days, reset=reset))


if __name__ == "__main__":
    main()
