"""
signals.py — LOGGED-ONLY signals (approved 2026-11-04, Builds 1-5). Nothing here feeds slip choice, ranking,
stake or a filter.

ISOLATION BOUNDARY. The live builder (sentinel.flex_entries / mixed_entries / reevaluate / stake_units /
would_send / claim), optimizer.build, ev_pool, correlation and integrity.screen never import this module, and
nothing they read is written here (tests/test_signals.py asserts both). sentinel.cycle calls it only:
  * after the slips are built, JIT-refreshed and claimed (logging), and
  * in the send loop (Build 1: message ORDER and the extra alert line; order never changes what is sent).

MISSING DATA. Every field is val(v) = {"value": v} or val(None, reason) = {"value": None, "missing": reason}:
never a silent default, never an inferred payout multiplier, never a back-filled observation. Rows written
before a field existed have no record; the harness reports them as "not_logged".

VERSIONING. Every record carries calc_version (VERSIONS). A logic change bumps the version; stored rows are
never rewritten, and the harness reports what each stored row's version says.

  Build 1  display_record (per alerted leg: fair prob, raw per-book prices, price age, source count, the 8
           integrity flags), slip_meta (kelly_growth from optimizer.slip_stats), american(), display_line(),
           send_order() (stable: ties keep the live builder's EV order)
  Build 2  observation() before/after a JIT refresh -> prob_delta() against the LAST DISTINCT price
  Build 3  line_gap(): PrizePicks line vs the books' consensus MAIN line, for legs the books left unpriced
           whose market was already cached (no extra request, no CLV: coverage only; NOT a middle)
  Build 4  leg_meta (odds_type designation, displayed line, board snapshot time) and slip_meta (entry type,
           leg count, displayed multiplier = missing "not_on_board", payout table version); shadow_legs():
           demon/goblin board rows priced from Kalshi ONLY (0 credits), logged as kind "shadow_promo"
  Build 5  load_targeted() / listed(): a manual watchlist (<= 20 entries) that only TAGS legs
"""
from __future__ import annotations

import hashlib
import json
import random
import statistics
from datetime import datetime, timezone
from pathlib import Path

from local import integrity, optimizer, snapshots
from local import prize_picks_ev_scraper as scanner

VERSIONS = {"display": "display/1", "slip_meta": "slip_meta/1", "leg_meta": "leg_meta/1",
            "prob_delta": "prob_delta/1", "line_gap": "line_gap/1", "shadow_promo": "shadow_promo/1",
            "watchlist": "watchlist/1"}
CLOSING_WARNING = snapshots.CLOSING_WARNING
SHADOW_LABEL = "shadow, Kalshi-only, not entry CLV"
BOOK_SHORT = {"pinnacle": "PIN", "draftkings": "DK", "fanduel": "FD", "betmgm": "MGM"}
TARGETED = Path("local/targeted_props.json")
TARGETED_MAX = 20
MIN_N = 30


def val(v, missing: str | None = None) -> dict:
    return {"value": v} if missing is None else {"value": None, "missing": missing}


def value(rec: dict | None, name: str):
    f = (rec or {}).get(name)
    return f.get("value") if isinstance(f, dict) else None


def reason(rec: dict | None, name: str) -> str | None:
    if rec is None:
        return "not_logged"
    f = rec.get(name)
    return f.get("missing") if isinstance(f, dict) else "not_logged"


# --------------------------------------------------------------------------- Build 1: alert presentation
def american(dec: float | None) -> str:
    """A decimal price as American odds, for the alert text only (the logged value stays decimal)."""
    if not isinstance(dec, (int, float)) or dec <= 1:
        return "?"
    return f"+{round((dec - 1) * 100)}" if dec >= 2 else f"-{round(100 / (dec - 1))}"


def _pooled(l: scanner.Leg) -> bool:
    return any(q.venue == "pool" for q in l.quotes)


def display_record(l: scanner.Leg, at: float) -> dict:
    """What the alert shows for one leg, from the leg's own pricing record, logged in slip_legs.display_json."""
    side_p = l.raw_pick
    src = snapshots.leg_source(l)
    q = l.quotes[0] if l.quotes else None
    detail = (q.detail or {}) if q else {}
    rec = {"calc_version": VERSIONS["display"], "source": src, "side": side_p[0] if side_p else None,
           "method": detail.get("method") or ("pooled" if _pooled(l) else None),
           "fair_p": val(round(side_p[1], 5)) if side_p else val(None, "unpriced"),
           "shade": val(round(l.shade, 5))}
    age = snapshots.price_age(l, at)
    rec["price_age_s"] = val(age) if age is not None else val(None, "no_fetched_ts")
    if _pooled(l):  # another sport's sentinel priced it: its pricing record isn't in this process
        for k in ("raw", "n_sources", "flags"):
            rec[k] = val(None, "pooled_leg_no_record")
        return rec
    if q is None:
        for k in ("raw", "n_sources", "flags"):
            rec[k] = val(None, "unpriced")
        return rec
    if src == "books":
        books = [b for b in detail.get("books") or [] if isinstance(b, dict) and b.get("over_price")]
        rec["raw"] = (val([{"book": BOOK_SHORT.get(b.get("book"), b.get("book")), "over": b["over_price"],
                            "under": b.get("under_price"), "fmt": b.get("fmt"), "fair_over": b.get("fair_over")}
                           for b in books]) if books else val(None, "no_per_book_prices"))
        rec["n_sources"] = val(len(books)) if books else val(None, "no_per_book_prices")
    else:
        parts = [r for r in detail.get("rungs") or [] if isinstance(r, dict)] or [detail]
        raw = [{"book": "KX", "rung": r.get("rung"), "over": r.get("over_price"), "under": r.get("under_price"),
                "fmt": r.get("fmt")} for r in parts if r.get("over_price") is not None]
        rec["raw"] = val(raw) if raw else val(None, "no_kalshi_prices")
        rec["n_sources"] = val(1)  # one venue; a two-rung price is still one source
    rec["flags"] = val(integrity.flags(integrity.features(l, at)))
    return rec


def _raw_text(rec: dict) -> str:
    raw = value(rec, "raw")
    if raw is None:
        return f"raw n/a ({reason(rec, 'raw')})"
    more = rec.get("side") != "Less"
    out = []
    for r in raw:
        price = r["over"] if more else r.get("under")
        if r.get("fmt") == "prob":  # Kalshi: the ask for our side, in dollars
            tag = f"KX{'' if r.get('rung') is None else ' ' + str(r['rung']) + '+'}"
            out.append(f"{tag}:{price:.2f}" if isinstance(price, (int, float)) else f"{tag}:?")
        else:
            out.append(f"{r['book']}:{american(price)}")
    return " ".join(out)


def display_line(rec: dict | None) -> str:
    """The extra alert line under a leg: fair prob next to the raw prices, price age, source count, flags."""
    if rec is None:
        return "   🔎 fair n/a (not logged)"
    fair = value(rec, "fair_p")
    parts = [f"fair {fair:.1%} ({_raw_text(rec)})" if fair is not None else f"fair n/a ({reason(rec, 'fair_p')})"]
    age = value(rec, "price_age_s")
    parts.append("age n/a" if age is None else f"age {age:.0f}s" if age < 60 else f"age {age / 60:.0f}m")
    n = value(rec, "n_sources")
    parts.append(f"{n} src" if n is not None else "src n/a")
    flags = value(rec, "flags")
    parts.append(f"flags n/a ({reason(rec, 'flags')})" if flags is None else
                 ("⚑ " + ", ".join(flags)) if flags else "no flags")
    return "   🔎 " + " · ".join(parts)


def payout_version(kind: str, cfg: dict | None = None) -> str:
    from local import pipeline_config
    cfg = cfg or pipeline_config.CONFIG
    blob = json.dumps({"table": {str(k): {str(h): m for h, m in v.items()} for k, v in cfg["payouts"].get(kind, {}).items()},
                       "reversion": cfg["reversion"]}, sort_keys=True)
    return f"{kind}:{hashlib.sha1(blob.encode()).hexdigest()[:12]}"


def slip_meta(entry: tuple) -> dict:
    """Slip-level record (slip_legs.slip_json): kelly_growth by the optimizer's own criterion
    (optimizer.slip_stats(...).growth, the full-Kelly log-growth the paper builder ranks by), entry type,
    leg count, payout table version, and the displayed multiplier, which the board never shows."""
    ev, kind, combo, stacks = entry
    if stacks:
        growth = val(None, "stacked_slip_unmodeled")
    else:
        try:
            growth = val(round(optimizer.slip_stats([optimizer.leg_prob(l) for l in combo], kind).growth, 8))
        except (ValueError, KeyError, TypeError) as err:
            growth = val(None, f"growth_error:{type(err).__name__}")
    return {"calc_version": VERSIONS["slip_meta"], "kelly_growth": growth, "entry_type": val(kind),
            "leg_count": val(len(combo)), "displayed_multiplier": val(None, "not_on_board"),
            "payout_table_version": val(payout_version(kind))}


def send_order(fresh: list, growth_of) -> list:
    """The send loop's order: logged kelly_growth, highest first. Python's sort is stable, so ties (and slips
    without a logged growth, which go last) keep the live builder's order (EV, best first)."""
    def key(e):
        g = growth_of(e)
        return (g is None, -(g or 0.0))
    return sorted(fresh, key=key)


# --------------------------------------------------------------------------- Builds 4/5: leg meta
def leg_meta(l: scanner.Leg, board_ts: float | None, has_board: bool, watch: dict | None) -> dict:
    pooled = _pooled(l)
    return {"calc_version": VERSIONS["leg_meta"],
            "odds_type": val(None, "pooled_leg_not_carried") if pooled else val(l.odds_type),
            "displayed_line": val(l.line),
            "board_ts": (val(None, "pooled_leg_not_carried") if pooled else
                         val(board_ts) if board_ts and has_board else val(None, "no_board_snapshot")),
            "watchlist": watch_field(l, watch)}


# --------------------------------------------------------------------------- Build 5: watchlist (tag only)
def _key(player: str, stat: str) -> tuple:
    return " ".join(scanner.norm(player).split()), " ".join(str(stat).lower().split())


def load_targeted(path: Path | str = TARGETED) -> dict | None:
    """{"props": [{"player": ..., "stat": ..., "sport": optional}]}, maintained by hand, at most 20 entries
    (extra entries are ignored, with a note). None when the file doesn't exist: no watchlist configured."""
    path = Path(path)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        props = data["props"] if isinstance(data, dict) else data
        rows = [p for p in props if isinstance(p, dict) and p.get("player") and p.get("stat")]
    except (OSError, ValueError, KeyError, TypeError) as err:
        return {"keys": set(), "n": 0, "error": f"unreadable: {err!r}", "notes": [f"{path}: unreadable ({err!r})"]}
    notes = [f"{path}: {len(rows)} entries, only the first {TARGETED_MAX} used"] if len(rows) > TARGETED_MAX else []
    rows = rows[:TARGETED_MAX]
    keys = {(*_key(p["player"], p["stat"]), str(p.get("sport") or "").lower() or None) for p in rows}
    return {"keys": keys, "n": len(rows), "error": None, "notes": notes}


def listed(l: scanner.Leg, watch: dict | None) -> bool:
    if not watch:
        return False
    k = _key(l.player, l.stat)
    return (*k, None) in watch["keys"] or (*k, (l.sport or "").lower()) in watch["keys"]


def watch_field(l: scanner.Leg, watch: dict | None) -> dict:
    if watch is None:
        return val(None, "no_watchlist_configured")
    if watch.get("error"):
        return val(None, "watchlist_unreadable")
    return val(listed(l, watch))


def watch_coverage(watch: dict | None, legs, good, eligible, chosen, events) -> dict | None:
    if watch is None:
        return None
    n = lambda xs: sum(listed(l, watch) for l in xs)
    return {"calc_version": VERSIONS["watchlist"], "entries": watch["n"], "on_board": n(legs),
            "priced": n([l for l in legs if l.quotes]), "good": n(good), "eligible": n(eligible),
            "jit_events": sum(1 for ev in events if value(ev, "watchlist")), "chosen": n(chosen)}


# --------------------------------------------------------------------------- Build 2: prob_delta
def observation(l: scanner.Leg) -> dict:
    """A leg's price as it stands: what prob_delta compares (the raw market price, P(More))."""
    q = l.quotes[0] if l.quotes else None
    detail = (q.detail or {}) if q else {}
    books = [b.get("book") for b in detail.get("books") or [] if isinstance(b, dict) and b.get("book")]
    return {"p_over": l.p_over, "fetched_ts": snapshots.fetched_ts(l), "source": snapshots.leg_source(l),
            "venue": q.venue if q else None, "line": l.line, "books": sorted(books) if q and q.venue == "books" else None,
            "method": detail.get("method")}


def jit_event(l: scanner.Leg, before: dict, after: dict, refreshed: bool, watch: dict | None,
              cycle_ts: float | None, at: float) -> dict:
    side = l.pick[0] if l.pick else None
    return {"calc_version": VERSIONS["prob_delta"], "cycle_ts": cycle_ts, "ts": at, "sport": l.sport,
            "player": l.player, "stat": l.stat, "line": l.line, "side": side, "source": after.get("source"),
            "start_ts": l.start_ts, "refreshed": refreshed, "watchlist": watch_field(l, watch),
            "before": before, "after": after}


def _side(side: str, p_over: float) -> float:
    return p_over if side == "More" else 1 - p_over


def prob_delta(ev: dict, prev_db: dict | None) -> dict:
    """prob_delta = current fair prob - the LAST DISTINCT earlier price of the same leg (same player, stat,
    line, source, start), on the side taken. The last distinct price is the pre-refresh price when it differs
    from the new one, else the newest earlier logged price row (`prev_db`, before the current fetch) that
    differs. Changes under snapshots.CHANGE are never logged, so moves that small aren't observable."""
    before, after, side = ev["before"], ev["after"], ev["side"]
    out = {"calc_version": VERSIONS["prob_delta"]}
    if not ev["refreshed"]:
        return {**out, "prob_delta": val(None, "not_refreshed")}
    if after.get("p_over") is None or side is None:
        return {**out, "prob_delta": val(None, "no_current_price")}
    if after.get("fetched_ts") is not None and after.get("fetched_ts") == before.get("fetched_ts"):
        return {**out, "prob_delta": val(None, "no_new_fetch")}
    prev = None
    if before.get("p_over") is not None and abs(before["p_over"] - after["p_over"]) > 1e-9:
        prev = {**before, "from": "pre_refresh_quote"}
    elif prev_db is not None:
        prev = {**prev_db, "from": "price_log"}
    if prev is None:
        return {**out, "prob_delta": val(None, "no_previous_distinct")}
    cur_p, prev_p = _side(side, after["p_over"]), _side(side, prev["p_over"])
    d = round(cur_p - prev_p, 6)
    pb, cb = prev.get("books"), after.get("books")
    if after.get("venue") != "books":
        comp = {"composition_changed": val(None, "single_source_venue"), "books_added": val(None, "single_source_venue"),
                "books_removed": val(None, "single_source_venue")}
    elif pb is None or cb is None:
        comp = {k: val(None, "no_book_list") for k in ("composition_changed", "books_added", "books_removed")}
    else:
        comp = {"composition_changed": val(set(pb) != set(cb)), "books_added": val(sorted(set(cb) - set(pb))),
                "books_removed": val(sorted(set(pb) - set(cb)))}
    return {**out, "prob_delta": val(d), "prev_from": prev["from"],
            "prev_p": val(round(prev_p, 6)), "cur_p": val(round(cur_p, 6)),
            "prev_ts": val(prev.get("fetched_ts")) if prev.get("fetched_ts") else val(None, "no_fetched_ts"),
            "cur_ts": val(after.get("fetched_ts")) if after.get("fetched_ts") else val(None, "no_fetched_ts"),
            "prev_n_books": val(len(pb)) if pb is not None else val(None, "no_book_list"),
            "cur_n_books": val(len(cb)) if cb is not None else val(None, "no_book_list"),
            **comp, "prev_line": val(prev.get("line")), "cur_line": val(after.get("line")),
            "direction": val("toward" if d > 1e-9 else "against" if d < -1e-9 else "none"),
            "prev_method": prev.get("method"), "cur_method": after.get("method")}


# --------------------------------------------------------------------------- Build 3: line gap (coverage only)
def book_main_line(outcomes: list[dict], player: str) -> dict | None:
    """One book's two-sided main line for a player: the point with both sides quoted (the one nearest 50/50
    if there are several). None when the player has no two-sided point in this market."""
    want = set(scanner.norm(player).split())
    pts: dict[float, dict] = {}
    for o in outcomes:
        if o.get("point") is not None and set(scanner.norm(o.get("description", "")).split()) == want:
            pts.setdefault(float(o["point"]), {})[str(o.get("name", "")).lower()] = float(o["price"])
    two = {pt: x for pt, x in pts.items() if "over" in x and "under" in x}
    if not two:
        return None
    imp = lambda x: (1 / x["over"]) / (1 / x["over"] + 1 / x["under"])
    pt = min(two, key=lambda p: (abs(imp(two[p]) - 0.5), p))
    return {"line": pt, "over": two[pt]["over"], "under": two[pt]["under"]}


def line_gap(l: scanner.Leg, cached: dict | None, sport: str, event_id: str, market: str) -> dict:
    """Build 3 (experimental, coverage only, NOT a middle): PrizePicks line vs the books' consensus main line
    (median of each book's main line), for a leg the books left unpriced, from the market ALREADY in the
    cache: reading it costs nothing, and nothing is requested for it."""
    rec = {"calc_version": VERSIONS["line_gap"], "sport": sport, "player": l.player, "stat": l.stat,
           "pp_line": val(l.line), "start_ts": l.start_ts, "event_id": event_id, "market": market}
    if cached is None:
        return {**rec, "pp_book_line_gap_units": val(None, "market_not_cached"),
                "consensus_line": val(None, "market_not_cached"), "both_sides_available": val(None, "market_not_cached")}
    per_book = {b: m for b, o in (cached.get("books") or {}).items() if (m := book_main_line(o, l.player))}
    if not per_book:
        return {**rec, "fetched_ts": cached.get("ts"), "pp_book_line_gap_units": val(None, "player_not_in_main_market"),
                "consensus_line": val(None, "player_not_in_main_market"),
                "both_sides_available": val(None, "player_not_in_main_market")}
    cons = statistics.median(m["line"] for m in per_book.values())
    return {**rec, "fetched_ts": cached.get("ts"),
            "books": {BOOK_SHORT.get(b, b): m for b, m in sorted(per_book.items())},
            "consensus_line": val(cons), "pp_book_line_gap_units": val(round(abs(l.line - cons), 4)),
            # PrizePicks: the parser keeps only two-way standard lines (More-only lines are dropped);
            # books: per_book holds only points quoted on both sides. Two placeable sides at two different
            # lines are NOT a middle: that needs both sides placeable with overlapping winning outcomes.
            "both_sides_available": val(True), "both_sides_basis": "pp two-way standard line; book two-sided main line"}


# --------------------------------------------------------------------------- Build 4: shadow promo legs
def shadow_legs(rows: list[dict], sport: str, now: float) -> list[scanner.Leg]:
    """Fresh Leg objects for the board's demon/goblin rows (parse_board.promo_rows), pre-game only. They are
    priced from Kalshi ONLY and logged as kind "shadow_promo"; never added to legs, good, eligible or the pool."""
    out = []
    for r in rows or []:
        l = scanner.Leg(r["player"], r["stat"], float(r["line"]), r["game"], team=r["team"], sport=sport,
                        odds_type=r["odds_type"], start_time=r["start_time"], position=r.get("position", ""))
        l.shadow = True
        out.append(l)
    return scanner.pregame(out, now)[0]


# --------------------------------------------------------------------------- harness statistics
def bootstrap_ci(xs: list[float], n_boot: int = 2000, seed: int = 20261104) -> tuple[float, float] | None:
    if len(xs) < 2:
        return None
    rng = random.Random(seed)  # fixed seed: the same rows give the same interval
    means = sorted(statistics.fmean(rng.choices(xs, k=len(xs))) for _ in range(n_boot))
    return means[int(0.025 * n_boot)], means[int(0.975 * n_boot) - 1]


def signal_cell(recs: list[dict]) -> str:
    """n distinct legs, distinct games and players, mean / median CLV, positive share and a 95% bootstrap CI.
    recs: distinct legs with a usable (ok) close."""
    if not recs:
        return "n=0"
    c = [r["clv"] for r in recs]
    games = len({(r.get("game"), r.get("start")) for r in recs})
    players = len({r.get("player") for r in recs})
    ci = bootstrap_ci(c)
    cis = "n/a" if ci is None else f"[{ci[0] * 100:+.2f}, {ci[1] * 100:+.2f}]"
    return (f"n={len(c):<4} games {games:<3} players {players:<3} mean {statistics.fmean(c) * 100:+6.2f} "
            f"median {statistics.median(c) * 100:+6.2f} pts  pos {sum(x > 1e-9 for x in c) / len(c):4.0%}  "
            f"95% CI {cis}" + ("  (insufficient data)" if len(c) < MIN_N else ""))


def quality_line(recs: list[dict]) -> str:
    """Closing-data quality of a slice (distinct legs): eligible (event started), then each exclusion."""
    started = [r for r in recs if r.get("close_reason") != "open"]
    why: dict[str, int] = {}
    for r in started:
        why[r["close_reason"]] = why.get(r["close_reason"], 0) + 1
    ex = {k: v for k, v in why.items() if k != "ok"}
    return (f"closing data: eligible {len(started)}, usable {why.get('ok', 0)}, missing {why.get('missing', 0)}, "
            f"stale {why.get('stale', 0)}, same fetch as entry {why.get('same_fetch', 0)}, market mismatch "
            f"{why.get('mismatch', 0)}, excluded {sum(ex.values())}"
            + (" (" + ", ".join(f"{k} {v}" for k, v in sorted(ex.items())) + ")" if ex else "")
            + f", open {sum(1 for r in recs if r.get('close_reason') == 'open')}")


def iso(ts: float | None) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds") if ts else ""
