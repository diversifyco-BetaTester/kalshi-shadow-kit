"""
watchdog.py — Telegram ping when an unattended process dies, duplicates, or goes silent.

Each check: `pgrep -f <pattern>` must find 1..max_count processes, and the log file (if
given) must have been written within max_silence_min. Messages on every state change and
repeats unresolved problems every REMIND_EVERY_S.

Edit WATCHES, then run exactly one copy:
    python -m ops.watchdog
    python -m ops.watchdog --once --dry-run     # print status, send nothing
"""
from __future__ import annotations

import argparse
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from ops.telegram import send

ROOT = Path(__file__).resolve().parent.parent
CHECK_EVERY_S = 60
REMIND_EVERY_S = 2 * 3600


@dataclass
class Watch:
    name: str
    pattern: str                 # pgrep -f pattern
    log: str | None = None       # file (relative to repo root) that must keep being written
    max_silence_min: float = 30.0
    max_count: int = 1


WATCHES = [
    Watch("dead-bucket shadow", "examples.weather_deadbucket.shadow run", "deadbucket_shadow.log", max_silence_min=10),
]


def pids(pattern: str) -> list[int]:
    out = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True).stdout.split()
    real = []
    for p in out:  # caffeinate and the watchdog itself repeat the pattern on their command lines
        cmd = subprocess.run(["ps", "-o", "command=", "-p", p], capture_output=True, text=True).stdout
        if "caffeinate" not in cmd and "watchdog" not in cmd:
            real.append(int(p))
    return real


def check(w: Watch) -> str | None:
    n = len(pids(w.pattern))
    if n == 0:
        return "process not running"
    if n > w.max_count:
        return f"{n} copies running (expected {w.max_count})"
    if w.log:
        p = ROOT / w.log
        if not p.exists():
            return f"log {w.log} missing"
        silent = (time.time() - p.stat().st_mtime) / 60
        if silent > w.max_silence_min:
            return f"log {w.log} silent for {silent:.0f} min (limit {w.max_silence_min:.0f})"
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description="Background process watchdog")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="print instead of sending")
    a = ap.parse_args()
    load_dotenv(ROOT / ".env")
    notify = print if a.dry_run else send
    state: dict[str, str | None] = {}
    last_sent: dict[str, float] = {}
    first = True
    while True:
        now = time.time()
        lines = []
        for w in WATCHES:
            problem = check(w)
            lines.append(f"{'✅' if problem is None else '🔴'} {w.name}: {problem or 'ok'}")
            if not first and state.get(w.name, "unset") != problem:
                notify(f"🔴 <b>Watchdog</b>: {w.name}\n{problem}" if problem else f"✅ <b>Watchdog</b>: {w.name} recovered")
                last_sent[w.name] = now
            elif problem and not first and now - last_sent.get(w.name, 0) >= REMIND_EVERY_S:
                notify(f"🔴 <b>Watchdog</b> (still): {w.name}\n{problem}")
                last_sent[w.name] = now
            state[w.name] = problem
            if first and problem:
                last_sent[w.name] = now
        if first:
            notify("🐕 <b>Watchdog online</b>\n" + "\n".join(lines))
            first = False
        print(f"{datetime.now():%H:%M:%S} " + " | ".join(lines), flush=True)
        if a.once:
            return
        time.sleep(CHECK_EVERY_S)


if __name__ == "__main__":
    main()
