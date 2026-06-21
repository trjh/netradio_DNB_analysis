#!/usr/bin/env bash
# Cross-repo tracklist sync. `track-metadata.json` lives in BOTH repos (analysis = canonical,
# player = mirror) and `listen_queue.json` lives in the player. This keeps them in sync via
# PRs — it NEVER commits to main directly — with 3-way conflict detection against a baseline
# marker (the sha256 of the content at the last successful sync). Symmetric: `make sync` runs
# this in EITHER repo and it figures out the direction itself.
#
#   copies equal            -> nothing
#   only player changed      -> branch+PR the player's version INTO analysis (+ regen TRACKLIST.md)
#   only analysis changed    -> update the player's copy from analysis
#   both changed since sync   -> report a conflict and exit (touch nothing)
# Then: PR any changed player data (track-metadata.json / listen_queue.json / marker).
# `TRACKLIST.md` is regenerated (offline, from the stored fields) whenever track-metadata.json
# changes on the analysis side. Enriching artwork_url/full_page_url is a separate step
# (`make tracklist`), so this sync never touches the network.
#
# Env (no hardcoded paths):  NETRADIO_ANALYSIS_REPO  NETRADIO_PLAYER_REPO
# Usage:  tracklist_sync.sh [--dry-run]
set -euo pipefail

DRY=false; [ "${1:-}" = "--dry-run" ] && DRY=true
ANALYSIS="${NETRADIO_ANALYSIS_REPO:?set NETRADIO_ANALYSIS_REPO to the analysis repo path}"
PLAYER="${NETRADIO_PLAYER_REPO:?set NETRADIO_PLAYER_REPO to the player repo path}"
A="$ANALYSIS/track-metadata.json"
P="$PLAYER/metadata/track-metadata.json"
MARKER="$PLAYER/metadata/.track-metadata.synced"

say() { printf '%s\n' "$*"; }
do_or_show() { if $DRY; then say "  [dry-run] $*"; else eval "$*"; fi; }

nhash() { python3 -c 'import json,sys,hashlib;print(hashlib.sha256(json.dumps(json.load(open(sys.argv[1])),sort_keys=True,separators=(",",":")).encode()).hexdigest())' "$1"; }
stamp() { date +%Y%m%d-%H%M%S; }

offer_merge() {  # $1 = repo dir
  if $DRY; then say "  [dry-run] would offer to merge the PR in $1"; return; fi
  read -r -p "  Accept (merge) this PR now? [y/N] " ans
  if [[ "${ans:-}" == [yY] ]]; then ( cd "$1" && gh pr merge --merge --delete-branch ) && say "  merged ✓"
  else say "  left open for review."; fi
}

make_pr() {  # $1=repo  $2=branch  $3=message  $4=space-separated files
  local repo="$1" br="$2" msg="$3" files="$4"
  do_or_show "( cd '$repo' && git checkout -b '$br' && git add $files && git commit -m '$msg' && git push -u origin '$br' && gh pr create --fill --base main )"
  offer_merge "$repo"
  do_or_show "( cd '$repo' && git checkout main )"
}

changed_in() {  # $1=repo $2=path -> 0 if working-tree change (tracked or untracked)
  [ -n "$(cd "$1" && git status --porcelain -- "$2")" ]
}

ha=$(nhash "$A"); hp=$(nhash "$P")
base=""; [ -f "$MARKER" ] && base="$(cat "$MARKER")"
say "track-metadata.json  analysis=${ha:0:12}  player=${hp:0:12}  base=${base:0:12}"

if [ "$ha" = "$hp" ]; then
  say "in sync — no track-metadata.json transfer needed."
  [ "$base" = "$ha" ] || do_or_show "printf '%s\n' '$ha' > '$MARKER'"
else
  [ -n "$base" ] || { say "ERROR: copies differ and no baseline marker exists ($MARKER)." >&2
                      say "Reconcile the two copies by hand once, write the agreed sha256 to the marker, then re-run." >&2
                      exit 1; }
  pc=false; ac=false
  [ "$hp" != "$base" ] && pc=true
  [ "$ha" != "$base" ] && ac=true
  if $pc && $ac; then
    say "CONFLICT: BOTH track-metadata.json copies changed since the last sync. Reconcile by hand; exiting." >&2
    exit 1
  elif $pc; then
    say "player copy is newer -> publishing to analysis"
    do_or_show "cp '$P' '$A'"
    do_or_show "( cd '$ANALYSIS' && python3 scripts/render_tracklist.py --no-resolve >/dev/null )"
    make_pr "$ANALYSIS" "tracklist-sync-$(stamp)" "tracklist: update track-metadata.json from player + regen TRACKLIST.md" "track-metadata.json TRACKLIST.md"
    do_or_show "printf '%s\n' '$hp' > '$MARKER'"
  else
    say "analysis copy is newer -> updating the player mirror"
    do_or_show "cp '$A' '$P'"
    do_or_show "( cd '$ANALYSIS' && python3 scripts/render_tracklist.py --no-resolve >/dev/null )"
    if ! $DRY && changed_in "$ANALYSIS" TRACKLIST.md; then
      make_pr "$ANALYSIS" "tracklist-regen-$(stamp)" "tracklist: regen TRACKLIST.md" "TRACKLIST.md"
    fi
    do_or_show "printf '%s\n' '$ha' > '$MARKER'"
  fi
fi

# --- player-side: PR any changed data (track-metadata.json / listen_queue.json / marker) ---
if $DRY; then
  say "  [dry-run] would PR any changed player data files (track-metadata.json / listen_queue.json / marker)"
else
  files=""
  for f in metadata/track-metadata.json metadata/listen_queue.json metadata/.track-metadata.synced; do
    changed_in "$PLAYER" "$f" && files="$files $f"
  done
  if [ -n "$files" ]; then
    make_pr "$PLAYER" "data-sync-$(stamp)" "data: sync track-metadata / listen_queue" "$files"
  else
    say "player repo: no data changes to commit."
  fi
fi
say "sync done."
