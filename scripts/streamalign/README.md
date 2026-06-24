# Stream Alignment Engine

> **What this is:** Stream Alignment Engine — status & usage (reference).
> **Fits in:** [../../README](../../README.md) (master index).

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

`track_mix.py` (original-track↔mix, G2) parses Tim's `origNNN`/`track` sync points
into per-track ground truth (the **rate** = mix-seconds per original-second) that
the chroma+DTW aligner is graded against.

**3. Score (measure the finding vs the answer key).** `score.py` — pairwise and
absolute error vs ground truth, plus redundant-overlap self-consistency.

**4. Emit.** `emit_labels.py` — write AUTO GENERATED labels from a solve's placements:
every label ends `" AUTO GENERATED"` and programmatic output always goes to
`<stem>.auto.labels.tsv` (the plain `<stem>.labels.tsv` name is reserved for
hand-generated/confirmed labels, so a hand file is never overwritten). Consumers read
both; hand labels win on conflict. `clips.py` — skip-check review clips for by-ear
verification.

## Usage

```
PYTHONPATH=scripts python3 -m streamalign groundtruth          # the hand answer key
PYTHONPATH=scripts python3 -m streamalign align d000-018 d001-026b
PYTHONPATH=scripts python3 -m streamalign --labels <dir> validate
PYTHONPATH=scripts .venv/bin/python -m streamalign track-mix \
    --meta track-metadata.json --sources sources_local        # G2 1st pass (needs librosa)
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
- **`track-mix`** — G2 1st pass: chroma+DTW-align every synced original to its mix
  region, grade the recovered rate against the sync ground truth, and print a
  per-track table (rate / gt_rate / err / confidence / cost / reliable / within-tol)
  plus a summary (reliable, within-tolerance, flagged, no-original). `--json` writes
  the full results. Needs librosa — run with `.venv/bin/python`.

## Status (current)

- **P0 ground truth** — resolves all hand placements; matches the player's trusted
  parser exactly (55 files, 0 mismatches).
- **P1 pairwise align** — reproduces hand alignments to ±1 sample on clean overlaps.
- **P2 skip detection** — recovers documented skips with exact magnitudes
  (positions ~2 s coarse; refinement pending).
- **P4 discovery + global solve** — skip-aware edge measurement (offset over the
  earliest skip-free segment) + consistency-based outlier rejection;
  `placement_diagnostics` separates corroborated from uncorroborated placements.
- **G2 / T1 original-track↔mix rate** — chroma + subsequence-DTW recovers the
  mix/original rate where waveform correlation cannot, with a **precision-first
  reliability gate** (`is_reliable`: warp-path R² ≥ 0.999 **and** mean DTW cost ≤
  0.03) so the engine flags rather than emits a wrong rate.
- **G2 / T2 1st pass at scale** (`batch_align` / `track-mix` CLI) — align every
  synced original to the mix region of the capture that actually **contains** its
  master span (not blindly `source_files[0]`, which is ordered by overlap). Of the 26
  synced tracks with an original on disk: **18 pass the gate, 15 within the strict
  rate tolerance (≤0.005)**; the rest are correctly flagged (gross mismatch / span
  longer than source / too-short region) for hand or 2nd-pass attention. Three
  reliable-but-rate-disagrees tracks (8, 10, 19) are the piecewise/2nd-pass
  candidates. A further **30 synced tracks have no original file** — the G4
  missing-source signal, surfaced in the same report.
  See [RATE_AXIS_SPIKE.md](./RATE_AXIS_SPIKE.md) — the **A7a rate-axis design note + go/no-go**:
  the rate axis *is* the DTW warp slope (no separate rate-search needed); A7b's open work is
  **piecewise segmentation** (start on tracks 8/10/19) + polarity, not rate discovery.

### Known limits (open work)

- Blind discovery of **small / skip-heavy overlaps** is unreliable; a low blind
  confidence means "no large clean overlap found", not "no overlap".
- Edge measurement still errs on **loop/pre-roll multi-match** (the loop-wrap files
  above) and **skip-in-overlap** pairs; these are flagged by
  `placement_diagnostics`, not yet fixed.
- ~~The unlabelled **tail** captures do not overlap the anchored region by enough for
  blind alignment to bridge them.~~ **Mostly solved** — see [TAIL_SOLVE.md](./TAIL_SOLVE.md)
  (`streamalign tail-solve`). The tail is its own dense overlap component whose end **wraps
  onto the loop-start anchor** `d000-018`; 14 of 16 files now place with full corroboration
  (max residual 0.000 s). Still open: `d376-395` (partial-overlap candidate, conf ~0.6) and
  `d396-415` (a both-sides butt-jointed orphan).

Per-PR build narrative and validation numbers live in the PR descriptions, not here.
