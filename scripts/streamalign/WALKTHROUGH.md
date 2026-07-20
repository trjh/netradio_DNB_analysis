# Stream Alignment Engine — code walkthrough

> **What this is:** Stream Alignment Engine — how the functions compose (reference).
> **Fits in:** [../../README](../../README.md) (master index).

A function-by-function tour of how the `streamalign` package fits together to
reconstruct the netradio master timeline from the capture audio. Read
[`README.md`](./README.md) first for the *what* and *why*; this is the *how*.

> **The one idea to hold onto.** Two captures that overlap are recordings of the
> *same broadcast*, so over a skip-free stretch they are literally the same samples
> shifted in time. Everything here is built on finding that shift (the **offset**)
> and watching where it changes (a **skip**).

---

## 1. The pipeline at a glance

```
                    labels/*.labels.tsv                  jaz_links/*.wav,*.au
                            │                                     │
              groundtruth.resolve_starts()              audio.load_audio()
              groundtruth.alignment_edges()             (ffmpeg → float32 mono,
                            │                             cached on disk)
            {stem: master_start}, [(a,b)…]                       │
                            │                                     │
                            ▼                                     ▼
        ┌───────────────────────────── P1: align one pair ──────────────────────┐
        │   align.align_pair(A, B)                                               │
        │     ├─ audio.load_audio(A), audio.load_audio(B)                        │
        │     ├─ align.coarse_offset()   ── decimated FFT x-corr → rough lag     │
        │     └─ align.refine_offset()   ── GCC-PHAT in a window → sub-sample    │
        │                                   offset + confidence                  │
        └───────────────────────────────────────────────────────────────────────┘
                            │ a single global offset (only valid if skip-free)
                            ▼
        ┌──────────────────────────── P2: characterise an overlap ──────────────┐
        │   skips.characterise_overlap(A, B, region, seed_offset)               │
        │     ├─ skips.walk_overlap()    ── slide windows, track local offset    │
        │     │      └─ skips.local_offset()  (per-window PHAT lock)             │
        │     └─ skips.detect_skips()    ── steps in the offset track = skips    │
        └───────────────────────────────────────────────────────────────────────┘
                            │ piecewise map: segments + every skip (size, sign)
                            ▼
        ┌──────────────── P4 (pending): global solve ──────────────┐
        │   anchor d000-018 = 0, chain every file's skip-aware map  │
        │   into absolute master coords; cross-check redundant      │
        │   overlaps with score.consistency_report()                │
        └───────────────────────────────────────────────────────────┘
                            │ {stem: master_start, master_end, skips}
                            ▼
        score.score_*  (grade vs ground truth)      P5: emit .labels.tsv
```

The CLI (`__main__.py`) wires these into `groundtruth`, `align`, and `validate`
sub-commands.

---

## 2. The offset convention (everything depends on this)

Throughout, for two signals `a` and `b`:

```
a[i]  ~  b[i - offset]        (over their overlap)
offset = master_start(b) - master_start(a)      in SAMPLES (16 kHz)
```

A **positive** offset means *b starts later* on the master timeline than *a*. To
place `b` given `a`'s position: `master_start(b) = master_start(a) + offset/SR`.

This convention is fixed by the cross-correlation in `align._xcorr_full`, where
`cc[k] = Σ_i a[i]·b[i-k]`, so the peak lag `k` means `a[i] ~ b[i-k]`. Every offset
reconstruction (`refine_offset`, `local_offset`) and the confidence check
(`_ncc_at`) is derived from this one equation — get it wrong and you get a constant
sign/scale error (which is exactly the bug caught and fixed during P1).

---

## 3. `audio.py` — get samples in front of numpy

| function | role | calls / called by |
|---|---|---|
| `load_audio(name)` | the workhorse: resolve → decode → cache → return float32 mono @16 kHz | calls `find_audio_file`, `_ffmpeg_decode`, `_cache_key`; called by `align_pair`, `characterise_overlap` |
| `find_audio_file(name)` | map a label name/stem to a real file, preferring `.wav` > `.au` > `.mp3` | uses `stem_of` |
| `_ffmpeg_decode(path)` | one ffmpeg subprocess → `np.frombuffer` (mono, normalized) | — |
| `_cache_key(path)` | sha1 of realpath+size+mtime+params → cache filename | — |
| `stem_of(name)` | `d019-040.wav` → `d019-040` | everywhere |
| `duration_seconds(name)` | decoded length / SR | helpers |

Why a cache: a global solve reads each ~20-min file many times; decoding once and
`np.load(mmap)`-ing thereafter keeps the inner loops fast. No third-party audio
libraries — `ffmpeg` handles every container/endianness, numpy does the rest.

---

## 4. `groundtruth.py` — Tim's hand work as the grading key

The labels encode Tim's manual alignments as `file start sync:` rows. This module
turns them into (a) the answer key and (b) the list of pairs he actually overlaid.

| function | role |
|---|---|
| `resolve_starts()` | `{stem: master_start_seconds}` — a faithful port of the player's trusted `parse_file_timeline` (3 passes). **Pass 1** seeds files whose sync row sits at local 0.0 (the number *is* the master start). **Pass 2** fills plain `file start` fallbacks. **Pass 3** iteratively resolves syncs expressed in another file's coordinates (`master = owner_master + row_seconds`), so chains like `d000-018 → … → d336-355` resolve. |
| `alignment_edges()` | `[(stem, verified_against_stem), …]` from the `verified [by] X` notes — the pairs Tim aligned, i.e. the graph edges + P1 validation pairs |
| `_verified_refs(text)` | pull capture-looking tokens after `verified` |
| `_read_label_rows`, `_stem` | tsv reading / name normalization |
| `ground_truth()` | thin alias of `resolve_starts` |

The subtlety it gets right: a trailing number in a sync row is a **master value**
when the row is at local 0.0 in its *own* file (pass 1), but a **reference anchor**
when embedded in another file at a non-zero timestamp (pass 3 uses the row's
timestamp, not the trailing number). Verified to match the player parser exactly
(55 files, 0 mismatches).

---

## 5. `align.py` — “where does B sit relative to A?” (P1)

The public entry point is `align_pair`; the rest are the coarse→fine machinery.

```
align_pair(A, B)
  a = load_audio(A);  b = load_audio(B)
  coarse = coarse_offset(a, b)          # integer offset, fast
  offset, conf = refine_offset(a, b, around=coarse)
  return {offset_seconds, offset_samples, confidence, …}
```

| function | role |
|---|---|
| `coarse_offset(a, b, decim=8)` | decimate (every 8th sample → 2 kHz), DC-remove, full FFT cross-correlation (`_xcorr_full`), take argmax → offset good to ±`decim` samples. Cheap even for 20-min files. |
| `refine_offset(a, b, around, radius)` | cut a window from the overlap at `around`, GCC-PHAT it (whiten the cross-spectrum → razor-sharp delay peak), `_parabolic` interpolate for **sub-sample** precision, and reconstruct `offset = (a0-b0) + (lag+frac)`. Returns `(offset, confidence)`. |
| `_xcorr_full(a, b)` | FFT cross-correlation, `cc[k]=Σ a[i]b[i-k]`; defines the lag convention |
| `_ncc_at(aseg, bseg, lag)` | normalized correlation of the *matched* samples at `lag` → the confidence in `[0,1]` (≈1 means identical = a true lock) |
| `_parabolic(y, k)`, `_next_pow2(n)` | sub-sample peak interpolation; FFT sizing |

Why two stages: a single full-resolution FFT over two 20-min files is wasteful and
the PHAT peak is sharpest in a focused window. Coarse finds the neighbourhood
cheaply; refine nails it to a fraction of a sample. **Result:** reproduces Tim's
hand alignments to ±1 sample at confidence 0.97–0.999.

**Limitation (why P2 exists):** `align_pair` returns *one* offset. That's only
meaningful for a large, skip-free overlap. For a partial overlap the peak is
swamped (confidence ≈ 0); for loop/pre-roll files the content matches at *several*
offsets (high-confidence wrong). Both are visible as outliers in `validate` and are
the reason alignment must become *localized* — which is P2.

---

## 6. `skips.py` — the piecewise truth (P2)

Instead of one offset per pair, walk the overlap and watch the offset move.

```
characterise_overlap(A, B, a_start, a_end, seed_offset)
  a = load_audio(A);  b = load_audio(B)
  walk  = walk_overlap(a, b, a_start, a_end, seed_offset)   # [(t, offset, conf), …]
  skips = detect_skips(walk)                                # steps in the track
  return {walk, skips}
```

| function | role |
|---|---|
| `local_offset(a, b, a_lo, a_hi, expected, radius)` | the per-window primitive: PHAT-align the A-window `[a_lo,a_hi)` against B searched `±radius` around `expected`; return `(offset, confidence)`. Same math as `refine_offset` but local and seeded. |
| `walk_overlap(…, win_s=8, hop_s=1, radius_s=3)` | slide the window across the overlap calling `local_offset`; **carry the offset forward only through confident windows** so it survives the low-confidence window straddling a skip and re-locks just past it. Returns the offset *track*. |
| `detect_skips(walk, min_jump_s=0.04)` | confident points form a piecewise-constant track; adjacent points differing by `> min_jump` bracket a skip (`delta` = its size & sign). Merges a step split across two gaps. |

The defaults are **load-bearing, not free knobs** (the docstring says so): a window
`< ~8 s` locks onto DnB's periodic beat (confidence collapses), and a radius
`≥ ~12 s` admits wrong-beat false locks. These were the values validated below, and
the regression test calls `characterise_overlap` with the **defaults** so they
can't silently regress (this is the iteration-2 review fix).

**Validated:** on `d084-103b` vs `d065-087` (4 skips documented in the labels) the
detector recovers all four with **exact magnitudes** (1.632, 0.672, 1.248, 1.248 s;
sum 4.800), median confidence 0.991. Skip *positions* land ~2 s early (8 s-window
edge) — a listed refinement; magnitudes (what feed `master_end = length + start +
Σ skips`) are exact.

---

## 7. `score.py` — “is the engine right?”

Master time is self-defined (reconstructed from the windows), so correctness has
two independent sources, and this module measures both. The unit is **samples at
16 kHz** (Audacity's 0.001 s = 16 samples).

| function | grades… |
|---|---|
| `score_pairwise(results, gt)` | P1 offset estimates vs `gt[b]-gt[a]` for each pair |
| `score_absolute(estimates, gt, anchor)` | a full `{stem: master_start}` solution vs ground truth, shifted to a common anchor |
| `consistency_report(placements, edges)` | **no ground truth needed** — for each redundant overlap, `residual = measured - (place[b]-place[a])`; a large residual flags a missed skip or bad lock. This is the finer P4 check (and where the engine can exceed Tim's hand precision). |
| `_stats(errors)` | median / max / mean in samples and ms |

---

## 8. `__main__.py` — the CLI that drives it end to end

```
python3 -m streamalign groundtruth          # dump resolve_starts(), flag missing audio
python3 -m streamalign align d000-018 d001-026b   # one pair + error vs ground truth
python3 -m streamalign validate             # the P0+P1 harness:
```

`validate` is the headline check: take every hand-verified pair
(`alignment_edges`), and for each `slice_check` it against `resolve_starts` —
cut equal-length slices over the labeled overlap and correlate them, asking
"does the audio confirm the labels?". Each pair is **confirmed** (residual ≈ 0,
high confidence), **suspect** (real overlap but the audio doesn't match at the
labeled offset — sorted worst-first, `resid_ms` measures how far off), or
**adjacent** (labels place them end-to-end, no overlap to compare — listed apart,
not an error). Grading by the overlap directly — rather than re-deriving the
offset with `align_pair`'s coarse full-signal search — is what makes it robust for
pairs that differ greatly in length: the coarse cross-correlation's lag decode
splits at `nfft/2`, which is only valid when the two files are similar length, so
a 20-min capture sitting ~39 min into a 47-min one used to score a garbage
multi-minute "error"; the equal-length slice compare keeps the peak well inside
`nfft/2` and confirms it.

---

## 9. How it reaches the goal (and what's still ahead)

Built today, validated against Tim's hand work:

- **P0** `groundtruth` + `score` + `audio` → the answer key and the measuring stick.
- **P1** `align_pair` → precise offset for a clean overlapping pair.
- **P2** `characterise_overlap` → the *piecewise* truth: every skip, with size+sign.

Still ahead (the existing pieces are the building blocks):

- **P2 refinement** — auto-seed `walk_overlap` (derive `seed_offset` from a wide
  `local_offset` at the overlap start, and the region from file lengths) so it runs
  on *unlabelled* files; narrow skip positions; widen radius adaptively for the rare
  large skip.
- **P4 global solve** — treat files as a graph (edges = overlaps from
  `alignment_edges` plus discovered ones), anchor `d000-018 = 0`, and propagate each
  pair's *skip-aware* map outward to absolute `master_start`/`master_end` for every
  file. `consistency_report` cross-checks the redundant overlaps; `score_absolute`
  grades the whole result.
- **P3 / P5** — render the audio/inverted-null verification artifacts, then emit
  `.labels.tsv` rows for the unlabelled tail (as a reviewable diff).

The same `local_offset` primitive, made rate- and sign-aware, is also the seed for
the eventual **original-track-vs-mix** mapping (which, unlike stream-vs-stream, does
have speed drift and polarity flips).
