# Plan: the labels layout — automated / review / summary (DP-24)

> **What this is:** the design for reorganising the analysis repo's `labels/` directory into
> three committed subdirectories — `labels/automated/` (machine output), `labels/review/` (the
> semi-manual annotation blend), `labels/summary/` (one importable label set per audio file) — and for the export
> step that compiles the first two into the third. **Direction agreed 2026-08-11; step 1 (the analysis
> migration, #133) merged 2026-08-25, with the server's hints reader (`align_inspect.parse_match_hints`) updated to match; the §3
> summary compile BUILT 2026-08-30 (`summary_compile.py`, build notes in
> [PLAN_align_export_writeback.md](https://github.com/trjh/netradio_DNB_analysis/blob/main/docs/PLAN_align_export_writeback.md) §0).** Extracted from
> [PLAN_align_tool.md](https://github.com/trjh/netradio_DNB_analysis/blob/main/docs/PLAN_align_tool.md) §6.14 because it touches a wider surface than DP-22: every label-producing script, the
> gitignore posture, `review_sync`, the export button, and the analysis repo's
> `PROCESS.md`.
>
> **Fits in:** [this repo's README](https://github.com/trjh/netradio_DNB_analysis/blob/main/README.md) (its master index) · sits between
> [PLAN_align_tool.md](https://github.com/trjh/netradio_DNB_analysis/blob/main/docs/PLAN_align_tool.md) (whose §6.7 route writes into `labels/review/` and whose
> §6.14 now just points here) and the **analysis repo** (which owns `labels/` and gets the
> migration PR). Supersedes the DP-25 write-back sketch (re-scoped, decision made 2026-08-11).
>
> Decision points (`DP-nn`) are listed in [`docs/DECISIONS.md`](https://github.com/trjh/netradio_DNB_analysis/blob/main/docs/DECISIONS.md).

- **Status:** all three steps built — migration (#133) and the server's hints-reader update (`align_inspect.parse_match_hints`) merged 2026-08-25, the summary compile built 2026-08-30
- **Drives:** the analysis-repo migration PR, the §6.7 review-note target path, `review_sync`'s pathspec, the export button, the output directory of `matchconv` (the `streamalign` module behind `match-hints`), `PROCESS.md`

## 1. The ask (2026-08-11)

> "So far all the scripts have produced purely automated data. But this one takes manual and
> automatic data and then lets me annotate it, resulting in a new blend of semi-manual data.
> Which should be kept. In git. Especially if I'm using an app on a phone. […] We also have the
> problem that the labels/ directory is getting more cluttered. There are a lot of scripts that
> produce multiple labels."

And the flow that must survive intact:

1. manual & automated data →
2. **manual review in Audacity** — where the automated input is **exactly ONE label set per
   audio file** (one per stream stem, one per original), never N per-pair files →
3. **one authoritative label file per stem, manually authorised**
   (`labels/<stem>.labels.tsv`) — reached only through the human fold; no automation writes
   it, ever.

**The four paths, stated once and used verbatim everywhere** (all relative to the analysis
repo root — a bare `review/` in any doc is an error, since it reads as a repo-root directory):

| Path | Contents |
|---|---|
| `labels/*.tsv` | hand-curated label files — the record of truth |
| `labels/review/*.tsv` | labels created by the align tool / its review page |
| `labels/automated/*.tsv` | labels created by other script output |
| `labels/summary/*.tsv` | one label file per stream or original, built from `labels/review/` + `labels/automated/` |

## 2. The layout

Three subdirectories, **all kept in git**; the `labels/` root keeps only the authoritative
files and the two human tools (`sort_tsv.py`, `publish.py`).

| Directory | Holds | Written by | Regenerable? |
|---|---|---|---|
| `labels/automated/` | analysis/processing output — the `match-hints` pairs today, every future script's label emissions | the analysis scripts; freely overwritten on re-run | yes — pure machine output |
| `labels/review/` | `<stem>.origNNN.review.tsv` — machine context rows + hand notations (the §6.6 grammar) | `align_server.py`'s review-note route; auto-committed by `review_sync` | **no — hand judgement; the reason this is in git** |
| `labels/summary/` | **one file per stem + one per original** — the single label set step 2 imports | the export button: compile `labels/automated/` + `labels/review/` → `labels/summary/` | yes — derived; committed anyway for phone visibility + DR |
| `labels/` (root) | the ~20 authoritative `<stem>.labels.tsv` + `sort_tsv.py` / `publish.py` | the human, via Audacity + the fold | **no — the record of truth** |

**Separation of regenerability is the design.** Machine output may be clobbered at will
because nothing manual ever lives beside it; hand judgement is never in a file any tool
regenerates. No note-preservation machinery exists because none is needed.

## 3. The summary compile (the export button's job)

**Built 2026-08-30** — `summary_compile.py` + `POST /api/align/compile-summary` + the page button + `make compile-summary`; build notes in [PLAN_align_export_writeback.md](https://github.com/trjh/netradio_DNB_analysis/blob/main/docs/PLAN_align_export_writeback.md) §0, tests in `tests/test_summary_compile.py`. Supersedes DP-25's write-back sketch (re-scoped, decision made 2026-08-11). **Compile, not edit:** read
everything under `labels/automated/` and `labels/review/`, emit `labels/summary/`, touch
nothing else. Idempotent — derived-only output, safe to run at any time, any number of times.

**The Audacity session the summaries exist to serve.** During manual review (step 2 of the
flow) the expectation is a project with **six tracks visible** — the two audio files, each
flanked by its authoritative labels and its summarized labels:

| # | Track | Source |
|---|---|---|
| 1 | stream audio | the capture |
| 2 | stream authoritative label data | `labels/<stem>.labels.tsv` |
| 3 | **stream summarized label data** | `labels/summary/<stem>.summary.tsv` — imports aligned from zero |
| 4 | original audio | the record |
| 5 | original authoritative label data | the hand labels for that record |
| 6 | **original summarized label data** | `labels/summary/origNNN.summary.tsv` — lines up with the record's start, then sync-lock moves with it |

The compile produces tracks 3 and 6; everything else already exists. "Track 3" / "track 6"
below refer to this table.

- **Per stem — `labels/summary/<stem>.summary.tsv`** (stream-clocked): every automated anchor
  row for that capture (all its pair files merged) + every review row for it, time-sorted.
- **Per original — `labels/summary/origNNN.summary.tsv`** (original-clocked): that original's
  rows from **every capture it appears in**, converted to the record's native clock, merged
  and time-sorted. Merging across captures also retires the clobbered-by-name hazard in
  `matchconv` (the `streamalign` module implementing `match-hints`, the Pass-1 converter —
  `scripts/streamalign/matchconv.py` in the analysis repo) — the merge is the point.
- **Cross-track alignment rows (decision made 2026-08-11).** The per-stem summary also
  carries, for each original it covers, that original's **seat row(s)** — the converter's own
  per-anchor `origNNN start: k confidence n/10` rows ride verbatim (#132 emits one per
  sync point); where the converter emitted none (every anchor seats the record before the
  capture's first sample, which it omits) the compile fills the gap with one row in the same
  grammar at the best-confidence anchor plus an `origNNN note:` saying so (decision made
  2026-08-28) — `origNNN start:` at
  the original's **native clip seat** (where the record, imported at its own rate, is
  placed so it lines up at that anchor: seat = a − b_native; NOT the rate-corrected
  zero, which is where the record's local 0 *sounds* in the broadcast, not where the
  clip goes — decision made 2026-08-17) — and every anchor row keeps its
  **marker number** identical in both files (`track sync: k` in the stem summary ↔
  `origNNN sync: k` in the original summary). Together these let the operator place track 4
  (drag the record so its start sits on the `origNNN start:` label in track 3) and then
  verify by eye that point *k* lines up across tracks 3 and 6.
- Every row is already self-describing (both clocks carried, one as position, one as text —
  the §6.6 grammar), so the compile is parsing + arithmetic; no DSP.
- **Sort discipline:** rows ride the same ordering `sort_tsv.py` produces (time-major), so a
  summary file diffs cleanly and imports predictably.

## 4. Consumers that change

| Surface | Change |
|---|---|
| `matchconv.hint_filenames` (analysis) | output directory → `labels/automated/` |
| `align_inspect.parse_match_hints` (the server) | scan `labels/automated/` first, labels root as the pre-migration fallback (the read path this table originally missed — caught 2026-08-22) |
| analysis `.gitignore` | retire `labels/*.hints.tsv` (automated/ is committed) |
| `PROCESS.md` (analysis) | step 9 + the artifact table describe the three directories and the summary import — **definitely needs the update** (decision made 2026-08-11) |
| §6.7 review-note route (the server) | target `labels/review/<stem>.origNNN.review.tsv` (the pre-DP-24 repo-root `review/` stays read-compatible until the migration lands) |
| `review_sync` pathspec (the server) | `labels/review/` + `labels/summary/` exactly — physically cannot reach the authoritative root files (PLAN_align_tool §6.7 invariant 9) |
| export button (the server) | the §3 compile |
| existing demo data | repo-root `review/d376-395.orig072.review.tsv` (pre-DP-24 location) (10 rows, on `align-review-data`) migrates into `labels/review/` |

## 5. Build order

1. **Analysis-repo migration PR**: create the three directories, move/regenerate the loose
   hints into `labels/automated/`, move the demo review file, `hint_filenames` outdir, gitignore,
   `PROCESS.md`.
2. **The server**: the §6.7 route + `review_sync` land pointing at the new paths (the change is
   re-scoped if it has not merged by then; otherwise a follow-up).
3. **Export button**: the summary compile. — **built 2026-08-30.**

## 6. Decided questions (decision made 2026-08-11)

- **Summary filename form: `<stem>.summary.tsv`** — so it appears that way in Audacity's
  track list.
- **`note QUESTION:` rows ride into summary — as does ALL automated output.** The compile
  drops nothing.
- **`review_sync` auto-commits `labels/summary/` too**, on the same cadence as
  `labels/review/`.
