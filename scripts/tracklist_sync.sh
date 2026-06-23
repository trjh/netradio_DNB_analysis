#!/usr/bin/env bash
# Cross-repo DATA sync. The files this touches are the shared data files plus TRACKLIST.md, which
# is a pure render of the data:
#   * track-metadata.json   (analysis = canonical, player = mirror)  — 3-way synced
#   * listen_queue.json     (player only)                            — mirrored when it changes
#   * TRACKLIST.md          (analysis only)                          — regenerated from the synced
#                            track-metadata.json and included in the analysis PR (render only)
# It NEVER touches source/scripts and NEVER drags unrelated commits into main: every PR is cut from
# a FRESH worktree off origin/main and contains ONLY those file(s). It never commits to main
# directly. Symmetric — `make sync` runs from EITHER repo.
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
ensure_on_main() {   # $1=repo $2=rel $3=src $4=branch $5=commit-msg [$6=derive-cmd] [extra rel...]
  local repo="$1" rel="$2" src="$3" br="$4" msg="$5"; shift 5
  local derive="${1-}"; [ "$#" -gt 0 ] && shift   # optional: a command run IN the worktree after
  local extras=("$@")                              # the copy (e.g. regen TRACKLIST.md), then the
  git -C "$repo" fetch -q origin main              # extra files it produces are added to the PR too.
  if $DRY; then
    local also=""; [ -n "$derive" ] && also=" (+ regen ${extras[*]})"
    if git -C "$repo" show "origin/main:$rel" 2>/dev/null | cmp -s - "$src"; then
      say "  [dry-run] $repo: origin/main already current for $rel"; OUTCOME=NOOP
    else
      say "  [dry-run] $repo: would PR $rel$also onto origin/main (branch $br)"; OUTCOME=DRY
    fi
    return 0
  fi
  git -C "$repo" worktree prune
  mkdir -p "$repo/.worktree"                                  # keep throwaway worktrees in-repo (gitignored)
  local wt; wt="$(mktemp -d "$repo/.worktree/sync.XXXXXX")"
  git -C "$repo" worktree add -q -B "$br" "$wt" origin/main   # -B: create-or-reset the stable branch
  mkdir -p "$wt/$(dirname "$rel")"; cp "$src" "$wt/$rel"
  git -C "$wt" add -- "$rel"
  if git -C "$wt" diff --cached --quiet; then
    # The primary file is already on origin/main. Do NOTHING — in particular do not regenerate the
    # derived extras (e.g. TRACKLIST.md carries a 'generated <timestamp>' line, so regenerating it
    # unconditionally would churn a spurious PR every run). Derived files only follow a real change.
    say "  $repo: origin/main already current for $rel"; OUTCOME=NOOP
  else
    # Primary file changed -> regenerate the derived extras from it and include them in this PR.
    # A failed derive is FATAL (not a warning): committing the changed data with a stale extra
    # would let OUTCOME become MERGED / the marker advance, after which later runs go NOOP and
    # never regenerate the stale extra. Abort so the data isn't landed without its derived file.
    local x
    if [ -n "$derive" ] && ! ( cd "$wt" && eval "$derive" ); then
      say "  ERROR: derive failed in $repo ($derive); refusing to land $rel with a stale ${extras[*]}." >&2
      git -C "$repo" worktree remove --force "$wt" >/dev/null 2>&1 || rm -rf "$wt"; git -C "$repo" worktree prune
      exit 1
    fi
    # Verify every declared extra was actually produced (non-empty) before staging/committing.
    if [ "${#extras[@]}" -gt 0 ]; then
      for x in "${extras[@]}"; do
        [ -s "$wt/$x" ] || { say "  ERROR: derive did not produce $x in $repo; aborting." >&2
          git -C "$repo" worktree remove --force "$wt" >/dev/null 2>&1 || rm -rf "$wt"; git -C "$repo" worktree prune; exit 1; }
      done
      git -C "$wt" add -- "${extras[@]}"
    fi
    git -C "$wt" commit -q -m "$msg"
    git -C "$wt" push -q -f -u origin "$br"
    local existing; existing="$( cd "$wt" && gh pr list --head "$br" --state open --json number --jq '.[0].number // empty' 2>/dev/null || true )"
    if [ -n "$existing" ]; then say "  $repo: updated open PR #$existing for $rel"
    else ( cd "$wt" && gh pr create --fill --base main ); fi
    if read -r -p "  Accept (merge) the $repo PR for $rel now? [y/N] " ans && [[ "${ans:-}" == [yY] ]]; then
      # NOTE: no `gh pr merge --delete-branch` — it tries to switch THIS worktree to `main` after
      # merging, which fails ("'main' is already used by worktree") since main is the live checkout.
      ( cd "$wt" && gh pr merge --merge ) || true
      # Trust origin/main, not gh's exit code: MERGED only if the primary AND every declared extra
      # are actually on origin/main now. Anything short of that stays OPEN and the next run re-PRs.
      git -C "$repo" fetch -q origin main
      local landed=yes
      git -C "$repo" show "origin/main:$rel" 2>/dev/null | cmp -s - "$src" || landed=no
      if [ "$landed" = yes ] && [ "${#extras[@]}" -gt 0 ]; then
        for x in "${extras[@]}"; do
          git -C "$repo" show "origin/main:$x" 2>/dev/null | cmp -s - "$wt/$x" || landed=no
        done
      fi
      if [ "$landed" = yes ]; then
        say "  merged ✓ (confirmed on origin/main)"; OUTCOME=MERGED
        # Only NOW is it safe to delete the merged remote branch (recreated next run). Never delete
        # it while OUTCOME=OPEN — that would orphan a still-open PR whose merge didn't land.
        git -C "$repo" push -q origin --delete "$br" 2>/dev/null || true
      else say "  merge NOT fully confirmed on origin/main — left pending"; OUTCOME=OPEN; fi
    else say "  left open for review."; OUTCOME=OPEN; fi
  fi
  git -C "$repo" worktree remove --force "$wt" >/dev/null 2>&1 || rm -rf "$wt"
  git -C "$repo" worktree prune
  git -C "$repo" branch -D "$br" >/dev/null 2>&1 || true   # local stable branch is free once the worktree is gone
}

landed() { [ "$1" = NOOP ] || [ "$1" = MERGED ]; }   # is the content durably on main?

# Echo the resolved repo paths up front so it's always clear which checkouts are in play.
say "NETRADIO_ANALYSIS_REPO=$ANALYSIS"
say "NETRADIO_PLAYER_REPO=$PLAYER"

# --- track-metadata.json: 3-way sync between the canonical (analysis) and the mirror (player) ---
ha=$(nhash "$A"); hp=$(nhash "$P")
base=""; [ -f "$MARKER" ] && base="$(cat "$MARKER")"
say "track-metadata.json  analysis=${ha:0:12}  player=${hp:0:12}  base=${base:0:12}"

# Pick the winning content (the file whose bytes both repos should converge on).
wsrc=""; whash=""
if [ "$ha" = "$hp" ]; then
  wsrc="$A"; whash="$ha"; say "working copies already equal -> verifying both mains hold it"
else
  if [ -z "$base" ]; then
    {
      say "ERROR: track-metadata.json copies differ and there is no baseline marker yet."
      say "  marker: $MARKER"
      say ""
      say "Bootstrap it once by recording the baseline hash, then re-run \`make sync\`. Pick one:"
      say ""
      say "  # keep the PLAYER copy (it wins; analysis is updated to match):"
      say "      printf '%s\\n' $ha > '$MARKER'"
      say ""
      say "  # keep the ANALYSIS copy (it wins; the player mirror is updated to match):"
      say "      printf '%s\\n' $hp > '$MARKER'"
      say ""
      say "(Each command records the OTHER copy's hash as the last-synced baseline, so the copy"
      say " you want to keep is detected as the newer one and propagated to both repos.)"
    } >&2
    exit 1
  fi
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
# whether the PRs merge now or later). SAFETY: before overwriting a working copy whose content
# differs from the winner, stash a timestamp-free backup next to it (gitignored). The 3-way marker
# can't tell "newer" from "reverted to older", so a regressed side could otherwise silently clobber
# good local edits (this happened once); the backup makes any such loss trivially recoverable.
reconcile_to() {   # $1=winner-src  $2=dest working copy
  [ "$1" = "$2" ] && return 0
  if [ -f "$2" ] && ! cmp -s "$1" "$2"; then
    cp "$2" "$2.presync-bak"
    say "  saved pre-sync backup: $2.presync-bak (before updating it to the sync winner)"
  fi
  cp "$1" "$2"
}
if ! $DRY; then
  reconcile_to "$wsrc" "$A"
  reconcile_to "$wsrc" "$P"
fi

# Land the winner on BOTH repos' origin/main (stable branches -> reused PRs).
# Analysis is canonical AND owns TRACKLIST.md (a pure render of track-metadata.json), so its PR
# regenerates and includes TRACKLIST.md alongside the data. The player has no TRACKLIST.md.
ensure_on_main "$ANALYSIS" "track-metadata.json"          "$wsrc" "sync/track-metadata" \
  "data: sync track-metadata.json + regen TRACKLIST.md" \
  "make tracklist >/dev/null" "TRACKLIST.md"; oa="$OUTCOME"
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
