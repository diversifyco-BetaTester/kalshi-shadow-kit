"""
sentinel.py — keep re-pricing your PrizePicks board against Kalshi and push a Telegram alert
when the board makes a +EV slip.

The board comes from you, never fetched from PrizePicks. Primary feed: click the bookmarklet
(scripts/prizepicks_scraper.js) on the board page; it downloads board.json, saved as
~/Downloads/<sport>_board.json (the default --feed; --feed '~/Downloads/board*.json' takes the
newest download instead, Chrome naming repeats "board (1).json"). Pasted text
in local/board_<sport>.txt still works; whichever file is newest wins. Each sport has its own
cache, local/lines_<sport>.csv, so one sentinel per sport can run side by side.
Every --interval seconds the sentinel:
  1. re-parses the newest board file if it changed,
  2. drops demon/goblin legs and any game that has started or starts within 10 minutes
     (live Kalshi rungs sit at 1c/99c and read as a fake 99% edge; pasted text has no start
     time, so only board.json legs can be checked),
  3. pulls Kalshi's live books for that sport and prices every standard leg; a leg Kalshi can't
     price gets a DraftKings/FanDuel de-vigged consensus from local/fetch_odds.py when
     ODDS_API_KEY is set (budgeted: see that file),
  4. builds the best slip(s) from legs >= --min-ev and alerts each one with a Kelly stake,
  5. leaves out of new slips any leg alerted in the last --leg-cooldown-min (120) minutes, by any
     sentinel (shared through local/ev_pool.json; in memory with --no-pool), so a slip is rebuilt
     around other legs instead of re-sending the same picks; and stays quiet about a slip that
     shares 4+ legs with one alerted in the last --dedupe-min minutes unless its EV is 5+ points higher.

Cross-sport (Flex mode): every sentinel publishes its legs >= --min-ev to local/ev_pool.json
(local/ev_pool.py) and reads the other sports' live legs back. Besides its own sport's best
5/6-pick slips it looks for mixed slips: at least one leg from each side, every leg from a different
game. The "already sent" list lives in the same file, so a mixed slip both sentinels find goes out
once. --no-pool keeps a sentinel to its own sport.

  python3 -m local.sentinel                                     # leave running; click the bookmarklet
  python3 -m local.sentinel --sport mlb                         # (pbpaste > local/board_mlb.txt also works)
  python3 -m local.sentinel --sport nfl --interval 30 --min-ev 0.62
  python3 -m local.sentinel --sport nhl --flex                   # 5/6-pick Flex clusters, legs >= 54.5%
  python3 -m local.sentinel --sport nfl --flex                   # reads ~/Downloads/nfl_board.json, pools with NHL/MLB

Flex mode: 5- and 6-pick Flex break even near 54.2% a leg (with PAYOUTS["flex"] in the scanner),
far below 2-pick Power's 57.7%. With --flex the threshold drops to 54.5%, and the sentinel alerts
only on +EV 5- or 6-pick Flex slips PrizePicks allows. At 54.5% a leg the edge is ~2-3% EV per $1,
smaller than the error in a Kalshi mid, so treat these alerts as thin.

Stake: quarter Kelly (--kelly) on the slip's whole payout table, as a fraction of bankroll, shown
in units of --unit-pct of bankroll (1u = 1% by default), capped at 2.0u and rounded DOWN to 0.1u:
never more than the Kelly fraction says. A slip whose Kelly stake is under 0.5u is skipped (not
sent) by default; pipeline_config staking.below_min "stake" sends it at the computed amount instead.
Kelly trusts the probabilities completely; Kalshi mids are estimates, which is why it is quartered and capped.

Stale board: past --max-board-age (20) minutes the sentinel stops pricing and alerting, since
PrizePicks moves lines (and flips them to goblin/demon) and an old board's line can be gone from the
app. Once the board file is older than --stale-min (15) minutes, a console line says so, with how many legs have dropped because
their games started; it repeats every --stale-min minutes while the board stays stale, and stops
once a new board arrives.

Telegram gets only new +EV slips from this process (the morning digest comes from
local/grade_slips.py). Board-age notices, budget notes and errors stay on stdout.

Injury guard (local/injury_guard.py): every leg >= --min-ev is checked against ESPN's injury
reports and, for NHL saves, the probable starting goalie, before any slip is built or the leg is
shared with the pool. OUT/IR/IL/suspended players are dropped; Questionable/Doubtful/Day-To-Day
players can't be "More"; a goalie must be his team's confirmed or expected starter. Each veto is
logged once ("[Vetoed: ...]"); alerted legs show "Status: ..." in their research line.
--no-injury-guard turns it off.

Research: before a slip is sent, each leg gets a one-line ESPN note (local/enricher.py: last-5
average, last-5 record over the line, games vs tonight's opponent), fetched for all legs at once and
capped at ~10 s; a failure shows "No recent data (...)" and never blocks the alert. --no-research
turns it off.

Every alerted slip is appended to local/slips_history.csv (local/ledger.py), with whether Telegram
took it, so results can be graded later.

Alerts go to TELEGRAM_ALERT_CHAT_ID (set it in .env, or pass --chat-id). It deliberately does NOT
fall back to TELEGRAM_CHAT_ID, which may be your public shadow feed. With neither set, alerts
print to the terminal only.
"""
from __future__ import annotations

import argparse
import asyncio
import glob
import html
import itertools
import json
import sqlite3
import math
import os
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from local import correlation, enricher, ev_pool, fetch_odds, injury_guard, integrity, ledger, optimizer, pipeline_config, snapshots
from local import stat_research
from local import steam as steam_mod
from local import prize_picks_ev_scraper as scanner
from local.parse_board import SPORTS, board_path, lines_path, load_board, write_csv
from ops.telegram import send_async

STALE_AFTER_MIN = 30  # PrizePicks moves lines; say so when the pasted board is older than this
STALE_NUDGE_MIN = 15  # print a "click the bookmarklet" notice once the board is this old
WAIT_NOTE_MIN = 10  # repeat the "waiting for initial board" note this often, not every cycle
MAX_BOARD_AGE_MIN = 20  # past this, stop pricing and alerting: PrizePicks flips lines within ~20 min
# (2026-10-16: an alerted 1.5 receptions was a demon 22 min later); ops/auto_refresh.py refreshes every 7
FLEX_SIZES = (5, 6)
FLEX_MIN_EV = 0.545  # just above the ~54.2% per-leg break-even of 5/6-pick Flex
FLEX_TOP_LEGS = 12  # C(12, 6) = 924 combos; every qualifying leg would explode on a full board
LEG_COOLDOWN_MIN = 120  # a leg (sport, player, stat, line, side) alerted this recently stays out of new slips
SAME_LEGS = 4  # a slip sharing this many legs (or all of a shorter slip) with a recent alert is a repeat...
EV_GAIN = 0.05  # ...unless its EV per $1 is at least this much higher (5 percentage points)
_STAKE = pipeline_config.CONFIG["staking"]
KELLY = _STAKE["kelly"]
UNIT_PCT = _STAKE["unit_pct"]
MIN_UNITS, MAX_UNITS = _STAKE["min_units"], _STAKE["max_units"]
BELOW_MIN = _STAKE["below_min"]  # "skip" | "stake"
RULE = "───────────────"


def newest_feed(feed_glob: str, board: str) -> Path | None:
    """The most recently written board file: a bookmarklet download or the pasted text file."""
    files = [Path(f) for f in glob.glob(os.path.expanduser(feed_glob))] + [Path(board)]
    files = [f for f in files if f.exists()]
    return max(files, key=lambda f: f.stat().st_mtime, default=None)


def refresh_csv(feed: Path | None, out: Path, sport: str, seen: tuple, on_board=None) -> tuple:
    """Re-parse the feed into the CSV when a newer board arrives. Returns (path, mtime) used.
    The CSV is stamped with the board's mtime, so board age (board_age_min) always describes the lines
    in use: a newer file without this sport's props (bookmarklet clicked on another league's tab)
    leaves both the old lines and their age in place."""
    if feed is None or (feed, feed.stat().st_mtime) == seen:
        return seen
    stamp, now_seen = time.strftime("%H:%M:%S"), (feed, feed.stat().st_mtime)
    try:
        rows, bad = load_board(feed, sport)
    except ValueError as e:  # a half-written download, or not JSON after all
        print(f"{stamp} could not read {feed}: {e}")
        return now_seen
    if not rows:
        print(f"{stamp} {feed} has 0 {sport.upper()} props; keeping {out}")
        return now_seen
    write_csv(rows, out)
    os.utime(out, (now_seen[1], now_seen[1]))
    if on_board:  # snapshot logging, the whole-number counter
        on_board(feed, now_seen[1], rows, bad)
    print(f"{stamp} new board {feed.name}: {len(rows)} props -> {out}" + (f" ({len(bad)} skipped)" if bad else ""))
    return now_seen


leg_key = ev_pool.leg_key


STACK_PAYOUT_FACTOR = 1.0  # what PrizePicks pays a correlated entry vs. the table; set by --stack-payout-factor


def entry_ev(combo, kind: str, stacks: list, factor: float | None = None) -> float | None:
    """Net EV per $1 of an entry. With any push-priced whole line: the exact (wins, pushes) distribution
    and PrizePicks' downgrade (a push removes the leg: a 6-pick pays the 5-pick table, ... 2 legs ->
    Power, 1 -> refund; optimizer.slip_stats). None for a stack holding a whole line (a correlated push
    isn't modeled). Otherwise the correlation model as before."""
    if any(l.p_push for l in combo):
        if stacks:
            return None
        return optimizer.slip_stats([optimizer.leg_prob(l) for l in combo], kind).net
    return correlation.slip_ev(combo, scanner.PAYOUTS[kind][len(combo)], stacks,
                               STACK_PAYOUT_FACTOR if factor is None else factor)


def flex_entries(good: list[scanner.Leg], accept=None, top: list[scanner.Leg] | None = None,
                 payout_factor: float | None = None) -> list[tuple[float, str, tuple[scanner.Leg, ...], list]]:
    """The best allowed +EV 5- and 6-pick Flex entries, one per size, best EV first:
    (ev, "flex", legs, stacks). Searches only the FLEX_TOP_LEGS likeliest legs (or `top`), where the
    best slip almost always lives; `accept` is an extra rule a combo must pass. Same-game legs follow
    local/correlation.py: a recognized stack (priced with its correlation and the stack payout factor)
    or nothing; a stacked slip that isn't +EV after the factor loses to the best independent one."""
    factor = STACK_PAYOUT_FACTOR if payout_factor is None else payout_factor
    if top is None:
        top = sorted((l for l in good if not l.promo and not l.push_unpriced), key=lambda l: -l.pick[1])[:FLEX_TOP_LEGS]
    best = {}
    for n in FLEX_SIZES:
        table = scanner.PAYOUTS["flex"][n]
        for combo in itertools.combinations(top, n):
            if not scanner.allowed(combo) or (accept and not accept(combo)):
                continue
            ok, stacks, _ = correlation.check(combo)
            if not ok:
                continue
            ev = entry_ev(combo, "flex", stacks, factor)
            if ev is not None and ev > 0 and ev > best.get(n, (0,))[0]:
                best[n] = (ev, "flex", combo, stacks)
    return sorted(best.values(), key=lambda e: -e[0])


game_id = correlation.game_id


def mixed_entries(own: list[scanner.Leg], others: list[scanner.Leg], sport: str) -> list[tuple]:
    """Best 5/6-pick Flex slips mixing this sport's legs with other sports' pooled legs: at least
    one leg from each side, no player twice, same-game legs only as a correlation.py stack."""
    if not own or not others:
        return []
    by_p = lambda ls: sorted(ls, key=lambda l: -l.pick[1])
    half = FLEX_TOP_LEGS // 2
    o, x = by_p(own), by_p(others)
    top = o[:max(half, FLEX_TOP_LEGS - len(x))] + x[:max(half, FLEX_TOP_LEGS - len(o))]

    def accept(combo) -> bool:
        sports = {l.sport for l in combo}
        return sport in sports and len(sports) > 1 and len({(l.sport, l.player) for l in combo}) == len(combo)
    return flex_entries([], accept, top[:FLEX_TOP_LEGS])


KALSHI_LIQUID_SPREAD = 0.05  # widest Kalshi bid/ask still trusted over the sportsbook consensus


def kalshi_liquid(l: scanner.Leg) -> bool:
    """Kalshi prices this leg from a real two-sided market (not interpolated between rungs) with a
    spread of at most KALSHI_LIQUID_SPREAD. Otherwise the multi-book consensus is used when it has one."""
    return any(q.venue == "kalshi" and not q.interpolated and q.spread <= KALSHI_LIQUID_SPREAD for q in l.quotes)


def source_tag(l: scanner.Leg) -> str:
    """Where a leg's price came from, for the alert, the pool and the ledger: 'Consensus: PIN, DK, FD'
    (· alt / · interp off the alternate ladder), 'Kalshi', 'Kalshi ~interp.', or a pooled leg's tag."""
    books = fetch_odds.sources(l)
    if books:
        return books
    pooled = next((q.text for q in l.quotes if q.venue == "pool"), None)
    if pooled is not None:
        return pooled
    if not l.quotes:
        return ""
    return "Kalshi ~interp." if all(q.interpolated for q in l.quotes) else "Kalshi"


def kelly_fraction(ps: list[float], table: dict[int, float], dist: list[float] | None = None) -> float:
    """Full-Kelly fraction of bankroll for an entry paying table[k]x on k hits (0 otherwise):
    the f maximizing E[log(1 - f + f*payout)]. 0 when the entry isn't +EV. `dist` (P(k hits)) replaces
    the independent-legs distribution, e.g. for a stacked slip."""
    dist = dist or scanner.hit_distribution(ps)
    pay = [table.get(k, 0.0) for k in range(len(dist))]
    if sum(d * m for d, m in zip(dist, pay)) <= 1:
        return 0.0
    g = lambda f: sum(d * math.log(1 - f + f * m) for d, m in zip(dist, pay) if d > 0)
    lo, hi = 0.0, 0.999  # f = 1 bets everything, and a 0x outcome makes log(0)
    for _ in range(100):  # g is concave: ternary search
        a, b = lo + (hi - lo) / 3, hi - (hi - lo) / 3
        lo, hi = (a, hi) if g(a) < g(b) else (lo, b)
    return (lo + hi) / 2


def stake_units(entry: tuple, kelly: float = KELLY, unit_pct: float = UNIT_PCT, below_min: str | None = None) -> float:
    """Quarter-Kelly stake in units, never above the Kelly amount: capped at MAX_UNITS, rounded down to
    0.1u. Under MIN_UNITS: 0.0 (skip the slip) or, with below_min "stake", the computed amount."""
    ev, kind, combo, stacks = entry
    table = scanner.PAYOUTS[kind][len(combo)]
    if stacks:  # correlated hits, and the payout PrizePicks pays a correlated entry
        table = {k: m * STACK_PAYOUT_FACTOR for k, m in table.items()}
    if any(l.p_push for l in combo):  # pushes downgrade the slip: Kelly over the exact (wins, pushes) outcomes
        full = optimizer.slip_stats([optimizer.leg_prob(l) for l in combo], kind).kelly_f
    else:
        dist = correlation.hit_distribution(combo, stacks) if stacks else None
        full = kelly_fraction([l.pick[1] for l in combo], table, dist)
    units = kelly * full / unit_pct
    if units < MIN_UNITS and (below_min or BELOW_MIN) == "skip":
        return 0.0
    return math.floor(min(units, MAX_UNITS) * 10 + 1e-9) / 10


def repeat_keys(keys: frozenset, ev: float, sent: list[tuple[float, frozenset, float]], now: float,
                window_s: float) -> bool:
    need = min(SAME_LEGS, len(keys))
    return any(now - t < window_s and len(keys & k) >= need and ev < e + EV_GAIN - 1e-9 for t, k, e in sent)


def repeat_of(entry: tuple, sent: list[tuple[float, frozenset, float]], now: float, window_s: float) -> bool:
    """True when a slip alerted in the last window_s shares SAME_LEGS legs (or all of a shorter
    slip) and this one's EV isn't at least EV_GAIN better. Leg keys include the sport, so mixed
    slips dedupe the same way."""
    return repeat_keys(frozenset(leg_key(l) for l in entry[2]), entry[0], sent, now, window_s)


def opponent(l: scanner.Leg) -> str:
    teams = [t.strip() for t in l.game.replace("@", " v ").split(" v ") if t.strip()]
    other = [t for t in teams if t != l.team]
    return other[0] if len(other) == 1 else "?"


def clock(ts: float, now: float) -> str:
    t = datetime.fromtimestamp(ts)
    hm = t.strftime("%I:%M %p").lstrip("0")
    return hm if t.date() == datetime.fromtimestamp(now).date() else f"{t:%a} {hm}"


def nudge_text(sport: str, age_min: float, started: int, left: int) -> str:
    """Console only (plain text, no Telegram HTML)."""
    return (f"{time.strftime('%H:%M:%S')} {sport.upper()} board is {age_min:.0f} min old: click the bookmarklet "
            f"for a fresh one. {started} legs dropped (game started or starts within "
            f"{scanner.START_BUFFER_MIN} min), {left} still pre-game.")


def should_nudge(state: dict, feed: Path | None, age_min: float, now: float, every_min: float) -> bool:
    """True at most once per every_min while the same board file stays older than every_min."""
    if feed is None or age_min < every_min:
        return False
    key = (str(feed), feed.stat().st_mtime)
    if state.get("board") == key and now - state.get("at", 0) < every_min * 60:
        return False
    state.update(board=key, at=now)
    return True


BOARD_ORDER = ("nfl", "mlb", "cfb", "nba", "nhl", "soccer")  # PrizePicks' league tabs, left to right
SPORT_ICON = {"nfl": "🏈", "cfb": "🏈", "nba": "🏀", "nhl": "🏒", "mlb": "⚾", "soccer": "⚽"}


def badge(status: str | None) -> str:
    """Injury-guard reason -> a short badge: 'Active / Confirmed Starter' -> '✅ Confirmed Starter'."""
    if not status:
        return ""
    if status.startswith("Active"):
        rest = status.partition(" / ")[2]
        return f"✅ {rest}" if rest else "✅ No injury"
    if status.startswith(("no ", "status unavailable")):
        return f"• {status.split(' (')[0]}"
    return f"⚠️ {status}"  # e.g. "Q (Ankle): Less only"


def payout_note(kind: str, n: int, stacked: bool) -> str:
    table = scanner.PAYOUTS.get(kind, {}).get(n)
    if not table:
        return f"{n}-Pick {kind.title()}"
    pays = " · ".join(f"{m:g}x ({k}/{n})" for k, m in sorted(table.items(), reverse=True))
    shade = f" · stack payout ×{STACK_PAYOUT_FACTOR:g}" if stacked and STACK_PAYOUT_FACTOR != 1 else ""
    return f"{n}-Pick {kind.title()} pays {pays}{shade}"


def alert_text(entry: tuple, sport: str, board_age_min: float, now: float, units: float,
               research: dict | None = None, status: dict | None = None) -> str:
    """The Telegram card, laid out for entering the slip by hand: stake at top and bottom; legs grouped
    by sport (in PrizePicks' tab order) and game time; each player's name in monospace (one tap copies it
    for the PrizePicks search); stat | line | MORE/LESS; the injury/starter badge and ESPN research;
    the win probability and where it came from; the payout table last.
    `research`: {id(leg): enricher note}; `status`: {id(leg): injury_guard reason}."""
    ev, kind, combo, stacks = entry
    sports = [s.upper() for s in BOARD_ORDER if any(l.sport == s for l in combo)] or [sport.upper()]
    stake = f"💵 <b>STAKE {units:.1f}u</b>"
    lines = [f"🎯 <b>{'+'.join(sports)} {len(combo)}-Pick {kind.title()}</b> · {stake}",
             f"📊 Net EV <b>{ev:+.1%}</b>"]
    lines += [f"<b>[STACK: {html.escape(st.label)} (+Correlated)]</b>" for st in stacks or []]
    order = lambda l: (BOARD_ORDER.index(l.sport) if l.sport in BOARD_ORDER else 9, l.start_ts or 0, l.game, l.player)
    game = None
    for l in sorted(combo, key=order):
        key = (l.sport, l.game, l.start_ts)
        if key != game:  # one header per game, in board order
            game = key
            when = clock(l.start_ts, now) if l.start_ts else "time unknown"
            lines += [RULE, f"{SPORT_ICON.get(l.sport, '•')} <b>{(l.sport or sport).upper()}</b> · {when} · "
                            f"{html.escape(l.team or '?')} vs {html.escape(opponent(l))}"]
        side = "MORE ⬆️" if l.pick[0] == "More" else "LESS ⬇️"
        lines.append(f"<code>{html.escape(l.player)}</code>")
        lines.append(f"   {html.escape(l.stat.title())} | {l.line:g} | <b>{side}</b>")
        notes = [badge((status or {}).get(id(l))), (research or {}).get(id(l), "")]
        notes = [html.escape(x) for x in notes if x]
        if notes:
            lines.append("   " + " · ".join(notes))
        tag = source_tag(l)
        push = f" · push {l.p_push:.0%}" if l.p_push else ""  # whole line: the % is P(win | no push)
        raw = l.raw_pick
        shaded = f" (market {raw[1]:.1%}, shaded)" if raw and raw[1] != l.pick[1] else ""
        lines.append(f"   📈 {l.pick[1]:.1%}{shaded}{push}" + (f" · {html.escape(tag)}" if tag else ""))
    lines += [RULE, stake + " · " + payout_note(kind, len(combo), bool(stacks))]
    if board_age_min > STALE_AFTER_MIN:
        lines.append(f"⚠ Board captured {board_age_min:.0f} min ago: check each line in the app.")
    return "\n".join(lines)


HEARTBEAT = "local/.heartbeat_{sport}.json"


def heartbeat(a, now: float, **stats) -> None:
    """Record that this sport's cycle ran (ops/telegram_listener.py /status reads it). Never raises."""
    path = Path(HEARTBEAT.format(sport=a.sport))
    try:
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"sport": a.sport, "ts": now, "pid": os.getpid(), **stats}))
        os.replace(tmp, path)
    except OSError:
        pass


def leg_floor(a, sport: str | None) -> float:
    """The fair probability a leg of `sport` needs to join a slip: pipeline_config min_fair_by_sport, never
    below --min-ev."""
    return max(a.min_ev, pipeline_config.CONFIG["min_fair_by_sport"].get((sport or a.sport).lower(), 0.0))


def clears_floor(a, l: scanner.Leg) -> bool:
    return bool(l.pick) and l.pick[1] >= leg_floor(a, l.sport or a.sport)


def background(a, coro) -> None:
    """Run `coro` alongside the polling loop (it gets time while the loop sleeps); errors are printed,
    never raised into a cycle. Tasks are kept on `a` so they aren't garbage-collected mid-run."""
    tasks = a.__dict__.setdefault("background_tasks", set())
    task = asyncio.ensure_future(coro)
    tasks.add(task)

    def done(t):
        tasks.discard(t)
        if not t.cancelled() and t.exception() is not None:
            print(f"{time.strftime('%H:%M:%S')} background task failed: {t.exception()!r}")
    task.add_done_callback(done)


def would_send(a, keys: frozenset, ev: float, sent: list, now: float, window: float, leg_window: float) -> bool:
    """The dedupe verdict for a slip, recording nothing (claiming happens after the JIT refresh)."""
    repeat = lambda k, e, s: repeat_keys(k, e, s, now, window)
    if a.pool:
        return a.pool.would_claim(keys, ev, now, window, repeat, leg_window)
    recent = [s for s in sent if now - s[0] < window]
    return not (keys & set(a.__dict__.get("sent_props_cache", {}))) and not repeat_keys(keys, ev, recent, now, window)


def release(a, keys: frozenset, sent: list, claimed_at: float) -> None:
    """Undo this sentinel's claim of a slip it couldn't deliver: the slip and its legs leave the "already
    sent" lists, so the legs aren't cooled down for an alert nobody received."""
    if a.pool:
        a.pool.release(keys, claimed_at)
        return
    sent[:] = [s for s in sent if not (s[0] == claimed_at and s[1] == keys)]
    cache = a.__dict__.get("sent_props_cache", {})
    for k in keys:
        if cache.get(k) == claimed_at:
            del cache[k]


def reevaluate(a, e: tuple) -> tuple[tuple | None, str]:
    """The same slip at its legs' current prices: (entry, "") or (None, why it no longer qualifies)."""
    ev0, kind, combo, _ = e
    low = [l for l in combo if not clears_floor(a, l)]
    if low:
        return None, f"{low[0].player} {low[0].stat} fell below {leg_floor(a, low[0].sport):.1%}"
    ok, stacks, _ = correlation.check(combo)
    if not ok:
        return None, "no longer an allowed slip"
    ev = entry_ev(combo, kind, stacks)
    if ev is None:  # a stack now holding a push-priced whole line: its correlated push isn't modeled
        return None, f"EV {ev0:+.1%} -> not computable (stacked whole line)"
    if ev <= 0:
        return None, f"EV {ev0:+.1%} -> {ev:+.1%}"
    new = (ev, kind, combo, stacks)
    if stake_units(new, a.kelly, a.unit_pct) <= 0:
        return None, f"Kelly stake under {MIN_UNITS:g}u at EV {ev:+.1%}"
    return new, ""


async def jit_refresh(a, e: tuple, touched: set) -> tuple[tuple | None, str]:
    """Just before a slip is claimed: re-price its books legs whose price is older than
    jit_refresh.max_age_s from a targeted refresh, then re-evaluate it. (entry or None, log note).
    `touched` collects legs re-priced this cycle, so a later slip sharing one is re-evaluated too."""
    cfg = pipeline_config.CONFIG["jit_refresh"]
    at = time.time()
    old_age = lambda l: (lambda age: age is None or age > cfg["max_age_s"])(snapshots.price_age(l, at))
    stale = [l for l in e[2] if snapshots.leg_source(l) == "books" and old_age(l)]
    if not stale:
        if any(id(l) in touched for l in e[2]):
            return reevaluate(a, e)
        return e, ""
    if not (cfg["enabled"] and a.odds):
        return (None, "JIT: stale books price, no odds refresh possible: skipped") if cfg["on_refused"] == "skip" else (e, "")
    credits, notes, copies = 0, [], {}
    for sport in sorted({l.sport or a.sport for l in stale}):
        if sport not in fetch_odds.SPORT_KEYS:
            continue
        targets = []
        for l in (l for l in stale if (l.sport or a.sport) == sport):
            if any(q.venue == "pool" for q in l.quotes):  # another sentinel's leg: price a clean copy
                c = scanner.Leg(l.player, l.stat, l.line, l.game, team=l.team, sport=sport, start_time=l.start_time,
                                position=l.position)
                copies[id(l)] = (l, c)
                targets.append(c)
            else:
                targets.append(l)
        got = await a.odds.reprice(sport, targets, at, cfg["max_age_s"])
        credits += got["credits"]
        notes += got["notes"]
    for l, c in copies.values():
        if any(q.venue == "books" for q in c.quotes):
            l.quotes, l.source_venue, l.fetched_ts = c.quotes, "books", None
    for l in stale:  # a fresh books price carries no Kalshi uncertainty: drop the old shade
        if not any(q.venue == "pool" for q in l.quotes):
            l.shade = integrity.shade(integrity.features(l, at))
    touched.update(id(l) for l in stale)
    still = [l for l in stale if old_age(l)]
    note = (f"JIT refresh: {len(stale)} stale books leg(s), {credits} credit(s)"
            + (f", {len(still)} still stale ({'; '.join(notes) or 'not re-priced'})" if still else ""))
    if still and cfg["on_refused"] == "skip":
        return None, note + ": slip skipped"
    new, why = reevaluate(a, e)
    return new, note + (f": dropped, {why}" if new is None else f": EV {e[0]:+.1%} -> {new[0]:+.1%}")


async def watch_legs(a, snaps, legs: list[scanner.Leg], quotes: list, now: float,
                     books_ok: bool = True) -> list[scanner.Leg]:
    """Watched legs (chosen ones, and eligible Kalshi ones: snapshots.open_legs) no longer on the board,
    re-priced from their entry source until their event starts: Kalshi from this cycle's quotes (free),
    books within the odds budget unless books_ok is false (no credits spent). Never alerted."""
    on_board = {(l.player, l.stat, l.line, l.quotes[0].venue) for l in legs if l.quotes}
    watch = []
    for o in snaps.open_legs(now):
        if (o["player"], o["stat"], o["line"], o["source"]) in on_board:
            continue  # already priced from its entry source this cycle
        l = scanner.Leg(o["player"], o["stat"], o["line"], o["game"], team=o["team"], sport=a.sport,
                        start_time=datetime.fromtimestamp(o["start_ts"], timezone.utc).isoformat())
        l.watch = True  # logged as kind 'watch': not on the board, never alerted
        watch.append((l, o["source"]))
    if not watch:
        return []
    scanner.attach([l for l, src in watch if src == "kalshi"], quotes)
    books = [l for l, src in watch if src == "books"]
    if books and books_ok and a.odds:
        await a.odds.price(a.sport, books, now)
    for l, src in watch:
        l.quotes = [q for q in l.quotes if q.venue == src]  # same source as the entry, or nothing
    return [l for l, _ in watch if l.quotes]


async def watch_only(a, now: float) -> None:
    """A cycle that prices and alerts nothing (the board is too old, or every board leg has started) still
    re-prices the watch list up to each start: otherwise every leg on it gets a stale close. Only when
    something is on the watch list, and from Kalshi only: no Odds API credit while nothing can be alerted."""
    snaps = getattr(a, "snapshots", None)
    if snaps is None or not a.flex:
        return
    try:
        if not snaps.open_legs(now):
            return
        quotes = await scanner.kalshi_quotes(SPORTS[a.sport]["series"])
        read_ts = time.time()
        for q in quotes:
            q.detail = {**(q.detail or {}), "fetched_ts": read_ts}
        watch = await watch_legs(a, snaps, [], quotes, now, books_ok=False)
        snaps.prices(None, now, watch, age_at=time.time())
        snaps.resolve_closes(now)
    except (sqlite3.Error, OSError, ValueError) as err:  # logging must never stop the loop
        print(f"{time.strftime('%H:%M:%S')} watch-only snapshot failed: {err!r}")


def waiting_for_board(a, now: float) -> bool:
    """True (after a console note, at most every WAIT_NOTE_MIN) while this sport has never had a board:
    no CSV yet because no feed file has landed. A fresh sport (cfb before its first download) waits
    quietly instead of failing every cycle."""
    if Path(a.csv).exists():
        a.waiting_since = None
        return False
    last = getattr(a, "waiting_note_at", None)
    if getattr(a, "waiting_since", None) is None:
        a.waiting_since = now
    if last is None or now - last >= WAIT_NOTE_MIN * 60:
        a.waiting_note_at = now
        mins = (now - a.waiting_since) / 60
        print(f"{time.strftime('%H:%M:%S')} waiting for initial {a.sport} board to land at {os.path.expanduser(a.feed)} "
              f"(or pasted into {a.board})" + (f": still waiting after {mins:.0f} min" if mins >= 1 else "..."), flush=True)
    return True


async def cycle(a, sent: list[tuple[float, frozenset, float]], chat_id: str) -> None:
    now = time.time()
    if waiting_for_board(a, now):
        heartbeat(a, now, state="waiting for first board")
        return
    legs = [l for l in scanner.load_legs(a.csv) if not l.promo]
    for l in legs:
        l.sport = a.sport
    if not legs:
        print(f"{time.strftime('%H:%M:%S')} {a.csv} has no standard legs; click the bookmarklet or paste into {a.board}")
        return
    all_legs = legs
    legs, started = scanner.pregame(legs, now)
    board = Path(a.csv)  # stamped with its board's mtime by refresh_csv
    board_age = (now - board.stat().st_mtime) / 60 if board.exists() else 0
    if should_nudge(a.nudge, board if board.exists() else None, board_age, now, a.stale_min):
        print(nudge_text(a.sport, board_age, started, len(legs)))  # stdout only: Telegram is for slips
    if not legs:
        await watch_only(a, now)  # nothing left to alert, but watched legs still need prices until their start
        return
    if a.max_board_age and board_age > a.max_board_age:
        # a line the app has since moved (28.5 on the board, 22.5 in the app) would alert as an edge
        print(f"{time.strftime('%H:%M:%S')} board is {board_age:.0f} min old (> {a.max_board_age:g}): "
              f"not pricing or alerting until a fresh board arrives")
        await watch_only(a, now)
        return
    quotes = await scanner.kalshi_quotes(SPORTS[a.sport]["series"])
    read_ts = time.time()  # the Kalshi read behind every Kalshi price this cycle (snapshots: price age)
    for q in quotes:
        q.detail = {**(q.detail or {}), "fetched_ts": read_ts}
    scanner.attach(legs, quotes)
    by_books = 0
    if a.odds:
        # Kalshi first when its book is liquid; otherwise (no market, wide spread, or only interpolated
        # between rungs) the multi-book consensus replaces it when the books have the line
        need = [l for l in legs if not kalshi_liquid(l)]
        by_books = await a.odds.price(a.sport, need, now)
        for l in need:
            if any(q.venue == "books" for q in l.quotes):
                l.quotes = [q for q in l.quotes if q.venue == "books"]
        for n in a.odds.notes:
            print(f"{time.strftime('%H:%M:%S')} {n}")
    shaded = integrity.apply_shading(legs)  # uncertain Kalshi prices toward 50%, before every floor and ranking
    tracker = getattr(a, "steam", None)
    if tracker is not None:  # a feature only: never changes p, EV, or which slips are sent
        board_ts = board.stat().st_mtime if board.exists() else 0
        if getattr(a, "steam_board_ts", None) != board_ts:
            tracker.board(board_ts, all_legs)
            a.steam_board_ts = board_ts
        tracker.update(legs, now)
    leg_window = getattr(a, "leg_cooldown_min", LEG_COOLDOWN_MIN) * 60
    if a.pool:
        cooled = a.pool.recent_legs(now, leg_window)
    else:
        cache = a.__dict__.setdefault("sent_props_cache", {})  # (sport, player, stat, line, side) -> sent at
        for k in [k for k, t in cache.items() if now - t >= leg_window]:
            del cache[k]
        cooled = set(cache)
    floor = leg_floor(a, a.sport)  # this sport's floor (pipeline_config min_fair_by_sport, never below --min-ev)
    whole = sum(1 for l in legs if l.push_unpriced and l.pick and l.pick[1] >= floor)
    whole_priced = sum(1 for l in legs if l.whole_line and l.quotes and not l.push_unpriced)
    # a whole line joins only with a market push price (Leg.p_push); without one, see Leg.whole_line
    good = [l for l in legs if l.pick and l.pick[1] >= floor and not l.push_unpriced]
    if getattr(a, "injury_guard", False) and good:  # before slips are built or legs are shared with the pool
        verdicts = await injury_guard.verify_legs(good)
        seen = a.__dict__.setdefault("veto_seen", {})
        for l in good:
            safe, reason = verdicts[id(l)]
            if not safe and seen.get(leg_key(l)) != reason:  # log a veto once, again only if its reason changes
                seen[leg_key(l)] = reason
                print(f"{time.strftime('%H:%M:%S')} [Vetoed: {l.player} {l.line:g} {l.stat} {l.pick[0]}: {reason}]")
        good = [l for l in good if verdicts[id(l)][0]]
    good, iflags = integrity.screen(good, time.time())  # shadow unless pipeline_config integrity.enforce
    flagged = sum(bool(f) for f in iflags.values())
    if a.sport == "mlb" and good and getattr(a, "injury_guard", False):  # lineup spots for the runs/RBI stack
        await injury_guard.annotate_lineups(good)
    priced = {l.stat for l in legs if l.quotes}  # stats with no price at all, Kalshi or books
    unpriced = Counter(l.stat for l in legs if l.stat not in priced)
    eligible = [l for l in good if leg_key(l) not in cooled]  # recently alerted legs: replaced, not re-sent
    entries = flex_entries(eligible) if a.flex else [e for e in scanner.rank_entries(eligible)[:1] if e[0] > 0]
    pooled = []
    if a.flex and a.pool:
        a.pool.publish(a.sport, [(l, source_tag(l)) for l in good], now)
        pooled = [l for l in a.pool.others(a.sport, now) if clears_floor(a, l) and leg_key(l) not in cooled]
        entries = sorted(entries + mixed_entries(eligible, pooled, a.sport), key=lambda e: -e[0])
    window = a.dedupe_min * 60
    fresh, skipped, touched, claimed = [], 0, set(), {}  # claimed: id(entry) -> its leg keys, to release
    for e in entries:  # best EV first, so a 5-leg subset of the 6-leg just sent counts as a repeat
        if stake_units(e, a.kelly, a.unit_pct) <= 0:  # Kelly stake under the minimum: not sent, legs not claimed
            skipped += 1
            continue
        if not would_send(a, frozenset(leg_key(l) for l in e[2]), e[0], sent, now, window, leg_window):
            continue  # a repeat: no refresh spent on it
        e, note = await jit_refresh(a, e, touched)  # stale books prices re-fetched before anything is claimed
        if note:
            print(f"{time.strftime('%H:%M:%S')} {note}")
        if e is None:
            continue
        keys = frozenset(leg_key(l) for l in e[2])  # a refreshed leg can change side
        if a.pool:  # shared with the other sentinels: a mixed slip goes out once
            new = a.pool.claim(keys, e[0], now, window, lambda k, ev, s: repeat_keys(k, ev, s, now, window),
                               leg_window)
        else:
            sent[:] = [s for s in sent if now - s[0] < window]
            new = not (keys & set(a.sent_props_cache)) and not repeat_keys(keys, e[0], sent, now, window)
            if new:
                sent.append((now, keys, e[0]))
                a.sent_props_cache.update(dict.fromkeys(keys, now))
        if new:
            fresh.append(e)
            claimed[id(e)] = keys
    whole_dropped = sum(1 for l in legs if l.push_unpriced)  # whole lines left out: no market push price
    heartbeat(a, now, state="ok", legs=len(legs), priced=sum(bool(l.quotes) for l in legs), good=len(good),
              slips=len(entries), new=len(fresh), board_age_min=round(board_age, 1), whole_dropped=whole_dropped)
    snaps = getattr(a, "snapshots", None)
    if snaps is not None and a.flex:
        try:
            paper = optimizer.build(eligible + pooled)  # paper mode: logged, never sent
            fresh_ids = {id(e) for e in fresh}
            coverage: dict[str, list[int]] = {}
            for l in legs:
                c = coverage.setdefault(l.stat, [0, 0])
                c[0] += 1
                c[1] += bool(l.quotes) and not l.push_unpriced  # a whole line counts once its push is priced
            built = time.time()  # price ages are measured here, when the slips were built and logged
            cid = snaps.cycle(now, board.stat().st_mtime if board.exists() else 0,
                              {"legs": len(legs), "priced": sum(bool(l.quotes) for l in legs), "good": len(good),
                               "whole_dropped": whole_dropped},
                              [snapshots.slip_summary(e, id(e) in fresh_ids, stake_units(e, a.kelly, a.unit_pct))
                               for e in entries[:10]],
                              snapshots.paper_summary(paper),
                              eligible=[snapshots.leg_ref(l, built) for l in eligible + pooled], coverage=coverage)
            chosen = []  # (slip id, legs) recorded this cycle: the stats feature is logged for these
            for e in fresh:  # CLV: the live builder's chosen (sent) slips
                sid = snaps.record_slip("live", cid, now, e[2], e[0], True, stake_units(e, a.kelly, a.unit_pct), age_at=built)
                chosen.append((sid, list(e[2])))
            if paper.chosen is not None:
                pe = (paper.chosen.stats.ev - 1, "flex", paper.chosen.legs, [])
                sid = snaps.record_slip("paper", cid, now, paper.chosen.legs, pe[0], False,
                                        stake_units(pe, a.kelly, a.unit_pct), age_at=built)
                if sid:
                    chosen.append((sid, list(paper.chosen.legs)))
            if chosen and getattr(a, "stat_feature", False) and pipeline_config.CONFIG["stat_feature"]["enabled"]:
                background(a, stat_research.log_gaps(snaps, chosen))  # never awaited by the alert path
            snaps.watch_eligible(eligible, now)  # Kalshi-priced eligible legs, chosen or not: watched to the start
            watch = await watch_legs(a, snaps, legs, quotes, now)
            snaps.prices(cid, now, legs + watch, age_at=time.time())
            snaps.resolve_closes(now)
        except (sqlite3.Error, OSError, ValueError) as err:  # logging must never stop the alerts
            print(f"{time.strftime('%H:%M:%S')} snapshot write failed: {err!r}")
    print(f"{time.strftime('%H:%M:%S')} {sum(bool(l.quotes) for l in legs)}/{len(legs)} standard legs priced, "
          f"{started} dropped (started or within {scanner.START_BUFFER_MIN} min), {len(good)} >= {floor:.1%}"
          + (f" (+{whole} whole-number lines left out: push risk)" if whole else "")
          + (f" (whole-number lines: {whole_priced} push-priced, {whole_dropped} without a push price)"
             if whole_priced or whole_dropped else "") + ", "
          f"{len(entries)} +EV slip(s) ({sum(len({l.sport for l in e[2]}) > 1 for e in entries)} cross-sport, "
          f"{len(pooled)} pooled legs from other sports), {len(fresh)} new"
          + (f", {skipped} skipped (Kelly stake under {MIN_UNITS:g}u)" if skipped else "")
          + (f", {shaded} Kalshi prices shaded toward 50%" if shaded else "")
          + (f", {flagged} with market-integrity flags ({'removed' if pipeline_config.CONFIG['integrity']['enforce'] else 'shadow: kept'})"
             if flagged else "")
          + (f", {sum(1 for l in good if leg_key(l) in cooled)} legs cooling down" if cooled else "")
          + (f" | {by_books} priced by sportsbooks" if a.odds else "")
          + (f" | unpriced (no Kalshi market or book line for that game day): " + ", ".join(f"{s} ({n})" for s, n in unpriced.most_common()) if unpriced else ""))
    for e in fresh:
        units = stake_units(e, a.kelly, a.unit_pct)
        try:
            research = await enricher.enrich_legs(list(e[2])) if a.research else None
            status = None
            if getattr(a, "injury_guard", False):  # cached feeds: re-checking the slip's legs here is free
                status = {k: reason for k, (_, reason) in (await injury_guard.verify_legs(list(e[2]))).items()}
            msg = alert_text(e, a.sport, board_age, now, units, research, status)
            print(msg)
            status = ("sent" if await send_async(msg, chat_id=chat_id) else "failed") if chat_id else "terminal"
        except Exception as err:  # noqa: BLE001 — one slip's alert failing must not strand its claimed legs
            print(f"{time.strftime('%H:%M:%S')} alert failed: {err!r}")
            status = "failed"
        if status == "failed":  # never reached you: free its legs for the next cycle instead of a 2 h cooldown
            release(a, claimed[id(e)], sent, now)
            print(f"{time.strftime('%H:%M:%S')} alert not delivered: {len(e[2])} legs released")
        try:
            ledger.append(ledger.row(e, units, now, status, source_tag), a.ledger)
        except OSError as err:  # a full disk must not stop the alerts
            print(f"{time.strftime('%H:%M:%S')} ledger write failed: {err!r}")


async def main() -> None:
    global STACK_PAYOUT_FACTOR
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sport", choices=sorted(SPORTS), default="nfl")
    ap.add_argument("--feed", help="the bookmarklet's download for this sport (default: ~/Downloads/<sport>_board.json; "
                                   "'~/Downloads/board*.json' takes the newest download of any name)")
    ap.add_argument("--board", help="text file you paste the board into (default: local/board_<sport>.txt)")
    ap.add_argument("--csv", help="this sport's line cache (default: local/lines_<sport>.csv)")
    ap.add_argument("--interval", type=float, default=60, help="seconds between Kalshi re-pricings")
    ap.add_argument("--min-ev", type=float, help=f"win probability that triggers an alert (default 0.60, "
                                                 f"or {FLEX_MIN_EV} with --flex)")
    ap.add_argument("--flex", action="store_true", help="Flex Play mode: alert on +EV 5/6-pick Flex clusters")
    ap.add_argument("--dedupe-min", type=float, default=30,
                    help=f"don't re-alert a slip sharing {SAME_LEGS}+ legs with one sent this many minutes ago "
                         f"(unless its EV is {EV_GAIN * 100:g}+ points higher)")  # no '%': argparse formats help
    ap.add_argument("--leg-cooldown-min", type=float, default=LEG_COOLDOWN_MIN,
                    help="minutes a leg stays out of new slips after it was alerted (shared by all sentinels)")
    ap.add_argument("--stack-payout-factor", type=float, default=STACK_PAYOUT_FACTOR,
                    help="multiplier PrizePicks applies to a correlated (stacked) entry's payout, e.g. 0.9; "
                         "1.0 = no shading seen")
    ap.add_argument("--kelly", type=float, default=KELLY, help="fraction of full Kelly to stake")
    ap.add_argument("--unit-pct", type=float, default=UNIT_PCT, help="1u as a fraction of bankroll")
    ap.add_argument("--stale-min", type=float, default=STALE_NUDGE_MIN,
                    help="print a stale-board notice (console only) when the board is older than this many minutes")
    ap.add_argument("--ledger", type=Path, default=ledger.LEDGER, help="CSV every alerted slip is appended to")
    ap.add_argument("--no-injury-guard", dest="injury_guard", action="store_false",
                    help="don't veto legs on ESPN injury status / NHL starting goalies")
    ap.add_argument("--no-research", dest="research", action="store_false",
                    help="don't add ESPN last-5 / head-to-head notes under each leg")
    ap.add_argument("--no-snapshots", dest="snapshot", action="store_false",
                    help="don't log boards, prices, closes and both builders' picks to data/snapshots_<sport>.sqlite3")
    ap.add_argument("--no-pool", action="store_true", help="don't share legs with other sports' sentinels")
    ap.add_argument("--no-stat-feature", dest="stat_feature", action="store_false",
                    help="don't log the stats-vs-market gap (ESPN game logs) on chosen legs (a feature, never a veto)")
    ap.add_argument("--max-board-age", type=float, default=MAX_BOARD_AGE_MIN,
                    help="minutes after which a board is too old to alert from (0 = no limit)")
    ap.add_argument("--odds-budget", type=int, default=fetch_odds.DAILY_CAP,
                    help="Odds API credits per day; a higher value raises the cap shared by every sentinel "
                         "for today (the reserve is always kept). The cap is split by sport with "
                         "pipeline_config odds_budget.shares (default NFL half, every other sport half)")
    ap.add_argument("--window-hours", type=float, default=fetch_odds.LOOKAHEAD_H,
                    help="only buy sportsbook odds for games starting within this many hours")
    ap.add_argument("--no-odds", action="store_true", help="don't price Kalshi-unpriced legs from The Odds API")
    ap.add_argument("--chat-id", help="Telegram chat for alerts (default: TELEGRAM_ALERT_CHAT_ID in .env)")
    ap.add_argument("--once", action="store_true", help="run one cycle and exit")
    a = ap.parse_args()
    a.min_ev = a.min_ev if a.min_ev is not None else FLEX_MIN_EV if a.flex else 0.60
    STACK_PAYOUT_FACTOR = a.stack_payout_factor
    a.feed = a.feed or f"~/Downloads/{a.sport}_board.json"
    a.csv = a.csv or str(lines_path(a.sport))
    a.board = a.board or str(board_path(a.sport))

    load_dotenv(".env")
    chat_id = a.chat_id or os.getenv("TELEGRAM_ALERT_CHAT_ID", "")
    a.odds = (None if a.no_odds or a.sport not in fetch_odds.SPORT_KEYS
              else fetch_odds.OddsBook.from_env(daily_cap=a.odds_budget, window_h=a.window_hours))
    if a.odds:
        a.odds.publish_cap()  # every sentinel sees this budget on its next pricing pass
        print(f"{time.strftime('%H:%M:%S')} {fetch_odds.budget_line(a.odds.usage(), a.odds.shares)}")
    a.nudge = {}
    a.pool = None if a.no_pool else ev_pool.EvPool()
    a.snapshots = snapshots.Snapshots(a.sport) if a.snapshot else None
    a.steam = steam_mod.SteamTracker()
    a.board_info = {}

    def on_board(feed, board_ts, rows, bad):
        a.board_info = {"board_ts": board_ts}  # whole lines now reach pricing; the cycle counts unpriced ones
        if a.snapshots is not None and not a.snapshots.has_board(board_ts):
            captured, props = snapshots.load_board_props(feed, a.sport)
            a.snapshots.board(board_ts, captured, props)
    a.on_board = on_board
    print(f"sentinel: {a.sport.upper()} | feed {a.feed} or {a.board} | every {a.interval:.0f}s | alert >= {leg_floor(a, a.sport):.1%}{' (flex 5/6)' if a.flex else ''}"
          + (" · floors " + ", ".join(f"{s.upper()} {leg_floor(a, s):.1%}" for s in sorted(pipeline_config.CONFIG["min_fair_by_sport"]))
             if pipeline_config.CONFIG["min_fair_by_sport"] else "") + " | "
          + (f"books fallback on (games within {a.odds.window_h:g} h, budget {a.odds.cap()}/day shared, "
             f"reserve {a.odds.reserve})" if a.odds
             else "no books fallback") + " | "
          + (f"Telegram chat {chat_id}" if chat_id else "no TELEGRAM_ALERT_CHAT_ID: terminal only"))
    sent: list[tuple[float, frozenset, float]] = []  # (alerted at, leg keys, EV) of recent slips
    a.seen = ()
    while True:
        try:
            a.seen = refresh_csv(newest_feed(a.feed, a.board), Path(a.csv), a.sport, a.seen, a.on_board)
            await cycle(a, sent, chat_id)
        except Exception as e:  # a network blip shouldn't kill an all-evening watch
            print(f"{time.strftime('%H:%M:%S')} cycle failed: {e!r}")
        if a.once:
            pending = a.__dict__.get("background_tasks") or set()
            if pending:  # let the stats feature finish logging before a one-shot run exits
                await asyncio.wait(pending, timeout=60)
            return
        await asyncio.sleep(a.interval)


if __name__ == "__main__":
    asyncio.run(main())
