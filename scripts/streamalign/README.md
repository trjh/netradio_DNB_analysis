# Stream Alignment Engine

Reconstruct the netradio DNB master timeline by **audio analysis**: place every
capture file on the master clock (precise, skip-resolved), validate against Tim's
~55 hand alignments, then emit alignment labels for the captures not yet done by
hand. See [`../../STREAM_PROVENANCE.md`](../../STREAM_PROVENANCE.md) for what the
data is and what "master time" means, and the player repo's `TASKLIST.md`
("Stream Alignment Engine") for the phase plan. For a function-by-function tour of
how the pieces compose, see [`WALKTHROUGH.md`](./WALKTHROUGH.md).

## Design

- **Stream-vs-stream is a pure time offset.** Over a skip-free overlap two captures
  are the *same broadcast samples* (amplitude/noise aside) — no clock drift, no
  polarity flip (those were original-track-vs-stream artifacts). Verified
  empirically: hand-aligned pairs reconstruct at confidence 0.97–0.999.
- **Skips are load-bearing.** A capture's local→master map is piecewise (slope 1,
  with `+N`/`−N` jumps at "skip ahead/back N"), so `master_end = length + start +
  Σ skips`, and a missed skip propagates downstream. The engine must find **every**
  skip — there is no honest "rough master-start".
- **No third-party audio deps.** ffmpeg decodes any container to float32 mono @
  16 kHz; numpy does the FFTs. Decoded arrays are cached on disk.

## Modules

| module | role |
|---|---|
| `audio.py` | ffmpeg → numpy loader (16 kHz mono), disk cache, file resolution |
| `groundtruth.py` | resolve Tim's hand `file start sync` rows → `{stem: master_start}`; extract `verified` edges. Port of the player's trusted `parse_file_timeline`. |
| `align.py` | pairwise alignment: decimated FFT cross-correlation (coarse) → GCC-PHAT (sub-sample) |
| `score.py` | grade vs ground truth (pairwise / absolute) + redundant-overlap consistency |
| `__main__.py` | CLI: `groundtruth`, `align A B`, `validate` |

```
PYTHONPATH=scripts python3 -m streamalign --labels <dir> validate
PYTHONPATH=scripts python3 -m streamalign align d000-018 d001-026b
```

## Status / progress log

### 2026-06-11 — P0 + P1 (clean overlaps) working

- **P0 ground truth:** `groundtruth.resolve_starts()` exactly matches the player's
  trusted parser (55 files, 0 mismatches) incl. the chained `d336-355=19875.171`.
- **P1 pairwise aligner:** on Tim's hand-verified pairs it reproduces the
  alignments to **±1 sample** (median 0.18 samples; 41/57 within 1 ms),
  confidence 0.97–0.999. ~0.4 s/pair.
- **Outliers (16/57) — diagnosed, motivate P2:**
  - *conf ≈ 0 (partial overlap):* e.g. `d066-085→d026-073b`. When the overlap is a
    small fraction of a long file, the whole-file cross-correlation peak is swamped
    → degenerate (≈2^26-sample) result.
  - *high conf, wrong offset (multi-match):* e.g. `d-25-005b→d000-018` (+94 s, conf
    0.996), `d228-247→d-14Nov10-c` (−62.7 s, conf 0.998). The pre-roll / loop-edge
    captures genuinely match at **more than one** offset (the broadcast is a loop).
  - **Lesson:** a single global offset is the wrong primitive. P2 moves to
    **localized matched-filter alignment at a seed point, then walk outward
    detecting skips** — which also directly yields the skip map.

### 2026-06-12 — P2 skip detection working

- `skips.py`: walk the overlap window-by-window tracking the local offset; steps in
  the offset track are skips. Key tuning learned empirically:
  - **window ≥ 8 s** — DnB's periodic beat makes short (≤4 s) windows lock onto the
    wrong beat (conf collapses); 8 s disambiguates (conf 0.99).
  - **tight search radius (~3 s)** — because the offset is tracked continuously,
    each skip step is small; a wide radius (≥12 s) invites wrong-beat false locks.
    (Will widen adaptively for the rare large skip, e.g. the documented 10 s one.)
- **Validated** against `d084-103b` vs `d065-087` (4 documented skips): recovered
  all four with **exact magnitudes** (1.632, 0.672, 1.248, 1.248 s; sum 4.800),
  median conf 0.991. Skip *positions* land ~2 s early (8 s-window edge) — magnitudes
  are exact; position refinement (narrow second pass within the bracket) is a TODO.

### 2026-06-12 — P4 groundwork: blind alignment + overlap-graph discovery

- `graph.py`: `blind_offset()` aligns two captures with NO seed (probe windows of
  one, find them in the other) — validated to recover known offsets to ±1 sample,
  and confidence cleanly separates overlap (~0.99) from none (~0.1).
  `discover_overlaps()` blind-aligns candidate pairs (pruned by filename-range
  proximity) and keeps the real overlaps; `connected_components()` finds islands.
- **PRELIMINARY observation (NOT a proven conclusion).** Discovery over the
  tail-region captures + the placed boundary suggested the tail splits into clusters
  (`d416-435…d456-470`, `d465-484…d505-531b`) with `d356-375`/`d376-395`/`d396-415`
  apparently isolated, none reaching the anchor. **But this cannot be trusted yet:**
  `blind_offset` false-negatives on small and skip-heavy overlaps (see its docstring
  and the limitation below), so an "isolated" file may simply have an overlap the
  detector missed. The earlier wording ("the tail is disconnected islands") was an
  overstatement — corrected here.
  - **Open problem:** robust *blind* detection of small / skip-heavy overlaps.
    Tried and rejected: decimated x-corr (aliases DnB highs), energy-envelope
    correlation, and full-file PHAT — all fail on a 210 s overlap inside ~1300 s
    files because the true peak doesn't dominate an unbounded search. Only a
    *bounded* search near a known offset locks (which is why seeded
    `characterise_overlap` works at conf 0.99 on the same pair). A real detector
    likely needs a coarse prior (filename ranges give ±~5 min) to bound the search,
    or a multi-peak / segmented strategy. **Until then, the tail's true connectivity
    is unknown**, and any contiguity assumption for placing isolated tail files is a
    decision for Tim (were the tail captures recorded back-to-back, no gaps?).

### 2026-06-12 — P4 global solve (mechanism validated)

- `solve.py`: `measure_edges()` aligns Tim's `verified` pairs with the precise
  aligner (drops conf<0.7); `solve_positions()` propagates offsets from
  `d000-018=0` by best-first (highest-confidence-path) BFS → absolute master starts.
- **Validated vs ground truth:** of 19 files placed from the verified edges,
  **11/19 match to ≤1 ms** (median 1.06 samples). The propagation mechanism is
  sound. The errors are all from **edge measurement**, not the solve:
  - gross error on `d-25-005b` (loop/pre-roll **multi-match** — `align_pair` locked
    a confident WRONG offset);
  - ~0.96 s on `d026-045`/`d041-064` (a **skip inside the overlap** — a single
    global offset can't represent it).
- **Conclusion:** the weak link is robust, skip-aware edge measurement. Next:
  measure each edge with `characterise_overlap` (use the segment offset nearest the
  file boundary) and reject inconsistent edges via `score.consistency_report`
  (redundant overlaps). That should both fix the ~1 s skip errors and catch the
  multi-match gross errors, and raise coverage beyond 19 files.

- **Honesty pass:** `solve.placement_diagnostics()` flags each placed file as
  *corroborated* (cross-checked by >1 agreeing edge) or *uncorroborated* (single
  edge — nothing catches a confident-but-wrong edge). On the real solve: 12/19
  corroborated, 7 uncorroborated (5 of those single-edge); the gross `d-25-005b`
  error is among the uncorroborated, so it's flagged "trust less" rather than
  silently presented as a result.

### Next

- **P4 robustness** — skip-aware edge measurement (fix the ~1 s skip errors) +
  consistency-based outlier *rejection* (auto-drop, beyond today's flagging);
  multi-match needs the bounded-search idea. Raise coverage toward all 55.
- **P2 refinement** — narrow skip positions (binary search within the bracket);
  adaptive radius for large skips; auto-seed the walk (no hand offset) and derive
  the overlap region from file lengths so it runs on unlabelled files.
- **P4** global graph solve anchored at `d000-018=0`, redundant-overlap cross-check;
  produce skip-aware absolute master starts/ends for every file.
- **P3** audio verification renderer (summed proof clips + inverted-null Audacity
  label file). **P5** emit labels for the unlabelled tail.
- **Future (Tim, 2026-06-11):** generalise the pairwise aligner to
  original-track-vs-stream mapping — that case *does* have speed/drift and polarity,
  so keep the primitive parameterisable (delay + rate + sign).
