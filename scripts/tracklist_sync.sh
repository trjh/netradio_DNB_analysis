#!/usr/bin/env bash
# Cross-repo DATA sync. The ONLY files this ever touches are the shared data files:
#   * track-metadata.json   (analysis = canonical, player = mirror)  — 3-way synced
#   * listen_queue.json     (player only)                            — mirrored when it changes
# It NEVER touches anything else (no TRACKLIST.md, no source/scripts) and NEVER drags unrelated
# commits into main: every PR is cut from a FRESH worktree off origin/main and contains ONLY the
# data file(s). It never commits to main directly. Symmetric — `make sync` runs from EITHER repo.
#
#   track-metadata.json copies equal      -> verify each repo's main has it (re-PR any that lag)
#   only player changed since baseline     -> winner = player; land it on BOTH mains
#   only analysis changed since baseline   -> winner = analysis; land it on BOTH mains
#   both changed since baseline            -> report a conflict and exit (touch nothing)
#
# DURABILITY: the local (gitignored) baseline marker advances ONLY once the winning content is
# actually present on BOTH repos' origin/main (a PR was merged, or main was already current) —
# never on a merely-opened/declined PR. Each run re-checks origin/main and recreates a missing
# PR (on a STABLE per-target branch, so re-runs reuse the same PR instead of duplicating) until
# it lands. The winning copy is also written into both working trees so local state is consistent.
# TRACKLIST.md is NOT part of sync — regenerate it separately (render_tracklist.py / make tracklist).
#
# Env (no hardcoded paths):  NETRADIO_ANALYSIS_REPO  NETRADIO_PLAYER_REPO
# Usage:  tracklist_sync.sh [--dry-run]
set -euo pipefail

DRY=false; [ "${1:-}" = "--dry-run" ] && DRY=true
ANALYSIS="${NETRADIO_ANALYSIS_REPO:?set NETRADIO_ANALYSIS_REPO to the analysis repo path}"
PLAYER="${NETRADIO_PLAYER_REPO:?set NETRADIO_PLAYER_REPO to the player repo path}"
A="$ANALYSIS/track-metadata.json"            # canonical
P="$PLAYER/metadata/track-metadata.json"     # mirror
PQ="$PLAYER/metadata/listen_queue.json"      # player-only
MARKER="$PLAYER/metadata/.track-metadata.synced"   # LOCAL baseline (gitignored, never PR'd)

say() { printf '%s\n' "$*"; }
nhash() { python3 -c 'import json,sys,hashlib;print(hashlib.sha256(json.dumps(json.load(open(sys.argv[1])),sort_keys=True,separators=(",",":")).encode()).hexdigest())' "$1"; }

# Ensure $repo's origin/main holds the content of file $src at path $rel. Sets the global OUTCOME:
#   NOOP   - origin/main already byte-identical (nothing to do)
#   MERGED - opened/reused a PR and merged it just now
#   OPEN   - a PR is open and was left unmerged (data NOT on main yet)
#   DRY    - dry-run; no action taken
# The PR is cut from a throwaway worktree off origin/main on a STABLE per-target branch, so it can
# never inherit the current branch's commits and re-runs reuse the same PR instead of duplicating.
OUTCOME=""
ensure_on_main() {   # $1=repo  $2=rel  $3=src  $4=branch  $5=commit-msg
  local repo="$1" rel="$2" src="$3" br="$4" msg="$5"
  git -C "$repo" fetch -q origin main
  if $DRY; then
    if git -C "$repo" show "origin/main:$rel" 2>/dev/null | cmp -s - "$src"; then
      say "  [dry-run] $repo: origin/main already current for $rel"; OUTCOME=NOOP
    else
      say "  [dry-run] $repo: would PR $rel onto origin/main (branch $br)"; OUTCOME=DRY
    fi
    return 0
  fi
  git -C "$repo" worktree prune
  local wt; wt="$(mktemp -d)"
  git -C "$repo" worktree add -q -B "$br" "$wt" origin/main   # -B: create-or-reset the stable branch
  mkdir -p "$wt/$(dirname "$rel")"; cp "$src" "$wt/$rel"
  git -C "$wt" add -- "$rel"
  if git -C "$wt" diff --cached --quiet; then
    say "  $repo: origin/main already current for $rel"; OUTCOME=NOOP
  else
    git -C "$wt" commit -q -m "$msg"
    git -C "$wt" push -q -f -u origin "$br"
    local existing; existing="$( cd "$wt" && gh pr list --head "$br" --state open --json number --jq '.[0].number // empty' 2>/dev/null || true )"
    if [ -n "$existing" ]; then say "  $repo: updated open PR #$existing for $rel"
    else ( cd "$wt" && gh pr create --fill --base main ); fi
    if read -r -p "  Accept (merge) the $repo PR for $rel now? [y/N] " ans && [[ "${ans:-}" == [yY] ]]; then
      ( cd "$wt" && gh pr merge --merge --delete-branch ) || true
      # Trust origin/main, not gh's exit code: OUTCOME is MERGED only if the content is actually
      # on origin/main now. If anything went wrong it stays OPEN and the next run re-PRs it.
      git -C "$repo" fetch -q origin main
      if git -C "$repo" show "origin/main:$rel" 2>/dev/null | cmp -s - "$src"; then
        say "  merged ✓ (confirmed on origin/main)"; OUTCOME=MERGED
      else say "  merge NOT confirmed on origin/main — left pending"; OUTCOME=OPEN; fi
    else say "  left open for review."; OUTCOME=OPEN; fi
  fi
  git -C "$repo" worktree remove --force "$wt" >/dev/null 2>&1 || rm -rf "$wt"
  git -C "$repo" worktree prune
}

landed() { [ "$1" = NOOP ] || [ "$1" = MERGED ]; }   # is the content durably on main?

# --- track-metadata.json: 3-way sync between the canonical (analysis) and the mirror (player) ---
ha=$(nhash "$A"); hp=$(nhash "$P")
base=""; [ -f "$MARKER" ] && base="$(cat "$MARKER")"
say "track-metadata.json  analysis=${ha:0:12}  player=${hp:0:12}  base=${base:0:12}"

# Pick the winning content (the file whose bytes both repos should converge on).
wsrc=""; whash=""
if [ "$ha" = "$hp" ]; then
  wsrc="$A"; whash="$ha"; say "working copies already equal -> verifying both mains hold it"
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
  elif $pc; then wsrc="$P"; whash="$hp"; say "player copy is newer -> winner = player"
  else           wsrc="$A"; whash="$ha"; say "analysis copy is newer -> winner = analysis"
  fi
fi

# Reconcile both live working copies to the winner (keeps local state consistent regardless of
# whether the PRs merge now or later).
if ! $DRY; then
  [ "$wsrc" = "$A" ] || cp "$wsrc" "$A"
  [ "$wsrc" = "$P" ] || cp "$wsrc" "$P"
fi

# Land the winner on BOTH repos' origin/main (stable branches -> reused PRs).
ensure_on_main "$ANALYSIS" "track-metadata.json"          "$wsrc" "sync/track-metadata" "data: sync track-metadata.json"; oa="$OUTCOME"
ensure_on_main "$PLAYER"   "metadata/track-metadata.json" "$wsrc" "sync/track-metadata" "data: sync track-metadata.json"; op="$OUTCOME"

# Advance the marker ONLY when the winner is durably on both mains.
if landed "$oa" && landed "$op"; then
  $DRY || printf '%s\n' "$whash" > "$MARKER"
  say "track-metadata.json fully synced (analysis=$oa player=$op; marker -> ${whash:0:12})."
else
  say "track-metadata.json NOT fully landed (analysis=$oa player=$op) — marker unchanged; re-run after merging the open PR(s)."
fi

# --- listen_queue.json (player-only): land it on the player main when it differs ---
if [ -f "$PQ" ]; then
  ensure_on_main "$PLAYER" "metadata/listen_queue.json" "$PQ" "sync/listen-queue" "data: sync listen_queue.json"
  say "listen_queue.json: $OUTCOME"
fi
say "sync done."
