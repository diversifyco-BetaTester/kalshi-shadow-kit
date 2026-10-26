import csv
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from local import ledger
from local import prize_picks_ev_scraper as scanner
from local.sentinel import source_tag

NOW = datetime(2026, 10, 14, 19, 0, tzinfo=timezone.utc).timestamp()


def leg(sport, name, p, start, src=None):
    l = scanner.Leg(name, "receiving yards", 28.5, "LV v BUF", team="LV", start_time=start, sport=sport)
    l.quotes = [scanner.Quote("books" if src else "kalshi", name, f"{src} de-vigged" if src else name, p, 0.0)]
    return l


def slip():
    return (0.129, "flex", (leg("nfl", "Ashton Jeanty", 0.415, "2026-10-18T17:00:00+00:00"),
                            leg("nhl", "Alex Ovechkin", 0.545, "2026-10-14T23:40:00+00:00", "DK"),
                            leg("mlb", "Griffin Jax", 0.555, "2026-10-15T17:00:00+00:00")), False)


def test_row_has_the_requested_fields():
    r = ledger.row(slip(), 0.5, NOW, "sent", source_tag)
    assert r["timestamp"] == "2026-10-14T19:00:00+00:00" and r["sports"] == "MLB+NFL+NHL"
    assert r["slip_type"] == "3-Pick Flex" and r["net_ev_pct"] == 12.9 and r["stake_units"] == 0.5
    assert r["earliest_start"] == "2026-10-14T23:40:00+00:00" and r["telegram"] == "sent"
    legs = __import__("json").loads(r["legs"])
    assert legs[0] == {"sport": "nfl", "player": "Ashton Jeanty", "stat": "receiving yards", "line": 28.5,
                       "side": "Less", "probability": 0.585, "source": "Kalshi", "team": "LV",
                       "start_time": "2026-10-18T17:00:00+00:00", "pricing": None, "push": 0.0}
    assert legs[1]["source"] == "Consensus: DK"


def test_header_written_once_and_rows_read_back(tmp_path):
    path = tmp_path / "slips.csv"
    for status in ("sent", "terminal"):
        ledger.append(ledger.row(slip(), 1.0, NOW, status, source_tag), path)
    lines = path.read_text().splitlines()
    assert lines[0] == ",".join(ledger.ALL_FIELDS) and sum(l.startswith("timestamp") for l in lines) == 1
    rows = ledger.read(path)
    assert [r["telegram"] for r in rows] == ["sent", "terminal"] and rows[0]["legs"][2]["player"] == "Griffin Jax"


def _writer(args):
    path, n, tag = args
    for i in range(n):
        r = ledger.row(slip(), 1.0, NOW + i, tag, source_tag)
        r["legs"] = r["legs"] * 20  # long rows make torn writes likely if locking were broken
        ledger.append(r, Path(path))
    return n


def test_concurrent_sentinels_never_corrupt_the_file(tmp_path):
    path = tmp_path / "slips.csv"
    with ProcessPoolExecutor(4) as pool:
        assert sum(pool.map(_writer, [(str(path), 50, f"p{i}") for i in range(4)])) == 200
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 200 and all(len(r) == len(ledger.ALL_FIELDS) and r["slip_type"] == "3-Pick Flex" for r in rows)
    assert sorted({r["telegram"] for r in rows}) == ["p0", "p1", "p2", "p3"]
    assert path.read_text().count("timestamp,sports") == 1
