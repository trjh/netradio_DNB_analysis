# Loop decisions log

Append-only record of the autonomous choices/assumptions I make while running
`AUTONOMOUS_PLAN.md`, for Tim to review at the end. Newest at the bottom.

## 2026-06-14 — loop started (replaces the generic autonomous check)

- Tim said "go". Working G0→G1→G2→G3→G4 per the plan; pushing through all
  non-destructive decisions and logging them here.
- **G0 layout:** the clip player is `player/public/clips.html` (a standalone page
  served by the existing player http.server, separate from the main player UI).
  Clips + a `manifest.json` live in `player/public/clips/` (gitignored — generated
  audio), fetched at `/clips/manifest.json`. Rationale: reuses static serving, no
  server change, keeps generated audio out of git.
- **Clip manifest format:** `{ "clips": [ { id, audio, title, description,
  duration, annotations: [{t, label}] } ] }`. The generator (G1) appends to it.
- **Clip generator location:** `scripts/streamalign/clips.py` in the analysis repo
  (it needs the engine + capture audio); writes clips + manifest to an output dir
  (default the player's `public/clips/`).

## 2026-06-14 — G0 done

- Clip review player built (`player/public/clips.html`), verified in headless
  Chrome, **passed local-review**, opened as player **PR #25**. Includes the seek
  slider + variable speed Tim asked for, the rolling ~2 s annotated window, a
  click-to-seek annotation timeline, and an annotation list.
- Built in a separate git worktree `player-g0` (branch `feat/clip-player`) off main,
  so the live server's working tree (on the lock-screen branch, for Tim's testing)
  is undisturbed. Worktree stays for ongoing player-side loop work.
- **Deferred:** serving the clip player on the live server — waits until Tim merges
  the player PRs (or I stand up a dedicated clips server). The G1 clip generator
  will append real clips to `public/clips/manifest.json`.
- **Next:** G1 — skip-aware edge measurement + consistency rejection (raise solve
  coverage), then the clip generator (skip-check clips per the spec) feeding the
  player, then emit AUTO GENERATED `labels.tsv`.

## 2026-06-14 — G2 originals→mix (T0 + T1 + T2 1st pass) done

- **T0 sync ground truth** + **T1 chroma+DTW aligner** built and both **passed
  local-review**; folded into analysis **PR #16** (branch `feat/g2-track-mix-gt`).
  T1 recovers the mix/original rate where waveform correlation can't, with a
  precision-first reliability gate (`is_reliable`: R²≥0.999 AND mean DTW cost≤0.03).
- **Decision — rate via Theil-Sen over the trimmed (10–90%) warp path**, not polyfit:
  resists the subsequence-DTW boundary flats that gave degenerate slopes.
- **Decision — score `norm_cost` at the SELECTED subseq endpoint, not dist[-1,-1]**
  (caught by review): dist[-1,-1] scores aligning the excerpt to the END of the
  original, inflating cost and falsely flagging matches that end mid-original.
- **Decision — for pure-compute fan-out (26 tracks) use a batched `.venv` python run
  + a `track-mix` CLI, NOT a Workflow.** Workflow subagents are for agent-judgment
  fan-out; chroma/DTW over 40 tracks is numeric and runs in ~18 s in one process.
- **Decision — align against the capture that CONTAINS the master span**
  (`_select_capture`), not `source_files[0]` (ordered by overlap). Fixed the empty-mix
  `nan` cases and turned track 23 from a 1.232 wrong-match into err 2e-5.
- **T2 1st-pass result:** of 26 synced tracks with an original on disk, **18 reliable,
  15 within the strict rate tol (≤0.005)**; 8 correctly flagged. 3 reliable-but-rate-
  disagrees (8,10,19) logged as **2nd-pass/piecewise candidates** (not gate-fixable
  without losing genuine matches).
- **G4 signal (opportunistic, per plan):** the same report shows **30 synced tracks
  with NO original file** on disk — the missing-source list to drive G4. Doing G4
  pass-1 inventory next (cheap, builds on this).
- **Still open in G1:** emit AUTO GENERATED `labels.tsv` for unlabelled recordings
  (the headline G1 deliverable) — not yet done; the engine placement/skip map exists.

## 2026-06-14 — G4 pass-1 done; G3 hit the tail wall; G4 pass-2 started

- **G4 pass-1 inventory** built (`scripts/g4_missing_sources.py`), **passed
  local-review** (2 iters — fail-fast on bad `--sources` was the BLOCKING fix),
  opened as **PR #17**. Result: 31 have, 1 placeholder, 59 missing → **51 sourceable
  (identified) + 9 needs-G3 (Mystery Tracks)**.
- **Decision — classify by name match, not just the `NNN-` prefix.** Track 14 had an
  unrelated 30 MB `014-LeRadioClub` DJ-mix m4a sharing the prefix next to the real
  missing original's `.null`; size-only picked the m4a and hid Tim's flagship gap
  ("Stay (Midnight Rockers Remix)"). Require a shared significant word with
  artist/title; trust a lone prefix file only when nothing competes.
- **WALL — G3 mystery-track extraction is blocked by tail placement.** Of the 9
  Mystery Tracks, **only track 67 sits in a placed capture**; the other 8 are in the
  **unplaced tail** (captures like d416-435, d456-470 have audio but no hand
  placement; their metadata spans are `source: "rough"`). Can't extract clean mix
  clips (or BPM/key/fingerprint) without first placing the tail. Per the README this
  is the known tail problem needing **either** a robust small-overlap blind detector
  **or Tim's confirmation that the tail was recorded contiguously.** → Logged and
  pivoted rather than burning ticks on it. fpcalc/chromaprint NOT installed yet
  (deferred with G3).
- **Decision — pivot to G4 pass-2 (no placement dependency).** Did Tim's flagship
  sourcing dossier: **track 014 "Stay (The Midnight Rockers Remix)"** — exists only on
  the 1996 2x12" promo (Maverick PRO-A-8554, B1/5:18), no digital; best route =
  Discogs copy → vinyl rip. `data/sourcing/014-...md`, opened as **PR #18** (prose
  research artifact — no local-review; nothing to compile/test). Remaining 50
  identified gaps = pending pass-2 sweep.
- **Open / needs Tim:** (1) tail contiguity confirmation to unblock G3 + tail
  placement; (2) G1 AUTO GENERATED labels.tsv emission still pending (also gated on
  tail placement for the unlabelled tail recordings).
