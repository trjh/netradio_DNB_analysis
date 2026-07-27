#!/usr/bin/env bash
# Cross-repo DATA sync. The files this touches are the shared data files plus the derived views
# (TRACKLIST.md / SOURCES.md), each a pure render of the data:
#   * track-metadata.json   (analysis = canonical, player = mirror)  — 3-way synced
#   * subscriptions.json    (player only)                            — mirrored when it changes
#   * listen_queue          (player only)   — RETIRED here (queue-scale P3, 2026-07-27): now rides the
#                            standing `queue-data` branch via queue_sync.py / `make queue-sync`. The
#                            per-change PR below only runs as a FALLBACK when NETRADIO_QUEUE_SYNC=0.
#   * harvest-queue.json    (player data/, snapshot of analysis .harvest/queue.json)   — RETIRED here
#                            for the same reason: it rides the SAME `queue-data` branch (Tim's decision
#                            2). We still refresh the live mirror; the PR only runs when NETRADIO_QUEUE_SYNC=0.
#   * TRACKLIST.md          (analysis only)                          — regenerated from the synced
#                            track-metadata.json and included in the analysis PR (render only)
#   * SOURCES.md            (player only)                            — regenerated from the synced
#                            track-metadata.json (+ source-inventory.json) and included in the player PR
#   (QUEUE_VIEW.md — the queue's Markdown render — was retired 2026-07-24: unusable at 8k+
#    entries; renderer archived in the player repo, see its PLAN_queue_markdown.md)
# It NEVER touches source/scripts and NEVER drags unrelated commits into main: every PR is cut from
# a FRESH worktree off origin/main and contains ONLY those file(s). It never commits to main
# directly. Symmetric — `make sync` runs from EITHER repo.
#
# The final step reconciles each repo's LIVE checkout: if it sits on `main` and is strictly
# behind origin/main, it is fast-forwarded (never merged, never stashed) — so a sync run leaves
# no repo needing the stash/pull/pop dance afterwards. See reconcile_main below.
#
# The run starts with a SELF-CHECK: both repos' copies of this script must be byte-identical,
# otherwise the sync refuses to run (copies have drifted before; see the check for the fix).
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
PQ="$PLAYER/metadata/listen_queue.json"      # player-only (pre-P2: single file)
PQD="$PLAYER/metadata/listen_queue"          # player-only (P2: shard dir — shards + index.json)
PS="$PLAYER/metadata/subscriptions.json"     # player-only
HQ="$ANALYSIS/.harvest/queue.json"           # analysis harvester's live work queue (the source)
PHQ="$PLAYER/data/harvest-queue.json"        # player mirror: committed snapshot + recovery source
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

# P2 (docs/PLAN_queue_scale.md §6): the listen queue's canon becomes a DIRECTORY of shards + a
# manifest once migrated, not one file. This mirrors that whole tree onto origin/main in ONE PR (the
# shard-set + manifest must land atomically). Modeled on ensure_on_main, minus the single-file cmp
# and the derived-view machinery (the queue has no derived view — QUEUE_VIEW.md was retired). Only
# the committed files (shard-*.json + index.json) are copied; the journal + derived views under the
# dir are gitignored, so git skips them. Dormant until the split (see split_listen_queue.py).
ensure_tree_on_main() {   # $1=repo $2=reldir $3=srcdir $4=branch $5=commit-msg
  local repo="$1" rel="$2" src="$3" br="$4" msg="$5"
  git -C "$repo" fetch -q origin main
  if $DRY; then
    say "  [dry-run] $repo: would mirror tree $rel onto origin/main (branch $br)"; OUTCOME=DRY; return 0
  fi
  git -C "$repo" worktree prune
  mkdir -p "$repo/.worktree"
  local wt; wt="$(mktemp -d "$repo/.worktree/sync.XXXXXX")"
  git -C "$repo" worktree add -q -B "$br" "$wt" origin/main
  mkdir -p "$wt/$rel"
  find "$wt/$rel" -maxdepth 1 -name 'shard-*.json' -delete 2>/dev/null || true   # mirror EXACTLY:
  rm -f "$wt/$rel/index.json"                                                     # drop then re-copy
  [ -e "$src/index.json" ] && cp "$src/index.json" "$wt/$rel/"
  local f; for f in "$src"/shard-*.json; do [ -e "$f" ] && cp "$f" "$wt/$rel/"; done
  # The pre-P2 base tracks the single "$rel.json"; the split deleted it from the live working tree.
  # Stage its DELETION in THIS SAME commit so the shard tree ATOMICALLY REPLACES it — otherwise the
  # dead single file stays tracked on main alongside the shards (two canonical reps, and a tool not
  # checking manifest_exists() could read the stale one). Only fires while it's still tracked.
  if git -C "$wt" ls-files --error-unmatch -- "$rel.json" >/dev/null 2>&1; then
    git -C "$wt" rm -q -- "$rel.json"
  fi
  git -C "$wt" add -A -- "$rel"
  if git -C "$wt" diff --cached --quiet; then
    say "  $repo: origin/main already current for $rel"; OUTCOME=NOOP
  else
    git -C "$wt" commit -q -m "$msg"
    git -C "$wt" push -q -f -u origin "$br"
    local existing; existing="$( cd "$wt" && gh pr list --head "$br" --state open --json number --jq '.[0].number // empty' 2>/dev/null || true )"
    if [ -n "$existing" ]; then say "  $repo: updated open PR #$existing for $rel"
    else ( cd "$wt" && gh pr create --fill --base main ); fi
    if read -r -p "  Accept (merge) the $repo PR for $rel now? [y/N] " ans && [[ "${ans:-}" == [yY] ]]; then
      ( cd "$wt" && gh pr merge --merge ) || true
      git -C "$wt" fetch -q origin main
      # Confirm the WHOLE atomic migration commit landed: the shard tree matches AND the legacy single
      # file is ABSENT on origin/main. Tree-only would falsely pass if main happened to match the
      # shards while STILL tracking "$rel.json" (a failed merge + an independently-current tree) —
      # declaring MERGED and deleting the branch while origin/main keeps two canonical reps. In
      # steady-state post-migration the file is already absent, so the check is trivially true.
      if git -C "$wt" diff --quiet FETCH_HEAD -- "$rel" \
         && ! git -C "$wt" cat-file -e "FETCH_HEAD:$rel.json" 2>/dev/null; then   # tree + no legacy
        say "  merged ✓ (confirmed on origin/main)"; OUTCOME=MERGED
        git -C "$repo" push -q origin --delete "$br" 2>/dev/null || true
      else say "  merge NOT fully confirmed on origin/main — left pending"; OUTCOME=OPEN; fi
    else say "  left open for review."; OUTCOME=OPEN; fi
  fi
  git -C "$repo" worktree remove --force "$wt" >/dev/null 2>&1 || rm -rf "$wt"
  git -C "$repo" worktree prune
  git -C "$repo" branch -D "$br" >/dev/null 2>&1 || true
}

landed() { [ "$1" = NOOP ] || [ "$1" = MERGED ]; }   # is the content durably on main?

# Echo the resolved repo paths up front so it's always clear which checkouts are in play.
say "NETRADIO_ANALYSIS_REPO=$ANALYSIS"
say "NETRADIO_PLAYER_REPO=$PLAYER"

# --- self-check: this script must be byte-identical in both repos ---------------------------
# Each repo carries a copy of scripts/tracklist_sync.sh, and copies drift silently (it
# happened: the analysis copy sat weeks behind, missing the QUEUE_VIEW.md derive). Refuse to
# run on divergent copies — data synced by two different versions of the sync is worse than a
# blocked run. Fix = copy the version you just edited over the other and PR it in BOTH repos.
SELF_P="$PLAYER/scripts/tracklist_sync.sh"
SELF_A="$ANALYSIS/scripts/tracklist_sync.sh"
if ! cmp -s "$SELF_P" "$SELF_A"; then
  {
    say "ERROR: scripts/tracklist_sync.sh differs between the two repos:"
    diff -u "$SELF_A" "$SELF_P" | head -40 || true
    say ""
    say "Copy the version you just edited over the other, PR it in BOTH repos, then re-run:"
    say "  cp '$SELF_P' '$SELF_A'    # player copy wins"
    say "  cp '$SELF_A' '$SELF_P'    # analysis copy wins"
  } >&2
  exit 1
fi
say "self-check: tracklist_sync.sh identical in both repos ✓"

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
# Each repo regenerates its own derived view from the synced track-metadata.json and includes it in
# the same PR: analysis owns TRACKLIST.md (what's in the stream), the player owns SOURCES.md (what
# I have on disk; `make sources` enriches source-inventory.json then renders SOURCES.md). The derive
# only runs when track-metadata.json actually changed, so neither view churns a spurious PR.
ensure_on_main "$ANALYSIS" "track-metadata.json"          "$wsrc" "sync/track-metadata" \
  "data: sync track-metadata.json + regen TRACKLIST.md" \
  "make tracklist >/dev/null" "TRACKLIST.md"; oa="$OUTCOME"
ensure_on_main "$PLAYER"   "metadata/track-metadata.json" "$wsrc" "sync/track-metadata" \
  "data: sync track-metadata.json + regen SOURCES.md" \
  "make sources >/dev/null" "SOURCES.md" "metadata/source-inventory.json"; op="$OUTCOME"

# Advance the marker ONLY when the winner is durably on both mains.
if landed "$oa" && landed "$op"; then
  $DRY || printf '%s\n' "$whash" > "$MARKER"
  say "track-metadata.json fully synced (analysis=$oa player=$op; marker -> ${whash:0:12})."
else
  say "track-metadata.json NOT fully landed (analysis=$oa player=$op) — marker unchanged; re-run after merging the open PR(s)."
fi

# --- listen_queue (player-only): land it on the player main when it differs ---
# RETIRED (queue-scale P3, 2026-07-27): the listen queue now rides the standing `queue-data` branch,
# committed every ~15 min + pushed hourly by the running player (queue_sync.py) and squash-merged by
# Tim via `make queue-sync`. This per-change PR section only runs as a FALLBACK when the queue-data
# automation is disabled (NETRADIO_QUEUE_SYNC=0). (QUEUE_VIEW.md — the old derived extra — retired
# 2026-07-24.) P2: post-migration the canon is the shard dir + manifest ($PQD/index.json is the
# migration marker); pre-migration it is the single $PQ. Mirror whichever is the live canon.
if [ "${NETRADIO_QUEUE_SYNC:-1}" = "0" ]; then
  if [ -f "$PQD/index.json" ]; then
    ensure_tree_on_main "$PLAYER" "metadata/listen_queue" "$PQD" "sync/listen-queue" \
      "data: sync listen_queue shards"
    say "listen_queue shards: $OUTCOME"
  elif [ -f "$PQ" ]; then
    ensure_on_main "$PLAYER" "metadata/listen_queue.json" "$PQ" "sync/listen-queue" \
      "data: sync listen_queue.json"
    say "listen_queue.json: $OUTCOME"
  fi
else
  say "listen_queue: handled by the queue-data branch (make queue-sync) — old PR flow skipped"
fi

# --- subscriptions.json (player-only): land it on the player main when it differs -----------
# Same treatment as listen_queue.json (no derived view). Before this section existed, local
# subscribe/unsubscribe edits only reached main via hand commits — and were silently lost when
# a working copy was rebuilt (a whole channel subscription went missing that way once).
if [ -f "$PS" ]; then
  ensure_on_main "$PLAYER" "metadata/subscriptions.json" "$PS" "sync/subscriptions" \
    "data: sync subscriptions.json"
  say "subscriptions.json: $OUTCOME"
fi

# --- harvest-queue.json (analysis .harvest/queue.json -> player data/, committed snapshot) ---
# The harvester's work queue { pending, done } lives in the analysis repo's gitignored .harvest/
# state dir, so a wiped Mac loses `done` — the record of which URLs have been fetched, INCLUDING
# the failures (too-short/404/blocked), which leave no trace in the signature pool. `done` is the
# one .harvest file not rebuildable from the pool, so we snapshot it into the player's committed
# data/ for progress history + disaster recovery. The live harvester stays the source of truth;
# this is a one-way mirror (analysis -> player), landed only when it differs from the player main.
# RETIRED (queue-scale P3, 2026-07-27): data/harvest-queue.json now rides the SAME `queue-data` branch
# as the listen-queue shards (Tim's decision 2) — committed + pushed by queue_sync.py, squash-merged
# via `make queue-sync`. We still refresh the player's live mirror ($PHQ) from the harvester here so
# the running player has current data on disk for queue_sync to commit; the per-change PR only runs as
# a FALLBACK when the queue-data automation is disabled (NETRADIO_QUEUE_SYNC=0).
if [ -f "$HQ" ]; then
  $DRY || { mkdir -p "$(dirname "$PHQ")"; cp "$HQ" "$PHQ"; }   # keep the live mirror in step (always)
  if [ "${NETRADIO_QUEUE_SYNC:-1}" = "0" ]; then
    ensure_on_main "$PLAYER" "data/harvest-queue.json" "$HQ" "sync/harvest-queue" \
      "data: sync harvest-queue.json"
    say "harvest-queue.json: $OUTCOME"
  else
    say "harvest-queue.json: refreshed live mirror; commit handled by the queue-data branch (make queue-sync)"
  fi
fi

# --- reconcile the LIVE checkouts: fast-forward main to origin/main -------------------------
# ensure_on_main lands data via throwaway worktrees and never touches the live checkout, so
# local `main` falls behind origin/main after every merged sync PR — and pulling by hand under
# a RUNNING player is the stash/pull/pop trap (a stash-pop conflict on listen_queue.json makes
# the store load an empty queue, and the next save clobbers the file). This step removes that
# chore: a live checkout sitting on `main`, strictly behind origin/main, is fast-forwarded.
# Dirty LIVE data files (the running player rewrites listen_queue.json continuously) are set
# aside verbatim and put back after the ff — never stashed, never merged — so the newest local
# data always survives on disk and the next sync run PRs whatever delta remains. Nothing is
# silenced (the worktree rule): any git failure is shown, and the live files are restored
# before moving on. A diverged main (local commits origin lacks) is reported, not rewritten.
reconcile_main() {   # $1=repo-path  $2=label  [rel paths of live data files to set aside...]
  local repo="$1" label="$2"; shift 2
  git -C "$repo" fetch -q origin main
  local head origin cur
  head="$(git -C "$repo" rev-parse HEAD)"; origin="$(git -C "$repo" rev-parse origin/main)"
  if [ "$head" = "$origin" ]; then say "  $label: main already at origin/main"; return 0; fi
  cur="$(git -C "$repo" symbolic-ref --short -q HEAD || echo '(detached)')"
  if [ "$cur" != main ]; then say "  $label: checked out on '$cur' — leaving main alone"; return 0; fi
  if ! git -C "$repo" merge-base --is-ancestor "$head" "$origin"; then
    say "  $label: main has commits origin/main lacks — not rewriting; reconcile by hand"; return 0
  fi
  local n; n="$(git -C "$repo" rev-list --count "$head..$origin")"
  if $DRY; then say "  [dry-run] $label: would fast-forward main $n commit(s) to origin/main"; return 0; fi
  local f aside=()
  for f in "$@"; do
    # P2: a pathspec may be the shard DIRECTORY (metadata/listen_queue), not just a file — so guard
    # with -e and back up with `cp -a` (recursive). `git diff/checkout -- <dir>` already handle a
    # dir pathspec and only touch TRACKED files (the gitignored journal/derived views are left be).
    # The -e guard also stops the deleted single listen_queue.json from RESURRECTING at migration
    # time: once the split removes it, it is not -e, so its `git checkout HEAD -- $f` never runs (the
    # ensure_tree_on_main sync commit is what carries its deletion onto main).
    if [ -e "$repo/$f" ] && ! git -C "$repo" diff --quiet HEAD -- "$f"; then
      rm -rf "$repo/$f.reconcile-bak"
      cp -a "$repo/$f" "$repo/$f.reconcile-bak"   # the live bytes, restored below (gitignored)
      git -C "$repo" checkout HEAD -- "$f"        # clean worktree AND index for this file/dir
      aside+=("$f")
    fi
  done
  local ok=true
  git -C "$repo" merge --ff-only origin/main || ok=false
  for f in ${aside[@]+"${aside[@]}"}; do rm -rf "$repo/$f"; mv "$repo/$f.reconcile-bak" "$repo/$f"; done
  if $ok; then say "  $label: main fast-forwarded $n commit(s) (live data kept: ${aside[*]:-none})"
  else say "  $label: fast-forward FAILED (above) — live files restored; reconcile by hand"; fi
}

say "reconciling live checkouts (fast-forward main -> origin/main):"
reconcile_main "$ANALYSIS" analysis "track-metadata.json" "TRACKLIST.md"
reconcile_main "$PLAYER"   player \
  "metadata/track-metadata.json" "metadata/listen_queue.json" "metadata/listen_queue" \
  "metadata/subscriptions.json" \
  "metadata/source-inventory.json" "SOURCES.md" "data/harvest-queue.json"
say "sync done."
