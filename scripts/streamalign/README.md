# Stream Alignment Engine

Reconstruct the netradio DNB master timeline by **audio analysis**: place every
capture file on the master clock (precise, skip-resolved), validated against Tim's
hand alignments, then emit alignment labels for the captures not yet done by hand.

See [`../../STREAM_PROVENANCE.md`](../../STREAM_PROVENANCE.md) for what the data is
and what "master time" means, and [`WALKTHROUGH.md`](./WALKTHROUGH.md) for a
function-by-function tour of how the pieces compose.

## Design

- **Stream-vs-stream is a pure time offset.** Over a skip-free overlap, two
  captures are the *same broadcast samples* (amplitude and noise aside). Clock
  drift and polarity inversion appear only between an *original track* and the
  stream — never between two stream captures.
- **The broadcast is a loop.** The captured programme is an ~9-hour loop. Two
  capture files whose names start `d-<negative>-<n>` (e.g. `d-25-000b`,
  `d-25-005b` — the leading `-` is a negative pre-roll) contain **both the end and
  the beginning of the loop**. They therefore match other files at *two* offsets,
  which any aligner has to expect (it is the cause of the loop/pre-roll
  "multi-match" mis-locks).
- **Skips are load-bearing.** A capture's local→master map is piecewise (slope 1,
  with `+N`/`−N` jumps at "skip ahead/back N seconds"), so
  `master_end = length + start + Σ skips`, and a missed skip propagates to every
  file placed downstream. Skips must be found, not approximated away.
- **No third-party audio deps in the core.** ffmpeg decodes any container to
  float32 mono @ 16 kHz; numpy does the FFTs. Decoded arrays are cached on disk.
  (Feature-based original-track↔mix work uses `librosa` in `.venv`; see
  `requirements-streamalign.txt`.)

## Modules — three roles

**1. Ground truth (the answer key).** `groundtruth.py` parses the label files for
Tim's hand-measured alignments — the `file start sync` placements and the
`verified` pairs — into `{stem: master_start}` and a list of known-overlapping
pairs. This is the data everything else is graded against.

**2. Find (measure from the audio).**
- `audio.py` — ffmpeg→numpy loader (16 kHz mono), on-disk decode cache, file
  resolution.
- `align.py` — pairwise offset between two captures: decimated FFT
  cross-correlation (coarse) → GCC-PHAT (sub-sample).
- `skips.py` — walk an overlap window-by-window tracking the local offset; steps in
  the offset track are skips (size + direction).
- `graph.py` — blind (seedless) pairwise alignment + overlap-graph discovery.
- `solve.py` — propagate pairwise offsets from the anchor (`d000-018 = 0`) into
  absolute master positions, with per-file corroboration diagnostics.
- `clips.py` — render skip-check review clips (A+B across a skip, B bridging the
  gap) into the clip player's `manifest.json` for Tim to verify by ear.

**3. Score (measure the finding vs the answer key).** `score.py` — pairwise and
absolute error vs ground truth, plus redundant-overlap self-consistency.

## Usage

```
PYTHONPATH=scripts python3 -m streamalign groundtruth          # the hand answer key
PYTHONPATH=scripts python3 -m streamalign align d000-018 d001-026b
PYTHONPATH=scripts python3 -m streamalign --labels <dir> validate
```

- **`groundtruth`** — prints each file's resolved hand master-start (seconds) and
  whether its audio is present, plus the file count. This is the table the engine
  is graded against.
- **`align A B`** — prints the measured offset (seconds + samples) and confidence
  for the pair, and, if both are in the ground truth, the expected offset and the
  error in ms.
- **`validate`** — aligns every hand-verified pair and prints a per-pair error
  table (error in ms / samples, confidence), worst first, then a summary (median /
  max error, how many fall within the pass tolerance, and pairs skipped for missing
  audio). This is the headline "does the engine match Tim's hand work" check.

## Status (current)

- **P0 ground truth** — resolves all hand placements; matches the player's trusted
  parser exactly (55 files, 0 mismatches).
- **P1 pairwise align** — reproduces hand alignments to ±1 sample on clean overlaps.
- **P2 skip detection** — recovers documented skips with exact magnitudes
  (positions ~2 s coarse; refinement pending).
- **P4 discovery + global solve** — skip-aware edge measurement (offset over the
  earliest skip-free segment) + consistency-based outlier rejection;
  `placement_diagnostics` separates corroborated from uncorroborated placements.

### Known limits (open work)

- Blind discovery of **small / skip-heavy overlaps** is unreliable; a low blind
  confidence means "no large clean overlap found", not "no overlap".
- Edge measurement still errs on **loop/pre-roll multi-match** (the loop-wrap files
  above) and **skip-in-overlap** pairs; these are flagged by
  `placement_diagnostics`, not yet fixed.
- The unlabelled **tail** captures do not overlap the anchored region by enough for
  blind alignment to bridge them; placing them needs either a robust small-overlap
  detector or confirmation that they were recorded contiguously.

Per-PR build narrative and validation numbers live in the PR descriptions, not here.
