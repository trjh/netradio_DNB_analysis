# Rate-axis research spike — design note + go/no-go (A7a)

| | |
|---|---|
| **What this is** | The G2 "rate axis" research spike (chunk **A7a** of `docs/ROADMAP_stream_analysis.md` in the player repo): a short, time-boxed investigation that designs the original↔mix **rate axis** and returns a **go/no-go**, *not* production code. |
| **Owner** | Tim |
| **Date** | 2026-06-21 |
| **Verdict** | **GO** — but the rate axis already exists; promote A7b to the *remaining* gaps (piecewise segmentation + polarity), not a new rate-search primitive. |

## The question (from the roadmap)

> T1 proved plain waveform correlation does **not** work (`PLAN_track_mix.md`), so design the
> **rate axis** (tempo/pitch-rate search added to the streamalign primitives) and validate the
> *idea* on one or two tracks Tim already hand-synced. **Deliverable: a design note + go/no-go**,
> promote to a Pass-2 sub-plan only if the spike works.

## Finding 1 — waveform correlation is dead (confirmed)

**In-repo anchor (verifiable here):** `scripts/streamalign/README.md` §Status states chroma+DTW
"recovers the mix/original rate **where waveform correlation cannot**" — the engine adopted DTW
precisely because correlation did not lock.

The underlying experiment lives in the **player repo** (the planning hub, *not* this repo):
`docs/PLAN_track_mix.md` §"2026-06-12 — T1 proof-of-concept" records a rate+offset+polarity
sweep using raw and PHAT cross-correlation of `010-Odyssey - Artificial Life` against its mix
region failing to lock — it reports best ncc ≈ 0.11 vs a ≈0.03 noise floor. The reason: the mix
is the DJ's EQ'd / level-ridden version captured as lossy 16 kHz RealAudio, spectrally far from
the clean source, so correlation (even whitened) is the wrong tool. *(Those exact ncc figures
are quoted from that external player-repo plan; the claim verifiable in **this** repo is the
README line above.)* Not re-litigated here.

## Finding 2 — the rate axis already exists (and it isn't a rate *search*)

The pivot the T1 PoC recommended (chroma + DTW) is **already implemented** in
`track_mix.chroma_dtw_rate()`:

- **Chroma CQT** features (12 pitch classes) are robust to the DJ's timbre/EQ and the lossy
  capture — they keep the harmonic content that survives.
- **Subsequence DTW** (`librosa.sequence.dtw(subseq=True)`, cosine metric) locates the mix
  excerpt *within* the original and returns the warp path.
- The **rate is the warp-path slope** — a robust **Theil-Sen** fit of mix-frame vs orig-frame
  (boundary flats trimmed). `rate = d(mix_time)/d(orig_time)`.

**Key design insight:** DTW recovers the rate *for free*. The roadmap framed A7 as "add a
tempo/pitch-rate **search** to the primitives" (resample the original over a rate grid, then
correlate). That extra axis is **unnecessary** — subsequence DTW already absorbs rate (and
local rate drift, and DJ edits) directly in the warp path. A separate rate-grid search would be
slower and redundant. The "rate axis" is the DTW slope, not a resampling sweep.

A **precision-first reliability gate** (`is_reliable`) guards it: trust the recovered rate only
when the warp path is both **straight** (R² ≥ 0.999) and a **close** chroma match (mean
per-frame DTW cost ≤ 0.03), and the slope is finite — so the engine flags rather than emits a
wrong rate.

## Validation — the idea works at scale (the spike's evidence)

The ground truth is Tim's `origNNN`/`track` sync pairs → per-track `rate` (the sheet's
`(trackB−trackA)/(origB−origA)`), via `track_sync_groundtruth()`. The aligner is graded against
it by `align_track` / `batch_align` (the `track-mix` CLI). Documented results (see
`scripts/streamalign/README.md` §Status, "G2/T1" and "G2/T2"):

- Of the **26** synced tracks with an original on disk: **18 pass the reliability gate, 15 are
  within the strict rate tolerance (|rate_err| ≤ 0.005)**.
- The remainder are **correctly flagged** (gross mismatch / mix span longer than the source /
  too-short region), not silently wrong — the precision-first gate doing its job.
- The threshold-calibration snapshot (tracks 8/10/13/16/23, in the `track_mix.py` gate comment):
  **13** (err 0.0019) and **16** (err 6e-5) clear the gate and the tolerance; **23** is clearly
  rejected (wrong-match, conf 0.77 / cost 0.050). Tracks **8** and **10** were rejected in *that*
  snapshot (8: empty mix → NaN; 10: degenerate slope, conf 0.99846 just under 0.999) but the
  later at-scale batch — after `_select_capture` picks the capture that actually contains each
  span — finds them **reliable-but-rate-disagrees** (the piecewise candidates below). So their
  verdict is sensitive to the mix region fed in; track 10's 0.99846-vs-0.999 is also the tightest
  threshold margin on record.

So the *idea* — chroma+DTW recovers the original↔mix rate where correlation can't — is **proven
on more than one or two hand-synced tracks**; it is validated across the whole synced set with a
working precision gate. (A *fresh* run needs the librosa `.venv` —
`PYTHONPATH=scripts .venv/bin/python -m streamalign track-mix` — which is not installed on every
machine; the figures above are the engine's own recorded validation against Tim's sync points.)

## What's actually still open (→ scope for A7b)

Because the single-rate axis is solved, A7b should target the gaps the batch run *already
surfaces*, in priority order:

1. **Piecewise segmentation (the real T2).** `chroma_dtw_rate` fits **one** global Theil-Sen
   slope per track — a single rate. DJ edits (cuts, loops, drops) and mid-track tempo rides make
   the true map **piecewise-linear** (`t_master = offset_k + rate_k·t_orig` per segment k). The
   batch run names the exact candidates: tracks **8, 10, 19** are *reliable but the rate
   disagrees* — i.e. the warp path is clean enough to trust yet a single slope is wrong, the
   signature of a piecewise track. **Start A7b here**, on 8/10/19: detect warp-path knees →
   segment → per-segment rate, validate each segment against the relevant `A`/`B` sync pair.
2. **Polarity / inversion.** Tim's notes flag "now inverted?" on some tracks; not handled today.
   Chroma is polarity-agnostic so DTW won't catch a phase flip — decide whether polarity even
   matters for the rate map, or detect it separately if it does.
3. **Premise & coverage failures.** Subsequence DTW requires mix ≤ original (flagged: "mix
   region longer than original"); empty/too-short mix regions NaN out. These bound applicability
   and partly reflect upstream placement/`_select_capture`, not the rate axis itself.
4. **Threshold / recall calibration.** The gate (R² ≥ 0.999, cost ≤ 0.03) is precision-first and
   calibrated on a handful of tracks; the tightest margin is track 10 (0.99846 vs 0.999). Widen
   the validation set before tightening thresholds; 15/26 within strict tolerance is *precision*
   — the gap to 26 is the **recall** question A7b should quantify.

## Go / no-go

**GO**, with a redirect. The rate axis is proven (chroma+DTW, validated at scale, precision-gated)
— so promote A7b to a Pass-2 sub-plan, scoped as:

- **Do:** piecewise / DJ-edit segmentation (T2), starting on the named candidates **8, 10, 19**;
  then polarity, then recall/threshold calibration on a wider set.
- **Don't:** build a separate tempo/pitch **rate-grid search** primitive — DTW already provides
  the rate axis; a resampling sweep would be redundant and slower.

This note supersedes the roadmap's "add a rate-search axis" framing for A7 with what the code
already demonstrates; the remaining science is segmentation, not rate discovery.
