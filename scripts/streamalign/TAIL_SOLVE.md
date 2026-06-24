# Tail solve — placing d376-395 … d525-532

> **What this is:** how the unlabelled tail captures get onto the master clock (status + method).
> **Fits in:** [README](./README.md) → "Known limits / the tail".

The tail was the standing blocker: the last 16 capture windows (`d376-395` through
`d525-532`) had no hand alignment, and the generic blind solver couldn't reach them —
"the unlabelled tail captures do not overlap the anchored region by enough for blind
alignment to bridge them." `tail.py` (CLI: `streamalign tail-solve`) solves all but one,
by using two facts the generic solver didn't exploit.

## The two facts

**1. The tail is its own dense overlap component ("Session B").** `d416-435` …
`d525-532` (14 files) overlap *each other* cleanly — every internal edge aligns at
confidence > 0.98, and the internal solve is rigid: all 14 files corroborated, **max
residual 0.000 s**. The component was never the problem; it just had no edge to the
anchored timeline, so it floated.

**2. The broadcast is a loop, and Session B's tail wraps onto the loop start.** The
last files (`d512-005`, `d525-532`, `d505-*`) overlap the *placed* loop-start anchor
`d000-018` (master 0) and `d001-026b` at confidence > 0.99. That wrap edge is the
missing anchor — it pins the whole rigid body to the master clock.

## Why the anchor is trustworthy (and the two-offset trap avoided)

The anchor is set **only** from the clean, single-offset file `d000-018` /
`d001-026b`. Three independent edges agree to **0.000 s**:

| edge | offset | conf | implied S\* = master(d416-435) |
|---|---|---|---|
| d512-005 → d000-018 | 869.061 | 0.996 | −6928.648 |
| d512-005 → d001-026b | 923.453 | 0.996 | −6928.648 |
| d525-532 → d000-018 | 386.440 | 0.995 | −6928.648 |

The pre-roll files `d-25-000b` / `d-25-005b` are deliberately **excluded** from setting
the anchor: they contain both the loop end and beginning, so they match at *two* offsets
(the documented multi-match hazard). Their edges *do* corroborate the same anchor once
their own negative file-start offset is applied — e.g. the `d-25-000b` estimate of
−5416.704 differs from the clean −6928.648 by exactly **1511.944 s**, which is precisely
`d-25-000b`'s `note: file start … −1511.944`. Same anchor, seen through the wrap.

**Independent sanity check:** under the documented ~9-hour loop (L ≈ 32400 s), the
recovered placements convert to forward master-minutes that match the filenames — e.g.
`d416-435` → minute ≈ 424. The audio solve and the capture-naming agree.

## Representation

Placements are reported in the **loop-wrap (negative) representation**, anchored on
`d000-018 = 0` — consistent with the project's existing negative-master pre-roll files.
Session B spans master **−6928.6 s … −386.4 s** (i.e. the ~1h55 of programme that runs
*into* the `d000-018` loop point). Add the loop length L (≈ 32400 s) for the
forward-equivalent positions (≈ 25471 … 32014 s), once L is pinned.

## What is NOT placed

- **`d376-395`** — overlaps the last placed file `d356-375` by only ~59 s (conf ~0.60;
  mid-overlap NCC ~0.77 degrading at the edges → a partial / skip-affected lock). A
  placement **candidate** (master_start ≈ 22219.4 s) worth a by-ear confirm, but not a
  corroborated edge, so not emitted.
- **`d396-415`** — butt-jointed on both sides (no measurable overlap with `d376-395` or
  `d416-435`) and too far from the loop start to wrap. A genuine **orphan**, placeable
  only from contiguity/gap evidence (by ear, or capture logs). It floats in the ~852 s
  gap between the `d376-395` candidate and Session B.

## Usage

```
PYTHONPATH=scripts python3 -m streamalign tail-solve            # report (default)
PYTHONPATH=scripts python3 -m streamalign tail-solve --emit     # write <stem>.auto.labels.tsv
```

`--emit` writes AUTO GENERATED labels for the **14 corroborated Session-B files only**
(never the candidate or the orphan), into `labels/` (or `--out`). As with all auto labels
they never overwrite a hand `<stem>.labels.tsv`. **Recommended before emitting:** confirm
the load-bearing wrap anchor by ear — render a clip of `d512-005`'s overlap with
`d000-018` and verify it is the same broadcast audio.
