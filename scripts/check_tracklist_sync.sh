#!/usr/bin/env bash
# Sanity check: the canonical `track-metadata.json` and the peer checkout's mirror
# (`metadata/track-metadata.json`) must not have diverged. Compared by NORMALISED JSON
# (sorted keys, no whitespace) so formatting/key-order differences don't trip a false alarm.
#
# Paths come from the environment (no hardcoded absolute paths):
#   NETRADIO_ANALYSIS_REPO  -> this repo's working copy (canonical)
#   NETRADIO_PLAYER_REPO    -> the peer checkout (mirror)
# Exit 0 = in sync · 1 = diverged · 2 = a file/var is missing.
set -euo pipefail

ANALYSIS="${NETRADIO_ANALYSIS_REPO:?set NETRADIO_ANALYSIS_REPO to the analysis repo path}"
PEER="${NETRADIO_PLAYER_REPO:?set NETRADIO_PLAYER_REPO to the peer checkout path}"
A="$ANALYSIS/track-metadata.json"
P="$PEER/metadata/track-metadata.json"

for f in "$A" "$P"; do
  [ -f "$f" ] || { echo "sanity: MISSING $f" >&2; exit 2; }
done

norm() {
  python3 -c 'import json,sys;print(json.dumps(json.load(open(sys.argv[1])),sort_keys=True,separators=(",",":")))' "$1"
}

if [ "$(norm "$A")" = "$(norm "$P")" ]; then
  echo "sanity: track-metadata.json in sync ✓"
else
  echo "sanity: track-metadata.json DIVERGED from the mirror ✗" >&2
  echo "  canonical: $A" >&2
  echo "  mirror:    $P" >&2
  echo "  -> run the metadata sync from the peer checkout to bring the two in step." >&2
  exit 1
fi
