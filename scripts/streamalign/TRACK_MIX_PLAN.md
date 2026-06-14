# Original-track ↔ mix alignment — plan

Automate what Tim did by hand with the `orig###`/`track###` sync points: for each
original source track, find where (and how) it appears in the DJ's mix / master
timeline. This is the **general** alignment problem — unlike stream-vs-stream it has
the DJ's transformations.

## What's different from stream-vs-stream (and why it's harder)

| | stream-vs-stream | original-track-vs-mix |
|---|---|---|
| time relation | pure constant offset | **offset + rate** (DJ pitched/time-stretched: "very slowed") |
| polarity | always same | can be **inverted** (Tim's notes: "now inverted?") |
| signal identity | identical samples (+ noise) | DJ **EQ / filtering / level rides**, different master/source quality (FLAC vs 16 kHz RealAudio), crossfades |
| breaks | skips (capture artifacts) | **DJ edits**: cuts, loops, drops — piecewise mapping |
| coverage | whole file overlaps | only the portion the DJ used appears in the mix |

So the unknown per track is a **piecewise-linear map** orig-time → master-time:
`t_master = offset_k + rate_k · t_orig` on each segment k, plus a polarity sign,
with the DJ's edits as the segment boundaries.

## Ground truth (rich — Tim has done a lot)

- **56 source files** in `sources_local/` named `NNN-Artist - Title.{mp3,flac,m4a,opus,wav}`.
- **Dozens of tracks (003–047+) have `orig###`/`track###` sync points** in
  `labels/*.labels.tsv`: paired musical moments, `track sync: N` (in the mix) and
  `origNNN sync: N` (in the original), both timestamped in the owning stream file's
  Audacity timeline. Consecutive pairs give the local **rate** via the sheet
  formula `speed = (trackB − trackA) / (origB − origA)` (`sheetscript/Code.js`).
- `scripts/alignfinder.py` is the earlier interactive prototype for exactly this
  (it already estimates a speed table across align points and handles polarity via
  `--invert`).

## Approach (reuse the streamalign primitives, add a rate axis)

1. **Locate** the track's region in the master timeline. Cheap prior:
   `track-metadata.json` already has each track's `master_begin_seconds` /
   `master_end_seconds`, so we know which mix capture(s) and roughly where to look —
   no blind global search needed (sidesteps the small-overlap discovery problem).
2. **Rate + offset + polarity lock.** Resample the original over a candidate rate
   range (DJ pitch: ~0.85–1.10), cross-correlate (PHAT) each against the mix region,
   take the best (rate, offset, sign). The streamalign `local_offset` primitive
   generalises to this with a rate parameter.
3. **Refine + segment.** Walk the overlap (like P2) allowing slow rate drift and
   detecting DJ-edit boundaries where the linear map breaks → piecewise map.
4. **Validate** against Tim's sync points: predicted (master-time ↔ orig-time)
   pairs and per-segment rate must match his `track`/`orig` pairs and `speed`
   values. The sync-point tracks are the answer key, exactly like `groundtruth.py`
   is for stream-vs-stream.

## Phases (each validatable on a track Tim already synced)

- **T0** parse `orig###`/`track###` sync points + the source-file inventory into a
  ground-truth table (offset/rate per track), mirroring `groundtruth.py`.
- **T1** rate-aware pairwise lock on one track (proof of concept) → validate the
  recovered rate against Tim's `speed`.
- **T2** piecewise / DJ-edit segmentation; polarity handling.
- **T3** run across all synced tracks, score vs ground truth; then extend to
  not-yet-synced tracks and emit candidate `orig###`/`track###` labels for review.

## Status

### 2026-06-12 — T1 proof-of-concept: waveform correlation does NOT work

Tried to lock `010-Odyssey - Artificial Life` to its mix region (master ~1700-2000,
in `d019-040`) by a rate+offset+polarity sweep using **raw** and **PHAT**
cross-correlation of the waveforms. Both fail to lock: best ncc ≈ 0.11 (noise floor
≈ 0.03), with only a faint bump near rate 1.0 — i.e. real-but-buried signal, no
confident match.

**Why (and the redirect):** the mix is the DJ's EQ'd/level-ridden version captured as
lossy 16 kHz RealAudio, spectrally far from the clean source (and possibly a
different master, with crossfade/layering at transitions). Waveform correlation —
even whitened — is the wrong tool. The standard tool for "same song, different
recording/EQ/tempo" is **feature-based alignment**:
- **Chroma** (12-bin pitch-class) or CQT features — robust to timbre/EQ, capture the
  harmonic content that survives the DJ's processing;
- **DTW** (dynamic time warping) over the feature sequences — recovers the
  *piecewise* time map directly, absorbing rate changes and DJ edits without a
  separate rate search;
- validate the warping path against Tim's `orig###`/`track###` sync pairs.

**Revised T1/T2:** build chroma+DTW alignment. This likely wants **`librosa`**
(chroma/CQT + DTW) — a dependency to install (Tim offered via Discord), or implement
a numpy STFT→chroma + a banded-DTW ourselves. Recommend librosa to move fast, then
keep the runtime dependency optional.
