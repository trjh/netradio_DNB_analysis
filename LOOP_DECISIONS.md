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
