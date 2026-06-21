#!/usr/bin/env bash
# Sanity check: the canonical analysis `track-metadata.json` and the player's mirror
# (`metadata/track-metadata.json`) must not have diverged. Compared by NORMALISED JSON
# (sorted keys, no whitespace) so formatting/key-order differences don't trip a false alarm.
#
# Paths come from the environment (no hardcoded absolute paths):
#   NETRADIO_ANALYSIS_REPO  -> the analysis repo working copy (canonical)
#   NETRADIO_PLAYER_REPO    -> the player repo working copy (mirror)
# Exit 0 = in sync · 1 = diverged · 2 = a file/var is missing.
set -euo pipefail

ANALYSIS="${NETRADIO_ANALYSIS_REPO:?set NETRADIO_ANALYSIS_REPO to the analysis repo path}"
PLAYER="${NETRADIO_PLAYER_REPO:?set NETRADIO_PLAYER_REPO to the player repo path}"
A="$ANALYSIS/track-metadata.json"
P="$PLAYER/metadata/track-metadata.json"

for f in "$A" "$P"; do
  [ -f "$f" ] || { echo "sanity: MISSING $f" >&2; exit 2; }
done

norm() {
  python3 -c 'import json,sys;print(json.dumps(json.load(open(sys.argv[1])),sort_keys=True,separators=(",",":")))' "$1"
}

if [ "$(norm "$A")" = "$(norm "$P")" ]; then
  echo "sanity: track-metadata.json in sync ✓"
else
  echo "sanity: track-metadata.json DIVERGED between repos ✗" >&2
  echo "  canonical (analysis): $A" >&2
  echo "  mirror    (player):   $P" >&2
  echo "  -> run the player's 'make tracklist-sync' to mirror the canonical copy," >&2
  echo "     or publish the player's curation via the analysis 'make tracklist-pr'." >&2
  exit 1
fi
