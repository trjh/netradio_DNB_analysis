# Plan: align export writes back to its input files (DP-25) → BUILT as the DP-24 §3 summary compile

> **What this is:** the record of the align inspector's export step: the original DP-25 ask
> (rewrite the input hints files instead of a browser download), its 2026-08-11 re-scope by
> DP-24 into **the summary compile**, and the build of 2026-08-30 — `summary_compile.py`
> + `POST /api/align/compile-summary` + the page's **compile summaries** button. The design
> proper lives in [PLAN_labels_layout.md](https://github.com/trjh/netradio_DNB_analysis/blob/main/docs/PLAN_labels_layout.md) §3; this doc keeps the
> history and the build notes.
>
> **Fits in:** [this repo's README](https://github.com/trjh/netradio_DNB_analysis/blob/main/README.md) (its master index) ·
> [PLAN_labels_layout.md](https://github.com/trjh/netradio_DNB_analysis/blob/main/docs/PLAN_labels_layout.md) §3 (the spec this implements) ·
> [PLAN_align_tool.md](https://github.com/trjh/netradio_DNB_analysis/blob/main/docs/PLAN_align_tool.md) (the tool) ·
> [RUNBOOK_align_workflow.md](https://github.com/trjh/netradio_DNB_analysis/blob/main/docs/RUNBOOK_align_workflow.md) §5–§6 (the workflow step).
>
> Decision points (`DP-nn`) are listed in [`docs/DECISIONS.md`](https://github.com/trjh/netradio_DNB_analysis/blob/main/docs/DECISIONS.md).

- **Status:** BUILT 2026-08-30; §2 (the Pass-2 write path, the inspector's seats as review rows) is DESIGNED 2026-08-30, not built — it is the next build item; the original DP-25 write-back sketch is superseded, kept below as the record
- **Drives:** `labels/summary/` in the analysis checkout — tracks 3 and 6 of the six-track Audacity review session

## 0. What was built (2026-08-30)

- **`summary_compile.py`**: reads `labels/automated/` + `labels/review/` in
  this checkout, emits `labels/summary/<stem>.summary.tsv`
  (stream-clocked) and `labels/summary/origNNN.summary.tsv` (original-clocked). Rows are
  copied **verbatim** (both `verified` families untouched, markers identical across the pair);
  the converter's per-anchor `origNNN start: k confidence n/10` **seat rows** ride with them
  (the native clip seat `a_k − b_native_k`, decision made 2026-08-11, corrected
  2026-08-17); the only rows the compile ADDS are a gap-filler where the converter emitted no
  seat at all (every anchor before the capture head): one row in the same grammar at the
  best-confidence anchor, tie → earliest, plus an `origNNN note:` naming its provenance
  (independent-review round, 2026-08-28 — the first cut invented a third `start:` grammar and
  duplicated the converter's rows). Review rows land on the record's clock by
  **reading** the `origNNN <orig_s>` their §6.6 text already carries; a review row the grammar
  cannot place there stays stream-side only and is counted (`unconverted`). Nothing is
  dropped. Sort: `sort_tsv.py`'s discipline (`file start` first, then time-major, then text).
- A torn final line (a review append in flight) is dropped and reported (`torn`), never
  copied; the review directory is read under `review_notes`' append lock; a non-UTF-8 input
  byte is a 500 that names no absolute path.
- **Whole-file, idempotent writes**: temp + fsync + rename per file, an unchanged file is
  left untouched, a `*.summary.tsv` the compile did not produce is stale derived output and is
  removed (the directory's `README.md` is not the compile's to touch). Identifiers come only
  from the file names in the two controlled directories (validated against the inspector's
  stem/orig shapes) and every path is containment-checked under its own `labels/` subdirectory — the route body
  names nothing (the §3.3 contract). The hand labels at the `labels/` root are neither read
  nor written (test-pinned).
- **`POST /api/align/compile-summary`** (`align_server.py`, behind the same `_gate`; 404 dark
  without an analysis checkout; `{"dry_run": true}` reports without writing). A real compile
  nudges `review_sync`, whose PATHSPEC already carries `labels/summary/*.summary.tsv`, so the
  files ride the `align-review-data` branch on the review cadence.
- **The page**: the Export section's first button is now **⟳ compile summaries**; the
  browser download of the hints pair stays as a second button (the DP-31/DP-32 ticked-points
  export is still the way to carry a session's refined seats out — folding that work into
  `labels/review/` is a separate call, see §1 below).
- **`make compile-summary`** (`ARGS=--dry-run`) runs the same compile from the shell.
- Tests: `tests/test_summary_compile.py` (hermetic scratch checkout — verbatim rows, seat
  arithmetic, orig-clock conversion, sort, idempotence, whole-file write + failure leaves old
  bytes, stale removal, root untouched, symlink escape ignored, route gates + nudge).

## 1. Open after the build

- **The inspector's refined seats do not reach `labels/review/`.** The compile reads
  automated + review only (§3: compile, not edit), so a Pass-2 session's ticked/refined points
  still leave via the download button. **Decided 2026-08-30: the inspector writes them as review
  rows — the design is §2 below**; it is the next build item.
- This repo's `labels/summary/README.md` still says *"Empty until the export-button
  compile ships"* — a one-line follow-up there.


## 2. The Pass-2 write path — agreed design (2026-08-30)

Decisions 1–6 were made 2026-08-30 and moved here 2026-09-07 so the design lives on `main`. Not built yet; it is to be built as two PRs (server, then page — item 5).

*Terms.* Pass 2 = the `/align` inspector: one capture↔original pair, one sync marker at a
time; the operator moves the original's **seat** (its native-clock instant for that
marker; the stream instant never moves), runs **snap-to-best** (returns an `ncc`
correlation score), and **ticks** markers to include. Today that state lives only in the
browser (`localStorage`) and leaves via the download button. The **compile** =
`summary_compile.py`: regenerates `labels/summary/` from `labels/automated/` +
`labels/review/`.

**1. The row family (decision 1 — adopted).** Appended to the existing per-pair file
`labels/review/<stem>.origNNN.review.tsv`, beside the note rows:

```text
<stream_s>	<stream_s>	note REVIEW seat r<N>: <k> origNNN <native_s> <hint|snap|hand> confidence <n>/10 <included|excluded> <date>
```

- `r<N>` — per-file monotonic write sequence, allocated under the append lock by scanning
  existing `seat r` rows (the `s<K>` pattern; independent counter). For each `(orig, k)`
  the row with the highest `r` is current — survives `sort_tsv.py` re-sorting, which text
  order alone cannot (all rows for a marker share the time column and prefix).
- Time columns = the marker's stream instant (capture-local seconds, 6 dp).
  `origNNN <native_s>` = the operator's seat on the original's native clock — the
  anchor-row shape, so the compile's orig-clock conversion places it unchanged.
- `hint|snap|hand` = provenance (converter's seat / last action was a snap / nudged by
  hand). `confidence n/10` = what the download emitted (ncc×10 when refined, else the
  hint score). `included|excluded` = the tick. `<date>` = ISO date of the write.
- Seat rows carry **no `verified` token** (§6.6 rule, test-pinned) and no session key: the
  inspector has no §6.2 mapping (mode / rate / anchor set) to record.
- **discard stored adjustments** writes one row per worked marker at the hint seat,
  `hint … excluded`. No reset grammar.

**2. The compile (decisions 2 + 3).** For each `(orig, k)` take the highest-`r` seat row:
- `included` → the operator's seat **replaces** the converter's `origNNN start: k
  confidence n/10` row (seat = `stream_s − native_s`) + an `origNNN note:` naming `r<N>`
  and the provenance (the gap-filler pattern). **Decision 3:** the summary's `origNNN
  sync: k` and `track sync: k` rows for that marker **move to the operator's instant and
  gain the bare ` verified` token** (the ` verified` token on `track sync:`/`origNNN sync:` rows, as the label grammar in `PROCESS.md` describes it — confirmed by correlation
  or the operator in the align flow; what the download delivered). The automated file is
  untouched.
- `excluded` or no row → converter rows verbatim (today's behaviour).
- **Decision 2:** the summary copies **only the current seat row per marker**; superseded
  rows stay in `labels/review/` as history and are counted (`superseded`).
- **Decision 5:** the compile **runs automatically after every seat write** (idempotent);
  the ⟳ button stays as the manual re-run. `labels/summary/` is therefore always current.

**3. Routes.** `POST /api/align/review-seat {stem, orig, k, stream_s, orig_s, how,
confidence, included}` — §3.3 validation (identifiers against listings; `k` must be one
of the pair's hint markers and `stream_s` must equal that marker's instant at 6 dp, so a
row can never seat a marker at a moved anchor); write + flush + fsync before the 200;
nudge `review_sync`; then the compile. `GET /api/align/review-seats?stem=&orig=` → the
current row per marker (the page's restore; a later Mac app port uses the same).

**4. The page (decision 4 — `localStorage` retired).** Tick → write now; snap → write
after the refine response; nudge → 600 ms debounce at the folded seat; pair open → restore
from the GET. Indicator beside the export section: `saved ✓ r14` / `saving…` / red sticky
`FAILED — <error> (seats NOT saved)`. A dark server (GET 404 on load) shows a persistent
red banner saying the checkout is not configured. Download button removed.

**5. Delivery (decision 6).** Two PRs from `main`: server side
(writer, routes, compile preference + auto-run, tests; doc updates to `PLAN_align_tool.md`
§6.6, the label grammar in `PROCESS.md`, `PLAN_align_export_writeback.md` §1), then the page side
(autosave, indicator, restore, download + `localStorage` removal, tooltips can ride here
or in their own PR). Each reviewed independently.

---

## A. The original DP-25 record (2026-08-08 → re-scoped 2026-08-11)

> **⚠ RE-SCOPED 2026-08-11 by DP-24** (PLAN_align_tool.md §6.14, the labels/
> reorganisation): the export button's job is now **compile `labels/automated/` +
> `labels/review/` into `labels/summary/`** — one importable label set per audio
> file (per stem and per original) — NOT rewriting its input files. The ask below
> is kept as the original record; the §3 constraints about overwrite semantics
> and partial sessions largely dissolve, because summary/ is derived-only and
> idempotent to rebuild.

### A.1 The ask (2026-08-08)

> "This export process — I don't like it. I don't want to hand-download shit with
> the browser. We should have the export button just rewrite the files we used for
> input."

### A.2 What happened then

The **Export** button downloads two files through the browser, which then have to
be found in the downloads folder and imported into Audacity by hand. The server
never writes anything.

The files it downloads are regenerated copies of the **same two files the session
was loaded from** — the Pass-1 hints pair in the labels directory:

| File | Side |
|---|---|
| `<stem>.origNNN.match.hints.tsv` | stream |
| `origNNN.<stem>.match.hints.tsv` | original |

So the round trip is: read those two files, adjust seats in the browser, download
two near-identical files, then manually reconcile. The ask is to close that loop.

### A.3 Scope — which files, and which files not

**In scope: the hints files only.** These are already machine-written scratch —
`streamalign match-hints` generates them, they are untracked and gitignored, and
neither the label pipeline nor the publish flow ever reads them. Rewriting them
is low-stakes by construction; the tool that produces them overwrites them on
every re-run already.

**Out of scope: the committed `.labels.tsv` files.** Those stay hand-owned and
reached only through the normal `sort_tsv.py` path (decision made 2026-08-08). No
part of this task touches them.

### A.4 Constraints any design had to answer

Listed as open questions, deliberately unanswered.

- **The server gains its first write path.** `align_server.py` reads today and
  writes nothing, so the question is not *whether the data is safe to write* —
  it is scratch — but **how the page names the file it wants written**.
  `align_inspect.py`'s §5c contract already answers that shape for reads:
  identifier-based requests, hard caps, no free-form paths. The write path
  belongs inside that contract, not beside it.
- **Overwrite semantics are undecided.** In place, or write-then-swap? Is the
  previous version kept? What happens when the file changed on disk since the
  session loaded it?
- **Partial sessions.** Today a download is all-or-nothing. A write-back that
  lands half a session should not be able to.
- **Which rows win.** Untouched rows, hand-moved rows, and re-snapped rows carry
  different provenance; the ` verified` token rules in
  the label grammar in [`PROCESS.md`](https://github.com/trjh/netradio_DNB_analysis/blob/main/PROCESS.md#label-grammar) (the two families never mix) must
  survive the round trip.
- **Relationship to the ` HINT` suffix drop** (DP-26, #128)
  — the writer must emit the post-DP-26 grammar, not the legacy one.
- **Does Audacity still sit in the loop at all**, or does write-back plus the
  existing `sort_tsv.py` path make the manual import step unnecessary? This may
  shrink `RUNBOOK_align_workflow.md` §6 rather than just rewording it.

### A.5 Status (of the original ask)

Superseded by the DP-24 §3 compile — built 2026-08-30, see §0 above.
