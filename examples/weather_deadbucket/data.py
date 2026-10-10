"""
data.py — the free public data the example needs, with a JSON file cache.

  Kalshi  settled daily-high markets (live + /historical)
  IEM     ASOS routine + special METARs (tmpf) and NWS CLI daily highs
          (Iowa Environmental Mesonet, mesonet.agron.iastate.edu — free, no key)

Settlement station per series comes from each market's rules ("CLIATL" -> ASOS KATL).
Kalshi closes each market at local-standard midnight, which is the NWS climate day,
so the observation window is simply the 24 h before close_time.
"""
from __future__ import annotations

import csv
import io
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import aiohttp

from kalshi.client import KalshiAPIError, KalshiClient

from .rule import event_day

CACHE = Path(__file__).resolve().parents[2] / "cache"
ASOS_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
CLI_URL = "https://mesonet.agron.iastate.edu/json/cli.py"

# Kalshi series -> ASOS station of the settlement climate report
STATIONS = {
    "KXHIGHNY": "KNYC", "KXHIGHCHI": "KMDW", "KXHIGHLAX": "KLAX", "KXHIGHMIA": "KMIA",
    "KXHIGHDEN": "KDEN", "KXHIGHAUS": "KAUS", "KXHIGHPHIL": "KPHL",
    "KXHIGHTATL": "KATL", "KXHIGHTBOS": "KBOS", "KXHIGHTDAL": "KDFW", "KXHIGHTDC": "KDCA",
    "KXHIGHTEWR": "KEWR", "KXHIGHTHOU": "KHOU", "KXHIGHTLV": "KLAS", "KXHIGHTMIN": "KMSP",
    "KXHIGHTNOLA": "KMSY", "KXHIGHTOKC": "KOKC", "KXHIGHTPHX": "KPHX", "KXHIGHTSAN": "KSAN",
    "KXHIGHTSATX": "KSAT", "KXHIGHTSDF": "KSDF", "KXHIGHTSEA": "KSEA", "KXHIGHTSFO": "KSFO",
    "KXHIGHTTTN": "KTTN",
}


def ts(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def num(x) -> float | None:
    return float(x) if x not in (None, "") else None


async def cached(name: str, fetch, use_cache: bool = True):
    p = CACHE / f"{name}.json"
    if use_cache and p.exists():
        return json.loads(p.read_text())
    data = await fetch()
    if use_cache:
        CACHE.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data))
    return data


async def settled_markets(k: KalshiClient, series: str, start: date, end: date) -> list[dict]:
    """Settled markets of `series` whose event day is in [start, end]."""
    async def fetch():
        keep = ("ticker", "event_ticker", "strike_type", "floor_strike", "cap_strike", "result", "close_time",
                "yes_sub_title")
        seen = {}
        for endpoint, params in (("/markets", {"series_ticker": series, "status": "settled"}),
                                 ("/historical/markets", {"series_ticker": series})):
            try:
                async for m in k.paginate(endpoint, "markets", params):
                    seen[m["ticker"]] = {f: m.get(f) for f in keep}
            except KalshiAPIError:
                pass
        return list(seen.values())
    ms = await cached(f"markets_{series}_{date.today()}", fetch)
    return [m for m in ms if (d := event_day(m["event_ticker"])) and start <= d <= end
            and (m.get("result") or "").lower() in ("yes", "no")]


async def asos_obs(session: aiohttp.ClientSession, series: str, start: date, end: date,
                   use_cache: bool = True) -> list[tuple[float, float]]:
    """Sorted (unix ts, tmpf) for the settlement station, UTC days start-1 .. end+1."""
    station = STATIONS[series][1:]  # IEM ASOS ids drop the leading K

    async def fetch():
        a, b = start - timedelta(days=1), end + timedelta(days=1)
        params = [("station", station), ("data", "tmpf"), ("tz", "Etc/UTC"), ("format", "onlycomma"),
                  ("latlon", "no"), ("missing", "M"), ("report_type", "3"), ("report_type", "4"),
                  ("year1", a.year), ("month1", a.month), ("day1", a.day),
                  ("year2", b.year), ("month2", b.month), ("day2", b.day)]
        async with session.get(ASOS_URL, params=params, timeout=aiohttp.ClientTimeout(total=120)) as r:
            r.raise_for_status()
            text = await r.text()
        out = []
        for row in csv.DictReader(io.StringIO(text)):
            if row.get("tmpf") not in (None, "", "M"):
                t = datetime.strptime(row["valid"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp()
                out.append([t, float(row["tmpf"])])
        return out
    obs = await cached(f"asos_{station}_{start}_{end}", fetch, use_cache)
    return sorted((t, v) for t, v in obs)


async def cli_highs(session: aiohttp.ClientSession, series: str, years: set[int]) -> dict[str, float]:
    """Event day (ISO) -> NWS CLI daily high."""
    out = {}
    for y in sorted(years):
        async def fetch(y=y):
            async with session.get(CLI_URL, params={"station": STATIONS[series], "year": y},
                                   timeout=aiohttp.ClientTimeout(total=60)) as r:
                r.raise_for_status()
                return await r.json()
        tag = date.today().isoformat() if y == date.today().year else "final"  # current year keeps growing
        d = await cached(f"cli_{series}_{y}_{tag}", fetch)
        for row in d.get("results", []):
            if isinstance(row.get("high"), (int, float)):
                out[row["valid"]] = float(row["high"])
    return out
