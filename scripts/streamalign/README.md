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

`matchconv.py` (original-track↔mix, align-tool Pass 1) turns a **MATCH** (Sonic
Visualiser's aligner, run headless via `sonic-annotator` + the `match-vamp` plugin)
path into sample-tight paired sync points: the MATCH path is only a *coarse map*
(its online DTW is forced to start the files together, so on a mix it lands seconds
off with the wrong rate), then a rate sweep scored by GCC-PHAT confidence finds the
true rate, a rate-corrected anchor grid walks the overlap at both polarities, and
whole-overlap **anchor mass** picks between loop-shifted rival seats (drum & bass
self-correlates at whole-bar shifts; the true seat is the one that explains the
WHOLE overlap, not just the looped stretch).

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
PYTHONPATH=scripts .venv/bin/python -m streamalign match-hints d376-395 72 --dry-run
```

- **`groundtruth`** — prints each file's resolved hand master-start (seconds) and
  whether its audio is present, plus the file count. This is the table the engine
  is graded against.
- **`align A B`** — prints the measured offset (seconds + samples) and confidence
  for the pair, and, if both are in the ground truth, the expected offset and the
  error in ms.
- **`validate`** — verifies every hand-verified pair by comparing ONLY its
  overlapping audio: equal-length slices tiled across the labeled overlap are
  cross-correlated, so a divergence anywhere in the overlap (not just its start) is
  caught. Each pair is **confirmed** (residual ≈ 0, high confidence), **suspect**
  (real overlap but the audio doesn't match at the labeled offset — worst first,
  `resid_ms` measures how far off), or **adjacent** (labels place the pair
  end-to-end, no overlap to compare — listed apart, not an error), then a summary
  (graded / confirmed / suspect / adjacent / skipped). The headline "does the audio
  confirm Tim's hand labels" check.
- **`track-mix`** — G2 1st pass: chroma+DTW-align every synced original to its mix
  region, grade the recovered rate against the sync ground truth, and print a
  per-track table (rate / gt_rate / err / confidence / cost / reliable / within-tol)
  plus a summary (reliable, within-tolerance, flagged, no-original). `--json` writes
  the full results. Needs librosa — run with `.venv/bin/python`.

- **`match-hints STEM NNN`** — align-tool Pass 1: seat original `NNN` inside capture
  `STEM` and emit paired hint labels — `<stem>.origNNN.match.hints.tsv` (`track sync:`
  rows at capture-local times, proposed `origNNN start:`/`end:`) and
  `origNNN.<stem>.match.hints.tsv` (`origNNN sync:` rows at original-local seconds; the stem keeps runs against different captures from colliding), every
  row ` HINT`-marked with the ` verified` token and a spelled-out confidence, plus the
  recovered rate and polarity in a summary note. Runs `sonic-annotator` itself (or
  takes a pre-exported `match:a_b` CSV via `--csv`); emits QUESTION rows instead of
  silent guesses when coverage is thin or a rival loop-shifted seat scores close.
  Never writes a `.labels.tsv`. Converter-trust behaviour (2026-08):
  - the stream is **trimmed to the original's expected neighbourhood** before MATCH
    runs (`--around SECONDS`, else auto-derived from the track's master span minus the
    capture's resolved master start), making MATCH's forced files-start-together
    assumption approximately true;
  - the trimmed MATCH path then **referees** each PHAT anchor: the per-anchor
    MATCH-vs-PHAT delta is printed, appended to the row text, and a disagreement
    beyond 0.25 s becomes a `note QUESTION:` row (PHAT stays the only anchor source);
  - where librosa is available, the **solo-anchor moments** (record playing alone —
    where PHAT locks best) seed the rate sweep alongside the blind probe fractions;
  - `--all` batches every track whose master span overlaps the capture (tracks with
    no original on disk are listed and skipped); an explicit list of track numbers
    (`match-hints STEM 71 72`) overrides.
- **`inspect-slice STEM NNN --stream-t S --orig-t S`** — align-tool Pass 2 slice
  provider for the player's `/align` inspector (JSON on stdout; all DSP happens
  here, in the venv). Default: base64 int16 stereo slices of the stream and the
  rate-corrected original around one sync point. `--refine` runs the GCC-PHAT
  snap-to-best instead; `--refine --engine match` snaps to the **trimmed MATCH
  path's** implied instant (with the PHAT-vs-MATCH delta reported, and a
  globally mis-seated path returned as an error, never a snap). `--context
  SECONDS` emits the zoomed-out **context strip** instead: decimated min/max
  columns (50/s, mono) over ±SECONDS (cap 60) — a small payload, not audio.
  `--overview --point STREAM_S:ORIG_S …` emits the **whole-capture overview**
  instead (AP-10): one coarse envelope (6 cols/s, capped) for the full stream plus
  the original's envelope piecewise-linearly stretched between the given sync
  points (head/tail extrapolated along `--rate`, clamped to the capture).
- **`inspect-worker`** — the keep-warm form of `inspect-slice`: a long-lived
  process reading JSON-lines requests on stdin
  (`op: slice|refine|context|overview|placed`, same fields and guards) and writing
  one JSON response line each, holding the decoded stream + rate-corrected
  original for the current (stem, orig, rate) pair so repeat requests skip the
  decode/resample cost (`placed` is label bookkeeping only — it touches no audio
  and leaves the pair cache alone). The player spawns it and falls back to
  one-shot subprocesses when it can't start.
- **`placed [STEM]`** — AP-26 review-tool data source: the hand-placed
  `origNNN sync:`/`track sync:` marker pairs as JSON, per capture stem ×
  original — marker key, capture-local stream instant, original-native instant
  reconstructed from the `origNNN start:` bookkeeping (exactly `sync-audit`'s
  seat math), a per-point `derivable` flag with the reason when false, the
  ` verified` token, and the audited verdict/confidence where a saved repo-root
  `sync-audit.json` (`sync-audit --json sync-audit.json`; gitignored) knows the
  point. The reported rate is original-seconds-per-stream-second (1 / the sheet
  speed — the match-hints summary / `inspect-slice --rate` convention).
  Read-only and DSP-free; no audio is opened.
- **`sync-audit`** — grade every hand `origNNN sync:` point against the audio (seat
  confidence, hand bookkeeping error, inspector residuals; see
  [SYNC_SWEEP_CALIBRATION.md](./SYNC_SWEEP_CALIBRATION.md)). Each point also reports
  whether its row text carries the ` verified` token (`--only-unchecked` lists the
  hand points the system has never touched), and a point that would skip for
  "no origNNN start: row" gets its seat **derived from a STRONG-audited neighbour
  capture** sharing the clip seating (master-timeline shift + the neighbours' median
  hand-error correction — the 066-A method), marked `seat<-<neighbour>` in the table.

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
