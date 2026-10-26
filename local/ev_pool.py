"""
ev_pool.py — legs and sent slips shared between sentinels, so one sport's sentinel can build
cross-sport Flex slips from another sport's +EV legs (PrizePicks entries can mix leagues).

local/ev_pool.json (gitignored with local/):
  legs: one entry per qualifying standard leg, rewritten by its own sentinel every cycle, so a leg
        that falls below the threshold or loses its price disappears on the next cycle. An entry is
        used only while its game is more than START_BUFFER_MIN away (expires_ts) and its writer has
        refreshed it within STALE_S, so a stopped sentinel's legs stop counting within minutes.
        Legs without a start time (pasted text) are never shared.
  sent: recent alerts (time, leg keys, EV) from every sentinel. Checking and recording a slip is one
        locked step, so two sentinels finding the same mixed slip send it once.
  sent_legs: when each leg (sport, player, stat, line, side) was last alerted, by any sentinel. A leg
        stays out of new slips for the leg cooldown (120 min), so the same pick isn't re-sent in a
        slightly different slip every few minutes.
Every read-modify-write holds an flock on local/ev_pool.lock.
"""
from __future__ import annotations

import fcntl
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

from local import snapshots
from local import prize_picks_ev_scraper as scanner

POOL = Path("local/ev_pool.json")
STALE_S = 300  # a writer refreshes every --interval (60 s); 5 missed cycles and its legs are ignored


def leg_key(leg: scanner.Leg) -> tuple:
    return leg.sport, leg.player, leg.stat, leg.line, leg.pick[0]


def to_entry(leg: scanner.Leg, tag: str, now: float) -> dict:
    side, p = leg.raw_pick  # the market's price; the reader re-applies `shade` (scanner.Leg.pick)
    return {"sport": leg.sport, "player": leg.player, "stat": leg.stat, "line": leg.line, "game": leg.game,
            "team": leg.team, "start_time": leg.start_time, "side": side, "p": p, "tag": tag,
            "shade": leg.shade,  # its own sentinel's price-uncertainty shade (integrity.shade)
            "venue": leg.quotes[0].venue if leg.quotes else "",  # the sharp source, for same-source CLV closes
            "fetched_ts": snapshots.fetched_ts(leg),  # when that source's price was read (price age)
            "p_push": leg.p_push or 0.0,  # whole line: p is P(win | no push); the push travels with it
            "position": leg.position, "bat_order": leg.bat_order,
            "updated_ts": now, "expires_ts": leg.start_ts - scanner.START_BUFFER_MIN * 60}


def from_entry(e: dict) -> scanner.Leg:
    leg = scanner.Leg(e["player"], e["stat"], e["line"], e["game"], team=e["team"], start_time=e["start_time"],
                      sport=e["sport"], position=e.get("position", ""), bat_order=e.get("bat_order"))
    push = e.get("p_push") or 0.0
    win = e["p"] * (1 - push)  # P(our side wins); for a half line just p
    p_over = win if e["side"] == "More" else 1 - push - win
    leg.quotes = [scanner.Quote("pool", f"{e['sport']}:{e['player']}", e.get("tag", ""), p_over, 0.0,
                                p_push=push if leg.whole_line else None)]
    leg.source_venue = e.get("venue", "")
    leg.fetched_ts = e.get("fetched_ts")
    leg.shade = e.get("shade") or 0.0
    return leg


class EvPool:
    def __init__(self, path: Path = POOL):
        self.path = path
        self.lock_path = path.with_suffix(".lock")

    @contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                try:
                    state = json.loads(self.path.read_text())
                except (OSError, ValueError):
                    state = {}
                state.setdefault("legs", [])
                state.setdefault("sent", [])
                state.setdefault("sent_legs", {})
                yield state
                tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
                tmp.write_text(json.dumps(state))
                os.replace(tmp, self.path)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    @staticmethod
    def _live(e: dict, now: float) -> bool:
        return e["expires_ts"] > now and now - e["updated_ts"] < STALE_S

    def publish(self, sport: str, legs: list[tuple[scanner.Leg, str]], now: float | None = None) -> None:
        """Replace this sport's entries with `legs` ((leg, source tag) pairs), dropping expired ones."""
        now = time.time() if now is None else now
        with self._locked() as state:
            state["legs"] = [e for e in state["legs"] if e["sport"] != sport and e["expires_ts"] > now]
            state["legs"] += [to_entry(l, tag, now) for l, tag in legs
                              if l.start_ts and l.start_ts - scanner.START_BUFFER_MIN * 60 > now]

    def others(self, sport: str, now: float | None = None) -> list[scanner.Leg]:
        """Live legs every other sport's sentinel has published."""
        now = time.time() if now is None else now
        with self._locked() as state:
            return [from_entry(e) for e in state["legs"] if e["sport"] != sport and self._live(e, now)]

    def recent_legs(self, now: float, leg_window_s: float) -> set[tuple]:
        """Legs any sentinel alerted within leg_window_s: keep them out of new slips."""
        with self._locked() as state:
            state["sent_legs"] = {k: t for k, t in state["sent_legs"].items() if now - t < leg_window_s}
            return {tuple(json.loads(k)) for k in state["sent_legs"]}

    def would_claim(self, keys: frozenset, ev: float, now: float, window_s: float, is_repeat,
                    leg_window_s: float = 0) -> bool:
        """claim()'s verdict without recording anything: lets a sentinel skip work (a just-in-time odds
        refresh) for a slip that would be refused as a repeat. claim() still decides, atomically."""
        with self._locked() as state:
            fresh = lambda k: now - state["sent_legs"].get(json.dumps(list(k)), -1e18) >= leg_window_s
            if leg_window_s and not all(fresh(k) for k in keys):
                return False
            sent = [(s["ts"], frozenset(tuple(k) for k in s["keys"]), s["ev"]) for s in state["sent"]
                    if now - s["ts"] < window_s]
            return not is_repeat(keys, ev, sent)

    def claim(self, keys: frozenset, ev: float, now: float, window_s: float, is_repeat,
              leg_window_s: float = 0) -> bool:
        """Atomically: if no leg was alerted within leg_window_s and `is_repeat(keys, ev, sent)` is false
        for the recent alerts, record this slip and its legs and return True (the caller sends it).
        The leg check is repeated here because another sentinel may have sent one of these legs since
        this one filtered its candidates."""
        with self._locked() as state:
            state["sent"] = [s for s in state["sent"] if now - s["ts"] < window_s]
            fresh = lambda k: now - state["sent_legs"].get(json.dumps(list(k)), -1e18) >= leg_window_s
            if leg_window_s and not all(fresh(k) for k in keys):
                return False
            sent = [(s["ts"], frozenset(tuple(k) for k in s["keys"]), s["ev"]) for s in state["sent"]]
            if is_repeat(keys, ev, sent):
                return False
            state["sent"].append({"ts": now, "keys": [list(k) for k in keys], "ev": ev})
            for k in keys:
                state["sent_legs"][json.dumps(list(k))] = now
            return True

    def release(self, keys: frozenset, claimed_at: float) -> None:
        """Undo claim(keys, ..., now=claimed_at) for a slip that was never delivered. A leg another
        sentinel claimed since (a later timestamp) keeps that claim."""
        mine = sorted(json.dumps(list(k)) for k in keys)
        with self._locked() as state:
            state["sent"] = [s for s in state["sent"]
                             if not (s["ts"] == claimed_at and sorted(json.dumps(k) for k in s["keys"]) == mine)]
            for k in mine:
                if state["sent_legs"].get(k) == claimed_at:
                    del state["sent_legs"][k]
