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

### Next

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
