# labels/summary/ — one importable label set per audio file

`<stem>.summary.tsv` (stream-clocked) and `origNNN.summary.tsv` (original-clocked):
the compile of `labels/automated/` + `labels/review/` into exactly one label file
per capture and one per original — the set the manual Audacity review imports
(tracks 3 and 6 of the six-track session; see `PROCESS.md`). Derived output —
regenerable at any time — committed anyway for phone visibility and disaster
recovery. Written by the companion player project's compile (2026-08-28): its
`/align` inspector's **⟳ compile summaries** button or `make compile-summary`
there — whole-file, idempotent, rows copied verbatim plus one `origNNN start:`
clip-seat row per original in each stem summary. Never hand-edited: the next
compile overwrites it. Rides the `align-review-data` branch with `review/`.
