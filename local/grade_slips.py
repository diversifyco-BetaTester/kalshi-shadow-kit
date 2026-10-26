"""
grade_slips.py — settle alerted slips in local/slips_history.csv from ESPN box scores.

A slip is graded once every leg started at least SETTLE_AFTER_H (4 h) ago. For each leg it finds
the ESPN game (scoreboard for the leg's US-Eastern date, matched on team and start time within 3 h),
reads the player's line from the box score summary (public, no key), and calls it:
  hit / miss       the stat landed on the picked side of the line
  push             landed exactly on it (whole-number lines; the sentinel doesn't alert those)
  void             didn't play (absent from the box score), or the game was postponed/canceled
  review           can't be decided automatically: the player wasn't found where they should be,
                   an MLB last name is ambiguous in ESPN's "2B: Name (n)" notes, or the stat isn't
                   mapped. The slip waits for a human (edit the row) instead of guessing.
  pending          game not final yet; tried again on the next run

Payout: a push or void removes the leg, as PrizePicks does, and the slip pays as the smaller entry:
scanner.PAYOUTS["flex"][n] for 3-6 legs (5: 10x/2x/0.4x, 6: 25x/2x/0.4x), a 2-pick Power (3x)
for 2, and a refund for 0-1. net_units = stake × (multiplier − 1).

  python3 -m local.grade_slips             # grade what's settled, then print the summary
  python3 -m local.grade_slips --summary   # summary only, no network: record, net units, ROI, hit rate by sport
  await grade_slips.grade_pending_slips()  # the same grading, from async code
  python3 -m local.grade_slips --log local/grader.log   # also append a timestamped run to the log
  python3 -m local.grade_slips --digest    # and send the last 24 h's results to Telegram

The digest goes to TELEGRAM_ALERT_CHAT_ID (never the public TELEGRAM_CHAT_ID feed); with no chat id
set it is only printed.

Daily at 6:00 AM: local/grade_daily.plist is a launchd job running the --log form (install steps in it).

Settlement alerts (--notify): every slip graded is marked settle_alert "due"; a --notify run sends one
Telegram card per due slip (🎉 win / ⚠️ loss / ↔️ refund, hits, units, then today's and all-time record)
and marks it "sent <time>", so a slip is announced exactly once. More than BULK_MAX due at once (a bulk
regrade) become ONE combined message. A failed send stays "failed <time>" and is retried for
RETRY_H hours. Slips graded before this existed have no "due" mark and are never announced. A --digest
run lists the slips it just graded, so those are marked "in digest" instead of sent twice.
local/grade_settle.plist runs `--log local/grader.log --notify` every 30 min, so a slip is announced
within ~30 min of settling (all legs started 4+ h ago and final); quiet runs aren't logged.

  python3 -m local.grade_slips --notify       # grade, then announce what just settled
  python3 -m local.grade_slips --test-alert   # send one sample settlement card (ledger untouched)
"""
from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import re
import statistics
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from local import ledger
from local import prize_picks_ev_scraper as scanner
from ops import telegram

SETTLE_AFTER_H = 4
MATCH_WINDOW_H = 3
ESPN = "https://site.api.espn.com/apis/site/v2/sports/{}"
PATHS = {"mlb": "baseball/mlb", "nhl": "hockey/nhl", "nfl": "football/nfl", "nba": "basketball/nba",
         "cfb": "football/college-football"}
TIMEOUT_S = 20
SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}
VOID_STATUSES = {"STATUS_POSTPONED", "STATUS_CANCELED", "STATUS_CANCELLED", "STATUS_SUSPENDED", "STATUS_FORFEIT"}

# Team codes that mean the same club: the CSV holds Kalshi's (via parse_board), ESPN has its own.
ALIASES = [{"CWS", "CHW"}, {"AZ", "ARI"}, {"ATH", "OAK"}, {"KC", "KCR"}, {"SD", "SDP"}, {"SF", "SFG"},
           {"TB", "TBR", "TBL"}, {"WSH", "WSN", "WAS"}, {"JAC", "JAX"}, {"LA", "LAR", "LAK"}, {"VGK", "VEG"},
           {"NJ", "NJD"}, {"SJ", "SJS"}, {"UTA", "UTAH"}, {"MTL", "MON"}, {"CBJ", "CLB"}, {"NSH", "NAS"},
           {"GS", "GSW"}, {"NY", "NYK"}, {"NO", "NOP"}, {"SA", "SAS"}, {"PHX", "PHO"}, {"BKN", "BRK"},
           {"CHA", "CHO"}]


def canon(code: str) -> str:
    code = (code or "").upper()
    return next((min(g) for g in ALIASES if code in g), code)


def name_key(name: str) -> tuple[str, ...]:
    return tuple(t for t in scanner.norm(name).split() if t not in SUFFIXES)


# --------------------------------------------------------------------------- ESPN
def get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "kalshi-shadow-kit grade_slips", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        return json.loads(r.read())


def num(v: str) -> float:
    if ":" in v:  # time on ice "18:42" -> minutes
        m, s = v.split(":")
        return int(m) + int(s) / 60
    return float(v)


def innings_to_outs(ip: str) -> float:
    whole, _, part = ip.partition(".")
    return int(whole) * 3 + int(part or 0)


class Box:
    """One game's player stats: {name key: {"team": canon code, stat key: value}} plus MLB notes."""

    def __init__(self, summary: dict):
        self.players: dict[tuple, dict] = {}
        self.notes: dict[str, dict[str, list[tuple[str, int]]]] = {}  # team -> stat -> [(name text, n)]
        bs = summary.get("boxscore", {})
        for team in bs.get("players", []):
            code = canon(team["team"]["abbreviation"])
            for grp in team.get("statistics", []):
                keys = grp.get("keys") or grp.get("names") or []
                for a in grp.get("athletes", []):
                    if a.get("didNotPlay") or not a.get("stats"):
                        continue  # NBA lists DNPs with empty stats: absent = void, not a zero
                    p = self.players.setdefault(name_key(a["athlete"]["displayName"]), {"team": code, "groups": set()})
                    p["groups"].add(grp.get("type") or grp.get("name") or "")
                    for k, v in zip(keys, a.get("stats", [])):
                        self._put(p, grp.get("type") or grp.get("name") or "", k, v)
        for t in bs.get("teams", []):
            code = canon(t["team"]["abbreviation"])
            for det in t.get("details", []):
                for s in det.get("stats", []):
                    self.notes.setdefault(code, {})[s["name"]] = self._parse_note(s.get("displayValue", ""))

    @staticmethod
    def _put(p: dict, group: str, key: str, v: str) -> None:
        """Store "12/19" under both halves of "completions/passingAttempts", "2-5" under "hits-atBats"."""
        prefix = "p." if group == "pitching" else ""  # MLB pitching reuses batting keys (hits, walks...)
        for sep in ("/", "-"):
            if sep in key and isinstance(v, str) and sep in v and key != "fullInnings.partInnings":
                for k, x in zip(key.split(sep), v.split(sep)):
                    Box._put(p, group, k, x)
                return
        if key == "fullInnings.partInnings":
            p["p.outs"] = innings_to_outs(v)
            return
        try:
            p[prefix + key] = num(v)
        except (TypeError, ValueError):
            pass

    @staticmethod
    def _parse_note(text: str) -> list[tuple[str, int]]:
        """'Adell (1, Kelly); Hamilton 2 (2, ...)' -> [('Adell', 1), ('Hamilton', 2)]. The number in
        parentheses is the season total; a game count only appears when it is above 1."""
        out = []
        for part in text.split(";"):
            m = re.match(r"\s*(?P<name>[^\d(]+?)(?:\s+(?P<n>\d+))?\s*\(", part)
            if m:
                out.append((m["name"].strip(), int(m["n"] or 1)))
        return out

    def find(self, player: str, team: str) -> dict | None:
        k = name_key(player)
        p = self.players.get(k)
        if p and p["team"] == team:
            return p
        cands = [v for n, v in self.players.items() if v["team"] == team and (set(k) <= set(n) or set(n) <= set(k))]
        return cands[0] if len(cands) == 1 else None

    def note_count(self, player: str, team: str, stat: str) -> int | None:
        """Game count for `player` in an MLB note (doubles/triples/stolenBases); 0 if not listed;
        None if the last name is ambiguous on the team."""
        k = name_key(player)
        last, first = k[-1], k[0]
        mates = [n for n, v in self.players.items() if v["team"] == team and n and n[-1] == last]
        for text, n in self.notes.get(team, {}).get(stat, []):
            nk = name_key(text.replace(",", " "))
            if nk and nk[0] == last and (len(nk) == 1 or first.startswith(nk[1])):
                if len(nk) == 1 and len(mates) > 1:
                    return None
                return n
        return 0


def value(sport: str, stat: str, p: dict, box: Box, player: str, team: str) -> float | None:
    """The leg's stat from a box-score entry; None when it can't be computed."""
    g = lambda k: p.get(k, 0.0)
    if sport == "nhl":
        simple = {"goals": "goals", "assists": "assists", "shots on goal": "shotsTotal", "blocked shots": "blockedShots",
                  "hits": "hits", "faceoffs won": "faceoffsWon", "time on ice": "timeOnIce", "saves": "saves"}
        if stat == "points":
            return g("goals") + g("assists")
        if stat == "power play points":
            return None
        return g(simple[stat]) if stat in simple else None
    if sport in ("nfl", "cfb"):  # same box-score groups
        simple = {"passing yards": "passingYards", "passing touchdowns": "passingTouchdowns",
                  "passing attempts": "passingAttempts", "passing completions": "completions",
                  "rushing yards": "rushingYards", "rushing attempts": "rushingAttempts",
                  "receiving yards": "receivingYards", "receptions": "receptions",
                  "receiving targets": "receivingTargets"}
        if stat == "touchdowns":
            return g("rushingTouchdowns") + g("receivingTouchdowns")
        return g(simple[stat]) if stat in simple else None
    if sport == "nba":
        parts = {"points": ["points"], "rebounds": ["rebounds"], "assists": ["assists"],
                 "threes": ["threePointFieldGoalsMade"], "blocks": ["blocks"], "steals": ["steals"],
                 "turnovers": ["turnovers"], "points + rebounds + assists": ["points", "rebounds", "assists"],
                 "points + rebounds": ["points", "rebounds"], "points + assists": ["points", "assists"],
                 "rebounds + assists": ["rebounds", "assists"], "blocks + steals": ["blocks", "steals"]}
        return sum(g(k) for k in parts[stat]) if stat in parts else None
    if sport == "mlb":
        pitching = {"strikeouts": "p.strikeouts", "hits allowed": "p.hits", "walks allowed": "p.walks",
                    "earned runs": "p.earnedRuns", "pitching outs": "p.outs"}
        batting = {"hits": "hits", "home runs": "homeRuns", "rbis": "RBIs", "walks": "walks", "runs": "runs",
                   "hitter strikeouts": "strikeouts"}
        if stat in pitching:
            return p.get(pitching[stat])
        if stat in batting:
            return g(batting[stat])
        if stat == "hits + runs + rbis":
            return g("hits") + g("runs") + g("RBIs")
        if stat in ("total bases", "singles", "stolen bases"):
            counts = [box.note_count(player, team, s) for s in ("doubles", "triples", "stolenBases")]
            if None in counts:
                return None
            d, t, sb = counts
            if stat == "stolen bases":
                return sb
            hr = g("homeRuns")
            return g("hits") + d + 2 * t + 3 * hr if stat == "total bases" else g("hits") - d - t - hr
    return None


def needs_group(sport: str, stat: str) -> str | None:
    """The box-score group a player must appear in for the leg to count as played; None if any."""
    if sport == "mlb":
        return "pitching" if stat in ("strikeouts", "hits allowed", "walks allowed", "earned runs", "pitching outs") \
            else "batting"
    if sport == "nhl":
        return "goalies" if stat == "saves" else None
    return None


# --------------------------------------------------------------------------- grading
class Grader:
    def __init__(self, get=get_json):
        self.get = get
        self.boards: dict[tuple, list] = {}
        self.boxes: dict[tuple, Box] = {}

    def game(self, sport: str, team: str, start_ts: float) -> dict | None:
        day = datetime.fromtimestamp(start_ts, scanner.EASTERN).strftime("%Y%m%d")
        if (sport, day) not in self.boards:
            self.boards[(sport, day)] = self.get(ESPN.format(PATHS[sport]) + f"/scoreboard?dates={day}").get("events", [])
        for e in self.boards[(sport, day)]:
            teams = {canon(c["team"]["abbreviation"]) for c in e["competitions"][0]["competitors"]}
            when = datetime.fromisoformat(e["date"].replace("Z", "+00:00")).timestamp()
            if canon(team) in teams and abs(when - start_ts) <= MATCH_WINDOW_H * 3600:
                return e
        return None

    def box(self, sport: str, event_id: str) -> Box:
        if (sport, event_id) not in self.boxes:
            self.boxes[(sport, event_id)] = Box(self.get(ESPN.format(PATHS[sport]) + f"/summary?event={event_id}"))
        return self.boxes[(sport, event_id)]

    def leg(self, leg: dict) -> dict:
        """{"result": hit|miss|push|void|review|pending, "actual": value or None, "note": why}."""
        sport, start = leg.get("sport", ""), leg.get("start_time", "")
        out = lambda result, actual=None, note="": {"result": result, "actual": actual, "note": note}
        if sport not in PATHS or not start:
            return out("review", note=f"no {sport.upper()} grader yet (settle by hand)" if sport else "no sport or start time")
        start_ts = datetime.fromisoformat(start.replace("Z", "+00:00")).timestamp()
        e = self.game(sport, leg["team"], start_ts)
        if not e:
            return out("review", note=f"no ESPN game for {leg['team']} near {start}")
        status = e["status"]["type"]
        if status["name"] in VOID_STATUSES:
            return out("void", note=status["name"])
        if not status.get("completed"):
            return out("pending", note=status["name"])
        box = self.box(sport, e["id"])
        team = canon(leg["team"])
        p = box.find(leg["player"], team)
        group = needs_group(sport, leg["stat"])
        if p is None or (group and group not in p["groups"]):
            if sport in ("nfl", "cfb"):  # football box scores omit players with no stats: can't tell DNP from zero
                return out("review", note="not in the box score")
            return out("void", note="did not play")
        v = value(sport, leg["stat"], p, box, leg["player"], team)
        if v is None:
            return out("review", note=f"can't compute {leg['stat']}")
        line = float(leg["line"])
        if abs(v - line) < 1e-9:
            return out("push", v)
        over = v > line
        return out("hit" if over == (leg["side"] == "More") else "miss", v)


def payout(results: list[str]) -> float:
    """PrizePicks Flex with pushes/voids removed (see module docstring)."""
    live = [r for r in results if r in ("hit", "miss")]
    n, hits = len(live), live.count("hit")
    if n >= 3:
        return scanner.PAYOUTS["flex"].get(n, {}).get(hits, 0.0)
    if n == 2:
        return scanner.PAYOUTS["power"][2][2] if hits == 2 else 0.0
    return 1.0


def grade(path: Path = ledger.LEDGER, now: float | None = None, grader: Grader | None = None) -> list[dict]:
    """Grade every settled, ungraded slip in place. Returns the rows graded this run."""
    now = time.time() if now is None else now
    if not path.exists():
        return []
    grader = grader or Grader()
    done = []
    with ledger.locked(path):
        rows = ledger.read(path)
        for r in rows:
            if r.get("status") == "graded":
                continue
            starts = [datetime.fromisoformat(l["start_time"].replace("Z", "+00:00")).timestamp()
                      for l in r["legs"] if l.get("start_time")]
            if len(starts) < len(r["legs"]) or max(starts) > now - SETTLE_AFTER_H * 3600:
                if len(starts) < len(r["legs"]):
                    r["status"] = "review"
                continue
            try:
                results = [grader.leg(l) for l in r["legs"]]
            except OSError as e:  # ESPN down or slow: leave it for the next run
                r["status"] = "pending"
                print(f"  {r['timestamp']} {r['slip_type']}: ESPN unavailable ({e!r})")
                continue
            r["leg_results"] = [{**l, **x} for l, x in zip(r["legs"], results)]
            kinds = [x["result"] for x in results]
            r["hits"], r["misses"] = kinds.count("hit"), kinds.count("miss")
            r["pushes"] = kinds.count("push") + kinds.count("void")
            if "pending" in kinds or "review" in kinds:
                r["status"] = "review" if "review" in kinds else "pending"
                continue
            mult = payout(kinds)
            stake = float(r["stake_units"] or 0)
            r.update(status="graded", payout_multiplier=mult, net_units=round(stake * (mult - 1), 3),
                     graded_at=ledger.iso(now), settle_alert="due")
            done.append(r)
        ledger.rewrite(rows, path)
    return done


LABEL = {"hit": "WON", "miss": "LOST", "push": "PUSH", "void": "VOID", "review": "REVIEW", "pending": "PENDING"}


async def grade_pending_slips(path: Path = ledger.LEDGER, now: float | None = None,
                              grader: Grader | None = None) -> list[dict]:
    """Async entry point: grade every settled, ungraded slip (see grade). The ESPN calls are blocking
    HTTP, so they run in a worker thread and don't stall an event loop such as a sentinel's."""
    return await asyncio.to_thread(grade, path, now, grader)


def sport_hit_rates(legs: list[dict]) -> str:
    """'MLB 3/4 (75%) · NHL 2/2 (100%) · NFL — · NBA — · CFB —' over distinct settled legs."""
    parts = []
    for sp in ("mlb", "nhl", "nfl", "nba", "cfb"):
        g = [x for x in legs if x.get("sport") == sp]
        hits = sum(x["result"] == "hit" for x in g)
        parts.append(f"{sp.upper()} {hits}/{len(g)} ({hits / len(g):.0%})" if g else f"{sp.upper()} —")
    return " · ".join(parts)


def stats(path: Path = ledger.LEDGER) -> dict:
    """All-time numbers behind summary() and summary_card()."""
    rows = ledger.read(path)
    graded = [r for r in rows if r.get("status") == "graded"]
    waiting = {s: sum(1 for r in rows if (r.get("status") or "open") == s) for s in ("open", "pending", "review")}
    mult = [float(r["payout_multiplier"]) for r in graded]
    legs = unique_legs(graded)
    by_sport = {sp: (sum(x["result"] == "hit" for x in g), len(g))
                for sp in ("mlb", "nhl", "nfl", "nba", "cfb") if (g := [x for x in legs if x.get("sport") == sp])}
    by_type: dict[str, list[float]] = {}
    for r in graded:
        by_type.setdefault(r["slip_type"], []).append(float(r["net_units"]))
    staked = sum(float(r["stake_units"]) for r in graded)
    net = sum(float(r["net_units"]) for r in graded)
    return {"logged": len(rows), "graded": len(graded), "wins": sum(m > 1 for m in mult),
            "losses": sum(m < 1 for m in mult), "pushes": sum(m == 1 for m in mult), "net": net, "staked": staked,
            "roi": net / staked if staked else None, "legs": len(legs),
            "hit_rate": sum(x["result"] == "hit" for x in legs) / len(legs) if legs else None,
            "expected": sum(float(x["probability"]) for x in legs) / len(legs) if legs else None,
            "by_sport": by_sport, "by_type": by_type, "waiting": waiting, "legs_list": legs}


def summary(path: Path = ledger.LEDGER) -> str:
    st = stats(path)
    w = st["waiting"]
    pending_line = f"Not graded yet: {w['open']} open, {w['pending']} pending, {w['review']} need review"
    if not st["graded"]:
        return f"No graded slips yet ({st['logged']} logged). {pending_line}"
    lines = [f"Record: {st['wins']}-{st['losses']}-{st['pushes']} (W = paid more than the stake, L = less, P = refund)",
             f"Win rate: {st['wins'] / st['graded']:.1%} of {st['graded']} graded slips",
             f"Net: {st['net']:+.2f}u on {st['staked']:.1f}u staked (ROI {st['roi']:+.1%})" if st["staked"]
             else f"Net: {st['net']:+.2f}u",
             f"Legs: {st['hit_rate']:.1%} hit vs {st['expected']:.1%} predicted ({st['legs']} distinct legs)",
             f"Hit rate by sport: {sport_hit_rates(st['legs_list'])}",
             pending_line]
    lines += [f"  {k}: {len(v)} slips, {sum(v):+.2f}u" for k, v in sorted(st["by_type"].items())]
    return "\n".join(lines)


def summary_card(path: Path = ledger.LEDGER) -> str:
    """The all-time summary as a short Telegram (HTML) card for a phone: one fact per line."""
    st = stats(path)
    w = st["waiting"]
    waiting = f"⏳ {w['open']} open · {w['pending']} pending · {w['review']} need review"
    if not st["graded"]:
        return f"📒 <b>P&amp;L</b>\nNo graded slips yet ({st['logged']} logged)\n{waiting}"
    icon = "🟢" if st["net"] > 0 else "🔴" if st["net"] < 0 else "⚪"
    lines = ["📒 <b>P&amp;L, all time</b>",
             f"{icon} <b>{st['net']:+.2f}u</b> on {st['staked']:.1f}u · ROI <b>{st['roi']:+.1%}</b>" if st["staked"]
             else f"{icon} <b>{st['net']:+.2f}u</b>",
             f"🧾 Record <b>{st['wins']}-{st['losses']}-{st['pushes']}</b> ({st['graded']} slip{'s' * (st['graded'] != 1)}, "
             f"{st['wins'] / st['graded']:.0%} won)",
             f"🎯 Legs {st['hit_rate']:.0%} hit vs {st['expected']:.0%} predicted ({st['legs']})",
             "<b>By sport</b>"]
    for sp, (hits, n) in st["by_sport"].items():
        lines.append(f"  {sp.upper()} {hits}/{n} · {hits / n:.0%}")
    if st["by_type"]:
        lines.append("<b>By slip</b>")
        lines += [f"  {k}: {len(v)} · {sum(v):+.2f}u" for k, v in sorted(st["by_type"].items())]
    lines.append(waiting)
    return "\n".join(lines)


SPORTS_ORDER = ("mlb", "nhl", "nfl", "nba")


def unique_legs(rows: list[dict]) -> list[dict]:
    """Settled hit/miss legs, each counted once even when it sat in several slips."""
    seen = {}
    for r in rows:
        for x in r["leg_results"]:
            if x["result"] in ("hit", "miss"):
                seen.setdefault((x.get("sport"), x["player"], x["stat"], x["line"], x["side"], x.get("start_time")), x)
    return list(seen.values())
DIGEST_SLIPS = 8  # one line each; the rest are counted


def record(rows: list[dict]) -> tuple[int, int, int, float, float]:
    """(wins, losses, pushes, net units, units staked); W = paid more than the stake, P = refund."""
    mult = [float(r["payout_multiplier"]) for r in rows]
    w, l = sum(m > 1 for m in mult), sum(m < 1 for m in mult)
    return (w, l, len(rows) - w - l, sum(float(r["net_units"]) for r in rows),
            sum(float(r["stake_units"]) for r in rows))


def roi(net: float, staked: float) -> str:
    return f"{net / staked:+.1%}" if staked else "n/a"


def digest(path: Path = ledger.LEDGER, now: float | None = None, hours: float = 24) -> str:
    """Telegram (HTML) summary of the slips graded in the last `hours`, plus all-time totals."""
    now = time.time() if now is None else now
    rows = ledger.read(path)
    graded = [r for r in rows if r.get("status") == "graded"]
    recent = [r for r in graded if r.get("graded_at")
              and datetime.fromisoformat(r["graded_at"]).timestamp() >= now - hours * 3600]
    day = datetime.fromtimestamp(now - 12 * 3600).strftime("%a %b %-d")
    lines = [f"☀️ <b>Slip results: {day}</b>"]
    if recent:
        w, l, p, net, staked = record(recent)
        lines += [f"Slips graded: <b>{len(recent)}</b> · Record <b>{w}-{l}-{p}</b>",
                  f"Net: <b>{net:+.2f}u</b> on {staked:.1f}u · ROI <b>{roi(net, staked)}</b>"]
        legs = unique_legs(recent)
        lines.append("Hit rate: " + sport_hit_rates(legs))
        if legs:
            lines.append(f"Legs: {sum(x['result'] == 'hit' for x in legs) / len(legs):.0%} hit vs "
                         f"{statistics.fmean(float(x['probability']) for x in legs):.0%} predicted")
        for r in recent[:DIGEST_SLIPS]:
            n = len(r["legs"])
            lines.append(f"• {html.escape(r['sports'])} {r['slip_type']}: {r['hits']}/{n} → "
                         f"{float(r['payout_multiplier']):g}x, {float(r['net_units']):+.2f}u")
        if len(recent) > DIGEST_SLIPS:
            lines.append(f"  …and {len(recent) - DIGEST_SLIPS} more")
    else:
        lines.append(f"No slips settled in the last {hours:g} h.")
    if graded:
        w, l, p, net, staked = record(graded)
        lines.append(f"All-time: {w}-{l}-{p} · {net:+.2f}u · ROI {roi(net, staked)} ({len(graded)} slips)")
    waiting = [r.get("status") for r in rows]
    if waiting.count("pending") or waiting.count("review"):
        lines.append(f"Waiting: {waiting.count('pending')} pending, {waiting.count('review')} need review "
                     f"(edit local/slips_history.csv)")
    return "\n".join(lines)


# --------------------------------------------------------------------------- settlement alerts
BULK_MAX = 3   # more slips than this settling in one run -> one combined message
RETRY_H = 24   # a failed settlement alert is retried this long after grading
MARK = {"hit": "✅", "miss": "❌", "push": "➖", "void": "➖"}


def headline(r: dict) -> str:
    """'🎉 Slip Settled: 6-Pick Flex Hit! (6/6) | +4.80u' / '⚠️ ... Loss (4/6) | -0.30u' / '↔️ ... Refund'.
    A push or void shrinks the slip as on PrizePicks: '6-Pick Flex → 5 (4/5)'."""
    n = len(r["legs"])
    live = int(r["hits"]) + int(r["misses"])
    net = float(r["net_units"])
    size = r["slip_type"] if live == n else f"{r['slip_type']} → {live}"
    if net > 0:
        icon, word = "🎉", "Hit!" if int(r["misses"]) == 0 else "Win"
    elif net < 0:
        icon, word = "⚠️", "Loss"
    else:
        icon, word = "↔️", "Refund"
    return (f"{icon} <b>Slip Settled: {html.escape(r['sports'])} {html.escape(size)} {word}</b> "
            f"({r['hits']}/{live}) | <b>{net:+.2f}u</b>")


def day_line(rows: list[dict], now: float) -> str:
    """Today's settled slips (graded since local midnight) and all-time, from the ledger."""
    today = datetime.fromtimestamp(now).astimezone().date()
    graded = [r for r in rows if r.get("status") == "graded"]
    todays = [r for r in graded if r.get("graded_at")
              and datetime.fromisoformat(r["graded_at"]).astimezone().date() == today]
    w, l, p, net, staked = record(todays)
    out = f"📅 Today: <b>{w}-{l}-{p}</b> · <b>{net:+.2f}u</b> on {staked:.1f}u · ROI <b>{roi(net, staked)}</b>"
    if graded:
        w, l, p, net, staked = record(graded)
        out += "\n" + f"📒 All-time: {w}-{l}-{p} · {net:+.2f}u · ROI {roi(net, staked)}"
    return out


def settle_card(r: dict, rows: list[dict], now: float) -> str:
    legs = " · ".join(f"{MARK.get(x['result'], '?')} {html.escape(x['player'].split()[-1])}" for x in r["leg_results"])
    return "\n".join([headline(r), legs, day_line(rows, now)])


def bulk_card(due: list[dict], rows: list[dict], now: float) -> str:
    w, l, p, net, staked = record(due)
    lines = [f"🧾 <b>{len(due)} slips settled</b> · {w}-{l}-{p} · <b>{net:+.2f}u</b>"]
    lines += [f"• {headline(r)}" for r in due[:DIGEST_SLIPS]]
    if len(due) > DIGEST_SLIPS:
        lines.append(f"  …and {len(due) - DIGEST_SLIPS} more")
    lines.append(day_line(rows, now))
    return "\n".join(lines)


def is_due(r: dict, now: float) -> bool:
    mark = r.get("settle_alert") or ""
    if r.get("status") != "graded":
        return False
    if mark == "due":
        return True
    if mark.startswith("failed") and r.get("graded_at"):
        return now - datetime.fromisoformat(r["graded_at"]).timestamp() < RETRY_H * 3600
    return False


def alert_chat() -> str:
    """TELEGRAM_ALERT_CHAT_ID only: never the public TELEGRAM_CHAT_ID feed (telegram.send falls back to it)."""
    load_dotenv(".env")
    return os.getenv("TELEGRAM_ALERT_CHAT_ID", "")


def notify_settled(path: Path = ledger.LEDGER, now: float | None = None, send=None, chat: str | None = None,
                   in_digest: bool = False) -> list[str]:
    """Announce every due slip once (see the module docstring). `send(text, chat_id=...)` is injectable.
    Returns what happened, for the log. Holds the ledger lock, so a concurrent grade can't double-send."""
    now = time.time() if now is None else now
    send = send or telegram.send
    chat = alert_chat() if chat is None else chat
    if not path.exists():
        return []
    with ledger.locked(path):
        rows = ledger.read(path)
        due = [r for r in rows if is_due(r, now)]
        if not due:
            return []
        if in_digest:
            for r in due:
                r["settle_alert"] = "in digest"
            ledger.rewrite(rows, path)
            return [f"{len(due)} settled slip(s) reported in the digest"]
        if not chat:
            return [f"{len(due)} settlement alert(s) not sent: no TELEGRAM_ALERT_CHAT_ID"]
        batches = [(due, bulk_card(due, rows, now))] if len(due) > BULK_MAX else [([r], settle_card(r, rows, now)) for r in due]
        out = []
        for slips, text in batches:
            try:
                ok = send(text, chat_id=chat)
            except Exception as e:  # a Telegram outage must not lose the grading
                ok, err = False, e
            stamp = f"{'sent' if ok else 'failed'} {ledger.iso(now)}"
            for r in slips:
                r["settle_alert"] = stamp
            out.append(f"settlement alert {'sent' if ok else 'FAILED'}: {len(slips)} slip(s)")
        ledger.rewrite(rows, path)
        return out


def test_alert_card(now: float | None = None) -> str:
    """A sample settlement card (fake slip, clearly marked), for checking the Telegram path."""
    now = time.time() if now is None else now
    legs = [{"player": p, "result": res} for p, res in (("Sample Player One", "hit"), ("Sample Player Two", "hit"),
                                                        ("Sample Player Three", "hit"), ("Sample Player Four", "hit"),
                                                        ("Sample Player Five", "push"), ("Sample Player Six", "hit"))]
    r = {"sports": "NFL", "slip_type": "6-Pick Flex", "legs": legs, "leg_results": legs, "hits": 5, "misses": 0,
         "net_units": 9.0, "payout_multiplier": 10.0, "stake_units": 1.0}
    return "🧪 <b>TEST</b> (sample slip, not in the ledger)\n" + "\n".join(
        [headline(r), " · ".join(f"{MARK[x['result']]} {x['player'].split()[-1]}" for x in legs),
         "📅 Today: <b>1-0-0</b> · <b>+9.00u</b> on 1.0u · ROI <b>+900.0%</b>"])


def send_digest(text: str) -> str:
    """Send to TELEGRAM_ALERT_CHAT_ID. Returns what happened, for the log."""
    load_dotenv(".env")
    chat = os.getenv("TELEGRAM_ALERT_CHAT_ID", "")
    if not chat:
        return "digest not sent: no TELEGRAM_ALERT_CHAT_ID"
    return "digest sent to Telegram" if telegram.send(text, chat_id=chat) else "digest send FAILED"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--summary", action="store_true", help="print the all-time summary without grading")
    ap.add_argument("--ledger", type=Path, default=ledger.LEDGER)
    ap.add_argument("--log", type=Path, help="also append this run, timestamped, to a log file")
    ap.add_argument("--digest", action="store_true", help="send the last 24 h's results to Telegram")
    ap.add_argument("--notify", action="store_true",
                    help="announce each slip settled since the last announcement on Telegram, once (quiet runs aren't logged)")
    ap.add_argument("--test-alert", action="store_true", help="send one sample settlement card and exit (ledger untouched)")
    a = ap.parse_args()
    if a.test_alert:
        chat = alert_chat()
        if not chat:
            print("not sent: set TELEGRAM_ALERT_CHAT_ID in .env")
            print(test_alert_card())
        else:
            print("test settlement alert " + ("sent" if telegram.send(test_alert_card(), chat_id=chat) else "FAILED"))
        return
    out = []
    graded: list = []
    if not a.summary:
        try:
            graded = grade(a.ledger)
            out.append(f"{len(graded)} slip(s) graded this run" if graded else "nothing new to grade")
            for r in graded:
                res = " ".join(f"{x['player'].split()[-1]}:{LABEL.get(x['result'], x['result'])}" for x in r["leg_results"])
                out.append(f"  {r['timestamp']} {r['sports']} {r['slip_type']}: {r['hits']}/{len(r['legs'])} hit, "
                           f"{r['payout_multiplier']}x, {r['net_units']:+.2f}u | {res}")
        except Exception as e:  # unattended run: say what broke in the log rather than vanish
            out.append(f"grading failed: {e!r}")
    if a.notify or a.digest:
        try:
            out += notify_settled(a.ledger, in_digest=a.digest)
        except Exception as e:
            out.append(f"settlement alerts failed: {e!r}")
    if a.notify and not a.digest and out == ["nothing new to grade"]:
        return  # a quiet 30-minute run: nothing graded, nothing announced, nothing failed: nothing logged
    out.append(summary(a.ledger))
    if a.digest:
        try:
            out.append(send_digest(digest(a.ledger)))
        except Exception as e:  # a Telegram outage must not lose the grading log
            out.append(f"digest failed: {e!r}")
    text = "\n".join(x for x in out if x)
    print(text)
    if a.log:
        a.log.parent.mkdir(parents=True, exist_ok=True)
        with open(a.log, "a") as f:
            f.write(f"=== {datetime.now().astimezone():%Y-%m-%d %H:%M %Z} ===\n{text}\n\n")


if __name__ == "__main__":
    main()
