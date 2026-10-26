"""
integrity.py — is a leg's sharp price trustworthy right now? Market-quality flags from the pricing record.

The risk this targets is a PHANTOM edge: our fair price is wrong (stale cache, one soft book, books that
disagree, a price read off an alternate ladder) rather than PrizePicks' line being soft. Box-score stats
can't see that (a stale price is a timing problem, and recent form is already in every book's number), so
the checks use only what the pricing record already holds. No network calls, no credits.

Features (features(leg, at)):
  kind        "kalshi" | "kalshi interp" | "kalshi whole" | "books main" | "books alt" | "books interp" |
              "books whole" (a whole line priced with its push from two adjacent rungs)
  age_s       seconds since the read behind the price (snapshots.fetched_ts)
  n_books     books in a books consensus (None for Kalshi)
  spread      max - min de-vigged P(over) across those books (main-line consensus only)
  pin_dev     Pinnacle's fair minus the other books' mean, for the PICKED side (< 0: the sharp book likes
              our side less than the soft ones do); None without Pinnacle and another book
  quote_spread  Kalshi bid/ask width (ask - bid); the widest of the rungs behind an interpolated or
              whole-line price. None for books, or a record without it (a pooled leg)
  volume      Kalshi contracts traded on the market; the thinnest rung behind a two-rung price. None
              when Kalshi didn't report it

Flags (flags(feat, cfg); pipeline_config "integrity"): stale (books age > max_age_s), single_book
(n_books < min_books), books_disagree (spread > max_spread), pin_against (pin_dev < -pin_against),
ladder_interp (books price interpolated between alternate rungs, when flag_interp), kalshi_wide
(quote_spread > kalshi_max_spread), kalshi_interp (Kalshi price interpolated between rungs: the yardage
props, when flag_kalshi_interp), kalshi_thin (volume < kalshi_min_volume). A leg with no flag is "clean".

Shading (shade(feat, cfg); pipeline_config "shading"), ACTIVE whatever enforce says: a Kalshi price's
picked side is pulled toward 50% by base_pts (every Kalshi leg) + spread_mult x quote_spread, + interp_pts
when interpolated, + thin_pts when thin, at most max_pts (scanner.Leg.shade / Leg.pick). The best-looking legs on a board of noisy mids
are disproportionately the mispriced ones (winner's curse); shading makes an uncertain 57% compete as
what it is. Books prices are not shaded: their quality is what the books flags above describe.

SHADOW by default (enforce false): flags are computed and reported, and the backtest slices CLV by
them; nothing is removed. enforce true removes flagged legs before both builders, the same place as
the injury guard; test it first as a --config replay (python3 -m local.backtest --config v.json with
{"integrity": {"enforce": true}}), which drops flagged legs from the paper optimizer's replay.
"""
from __future__ import annotations

from local import pipeline_config, snapshots
from local import prize_picks_ev_scraper as scanner

FLAGS = ("stale", "single_book", "books_disagree", "pin_against", "ladder_interp", "kalshi_wide", "kalshi_interp",
         "kalshi_thin")


def kind(detail: dict, source: str) -> str:
    method = (detail or {}).get("method") or ""
    if method.startswith("whole line"):
        return "kalshi whole" if source == "kalshi" else "books whole"
    if source == "kalshi":
        return "kalshi interp" if "interpolation" in method else "kalshi"
    if method.startswith("alternate ladder"):
        return "books interp" if "interpolation" in method else "books alt"
    return "books main"


def kalshi_quality(detail: dict) -> tuple[float | None, float | None]:
    """(bid/ask spread, contracts traded) of a Kalshi pricing record. A price made from two rungs
    (interpolated, or a whole line's push) is only as good as its worse rung: the widest spread and
    the smallest volume. The spread is the record's overround (YES ask + NO ask - 1 = ask - bid)."""
    parts = [r for r in detail.get("rungs") or [] if isinstance(r, dict)] or [detail]
    spreads = [r["overround"] for r in parts if r.get("overround") is not None]
    vols = [r["volume"] for r in parts if r.get("volume") is not None]
    return (round(max(spreads), 5) if spreads else None), (min(vols) if len(vols) == len(parts) else None)


def detail_features(detail: dict | None, source: str, side: str, age_s: float | None) -> dict:
    """The features from one pricing record (what snapshots stores as pricing_json)."""
    detail = detail or {}
    recs = detail.get("books") or []
    f = {"kind": kind(detail, source), "age_s": age_s, "n_books": None, "spread": None, "pin_dev": None,
         "quote_spread": None, "volume": None}
    if source == "kalshi":
        f["quote_spread"], f["volume"] = kalshi_quality(detail)
    if source != "books":
        return f
    f["n_books"] = len(recs)
    fairs = {r["book"]: r["fair_over"] for r in recs if isinstance(r, dict) and r.get("fair_over") is not None}
    if len(fairs) >= 2:
        f["spread"] = round(max(fairs.values()) - min(fairs.values()), 5)
    if "pinnacle" in fairs and len(fairs) >= 2:
        others = [p for b, p in fairs.items() if b != "pinnacle"]
        dev = fairs["pinnacle"] - sum(others) / len(others)
        f["pin_dev"] = round(dev if side == "More" else -dev, 5)
    return f


def features(l: scanner.Leg, at: float) -> dict:
    q = l.quotes[0] if l.quotes else None
    return detail_features(q.detail if q else None, snapshots.leg_source(l), l.pick[0] if l.pick else "More",
                           snapshots.price_age(l, at))


def flags(f: dict, cfg: dict | None = None) -> list[str]:
    c = (cfg or pipeline_config.CONFIG)["integrity"]
    out = []
    books = f["kind"].startswith("books")
    if books and f["age_s"] is not None and f["age_s"] > c["max_age_s"]:
        out.append("stale")
    if books and f["n_books"] is not None and f["n_books"] < c["min_books"]:
        out.append("single_book")
    if f["spread"] is not None and f["spread"] > c["max_spread"]:
        out.append("books_disagree")
    if f["pin_dev"] is not None and f["pin_dev"] < -c["pin_against"]:
        out.append("pin_against")
    if c["flag_interp"] and f["kind"] == "books interp":
        out.append("ladder_interp")
    kalshi = f["kind"].startswith("kalshi")
    if kalshi and f.get("quote_spread") is not None and f["quote_spread"] > c["kalshi_max_spread"] + 1e-9:
        out.append("kalshi_wide")
    if c["flag_kalshi_interp"] and f["kind"] == "kalshi interp":
        out.append("kalshi_interp")
    if kalshi and f.get("volume") is not None and f["volume"] < c["kalshi_min_volume"]:
        out.append("kalshi_thin")
    return out


def shade(f: dict, cfg: dict | None = None) -> float:
    """Points to pull a Kalshi price's picked side toward 50% (0 for books, or with shading off)."""
    cfg = cfg or pipeline_config.CONFIG
    s, c = cfg["shading"], cfg["integrity"]
    if not s["enabled"] or not f["kind"].startswith("kalshi"):
        return 0.0
    pts = s["base_pts"] + s["spread_mult"] * (f.get("quote_spread") or 0.0)
    if f["kind"] == "kalshi interp":
        pts += s["interp_pts"]
    if f.get("volume") is not None and f["volume"] < c["kalshi_min_volume"]:
        pts += s["thin_pts"]
    return round(min(pts, s["max_pts"]), 5)


def apply_shading(legs: list[scanner.Leg], cfg: dict | None = None) -> int:
    """Set each leg's shade from its own pricing record; returns how many legs are shaded. A pooled leg
    (venue "pool") keeps the shade its own sentinel gave it: its record isn't here to recompute from."""
    n = 0
    for l in legs:
        if any(q.venue == "pool" for q in l.quotes):
            n += bool(l.shade)
            continue
        l.shade = shade(features(l, 0.0), cfg) if l.quotes else 0.0
        n += bool(l.shade)
    return n


def screen(legs: list[scanner.Leg], at: float, cfg: dict | None = None) -> tuple[list[scanner.Leg], dict[int, list[str]]]:
    """(legs kept, {id(leg): flags}). Keeps everything unless integrity.enforce is true."""
    cfg = cfg or pipeline_config.CONFIG
    got = {id(l): flags(features(l, at), cfg) for l in legs}
    if not cfg["integrity"]["enforce"]:
        return legs, got
    return [l for l in legs if not got[id(l)]], got
