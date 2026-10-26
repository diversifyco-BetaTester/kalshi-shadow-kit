"""
parse_board.py — turn text copied from the PrizePicks NFL board into local/lines_<sport>.csv, then scan it.

Paste the board from the app (you copy it; nothing here touches PrizePicks), e.g.
    Drake Maye NE - QB Drake Maye vs LV Sun 10:00am 19.5 Pass Comp, Jared Goff DET - QB ...

or as the web board copies it, one field per line, with the player repeated and the
Less/More buttons included (the repeat and the buttons are optional):
    James Cook III
    BUF - RB
    James Cook III
    @ LA Mon 5:15pm
    75.5
    Rush Yards
    Less
    More
Both forms can be mixed in one paste.

  pbpaste | python3 -m local.parse_board --min-ev 0.60        # clipboard -> CSV -> scan
  pbpaste | python3 -m local.parse_board --sport mlb           # MLB board (also: nhl; default nfl)
  python3 -m local.parse_board board.txt --min-ev 0.60        # file -> CSV -> scan
  python3 -m local.parse_board --min-ev 0.60                  # re-scan the existing local/lines_<sport>.csv
  python3 -m local.parse_board board.txt --no-scan            # only write the CSV

Stats and team codes are converted to Kalshi's for the chosen --sport, and the scan uses
that sport's Kalshi prop series. Chunks the parser can't
read are listed, never silently dropped, and a paste that yields 0 props never overwrites the
CSV. Other scanner flags pass through, e.g. --top 5.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import re
import sys
from pathlib import Path

from local import prize_picks_ev_scraper as scanner

# Per sport: PrizePicks stat label -> Kalshi's stat phrase ("Player: N+ <phrase>"), the Kalshi
# series holding those per-game props, and PrizePicks team codes Kalshi spells differently.
# Labels are matched lowercased with spaces around "+" removed; unknown labels pass through.
SPORTS = {
    "nfl": {
        "stats": {"pass yards": "passing yards", "pass comp": "passing completions",
                  "pass attempts": "passing attempts", "pass tds": "passing touchdowns",
                  "rush yards": "rushing yards", "rush attempts": "rushing attempts",
                  "receiving yards": "receiving yards", "rec yards": "receiving yards",
                  "receptions": "receptions", "rec targets": "receiving targets", "sacks": "sacks",
                  "fg made": "field goals made", "punts": "punts",
                  # Kalshi's "N+ touchdowns" counts TDs the player SCORES (rush/rec), not passing TDs,
                  # the same as PrizePicks' Anytime TD
                  "anytime tds": "touchdowns", "anytime td": "touchdowns",
                  "anytime touchdown": "touchdowns", "anytime touchdowns": "touchdowns"},
        "series": ["KXNFLPASSYDS", "KXNFLPASSCOMP", "KXNFLPASSATT", "KXNFLPASSTDS", "KXNFLRSHYDS",
                   "KXNFLRSHATT", "KXNFLRECYDS", "KXNFLREC", "KXNFLTD", "KXNFLANYTD"],
        "teams": {"JAX": "JAC", "LA": "LAR"},
    },
    "mlb": {
        "stats": {"pitcher strikeouts": "strikeouts", "strikeouts": "strikeouts", "total bases": "total bases",
                  "home runs": "home runs", "hits": "hits", "hits+runs+rbis": "hits + runs + rbis",
                  "rbis": "rbis", "stolen bases": "stolen bases", "walks allowed": "walks allowed",
                  "hits allowed": "hits allowed", "earned runs allowed": "earned runs",
                  "earned runs": "earned runs"},
        "series": ["KXMLBKS", "KXMLBTB", "KXMLBHR", "KXMLBHIT", "KXMLBHRR", "KXMLBRBI", "KXMLBSB",
                   "KXMLBWA", "KXMLBHA", "KXMLBERA"],
        "teams": {"CHW": "CWS", "ARI": "AZ", "OAK": "ATH", "KCR": "KC", "SDP": "SD", "SFG": "SF",
                  "TBR": "TB", "WSN": "WSH", "WAS": "WSH"},
    },
    "nba": {
        # PrizePicks labels as on its WNBA board (same app); Kalshi titles read "Player: 25+ points"
        "stats": {"points": "points", "rebounds": "rebounds", "assists": "assists", "3-pt made": "threes",
                  "3-pointers made": "threes", "blocked shots": "blocks", "blocks": "blocks", "steals": "steals",
                  "turnovers": "turnovers", "pts+rebs+asts": "points + rebounds + assists",
                  "pts+rebs": "points + rebounds", "pts+asts": "points + assists", "rebs+asts": "rebounds + assists",
                  "blks+stls": "blocks + steals"},
        "series": ["KXNBAPTS", "KXNBAREB", "KXNBAAST", "KXNBA3PT", "KXNBABLK", "KXNBASTL", "KXNBASTOCK",
                   "KXNBASTOCKS", "KXNBAPRA", "KXNBAPR", "KXNBAPA", "KXNBARA"],
        "teams": {},
    },
    "nhl": {
        # Kalshi lists points, assists, goals and saves today. Shots on goal, hits, faceoffs won and
        # blocked shots are mapped so they parse, but no Kalshi (or Polymarket) market prices them
        # yet: the sentinel reports them as unpriced. Add the series below when Kalshi lists one.
        "stats": {"points": "points", "assists": "assists", "goals": "goals",
                  "goalie saves": "saves", "saves": "saves",
                  "shots on goal": "shots on goal", "sog": "shots on goal", "shots": "shots on goal",
                  "hits": "hits", "faceoffs won": "faceoffs won", "faceoff wins": "faceoffs won",
                  "blocked shots": "blocked shots", "blocks": "blocked shots"},
        "series": ["KXNHLPTS", "KXNHLAST", "KXNHLGOAL", "KXNHLSAVE"],
        "teams": {"LAK": "LA", "VEG": "VGK", "NJD": "NJ", "SJS": "SJ", "TBL": "TB", "WAS": "WSH",
                  "UTAH": "UTA", "MON": "MTL", "CLB": "CBJ", "NAS": "NSH"},
    },
}
# College football: PrizePicks uses the NFL stat labels. Kalshi lists only team-level CFB markets
# (team rushing yards, team touchdowns...), no player props (checked 2026-10-16), so CFB legs are
# priced by the sportsbooks alone (local/fetch_odds.py).
SPORTS["cfb"] = {"stats": dict(SPORTS["nfl"]["stats"]), "series": [], "teams": {}}
# Soccer: PrizePicks puts every competition (Bundesliga, 2. Bundesliga, MLS, Liga MX, EPL...) under one SOCCER
# league, with full team names ("Leipzig", "Union Berlin"), kept as they are. Priced by Kalshi only (no Odds
# API mapping: one sport key per competition): its player markets ("Chris Wood: 1+ goals", "Neco Williams: 1+
# assists?", "Xaver Schlager: score or assist?") open around match day. Checked 2026-10-23.
SPORTS["soccer"] = {
    "stats": {"goals": "goals", "assists": "assists", "goal+assist": "goal+assist", "shots": "shots",
              "shots on target": "shots on target", "goalie saves": "saves", "saves": "saves", "tackles": "tackles",
              "fouls": "fouls", "passes attempted": "passes attempted", "clearances": "clearances",
              "crosses": "crosses", "shots assisted": "shots assisted", "attempted dribbles": "attempted dribbles"},
    "series": ["KXEPLGOAL", "KXEPLAST", "KXEPLSOA", "KXLALIGAGOAL", "KXSERIEAGOAL", "KXBUNDESLIGAGOAL",
               "KXLIGUE1GOAL", "KXMLSGOAL", "KXLIGAMXGOAL", "KXUCLGOAL", "KXUCLAST", "KXUCLSOA", "KXUCLSOT",
               "KXUCLSHOT", "KXUCLSAVE"],
    "teams": {},
}

CSV_FIELDS = ["player", "stat", "line", "game", "team", "odds_type", "start_time", "position"]


def lines_path(sport: str) -> Path:
    """Each sport keeps its own cache, so an MLB and an NHL sentinel never overwrite each other."""
    return Path(f"local/lines_{sport}.csv")


def board_path(sport: str) -> Path:
    """Pasted text has no league in it, so each sport gets its own paste file too."""
    return Path(f"local/board_{sport}.txt")


def write_csv(rows: list[dict], out: str | Path) -> None:
    """Write via a temp file and rename, so a reader never sees a half-written CSV."""
    out = Path(out)
    tmp = out.with_name(f".{out.name}.{os.getpid()}.tmp")
    with open(tmp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, out)

# Single-line form: "Drake Maye NE - QB Drake Maye vs LV Sun 10:00am 19.5 Pass Comp"
ENTRY = re.compile(r"(?P<player>.+?) (?P<team>[A-Z]{2,4}) - (?P<pos>[A-Z0-9/]{1,5}) .+? (?P<where>vs|@) (?P<opp>[A-Z]{2,4})"
                   r" \w{3} [\d:]+\s?[ap]m (?P<line>\d+(?:\.\d+)?) (?P<stat>.+)")
# Card form: each card is found by its "TEAM - POS" line; the player is the line above it.
CARD_TEAM = re.compile(r"(?P<team>[A-Z]{2,4}) - (?P<pos>[A-Z0-9/]{1,5})")  # "BUF - RB", "LAD - SP", "NYY - 1B"
CARD_GAME = re.compile(r"(?:.+? )?(?P<where>vs|@) (?P<opp>[A-Z]{2,4})(?: \w{3} [\d:]+\s?[ap]m)?")
CARD_LINE = re.compile(r"\d+(?:\.\d+)?")
BUTTONS = {"less", "more", "higher", "lower"}
# (sport, position, Kalshi stat) props to leave out on purpose, with the reason shown to the user.
SKIP = {("nfl", "QB", "touchdowns"): "QB anytime TD skipped (passing vs scoring rule risk)"}


def row(sport: str, player: str, team: str, pos: str, where: str, opp: str, line: str, stat: str) -> dict | str:
    """The CSV row for one prop, or a string saying why it was deliberately skipped."""
    cfg = SPORTS[sport]
    stat = re.sub(r"\s*\+\s*", "+", " ".join(stat.lower().split()))
    stat = cfg["stats"].get(stat, stat)
    if (sport, pos, stat) in SKIP:
        return f"{player.strip()} {line} {stat}: {SKIP[(sport, pos, stat)]}"
    team, opp = cfg["teams"].get(team, team), cfg["teams"].get(opp, opp)
    return {"player": player.strip(), "stat": stat, "line": line,
            "game": f"{opp}@{team}" if where == "vs" else f"{team}@{opp}", "team": team, "odds_type": "standard",
            "start_time": "", "position": pos}


def is_field(s: str) -> bool:
    """True for a line that is a card field other than a name or stat label."""
    return bool(CARD_TEAM.fullmatch(s) or CARD_GAME.fullmatch(s) or CARD_LINE.fullmatch(s))


def card_at(lines: list[str], j: int, sport: str) -> tuple[dict | str, int, int] | None:
    """Card anchored on the TEAM - POS line at j: (row, first index, last index), or None."""
    t = CARD_TEAM.fullmatch(lines[j])
    if not t or j == 0 or is_field(lines[j - 1]):
        return None
    player, k = lines[j - 1], j + 1
    if k < len(lines) and lines[k] == player:  # the board repeats the name
        k += 1
    if k + 2 >= len(lines):
        return None
    game, line, stat = lines[k:k + 3]
    g = CARD_GAME.fullmatch(game)
    if not (g and CARD_LINE.fullmatch(line)) or is_field(stat):
        return None
    return row(sport, player, t["team"], t["pos"], g["where"], g["opp"], line, stat), j - 1, k + 2


def parse(text: str, sport: str = "nfl") -> tuple[list[dict], list[str]]:
    """Accept single-line entries, multi-line cards, or a mix.

    Lines that belong to no complete card (a paste that starts or ends mid-card) are returned
    as fragments, grouped, so the caller can show what was skipped.
    """
    lines = [l.strip() for l in text.splitlines() if l.strip() and l.strip().lower() not in BUTTONS]
    rows, bad, used = [], [], set()
    for j in range(len(lines)):
        card = None if j in used else card_at(lines, j, sport)
        if card:
            (rows if isinstance(card[0], dict) else bad).append(card[0])
            used.update(range(card[1], card[2] + 1))
    fragment = []
    for i, l in enumerate(lines):
        if i in used:
            if fragment:
                bad.append(" / ".join(fragment))
                fragment = []
            continue
        for chunk in (c.strip() for c in l.split(",")):
            m = ENTRY.fullmatch(chunk) if chunk else None
            if m:
                r = row(sport, m["player"], m["team"], m["pos"], m["where"], m["opp"], m["line"], m["stat"])
                (rows if isinstance(r, dict) else bad).append(r)
            elif chunk:
                fragment.append(chunk)
    if fragment:
        bad.append(" / ".join(fragment))
    return rows, bad


def not_standard(prop: dict) -> str | None:
    """Why a projection is not a plain standard two-way line (so neither More/Less pricing nor the
    standard Flex payouts apply), or None if it is. Strict: a missing odds_type is not assumed
    standard. The other fields only exist in boards from the current bookmarklet; older boards
    simply don't have them. adjusted_odds is NOT a reason: standard lines flagged with it pay the
    normal Flex table (checked in the app 2026-10-15), and they are most of the board (NHL 54%,
    NFL 83% on 2026-10-15)."""
    odds_type = str(prop.get("odds_type") or "").lower()
    if odds_type != "standard":
        return f"{odds_type or 'unknown odds_type'} line"
    if prop.get("is_promo"):
        return "promo line"
    if prop.get("discount_name") or prop.get("discount_percentage"):
        return f"discounted line ({prop.get('discount_name') or str(prop.get('discount_percentage')) + '%'})"
    wager = str(prop.get("allowed_wager_types") or "")
    if wager and "under" not in wager:
        # standard but More-only (2026-10-16: 259 of 2,761 standard projections): no Less to price.
        # Projections without the field (about 400) are kept as two-way, as before it existed.
        return f"{wager}-only line"
    if prop.get("event_type") == "combo":
        return "combo projection"
    if prop.get("flash_sale_line_score") not in (None, "", prop.get("line")):
        return "flash-sale (discounted) line"
    if prop.get("projection_type") not in (None, "", "Single Stat"):
        return f"{prop['projection_type']} projection"
    if prop.get("status") not in (None, "", "pre_game"):
        return f"{prop['status']} projection"
    if prop.get("is_live") or prop.get("in_game"):
        return "live line"
    return None


def active_line(props: list[dict]) -> tuple[dict | None, str]:
    """One player/stat/game can carry several standard projections when a line moved and the old one
    lingers in the app's data. Keep the most recently updated; with no timestamps, keep them only if
    they agree. Returns (prop or None, why dropped)."""
    if len(props) == 1:
        return props[0], ""
    if all(p.get("updated_at") for p in props):
        return max(props, key=lambda p: p["updated_at"]), ""
    if len({p["line"] for p in props}) == 1:
        return props[0], ""
    return None, "standard lines disagree (" + ", ".join(f"{p['line']:g}" for p in props) + ") with no timestamp"


def parse_json(data: dict, sport: str) -> tuple[list[dict], list[str]]:
    """Parse board.json from scripts/prizepicks_scraper.js.

    Structured props are used when the bookmarklet found them; otherwise it saved the page's
    visible text as `board_text`, which goes through the card parser above. Other leagues are
    ignored. Only plain standard two-way lines are kept (not_standard: demon, goblin, promo,
    flash-sale, live, or unknown odds_type lines are dropped; adjusted-odds standard lines are kept),
    one active line per player/stat/game (active_line). Whole-number lines (2 shots on goal) are KEPT:
    the sentinel prices their push from two adjacent market rungs (Leg.p_push) and leaves out any it
    can't. Two-player combo props ("A + B", "Shots On Goal (Combo)") are
    skipped too: no single Kalshi market prices them. start_time is kept so the sentinel can drop
    games that have started. Pasted text can't tell a Goblin/Demon card from a standard one, so
    prefer board.json.
    """
    if not data.get("props"):
        return parse(data.get("board_text", ""), sport)
    bad, other = [], 0
    skipped: dict[str, int] = {}
    groups: dict[tuple, list[dict]] = {}
    for prop in data["props"]:
        league = str(prop.get("league") or "").upper()
        if league and league != sport.upper():  # also drops "NFL1H"/"NFL1Q": Kalshi prices full games
            other += 1
            continue
        name, line = prop.get("player"), prop.get("line")
        if not name or not isinstance(line, (int, float)):
            bad.append(f"incomplete prop: {str(prop)[:80]}")
            continue
        why = ("combo prop" if "(combo)" in str(prop.get("stat") or "").lower() or " + " in name
               else not_standard(prop))
        if why:
            skipped[why] = skipped.get(why, 0) + 1
            continue
        groups.setdefault((name, str(prop.get("stat") or ""), str(prop.get("start_time") or "")), []).append(prop)
    rows = []
    for (name, stat, _), props in groups.items():
        prop, why = active_line(props)
        if prop is None:
            bad.append(f"{name} {stat}: {why}")
            continue
        line = prop["line"]
        r = row(sport, name, str(prop.get("team") or ""), str(prop.get("position") or ""), "@",
                str(prop.get("opponent") or ""), f"{line:g}", stat)
        if isinstance(r, str):
            bad.append(r)
            continue
        r["game"] = " v ".join(sorted(r["game"].split("@")))  # home/away isn't in the data
        r["odds_type"] = "standard"
        r["start_time"] = str(prop.get("start_time") or "")
        rows.append(r)
    bad += [f"{n} × {why} skipped" for why, n in sorted(skipped.items(), key=lambda x: -x[1])]
    if other:
        bad.append(f"{other} props from other leagues ignored (--sport {sport})")
    return rows, bad


def load_board(path: str | Path, sport: str) -> tuple[list[dict], list[str]]:
    """A board file in either form: board.json from the bookmarklet, or pasted text."""
    text = Path(path).read_text()
    return parse_json(json.loads(text), sport) if str(path).endswith(".json") else parse(text, sport)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("board", nargs="?", help="text file copied from the board, or - for stdin. "
                    "Omit it (with nothing piped in) to re-scan the existing --out CSV.")
    ap.add_argument("--sport", choices=sorted(SPORTS), default="nfl", help="which stat names, teams and Kalshi series")
    ap.add_argument("--out", help="CSV to write and scan (default: local/lines_<sport>.csv)")
    ap.add_argument("--no-scan", action="store_true", help="write the CSV and stop")
    ap.add_argument("--min-ev", "--min-p", dest="min_p", type=float, help="win probability to highlight, e.g. 0.60")
    ap.add_argument("--poly-tag", default="", help="also price legs on Polymarket under this tag (default: Kalshi only)")
    a, scan_args = ap.parse_known_args()
    a.out = a.out or str(lines_path(a.sport))
    if a.min_p is not None:
        scan_args += ["--min-p", str(a.min_p)]
    scan_args += ["--poly-tag", a.poly_tag]

    text = ""
    if a.board not in (None, "-"):
        text = open(a.board).read()
    elif a.board == "-" or not sys.stdin.isatty():
        text = sys.stdin.read()
    if not text.strip() and a.board in (None, "-"):
        print(f"no board text given: re-scanning {a.out}")
    else:
        is_json = (a.board or "").endswith(".json") or text.lstrip().startswith("{")
        rows, bad = parse_json(json.loads(text), a.sport) if is_json else parse(text, a.sport)
        for chunk in bad:
            print(f"  skipped: {chunk[:110]}")
        if not rows:
            print(f"parsed 0 lines; {a.out} left unchanged")
            return
        write_csv(rows, a.out)
        print(f"parsed {len(rows)} lines -> {a.out}")
    if a.no_scan:
        return

    if "--kalshi-series" not in scan_args:
        scan_args += ["--kalshi-series", *SPORTS[a.sport]["series"]]
    print()
    asyncio.run(scanner.main([a.out, *scan_args]))


if __name__ == "__main__":
    main()
