# Next steps for Tim

What's built, what needs your hand, where we are. Updated 2026-06-14.

## ⚠️ HEADLINE — the one thing only you can answer: is the tail contiguous?

Everything past the anchored region is blocked on this. The last **hand-placed**
capture is **`d336-355`** (master ≈ 19875 s). The tail captures below have audio but
**no placement**, so the engine can't anchor them to master time — which blocks G3
(identifying 8 of the 9 Mystery Tracks, which live here), tail label emission, and
full timeline coverage:

```
d356-375  d376-395  d396-415  d416-435  d436-455  d456-470  (… and beyond)
```

**Please check (you said you would): were these recorded back-to-back with no gaps?**
i.e. does each file start exactly where the previous one's (skip-resolved) audio ends?

- **If yes (contiguous):** I'll chain them — each file's master start = the previous
  file's skip-resolved master end — place the whole tail, flag every boundary as
  `ASSUMED-CONTIGUOUS`, and generate skip-check clips so you can spot-verify by ear.
  That unblocks G3 + tail labels.
- **If no / gaps:** chaining would smear everything downstream of the first gap, so I
  won't assume it; we'd need a robust small-overlap blind detector (open problem) or
  another anchor.

How to check a boundary quickly: open consecutive files in Audacity and see whether
the end of one and the start of the next are the same broadcast audio (or compute the
offset with `python3 -m streamalign align <fileA> <fileB>` — a clean ~1.0-confidence
match at offset ≈ fileA length means contiguous).

## Open PRs (newest first — I never merge; these are yours)

| repo | PR | what |
|---|---|---|
| analysis | **#18** | G4 sourcing dossier — "Stay (Midnight Rockers Remix)" + pass-2 framing |
| analysis | **#17** | G4 pass-1 — missing-originals inventory (`g4_missing_sources.py`) |
| analysis | **#16** | G2 originals→mix — sync ground truth (T0) + chroma/DTW aligner (T1) + 1st pass at scale (T2) |
| player | **#23–#25** | admin error banner, lock-screen MediaSession, clip review player |

(Earlier engine PRs #10/#11 — if still open, merge those first; they're the base.)

## Where we are on the goals

| goal | status |
|---|---|
| **G0** clip review player | ✅ player PR #25 (seek slider, variable speed, rolling annotations) |
| **G1** master timeline | ⚠️ engine P0–P4 done & validated; **tail placement + AUTO GENERATED `labels.tsv` emission blocked on the headline above** |
| **G2** originals→mix | ✅ T0 sync ground truth, T1 chroma+DTW aligner, T2 1st pass at scale — **18/26 reliable, 15 within strict rate tol**. 2nd pass (piecewise/polarity for tracks 8/10/19) in progress |
| **G3** identify Mystery Tracks | ⚠️ 8/9 blocked by the tail headline; trying AcoustID on the 1 placeable one (track 67) |
| **G4** missing originals + sourcing | ✅ pass-1 inventory done (**51 sourceable, 9 need G3**); pass-2 sourcing sweep of the 51 in progress (flagship "Stay" done) |

## What I'm doing next (you approved all three)

1. **G4 pass-2 sourcing sweep** — a dossier for each of the remaining 50 identified
   missing originals (where to buy/rip, format, confidence). Many are DnB white-label
   12"s with uncertain yield; each gets the best available option or "no good source".
2. **AcoustID on track 67** — installing fpcalc/chromaprint and fingerprinting the one
   Mystery Track that sits in a placed capture.
3. **G2 2nd pass** — piecewise/polarity refinement for the 3 reliable-but-rate-
   disagrees tracks (8, 10, 19).

Decisions and assumptions are logged append-only in `LOOP_DECISIONS.md` (PR #12 /
branch `plan/autonomous-loop`).
