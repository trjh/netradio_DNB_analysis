# Next steps for Tim

What's built, what needs your hand, where we are. Updated 2026-06-14 (eve).

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

## ⏳ Pending follow-ups (the two end-of-session handoffs — still open)

These are the tasks I handed back at the end of the last two work sessions. Each is
**blocked on you**; once you act, I continue.

1. **Tail-contiguity check (yours) → I place the tail.** The headline above. You said
   you'd check whether the tail files were recorded contiguously. *Then:* if yes, I
   chain-place the tail (`ASSUMED-CONTIGUOUS` boundaries + skip-check clips), which
   unblocks **G1 tail `labels.tsv` emission** and **G3 for 8 of the 9 Mystery Tracks**.
2. **Register an AcoustID *application* (yours) → I run G3 fingerprint lookups.** The
   key in `.env` (`vR4IuLRX`) fails lookup with "invalid API key" — it's a user key,
   not an application key. Draft + 2-minute steps in
   `data/g3-acoustid-application.md` (**PR #20**); drop the new key into `.env`. *Then:*
   I fingerprint track 67 now, and the rest once the tail is placed. The lookup
   pipeline is already proven (extract → `fpcalc` → AcoustID `POST`).
3. **G2 2nd-pass review clips (optional, on your word).** Tracks 8/10/19 align
   "reliable" but their recovered rate disagrees with your hand sync points. I
   diagnosed it: the warp slopes are *constant* within each track, so this is **not** a
   DJ piecewise edit — it's likely a wrong-section chroma lock (or genuine
   disagreement) that needs an **ear-check**. Say the word and I'll render orig-vs-mix
   review clips for the three so you can judge by ear.

Heads-up from last session: `brew install chromaprint` pulled in python@3.14 and
briefly broke the analysis `.venv` (built on 3.13); I fixed it with
`brew install python@3.13`. Verify the venv after any brew install.

## Open PRs (newest first — I never merge; these are yours)

| repo | PR | what |
|---|---|---|
| analysis | **#20** | G3 — draft AcoustID application (fixes the invalid-key blocker) |
| analysis | **#18** | G4 pass-2 — sourcing dossiers for **all 51** missing originals |
| analysis | **#17** | G4 pass-1 — missing-originals inventory (`g4_missing_sources.py`) |
| analysis | **#16** | G2 originals→mix — sync ground truth (T0) + chroma/DTW aligner (T1) + 1st pass (T2) |
| player | **#23–#25** | admin error banner, lock-screen MediaSession, clip review player |

(Earlier engine PRs #10–#15 — if still open, they're the base; merge those first.)

## Where we are on the goals

| goal | status |
|---|---|
| **G0** clip review player | ✅ player PR #25 (seek slider, variable speed, rolling annotations) |
| **G1** master timeline | ⚠️ engine P0–P4 done & validated; **tail placement + AUTO GENERATED `labels.tsv` emission blocked on the headline above** |
| **G2** originals→mix | ✅ T0 sync ground truth, T1 chroma+DTW aligner, T2 1st pass at scale — **18/26 reliable, 15 within strict rate tol**. 2nd pass: diagnosed (not piecewise) → optional review clips for 8/10/19, follow-up #3 |
| **G3** identify Mystery Tracks | ⚠️ blocked twice over: 8/9 by the tail headline, and the fingerprint lookup by the AcoustID key (follow-ups #1 + #2) |
| **G4** missing originals + sourcing | ✅ **done** — pass-1 inventory (51 sourceable, 9 need G3) + pass-2 dossiers for **all 51** (`data/sourcing/`, PR #18): 31 buy-digital, 11 buy-physical→rip, 5 streaming, 3 promos. Verify the exact version before buying (remix traps) |

Decisions and assumptions are logged append-only in `LOOP_DECISIONS.md` (PR #12 /
branch `plan/autonomous-loop`).
