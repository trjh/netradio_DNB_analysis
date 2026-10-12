# Decision points

> **What this is:** the log of numbered design decisions (`DP-nn`) that the code, the tests and the
> documents cite.
>
> **Fits in:** [README](../README.md) (master) → Reference.

The code, the tests and the design documents cite decisions as `DP-nn`. The numbers come from one
large design phase in August 2026, when the align tools and the label workflow took shape, and they
have stuck. This file lists each one, with a line of summary and a pointer to the first place in
this repo where the decision was written down.

A new decision takes the next free number. Numbers are never reused.

| DP | Decision | First written down |
|---|---|---|
| DP-01 | The machine-checked `verified` token on `track sync:` and `origNNN sync:` rows flows through `sort_tsv` into the published sheet rows, and never mixes with the file-sync `verified` | [`labels/sort_tsv.py`](https://github.com/trjh/netradio_DNB_analysis/blob/main/labels/sort_tsv.py) |
| DP-02 | `match-hints` trims the stream around the expected position before running MATCH: the track's place on the timeline, or `--around SECONDS` | [`scripts/streamalign/__main__.py`](https://github.com/trjh/netradio_DNB_analysis/blob/main/scripts/streamalign/__main__.py) |
| DP-03 | MATCH is a referee, not an anchor source: GCC-PHAT gives the anchors, and a disagreement over about 0.25 s writes a `note QUESTION:` row | [`scripts/streamalign/__main__.py`](https://github.com/trjh/netradio_DNB_analysis/blob/main/scripts/streamalign/__main__.py) |
| DP-04 | Solo-anchor moments from `streamalign hints` seed the spots `match-hints` probes | [`scripts/streamalign/__main__.py`](https://github.com/trjh/netradio_DNB_analysis/blob/main/scripts/streamalign/__main__.py) |
| DP-05 | Batch mode: `match-hints` for every known original of a capture | [`scripts/streamalign/__main__.py`](https://github.com/trjh/netradio_DNB_analysis/blob/main/scripts/streamalign/__main__.py) |
| DP-06 | Start-row bookkeeping: `sync-audit` derives a missing `origNNN start:` seat from its neighbours, or reports it | [`scripts/streamalign/sync_audit.py`](https://github.com/trjh/netradio_DNB_analysis/blob/main/scripts/streamalign/sync_audit.py) |
| DP-07 | A keep-warm DSP worker holds the decoded pair and answers slice and refine requests over a pipe | [`scripts/streamalign/__main__.py`](https://github.com/trjh/netradio_DNB_analysis/blob/main/scripts/streamalign/__main__.py) |
| DP-08 | A refine-algorithm selector: GCC-PHAT or MATCH cost | [`scripts/streamalign/__main__.py`](https://github.com/trjh/netradio_DNB_analysis/blob/main/scripts/streamalign/__main__.py) |
| DP-09 | The inspector's context strip: a zoomed-out lane pair around the point, playable from anywhere in it | [`scripts/streamalign/__main__.py`](https://github.com/trjh/netradio_DNB_analysis/blob/main/scripts/streamalign/__main__.py) |
| DP-10 | A whole-capture overview: the stream waveform with numbered sync flags | [`scripts/streamalign/README.md`](https://github.com/trjh/netradio_DNB_analysis/blob/main/scripts/streamalign/README.md) |
| DP-11 | Residuals shown as a continuous colour gradient, with the numeric alignment values beside them | [`public/align.html`](https://github.com/trjh/netradio_DNB_analysis/blob/main/public/align.html) |
| DP-12 | The inspector's settings and routing choices persist between sessions | [`public/align.html`](https://github.com/trjh/netradio_DNB_analysis/blob/main/public/align.html) |
| DP-13 | The implied rate across the selected points is shown, and updates as the points change | [`public/align.html`](https://github.com/trjh/netradio_DNB_analysis/blob/main/public/align.html) |
| DP-14 | Lane annotations sit outside the panels, naming each file and the original's rate correction | [`public/align.html`](https://github.com/trjh/netradio_DNB_analysis/blob/main/public/align.html) |
| DP-15 | A now-playing cursor moves through every lane during playback | [`public/align.html`](https://github.com/trjh/netradio_DNB_analysis/blob/main/public/align.html) |
| DP-16 | The inspector exports verified points as label rows ("include in results") | [`docs/PLAN_align_tool.md`](https://github.com/trjh/netradio_DNB_analysis/blob/main/docs/PLAN_align_tool.md) §5.3 |
| DP-17 | A launcher script, `scripts/run_align.sh`, for the standalone align server | [`docs/PLAN_align_tool.md`](https://github.com/trjh/netradio_DNB_analysis/blob/main/docs/PLAN_align_tool.md) §5.3 |
| DP-18 | The overview draws every hints set's flags for the capture; clicking another pair's flag switches to that pair | [`docs/PLAN_align_tool.md`](https://github.com/trjh/netradio_DNB_analysis/blob/main/docs/PLAN_align_tool.md) §5.3 |
| DP-19 | A visual summary of a change, built with no third-party service | [`docs/PLAN_align_tool.md`](https://github.com/trjh/netradio_DNB_analysis/blob/main/docs/PLAN_align_tool.md), the build record |
| DP-20 | The points `sync-audit` could not find are checked by ear, at the user's pace | [`docs/PLAN_align_tool.md`](https://github.com/trjh/netradio_DNB_analysis/blob/main/docs/PLAN_align_tool.md), *Closing status* |
| DP-21 | A minimal native window for the inspector, built later rather than first | [`docs/PLAN_align_tool.md`](https://github.com/trjh/netradio_DNB_analysis/blob/main/docs/PLAN_align_tool.md), *Closing status* |
| DP-22 | The whole-song left/right review mode: a phone-first web page, then a Mac app | [`docs/PLAN_align_tool.md`](https://github.com/trjh/netradio_DNB_analysis/blob/main/docs/PLAN_align_tool.md) §6 |
| DP-23 | Originals are kept as FLAC beside their lossless masters, and review audio serves FLAC first with no transcode fallback | [`docs/PLAN_align_tool.md`](https://github.com/trjh/netradio_DNB_analysis/blob/main/docs/PLAN_align_tool.md) §6.8 |
| DP-24 | The labels layout: `labels/automated/`, `labels/review/` and `labels/summary/` | [`docs/PLAN_align_tool.md`](https://github.com/trjh/netradio_DNB_analysis/blob/main/docs/PLAN_align_tool.md) §6.14 |
| DP-25 | The export writes back to its input files, built as the summary compile | [`docs/PLAN_align_export_writeback.md`](https://github.com/trjh/netradio_DNB_analysis/blob/main/docs/PLAN_align_export_writeback.md) §A |
| DP-26 | Anchor rows (sync, start and end) are written without the trailing ` HINT`: the ` verified` token and the confidence carry their provenance, and every reader accepts rows with or without it | [`scripts/align_inspect.py`](https://github.com/trjh/netradio_DNB_analysis/blob/main/scripts/align_inspect.py) |
| DP-27 | The overview's numbered point flags sit on the lane's bottom edge, clear of the legend | [`docs/PLAN_align_tool.md`](https://github.com/trjh/netradio_DNB_analysis/blob/main/docs/PLAN_align_tool.md) §5.3 |
| DP-28 | The inspector's context strip comes before its controls, and the controls' text is two points larger | [`docs/PLAN_align_tool.md`](https://github.com/trjh/netradio_DNB_analysis/blob/main/docs/PLAN_align_tool.md) §5.3 |
| DP-29 | The inspector's title shows the original's native clock, seconds into the original at its own rate, beside the stream time, and updates as the seat moves | [`docs/PLAN_align_tool.md`](https://github.com/trjh/netradio_DNB_analysis/blob/main/docs/PLAN_align_tool.md) §5.3 |
| DP-30 | A pair's point adjustments survive a reload, with a note saying what was restored and a control to discard them | [`docs/PLAN_align_tool.md`](https://github.com/trjh/netradio_DNB_analysis/blob/main/docs/PLAN_align_tool.md) §5.3 |
| DP-31 | Points start not included; each is untouched, examined or included, and only included points export | [`docs/PLAN_align_tool.md`](https://github.com/trjh/netradio_DNB_analysis/blob/main/docs/PLAN_align_tool.md) §5.3 |
| DP-32 | The export carries the inspector's refined confidence (the correlation × 10) for a refined point, and the hint's own score otherwise | [`docs/PLAN_align_tool.md`](https://github.com/trjh/netradio_DNB_analysis/blob/main/docs/PLAN_align_tool.md) §5.3 |
| DP-33 | The disagreement between MATCH and GCC-PHAT is drawn on the waveforms, not smoothed away | [`scripts/align_inspect.py`](https://github.com/trjh/netradio_DNB_analysis/blob/main/scripts/align_inspect.py) |
| DP-34 | That disagreement is drawn on the inspector's lanes only, with the gap at the seat as a number; the overview and the context strip keep their full detail | [`docs/PLAN_align_tool.md`](https://github.com/trjh/netradio_DNB_analysis/blob/main/docs/PLAN_align_tool.md) §5.3 |
| DP-35 | The disagreement shading fills the lane body, and the lane titles say what it means | [`docs/PLAN_align_tool.md`](https://github.com/trjh/netradio_DNB_analysis/blob/main/docs/PLAN_align_tool.md) §5.3 |
| DP-36 | The review mode plays each source at its unmodified level, compensates the stereo-to-mono down-mix, and gives each source a 0–200 % volume slider | [`docs/PLAN_align_tool.md`](https://github.com/trjh/netradio_DNB_analysis/blob/main/docs/PLAN_align_tool.md) §6.9 |
| DP-37 | The review mode's waveform lanes are twice as tall and zoom the time axis ×1, ×2 or to the most the data shows | [`docs/PLAN_align_tool.md`](https://github.com/trjh/netradio_DNB_analysis/blob/main/docs/PLAN_align_tool.md) §6.9 |
