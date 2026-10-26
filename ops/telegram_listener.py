"""
telegram_listener.py — answer a few read-only commands (and one refresh) from your own Telegram chat.

Long polling on getUpdates; replies through ops/telegram.send. Runs as its own process (one copy:
Telegram allows a single poller per bot token), touching nothing the sentinels write except reading it.

  /status    sentinels (process alive + last cycle from local/.heartbeat_<sport>.json), board ages,
             the newest board dump, and Odds API credits from the shared cache (no API call)
  /pool      legs waiting in local/ev_pool.json to be combined into slips, likeliest first
  /pnl       settled record, net units, ROI and hit rate by sport (also /summary), from
             local/grade_slips.py and local/slips_history.csv; grades nothing, so it answers instantly
  /refresh   run ops/auto_refresh.py --once now (Chrome board dump + sync). One at a time; it waits for
             a scheduled refresh in progress (both take data/auto_refresh.lock)
  /help

Security: only TELEGRAM_ALERT_CHAT_ID (.env, the chat alerts already go to) is obeyed; anything else is
ignored and logged. The id is read from .env rather than written here: ops/ is the public kit. No
command takes arguments or runs anything but the fixed refresh above. Commands older than STALE_S
(sent while the listener was down) are dropped, so a /refresh from hours ago never fires.

  python3 -m ops.telegram_listener              # foreground
  local/telegram_listener.plist                 # launchd: start at login, restart if it exits
"""
from __future__ import annotations

import argparse
import glob
import html
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from ops import telegram

ROOT = Path(__file__).resolve().parents[1]
SPORTS = ("nhl", "mlb", "nfl", "nba", "cfb")
POLL_S = 50                 # getUpdates long-poll timeout
STALE_S = 300
OFFSET = ROOT / "data" / "telegram_listener.offset"
POOL = ROOT / "local" / "ev_pool.json"
ODDS_CACHE = ROOT / "local" / ".odds_cache.json"
HEARTBEAT = ROOT / "local" / ".heartbeat_{sport}.json"
LEDGER = ROOT / "local" / "slips_history.csv"
LINES = ROOT / "local" / "lines_{sport}.csv"
DOWNLOADS = Path(os.environ.get("DOWNLOADS", Path.home() / "Downloads"))
POOL_STALE_S = 300          # ev_pool's STALE_S: a writer that stopped refreshing
MAX_LINES = 40


def ago(ts: float | None, now: float) -> str:
    if not ts:
        return "never"
    s = now - ts
    return f"{s:.0f}s ago" if s < 120 else f"{s / 60:.0f} min ago" if s < 7200 else f"{s / 3600:.1f} h ago"


def _json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def sentinel_pids() -> dict[str, list[int]]:
    """{sport: pids} of running local.sentinel processes (the caffeinate wrapper excluded)."""
    out: dict[str, list[int]] = {s: [] for s in SPORTS}
    r = subprocess.run(["ps", "-ax", "-o", "pid=,command="], capture_output=True, text=True)
    for line in r.stdout.splitlines():
        pid, _, cmd = line.strip().partition(" ")
        if "-m local.sentinel" in cmd and not cmd.startswith("caffeinate"):
            parts = cmd.split()
            sport = parts[parts.index("--sport") + 1] if "--sport" in parts else "nfl"
            out.setdefault(sport, []).append(int(pid))
    return out


# --------------------------------------------------------------------------- commands
def status(now: float | None = None, pids: dict[str, list[int]] | None = None) -> str:
    now = time.time() if now is None else now
    pids = sentinel_pids() if pids is None else pids
    lines = ["<b>Pipeline status</b>"]
    for sport in SPORTS:
        hb = _json(Path(str(HEARTBEAT).format(sport=sport)))
        lines_csv = Path(str(LINES).format(sport=sport))
        board = f"board {ago(lines_csv.stat().st_mtime, now)}" if lines_csv.exists() else "no board yet"
        n = len(pids.get(sport, []))
        if not n:
            alive = "⛔ not running"
        elif not hb or now - hb.get("ts", 0) > 180:
            alive = f"⚠️ running, last cycle {ago(hb.get('ts'), now)}"
        else:
            alive = "✅ " + (f"{hb.get('priced', 0)}/{hb.get('legs', 0)} priced, {hb.get('good', 0)} ≥ min, "
                             f"{hb.get('slips', 0)} slip(s)" if hb.get("state") == "ok" else hb.get("state", "ok"))
        dup = f" ({n} copies!)" if n > 1 else ""
        lines.append(f"• <b>{sport.upper()}</b>{dup}: {alive} · {board}")
    dumps = [(Path(f).stat().st_mtime, Path(f).name) for f in glob.glob(str(DOWNLOADS / "*_board.json"))]
    lines.append(f"Latest board dump: {datetime.fromtimestamp(max(dumps)[0]):%H:%M} ({ago(max(dumps)[0], now)})"
                 if dumps else "Latest board dump: none in ~/Downloads")
    u = _json(ODDS_CACHE).get("usage", {})
    if u:
        groups = " · ".join(f"{g} {n}" for g, n in sorted((u.get("by_group") or {}).items()))
        lines.append(f"Odds API: {u.get('remaining', '?')} credits left this month · today {u.get('used', 0)}/"
                     f"{u.get('cap', '?')}" + (f" ({groups})" if groups else "") + f" (as of {u.get('date', '?')})")
    else:
        lines.append("Odds API: no usage recorded yet")
    return "\n".join(lines)


def pnl() -> str:
    """local/grade_slips.py's summary card. Imported here, not at the top: local/ is the private,
    gitignored half of the repo and the rest of this listener works without it."""
    try:
        from local import grade_slips
    except ImportError as e:
        return f"P&amp;L unavailable: local/grade_slips.py not found ({html.escape(str(e))})"
    try:
        return grade_slips.summary_card(LEDGER)
    except (OSError, ValueError, KeyError) as e:
        return f"P&amp;L unavailable: couldn't read the ledger ({html.escape(type(e).__name__)})"


def pool(now: float | None = None) -> str:
    now = time.time() if now is None else now
    state = _json(POOL)
    legs = [e for e in state.get("legs", []) if e.get("expires_ts", 0) > now and now - e.get("updated_ts", 0) < POOL_STALE_S]
    cooling = sum(1 for t in (state.get("sent_legs") or {}).values() if now - t < 120 * 60)
    if not legs:
        return f"<b>Pool</b>: empty (no live legs){f' · {cooling} legs cooling down' if cooling else ''}"
    legs.sort(key=lambda e: -e["p"])
    by = {}
    for e in legs:
        by[e["sport"]] = by.get(e["sport"], 0) + 1
    lines = [f"<b>Pool</b>: {len(legs)} live legs ({', '.join(f'{s.upper()} {n}' for s, n in sorted(by.items()))})"
             + (f" · {cooling} cooling down" if cooling else "")]
    for e in legs[:MAX_LINES]:
        start = datetime.fromtimestamp(e["expires_ts"] + 600).strftime("%a %H:%M")
        tag = f" ({html.escape(e['tag'])})" if e.get("tag") else ""
        lines.append(f"• {e['sport'].upper()} {html.escape(e['player'])} {e['line']:g} {html.escape(e['stat'])} "
                     f"<b>{e['side']}</b> {e['p']:.1%}{tag} · {start}")
    if len(legs) > MAX_LINES:
        lines.append(f"…and {len(legs) - MAX_LINES} more")
    return "\n".join(lines)


class Refresher:
    """Runs ops.auto_refresh --once in a thread, one at a time, and reports back when it ends."""

    def __init__(self, reply, leagues: list[str], runner=None):
        self.reply, self.leagues, self.thread = reply, leagues, None
        self.runner = runner or self._run

    def _run(self) -> str:
        cmd = [sys.executable, "-m", "ops.auto_refresh", "--once", "--leagues", *self.leagues]
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=600)
        out = (r.stdout.strip().splitlines() or [""])[-1] or r.stderr.strip()[-300:]
        return out if r.returncode == 0 else f"exit {r.returncode}: {out}"

    def start(self) -> str:
        if self.thread and self.thread.is_alive():
            return "A refresh is already running; I'll report when it finishes."

        def work():
            try:
                self.reply(f"🔄 Refresh done: {html.escape(self.runner())}")
            except Exception as e:  # noqa: BLE001 — report, don't kill the listener
                self.reply(f"🔄 Refresh failed: {html.escape(repr(e))}")
        self.thread = threading.Thread(target=work, daemon=True)
        self.thread.start()
        return f"🔄 Refreshing {' '.join(self.leagues)} from Chrome…"


HELP = ("<b>Commands</b>\n/status: sentinels, boards, Odds API credits\n/pool: legs waiting to be combined\n"
        "/pnl (or /summary): record, units, ROI, hit rate by sport\n/refresh: Chrome board dump now\n/help")


def handle(update: dict, allowed: str, refresher: Refresher, now: float | None = None) -> str | None:
    """The reply for one update, or None to stay silent (other chats, stale or non-command messages)."""
    now = time.time() if now is None else now
    msg = update.get("message") or update.get("edited_message") or {}
    chat = str((msg.get("chat") or {}).get("id", ""))
    text = (msg.get("text") or "").strip()
    if chat != allowed:
        if text:
            print(f"{datetime.now():%H:%M:%S} ignored a message from chat {chat}", flush=True)
        return None
    if not text.startswith("/") or now - msg.get("date", 0) > STALE_S:
        return None
    cmd = text.split()[0].split("@")[0].lower()
    if cmd == "/status":
        return status(now)
    if cmd == "/pool":
        return pool(now)
    if cmd in ("/pnl", "/summary"):
        return pnl()
    if cmd == "/refresh":
        return refresher.start()
    if cmd in ("/help", "/start"):
        return HELP
    return f"Unknown command {html.escape(cmd)}.\n\n{HELP}"


# --------------------------------------------------------------------------- polling
def get_updates(token: str, offset: int | None, timeout: int = POLL_S) -> list[dict]:
    q = f"timeout={timeout}" + (f"&offset={offset}" if offset is not None else "") + '&allowed_updates=["message"]'
    with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/getUpdates?{q}", timeout=timeout + 15) as r:
        d = json.loads(r.read())
    if not d.get("ok"):
        raise RuntimeError(d.get("description", "getUpdates failed"))
    return d.get("result", [])


def run(token: str, allowed: str, leagues: list[str], fetch=get_updates, reply=None, once: bool = False) -> None:
    reply = reply or (lambda text: telegram.send(text, token=token, chat_id=allowed))
    refresher = Refresher(reply, leagues)
    offset = int(OFFSET.read_text()) if OFFSET.exists() else None
    backoff = 1
    print(f"{datetime.now():%H:%M:%S} listening for commands from chat {allowed}", flush=True)
    while True:
        try:
            updates = fetch(token, offset)
            backoff = 1
        except urllib.error.HTTPError as e:
            if e.code == 409:  # another getUpdates poller (or a webhook) on this token
                print(f"{datetime.now():%H:%M:%S} conflict: another listener is polling this bot; retrying", flush=True)
            else:
                print(f"{datetime.now():%H:%M:%S} getUpdates HTTP {e.code}", flush=True)
            updates = []
            time.sleep(min(backoff, 60)); backoff *= 2
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, RuntimeError) as e:
            print(f"{datetime.now():%H:%M:%S} getUpdates failed: {e!r}", flush=True)
            updates = []
            time.sleep(min(backoff, 60)); backoff *= 2
        for u in updates:
            offset = u["update_id"] + 1
            out = handle(u, allowed, refresher)
            if out:
                reply(out)
        if updates:
            OFFSET.parent.mkdir(parents=True, exist_ok=True)
            OFFSET.write_text(str(offset))
        if once:
            return


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--leagues", nargs="+", default=["NFL", "NHL", "MLB", "NBA", "CFB"],
                    help="sport tabs /refresh loads (passed to ops.auto_refresh --leagues)")
    a = ap.parse_args()
    load_dotenv(ROOT / ".env")
    token, allowed = os.getenv("TELEGRAM_BOT_TOKEN", ""), os.getenv("TELEGRAM_ALERT_CHAT_ID", "").strip()
    if not token or not allowed:
        raise SystemExit("TELEGRAM_BOT_TOKEN and TELEGRAM_ALERT_CHAT_ID must be set in .env (the whitelist)")
    run(token, allowed, [x.upper() for x in a.leagues])


if __name__ == "__main__":
    main()
