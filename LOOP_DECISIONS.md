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
