"""
auto_refresh.py — refresh the sentinels' PrizePicks boards from your own open Chrome tab.

Every --interval minutes (7) between --start-hour and --end-hour (12:00-21:00 local), in the first
open Chrome tab whose title or URL contains "PrizePicks":
  1. reload it (skip with --no-reload) and wait for the board,
  2. click each sport tab in --leagues (NFL, NHL, MLB), CLICK_DELAY_S apart, so the app loads every
     league into its in-memory store (a reload starts on the featured sport only),
  3. run scripts/prizepicks_scraper.js in the page (the bookmarklet's code) and take its JSON,
     re-clicking any league that came back empty once,
  4. click back to the sport you had selected,
then write ~/Downloads/board.json and run ops/sync_boards.sh.

Why reload: the sentinels judge a board's age by its file time. Dumping a tab left open for hours
would write old lines under a new timestamp, and the 60-minute "too old to alert" guard would never
fire. Reloading and clicking sport tabs are what you would do by hand in your logged-in browser;
nothing here calls PrizePicks itself. If the tab comes back without projections (logged out, a
"verify you are human" page, a slow load), nothing is written and the reason is printed; the script
never tries to get past a challenge.

How it reaches the page: AppleScript's JavaScript runs in an isolated world that sees the DOM (so
clicking the sport tabs works) but not the app's objects, so the dump is injected as a <script>
element and hands its JSON back through a hidden element (see JXA).

One-time setup:
  Chrome > View > Developer > Allow JavaScript from Apple Events
  the first run asks to let your terminal control Google Chrome: allow it

  caffeinate -i python3 -m ops.auto_refresh                    # loop
  python3 -m ops.auto_refresh --once                           # one refresh now, any hour
  python3 -m ops.auto_refresh --once --leagues NFL NHL MLB NBA
  python3 -m ops.auto_refresh --diagnose                       # read-only: what the tab returns
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DUMP_JS = ROOT / "scripts" / "prizepicks_scraper.js"
SYNC = ROOT / "ops" / "sync_boards.sh"
DOWNLOADS = Path(os.environ.get("DOWNLOADS", Path.home() / "Downloads"))
INTERVAL_MIN = 7  # PrizePicks bumps lines and flips them to goblin/demon within ~20 min
START_HOUR, END_HOUR = 12, 21
LOAD_TIMEOUT_S = 60
LEAGUES = ("NFL", "NHL", "MLB")
CLICK_DELAY_S = 2.5  # after each sport tab click, for the app to fetch that league
RETRY_DELAY_S = 5.0  # a league that came back empty gets one more click and this long
SELECT_TIMEOUT_S = 10
OSA_TIMEOUT_S = 90

# JXA, run as: osascript -l JavaScript -e <this> <command> [args]. Chrome is never launched: no
# running Chrome, no tabs.
JXA = r"""
function run(argv) {
  const chrome = Application("Google Chrome");
  if (!chrome.running()) return JSON.stringify({tabs: []});
  if (argv[0] === "list") {
    const tabs = [];
    chrome.windows().forEach((w, wi) => w.tabs().forEach((t, ti) => {
      const title = t.title(), url = t.url();
      if (/prizepicks/i.test(title + " " + url)) tabs.push({w: wi, t: ti, title: title, url: url});
    }));
    return JSON.stringify({tabs: tabs});
  }
  const tab = chrome.windows()[Number(argv[1])].tabs()[Number(argv[2])];
  if (argv[0] === "reload") { tab.reload(); return "{}"; }
  if (argv[0] === "loading") return JSON.stringify({loading: tab.loading()});
  if (argv[0] === "click" || argv[0] === "selected") {
    // sport tabs: <nav class="league-navigation"> <button role="tab" aria-selected> <span class="name">NHL
    const js = "(function(){var tabs=[].slice.call(document.querySelectorAll('nav.league-navigation button[role=tab]'));" +
      "if(!tabs.length)return JSON.stringify({nav:false});" +
      "var name=function(b){var n=b.querySelector('.name');return (n?n.textContent:b.textContent).trim();};" +
      "var cur=tabs.filter(function(b){return b.getAttribute('aria-selected')==='true';}).map(name)[0]||null;" +
      "var want=" + JSON.stringify(argv[3] || "") + ";var b=tabs.filter(function(x){return name(x)===want;})[0];" +
      (argv[0] === "click" ? "var did=!!b&&cur!==want;if(did)b.click();" : "var did=false;") +
      "return JSON.stringify({nav:true,selected:cur,found:!!b,clicked:did});})()";
    return tab.execute({javascript: js});
  }
  if (argv[0] === "dump" || argv[0] === "probe") {
    // AppleScript's JavaScript runs in an isolated world: it sees the DOM but not the app's objects.
    // So inject the code as a <script> element (runs in the page's own world), which leaves its result
    // in a hidden element we read back here and remove.
    const page = argv[0] === "dump" ? "window.__boardJsonOnly = true;\n" + argv[3]
      : "(function(){var r=" + argv[3] + ";var h=document.createElement('script');h.type='application/json';" +
        "h.id='__board_json__';h.textContent=r;document.documentElement.appendChild(h);})();";
    const js = "(function(){var old=document.getElementById('__board_json__');if(old)old.remove();" +
      "var s=document.createElement('script');s.textContent=" + JSON.stringify(page) + ";" +
      "document.documentElement.appendChild(s);s.remove();" +
      "var h=document.getElementById('__board_json__');var out=h?h.textContent:'<<no result: the injected script " +
      "did not run in the page (blocked by its CSP, or it threw)>>';if(h)h.remove();return out;})()";
    const out = tab.execute({javascript: js});
    // Chrome hands back undefined/null as "missing value"; say what came back instead of printing nothing
    return out === undefined || out === null ? "<<" + String(out) + ">>" : (typeof out === "string" ? out : JSON.stringify(out));
  }
  throw new Error("unknown command " + argv[0]);
}
"""


class ChromeError(RuntimeError):
    pass


PROBE_JS = r"""(() => {  // runs in the page's world (injected), like the dump
  let reactKey = null, store = null, nextProjections = null;
  try {
    for (const el of [document.getElementById("__next"), document.getElementById("root"), document.body.firstElementChild]) {
      if (!el) continue;
      reactKey = reactKey || Object.keys(el).find((k) => k.startsWith("__reactContainer$") || k.startsWith("__reactFiber$")) || null;
    }
  } catch (e) { reactKey = "error: " + e; }
  try { nextProjections = window.__NEXT_DATA__ ? JSON.stringify(window.__NEXT_DATA__).split('"projection"').length - 1 : null; }
  catch (e) { nextProjections = "error: " + e; }
  return JSON.stringify({title: document.title, url: location.href, readyState: document.readyState,
    bodyText: document.body ? document.body.innerText.length : null, reactRoot: reactKey,
    nextDataProjections: nextProjections, flagAlreadySet: window.__boardJsonOnly === true});
})()"""


def osa_raw(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["osascript", "-l", "JavaScript", "-e", JXA, *args], capture_output=True, text=True,
                          timeout=OSA_TIMEOUT_S)


def describe(raw: str, r: subprocess.CompletedProcess | None = None) -> str:
    """What one dump/probe returned: exit code, stderr, size, a preview, and whether it parses."""
    bits = []
    if r is not None:
        bits.append(f"osascript exit {r.returncode}")
        if r.stderr.strip():
            bits.append(f"stderr: {r.stderr.strip()[:300]}")
    bits.append(f"{len(raw):,} chars")
    if raw:
        bits.append(f"starts {raw[:160]!r}" + (f" … ends {raw[-60:]!r}" if len(raw) > 220 else ""))
    try:
        board = json.loads(raw)
        if isinstance(board, dict):
            bits.append(f"JSON ok: method={board.get('method')!r}, props={len(board.get('props') or [])}, "
                        f"board_text={len(board.get('board_text') or '')} chars")
    except ValueError as e:
        bits.append(f"not JSON ({e})")
    return " | ".join(bits)


def osa(*args: str) -> str:
    r = osa_raw(*args)
    if r.returncode:
        msg = r.stderr.strip()
        if "Apple Events" in msg or ("JavaScript" in msg and "turned off" in msg):
            msg += "\n  -> Chrome > View > Developer > Allow JavaScript from Apple Events"
        if "-1743" in msg or "not allowed" in msg.lower():
            msg += "\n  -> System Settings > Privacy & Security > Automation: let your terminal control Google Chrome"
        raise ChromeError(msg)
    return r.stdout.strip()


def tabs() -> list[dict]:
    return json.loads(osa("list") or '{"tabs": []}')["tabs"]


def dump_once(w: str, t: str, src: str) -> tuple[dict | None, str]:
    """(board with projections, or None; what came back, see describe)."""
    r = osa_raw("dump", w, t, src)
    raw = r.stdout.strip()
    try:
        board = json.loads(raw) if r.returncode == 0 else None
    except ValueError:
        board = None
    return (board if isinstance(board, dict) and board.get("props") else None), describe(raw, r)


def sport_tab(w: str, t: str, cmd: str, league: str = "") -> dict:
    """{"nav": sport tabs present, "selected": current sport, "found": league has a tab, "clicked": bool}."""
    return json.loads(osa(cmd, w, t, league))


def wait_for_board(w: str, t: str, timeout: float) -> bool:
    """Page loaded and the sport tabs rendered."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not json.loads(osa("loading", w, t))["loading"] and sport_tab(w, t, "selected")["nav"]:
            return True
        time.sleep(2)
    return False


def select(w: str, t: str, league: str, delay: float) -> str | None:
    """Click a sport tab and wait until it's selected plus `delay` for its data. None if fine, else why."""
    st = sport_tab(w, t, "click", league)
    if not st["nav"]:
        return "no sport tabs on the page"
    if not st["found"]:
        return f"no {league} tab on the board"
    deadline = time.time() + SELECT_TIMEOUT_S
    while sport_tab(w, t, "selected")["selected"] != league:
        if time.time() > deadline:
            return f"{league} tab never became selected"
        time.sleep(0.5)
    time.sleep(delay)
    return None


def leagues(board: dict) -> dict[str, int]:
    out: dict[str, int] = {}
    for p in board.get("props", []):
        out[str(p.get("league"))] = out.get(str(p.get("league")), 0) + 1
    return dict(sorted(out.items(), key=lambda x: -x[1]))


def write_board(board: dict, downloads: Path | None = None) -> Path:
    """Atomic write as board.json, so sync_boards.sh never reads half a file. DOWNLOADS is read at
    call time (a default argument would freeze it at import)."""
    downloads = downloads or DOWNLOADS
    out = downloads / "board.json"
    tmp = downloads / f".board.json.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(board))
    os.replace(tmp, out)
    return out


LOCK = ROOT / "data" / "auto_refresh.lock"


def refresh(reload: bool = True, wanted: tuple[str, ...] = LEAGUES) -> str:
    """One refresh, holding LOCK: the loop and a Telegram /refresh (ops/telegram_listener.py) must never
    click through the same tab at once. A second caller waits for the first to finish."""
    import fcntl
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            return _refresh(reload, wanted)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _refresh(reload: bool = True, wanted: tuple[str, ...] = LEAGUES) -> str:
    """One refresh in one tab: load every league in `wanted`, dump, write, sync. Returns a one-line report."""
    found = tabs()
    if not found:
        return "no PrizePicks tab open in Chrome: nothing refreshed"
    tab, notes = found[0], []
    if len(found) > 1:
        notes.append(f"{len(found)} PrizePicks tabs open: using the first ([{tab['w']},{tab['t']}])")
    w, t = str(tab["w"]), str(tab["t"])
    original = sport_tab(w, t, "selected")["selected"]  # before a reload resets it to the featured sport
    if reload:
        osa("reload", w, t)
        time.sleep(2)
    if not wait_for_board(w, t, LOAD_TIMEOUT_S if reload else 0.1):
        return "; ".join(notes + ["the board never showed its sport tabs (logged out, a challenge page, or slow): "
                                  "boards unchanged"])
    for lg in wanted:
        problem = select(w, t, lg, CLICK_DELAY_S)
        if problem:
            notes.append(problem)
    board, why = dump_once(w, t, DUMP_JS.read_text())
    empty = [lg for lg in wanted if board is not None and leagues(board).get(lg, 0) == 0]
    for lg in empty:  # slow fetch: one more click, a longer wait, a second dump
        if sport_tab(w, t, "selected")["selected"] == lg:  # clicking the selected tab does nothing: step away
            select(w, t, next(x for x in (*wanted, "NFL", "NBA") if x != lg), 0)
        select(w, t, lg, RETRY_DELAY_S)
    if empty:
        board, why = dump_once(w, t, DUMP_JS.read_text())
        notes += [f"{lg}: no projections even after a retry" for lg in wanted
                  if board is not None and leagues(board).get(lg, 0) == 0]
    if original and original != sport_tab(w, t, "selected")["selected"]:
        select(w, t, original, 0)  # leave the tab on the sport you were looking at
    if board is None:
        return "; ".join(notes + [f"tab had no projections (logged out, a challenge page, or slow) [{why}]",
                                  "boards unchanged"])
    write_board(board)
    sync = subprocess.run([str(SYNC)], capture_output=True, text=True, env={**os.environ, "DOWNLOADS": str(DOWNLOADS)})
    report = (f"{len(board['props'])} props {leagues(board)} | " + (sync.stdout.strip() or sync.stderr.strip()))
    return report + ("" if not notes else " | " + "; ".join(notes))


def next_run(now: datetime, start_h: int, end_h: int) -> datetime:
    """Now if inside the window, else the next start_h:00."""
    if start_h <= now.hour < end_h:
        return now
    day = now if now.hour < start_h else now + timedelta(days=1)
    return day.replace(hour=start_h, minute=0, second=0, microsecond=0)


def diagnose() -> None:
    """Read-only: no reload, no board written. For each PrizePicks tab, a tiny probe script and then the
    real dump, each with exactly what osascript returned."""
    found = tabs()
    print(f"{len(found)} PrizePicks tab(s): " + "; ".join(f"[{t['w']},{t['t']}] {t['title'][:50]} {t['url'][:80]}" for t in found))
    src = DUMP_JS.read_text()
    for tab in found:
        w, t = str(tab["w"]), str(tab["t"])
        r = osa_raw("probe", w, t, PROBE_JS)
        print(f"\n[{w},{t}] probe: {describe(r.stdout.strip(), r)}")
        r = osa_raw("dump", w, t, src)
        print(f"[{w},{t}] dump:  {describe(r.stdout.strip(), r)}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="refresh once now (ignores the hour window) and exit")
    ap.add_argument("--no-reload", action="store_true", help="don't reload first (the tab's data may be old)")
    ap.add_argument("--leagues", nargs="+", default=list(LEAGUES), help="sport tabs to load before dumping")
    ap.add_argument("--diagnose", action="store_true",
                    help="read-only: show exactly what Chrome returns for each PrizePicks tab, then exit")
    ap.add_argument("--interval", type=float, default=INTERVAL_MIN, help="minutes between refreshes")
    ap.add_argument("--start-hour", type=int, default=START_HOUR)
    ap.add_argument("--end-hour", type=int, default=END_HOUR, help="no refresh starts at or after this hour")
    a = ap.parse_args()
    if a.diagnose:
        return diagnose()
    stamp = lambda: datetime.now().strftime("%H:%M:%S")
    while True:
        when = next_run(datetime.now(), a.start_hour, a.end_hour)
        if not a.once and when > datetime.now():
            print(f"{stamp()} outside {a.start_hour}:00-{a.end_hour}:00: sleeping until {when:%a %H:%M}", flush=True)
            time.sleep((when - datetime.now()).total_seconds())
        try:
            print(f"{stamp()} {refresh(not a.no_reload, tuple(x.upper() for x in a.leagues))}", flush=True)
        except (ChromeError, subprocess.TimeoutExpired, OSError, ValueError) as e:
            print(f"{stamp()} refresh failed: {e}", flush=True)  # keep looping: Chrome may be back next time
        if a.once:
            return
        time.sleep(a.interval * 60)


if __name__ == "__main__":
    main()
