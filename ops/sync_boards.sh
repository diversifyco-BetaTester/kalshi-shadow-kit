#!/usr/bin/env bash
# sync_boards.sh — copy the newest bookmarklet download to each sentinel's feed, then clear the downloads.
#
# The bookmarklet saves ~/Downloads/board.json (Chrome names repeats "board (1).json"). Every download
# carries every league whose tab you opened, and each sentinel keeps only its own, so the same file feeds:
#   ~/Downloads/{nhl,mlb,nfl,nba,cfb,soccer}_board.json
# A sport's board is only replaced when the download has that sport's props: an NFL-only download must
# not overwrite (and make look fresh) an NHL board from an hour ago.
# Copies keep the download's timestamp (cp -p): the sentinels judge a board's age by it, so a stale
# board still triggers the "click the bookmarklet" nudge. Each copy is written to a temp file and
# renamed, so a sentinel never reads half a file.
#
#   ops/sync_boards.sh            # or DOWNLOADS=/some/dir ops/sync_boards.sh
set -euo pipefail

dir="${DOWNLOADS:-$HOME/Downloads}"
shopt -s nullglob
boards=("$dir"/board*.json)
if (( ${#boards[@]} == 0 )); then
  echo "No $dir/board*.json to sync: click the bookmarklet first." >&2
  exit 1
fi

latest="${boards[0]}"
for f in "${boards[@]}"; do
  [[ "$f" -nt "$latest" ]] && latest="$f"
done
present=$(python3 -c '
import json, sys
props = json.load(open(sys.argv[1])).get("props") or []
print(" ".join(s for s in ("nhl", "mlb", "nfl", "nba", "cfb", "soccer") if any(str(p.get("league", "")).lower() == s for p in props)))
' "$latest" 2>/dev/null) || {
  echo "$latest is not valid JSON (still downloading?); nothing changed." >&2
  exit 1
}
if [[ -z "$present" ]]; then
  echo "$latest has no NHL, MLB, NFL, NBA, CFB or SOCCER props (open those tabs, then click the bookmarklet); nothing changed." >&2
  exit 1
fi

synced=() kept=()
for sport in nhl mlb nfl nba cfb soccer; do
  if [[ " $present " == *" $sport "* ]]; then
    tmp="$dir/.${sport}_board.json.$$"
    cp -p "$latest" "$tmp"
    mv -f "$tmp" "$dir/${sport}_board.json"
    synced+=("$(tr a-z A-Z <<<"$sport")")
  else
    kept+=("$(tr a-z A-Z <<<"$sport")")
  fi
done
rm -f -- "${boards[@]}"

if (( ${#kept[@]} == 0 )); then
  echo "Boards synced for NHL, MLB, NFL, NBA, CFB, and SOCCER."
else
  echo "Boards synced for ${synced[*]}. Not in this download, previous board kept: ${kept[*]}."
fi
