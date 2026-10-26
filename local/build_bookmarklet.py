"""
build_bookmarklet.py — print scripts/prizepicks_scraper.js as a javascript: URL.

  python3 -m local.build_bookmarklet | pbcopy
Then in Chrome: Bookmarks > Add page..., name it "board.json", paste as the URL.
"""
from pathlib import Path
from urllib.parse import quote

src = Path(__file__).resolve().parent.parent / "scripts" / "prizepicks_scraper.js"
print("javascript:" + quote(src.read_text(), safe=""))
