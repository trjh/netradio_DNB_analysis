# Align workflow — fresh capture + original → verified labels

> **What this is:** the end-to-end operator walkthrough of the CURRENT align flow — from a
> capture + an original on disk, through `streamalign match-hints` (Pass 1) and the `/align`
> inspector (Pass 2), to verified sync rows in the Audacity labels. Compact by design: each
> step points at the doc that owns its detail.
>
> **Fits in:** [the repo's README](https://github.com/trjh/netradio_DNB_analysis/blob/main/README.md) (master index). The tool's design/build record is
> [`PLAN_align_tool.md`](https://github.com/trjh/netradio_DNB_analysis/blob/main/docs/PLAN_align_tool.md); the label semantics (incl. the two
> `verified` families) are in [the label grammar in `PROCESS.md`](https://github.com/trjh/netradio_DNB_analysis/blob/main/PROCESS.md#label-grammar);
> the surrounding labelling pipeline (steps 6–11) is [`PROCESS.md`](https://github.com/trjh/netradio_DNB_analysis/blob/main/PROCESS.md) — this doc
> references it, never duplicates it.
>
> Decision points (`DP-nn`) are listed in [`docs/DECISIONS.md`](https://github.com/trjh/netradio_DNB_analysis/blob/main/docs/DECISIONS.md).

## Prerequisites (once per machine)

- This repo checked out with its venv built (`make env` builds `.venv` with uv from
  both requirement sets and also installs the MATCH binaries: `sonic-annotator` + `match-vamp`).
  Activating that venv puts its `scripts/` on the path, which is why the commands below carry no
  `PYTHONPATH`.
- The server finds this checkout from its own location; no setting names it.
- This repo's `.env`: `NETRADIO_LABELS_DIR` (default `labels/`) and `NETRADIO_MP3_DIR`;
  optionally `NETRADIO_SOURCES_DIR` (the originals; default `sources_local/`); for the align
  server, `NETRADIO_ALIGN_HOST` / `NETRADIO_ALIGN_PORT`, and `NETRADIO_AUTH_USER` /
  `NETRADIO_AUTH_PASS` for Basic Auth.
- The original on disk as `sources_local/NNN-Artist - Title.<ext>` — no original, no
  alignment (as of 2026-10-11, some originals are still missing).

## 1. Pass 1 — propose the sync points (`match-hints`)

Run in **this repo's checkout** (`scripts/run_align.sh help` prints these commands).

```bash
. .venv/bin/activate && python -m streamalign \
    match-hints <stem> <NNN>
#   match-hints <stem> --all              # batch: every original overlapping the capture
#   match-hints <stem> <NNN> --around S   # track not placed on the timeline yet
```

`streamalign` reads captures from the repo's `jaz_links/` symlink by default; set
`NETRADIO_AUDIO_DIR` if your captures are elsewhere.

Two algorithms do the work, and both run headless inside the converter. MATCH builds a
coarse map of where the original sits in the capture, and acts as referee. GCC-PHAT then
seats each anchor precisely.

**You never open Sonic Visualiser.** SV+MATCH came out of the standard flow
(decision made 2026-08-04). It survives only as an optional cross-check.

The converter writes two files per pair into `labels/automated/`:

| File | Side |
|---|---|
| `<stem>.origNNN.match.hints.tsv` | stream |
| `origNNN.<stem>.match.hints.tsv` | original |

Both land in this repo's `labels/automated/` (committed machine output since
DP-24, freely overwritten on re-run); neither the label pipeline nor the publish flow
ever reads them.

Anchor rows read `track sync: k verified confidence n/10`; boundary rows come one
`origNNN start:/end:` pair per sync point, named by its marker number (`start: k` /
`end: k`, bare `confidence n/10`, never ` verified`) at the point's own implied
NATIVE clip seat. Anchors carry no ` HINT` suffix. For the full grammar see
[the label grammar in `PROCESS.md`](https://github.com/trjh/netradio_DNB_analysis/blob/main/PROCESS.md#label-grammar).

Before trusting a pair, answer any `note QUESTION:` rows it carries — those flag thin
coverage, or a rival seat one loop-length away.

Full step detail: [`PROCESS.md`](https://github.com/trjh/netradio_DNB_analysis/blob/main/PROCESS.md) step 9 (Pass 1).

## 2. Open the inspector

In the same checkout:

```bash
scripts/run_align.sh start     # own process/port (8766); status | stop | restart | help
```

`start` prints the data-loading primer (empty page = no hints files yet, not a bug); the
page lists every `*.match.hints.tsv` set it finds in the labels dir.

## 3. Pass 2 — verify / hand-tune each point

Per point, in the inspector (stream fixed, original shiftable):

- **Listen and look:** combined playback (`space`) and the **subtraction null** (`s`) —
  the live inverse-sum lane, no Invert/un-Invert dance; slow/loop as needed (`[` / `]`).
- **Snap-to-best** under either engine (**PHAT | MATCH**); the **MATCH−PHAT gap shading**
  lightly tints the whole body of the inspector's stream/original lanes wherever the two
  engines disagree (clean = agreement ≤ 20 ms; green → red = growing gap; the numeric gap
  at the seat rides the metrics line, and the lane titles explain the shading in words) so
  an engine dispute is visible, not smoothed over. (DP-34/35: the shading lives on the
  inspector lanes only — the overview/context stay pure data.)
- **Re-snap after any hand move** — the snap *is* the machine check behind the
  ` verified` token; a hand-moved, never-re-snapped seat has no measurement.
- **Tick "include"** when examined *and* accepted (gray = untouched, colored = examined,
  ✓ = included; only ticked points export). Adjustments persist per pair (localStorage)
  across reloads, with a discard button.
- The all-pairs overview shows every hints set on the capture; click a foreign flag to
  switch pairs.

## 4. Export — compile the summaries

Press **⟳ compile summaries** (or run `make compile-summary`, `ARGS=--dry-run` to preview).
The server compiles `labels/automated/` + `labels/review/` in this checkout into
`labels/summary/` — **one importable label set per audio file** (DP-24 §3,
[`PLAN_labels_layout.md`](https://github.com/trjh/netradio_DNB_analysis/blob/main/docs/PLAN_labels_layout.md)):

| File | Clock | Carries |
|---|---|---|
| `labels/summary/<stem>.summary.tsv` | capture-local | every automated row for the capture (all its pairs + `<stem>.hints.tsv`), every review row — including the converter's per-anchor `origNNN start: k confidence n/10` **clip-seat** rows (`a_k − b_native_k`); where the converter emitted none, one compiled seat row in the same grammar at the best-confidence anchor + an `origNNN note:` |
| `labels/summary/origNNN.summary.tsv` | original-native | the original's rows from every capture it appears in, plus its review rows on the record's clock |

Rows are verbatim (nothing dropped, no `verified` token rewritten, markers identical in both
files); the write is whole-file and idempotent; the files ride the `align-review-data`
branch with the review notes. The **⬇ download hints pair** button still exists for a
session's refined seats (DP-26 grammar, ticked points only, refined confidence as ncc × 10) —
that path writes nothing server-side.

## 5. Fold into Audacity

**File ▸ Import ▸ Labels** on `<stem>.summary.tsv` (track 3, imports aligned from zero) and
`origNNN.summary.tsv` (track 6 — drag the record so its start sits on the stem summary's
`origNNN start: k` label for the anchor you trust — the seats slope anchor-to-anchor by design,
that spread is the drift — then check that anchor *k* lines up across tracks 3 and 6; in a
capture covering several originals `track sync: k` repeats per original, the `origNNN sync: k`
row in track 6 is the unambiguous side). They land
as their **own** label tracks beside your hand labels; nothing existing is touched. Copy the
rows you accept into your hand labels; strip the ` verified` token from any row you moved by
hand without re-snapping (it then honestly reads as unchecked).

## 6. Back through the label pipeline

Export/sort via the normal `sort_tsv.py` flow ([`PROCESS.md`](https://github.com/trjh/netradio_DNB_analysis/blob/main/PROCESS.md) step 6), then
grade:

```bash
. .venv/bin/activate && python -m streamalign sync-audit --tracks <n>   # re-grade points
. .venv/bin/activate && python -m streamalign track-mix  --tracks <n>   # grade the rate
```

`sync-audit` is a report, not a gate — resolve what it flags (re-snap or strip the token)
before step 11 publishes the file.

## 7. Optional: the whole-song ear pass (`/align/review`)

**DP-22** ([`PLAN_align_tool.md`](https://github.com/trjh/netradio_DNB_analysis/blob/main/docs/PLAN_align_tool.md) §6; web POC built) adds a
whole-pair listening pass on top of this per-point flow — open
`http://<align-host>:8766/align/review` (same server, phone-first):

- pick the pair; the capture plays in the **left** ear, the rate-corrected original in
  the **right** — a wandering seat is audible as inter-ear flam/echo growth;
- tick/untick anchors while stopped (the [`PLAN_align_tool.md`](https://github.com/trjh/netradio_DNB_analysis/blob/main/docs/PLAN_align_tool.md) §6.2 fitted rate + per-anchor deviations update
  live); choose **constant-rate** (drift audible) or **re-anchor** (exact re-sync at each
  anchor) and watch the drift readout;
- pause and **tap the waveform to drop a free-text note** — notes append to
  `labels/review/<stem>.origNNN.review.tsv` in this checkout (this repo is public; the
  `align-review-data` squash PR is the contents gate — `make review-sync`), and reach the
  hand labels only through the manual fold;
- **per-source volume sliders** (DP-36, both surfaces): stream and original each run
  0–200 % — 100 % is the **unmodified** level (a stereo source reaches its ear as the
  standard 0.5·(L+R) mono down-mix; mono passes at unity — peaks never exceed the
  source's own), live during playback, double-tap/double-click the % readout to snap
  back to 100 %. Above 100 % the readout turns amber: clipping is possible again there,
  by choice — there is deliberately no limiter;
- **waveform zoom + lane height** (DP-37, both surfaces): the dual lanes are doubled in
  height (web `lanes` toggle restores the compact view for small screens) and the
  viewport zooms ×1/×2/**max** around the playhead — max is labelled with what the
  envelope data honestly resolves to on your screen ("max (×N)"; zoomed views fetch the
  inspector's 50 cols/s context strips through the same DSP boundary — never
  interpolation), and markers/notes/shading stay position-true at every zoom.

**The Mac native surface** ([`PLAN_align_tool.md`](https://github.com/trjh/netradio_DNB_analysis/blob/main/docs/PLAN_align_tool.md) §6.9-b) is built — `make review-app-run` compiles
(`swiftc` + Command Line Tools, no Xcode) and launches `native/align_review_app`
against `NETRADIO_ALIGN_URL` (default `http://127.0.0.1:8766`): the same pair
picker / anchor selection / in-waveform notes as the web page, but playback is
**sample-locked by `AVAudioEngine`** — no drift corrector, the drift readout is
telemetry — and keeps playing with the display asleep (the iOS-web failure mode
this surface exists to dodge).
