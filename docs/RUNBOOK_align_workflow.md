# Align workflow — fresh capture + original → verified labels

> **What this is:** the end-to-end operator walkthrough of the CURRENT align flow — from
> finding the tracks that still lack precise labels, through `streamalign match-hints`
> (Pass 1), the `/align` inspector (Pass 2) and the whole-song listen-through, to verified
> sync rows in the Audacity labels. Compact by design: each step points at the doc that owns
> its detail.
>
> **Fits in:** [the repo's README](https://github.com/trjh/netradio_DNB_analysis/blob/main/README.md) (master index). The tool's design/build record is
> [`PLAN_align_tool.md`](https://github.com/trjh/netradio_DNB_analysis/blob/main/docs/PLAN_align_tool.md); the label semantics (incl. the two
> `verified` families) are in [the label grammar in `PROCESS.md`](https://github.com/trjh/netradio_DNB_analysis/blob/main/PROCESS.md#label-grammar);
> the surrounding labelling pipeline (steps 6–11) is [`PROCESS.md`](https://github.com/trjh/netradio_DNB_analysis/blob/main/PROCESS.md) — this doc
> references it, never duplicates it.
>
> Decision points (`DP-nn`) are listed in [`docs/DECISIONS.md`](https://github.com/trjh/netradio_DNB_analysis/blob/main/docs/DECISIONS.md).

The flow in one line: **find** the work (§0) → **propose** sync points (§1) → **check** each
point by eye and ear (§2–§3) → **listen** to the whole pair (§4) → **compile** (§5) → **fold**
into Audacity, where you finalise (§6) → **audit** (§7). Nothing before §6 touches your hand
labels; Audacity stays the place where a label becomes final. §8 covers working offline.

## Prerequisites (once per machine)

- This repo checked out with its venv built (`make env` builds `.venv` with uv from
  both requirement sets and also installs the MATCH binaries: `sonic-annotator` + `match-vamp`).
  Activating that venv puts its `scripts/` on the path, which is why the commands below carry no
  `PYTHONPATH`. `make align-check` confirms the venv and the originals directory.
- The server finds this checkout from its own location; no setting names it.
- This repo's `.env`: `NETRADIO_LABELS_DIR` (default `labels/`) and `NETRADIO_MP3_DIR`;
  optionally `NETRADIO_SOURCES_DIR` (the originals; default `sources_local/`); for the align
  server, `NETRADIO_ALIGN_HOST` / `NETRADIO_ALIGN_PORT`, and `NETRADIO_AUTH_USER` /
  `NETRADIO_AUTH_PASS` for Basic Auth.
- The original on disk as `sources_local/NNN-Artist - Title.<ext>` — no original, no
  alignment (as of 2026-10-20, some originals are still missing).
- For the Mac listen-through (§4): `make review-app` builds the app and runs its selftest
  (`swiftc` from the Command Line Tools; no Xcode).

## 0. Find the work

Two kinds of gap remain, and they need different starts.

**Tracks with only a rough position.** A track the hand labels do not reach yet takes its
position from the 1998/2017 notes (`labels/remainder.tsv`). The build marks it
`"source": "rough"` in `track-metadata.json`:

```bash
python3 -c "import json; t=json.load(open('track-metadata.json'))['tracks']
for n,r in sorted(t.items(), key=lambda x: int(x[0])):
    if r.get('source') == 'rough':
        print(n, r.get('artist','?'), '-', r.get('title','?'), '|', ', '.join(r.get('source_files', [])))"
```

The rough tracks sit in the captures past the labelled frontier. Start at the first of them:
its capture is placed (its predecessor's `file_<next>:` link carries it), so §1 runs on it
unaided.

**Tracks with a position but no sync points.** A track placed by hand labels can still lack
`track sync:`/`origNNN sync:` pairs. `placed` lists every hand-seated pair; a track with an
original on disk and no entry in that list is unseated:

```bash
. .venv/bin/activate && python -m streamalign placed | python3 -c "import json,sys
for p in json.load(sys.stdin)['pairs']: print(p['stem'], 'orig%03d' % p['orig'], len(p['points']), 'points')"
```

**Before any capture with no labels of its own**, ask the engine what it knows (PROCESS step
2). `hints` needs no placement, and its `SYNC ANCHOR A/B` rows (an original playing alone, at
confidence ~9.8/10) are the seed §1 needs:

```bash
. .venv/bin/activate && python -m streamalign hints <stem>        # -> labels/automated/<stem>.hints.tsv
. .venv/bin/activate && python -m streamalign tail-solve          # report: tail placements, orphans
```

`tail-solve` places the tail captures through the loop-wrap anchor and names any capture it
cannot place (`orphan … butt-jointed both sides, no audio anchor`). An orphan's position comes
only from its predecessor's `file_<next>:` hand link (PROCESS step 3). Run `tail-solve`
**without** `--emit` while exploring: `--emit` writes `<stem>.auto.labels.tsv`, which the
build and the sheet both read.

## 1. Pass 1 — propose the sync points (`match-hints`)

Run in **this repo's checkout** (`scripts/run_align.sh help` prints these commands).

```bash
. .venv/bin/activate && python -m streamalign \
    match-hints <stem> <NNN>
#   match-hints <stem> --all              # batch: every original overlapping the capture
#   match-hints <stem> <NNN> --around S   # capture not placed yet (see below)
#   add --dry-run to print the rows without writing
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

### A capture with no placement yet

When a capture has no master position, the converter cannot derive the neighbourhood to trim
to, and an unseeded run can lock onto a **loop-shifted** seat: the right record, the wrong
repetition. Seed `--around` from the `hints` file, not from the notes' track start:

1. Take a `SYNC ANCHOR` row for the track from `<stem>.hints.tsv`: capture instant `t`,
   original instant `o`.
2. Pass `--around` as `t − o`, the capture second where the original's 0:00 would sit.
3. Check the result against the anchor: for a `track sync: k` / `origNNN sync: k` pair,
   `stream − orig` must come out near `t − o`. Several seconds off means a loop-shifted seat;
   the `rival whole-bar-shifted seat` QUESTION row says the same thing.

Worked example, `d396-415` and track 077 (2026-10-20): the hints anchor reads
`352.372` ↔ `3:25.82` (205.82 s), so `--around 146`. That run gives
`track sync: 5` at 355.151 ↔ 208.641, which is 146.5 s apart: it agrees with the anchor. A run
with `--around 244` (the notes' track start) seats the record about 30 s away, on a different
repetition, at similar confidence. The anchors' confidence alone does not tell the two apart.

Reject a pair outright when the fit is implausible: a rate more than a few percent from 1.0,
`polarity INVERTED` with a low `sweep score`, or anchors that disagree with every hints anchor.
The original on disk may be a different edit. Note it in the hand labels and move on.

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

## 4. The whole-song listen-through (`/align/review`, or the Mac app)

Do this **before** you fold into Audacity. Pass 2 checks each point in a few-second window;
the listen-through checks the whole pair, and it is where you annotate the broadcast. A
loop-shifted pair (§1) that survived Pass 2 is audible here within a bar. Both surfaces list
every `*.match.hints.tsv` pair and every hand-seated pair, so a fresh Pass 1 is enough to
listen to.

**DP-22** ([`PLAN_align_tool.md`](https://github.com/trjh/netradio_DNB_analysis/blob/main/docs/PLAN_align_tool.md) §6; web POC built) — open
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
against `NETRADIO_ALIGN_URL`, else a URL derived from `NETRADIO_ALIGN_HOST` and
`NETRADIO_ALIGN_PORT` (a `0.0.0.0` bind is reached on `127.0.0.1`; default
`http://127.0.0.1:8766`): the same pair
picker / anchor selection / in-waveform notes as the web page, but playback is
**sample-locked by `AVAudioEngine`** — no drift corrector, the drift readout is
telemetry — and keeps playing with the display asleep (the iOS-web failure mode
this surface exists to dodge).

What to note as you listen, so the Audacity pass has less to find: the audible track
changeovers (`startNNN:` candidates), skips and dropouts, anything a QUESTION row asked, and
the anchors you untick and why.

## 5. Export — compile the summaries

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

## 6. Fold into Audacity — where you finalise

A capture with no project yet starts from its predecessor's `.aup3` (PROCESS step 1).

**File ▸ Import ▸ Labels** on `<stem>.summary.tsv` (track 3, imports aligned from zero) and
`origNNN.summary.tsv` (track 6 — drag the record so its start sits on the stem summary's
`origNNN start: k` label for the anchor you trust — the seats slope anchor-to-anchor by design,
that spread is the drift — then check that anchor *k* lines up across tracks 3 and 6; in a
capture covering several originals `track sync: k` repeats per original, the `origNNN sync: k`
row in track 6 is the unambiguous side). They land
as their **own** label tracks beside your hand labels; nothing existing is touched. Copy the
rows you accept into your hand labels; strip the ` verified` token from any row you moved by
hand without re-snapping (it then honestly reads as unchecked).

Finalising is the rest of the hand pass (PROCESS step 5): `startNNN:` at each boundary,
`file note: skip …` at each skip, `file start sync:` and `file end: … COMPLETE`. Use the
listen-through notes from §4 as the checklist.

## 7. Back through the label pipeline

Export/sort via the normal `sort_tsv.py` flow ([`PROCESS.md`](https://github.com/trjh/netradio_DNB_analysis/blob/main/PROCESS.md) step 6), then
grade:

```bash
. .venv/bin/activate && python -m streamalign sync-audit --tracks <n>   # re-grade points
. .venv/bin/activate && python -m streamalign track-mix  --tracks <n>   # grade the rate
```

`sync-audit` is a report, not a gate — resolve what it flags (re-snap or strip the token)
before step 11 publishes the file.

## 8. Working offline

Everything in §0–§7 runs on one machine with no network, provided the audio is on its disk:

- **Before you go:** check that the captures you plan to work on and their originals are
  on this disk as full-size files (`ls -laL jaz_links/<stem>.wav sources_local/NNN-*`;
  `make align-check`). Build the Mac app (`make review-app`). Run `git pull` so the
  converter and the server are current.
- **What works offline:** `hints`, `tail-solve`, `match-hints` (`sonic-annotator` is local),
  the inspector, the listen-through on either surface (the server binds loopback unless
  `.env` says otherwise), `compile-summary`, Audacity, `sort_tsv.py`, `sync-audit`,
  `track-mix`, `make stream-data` and `validate`.
- **What waits for the network:** the align server commits review notes and summaries to the
  local `align-review-data` branch as before, but its hourly push fails quietly, records the
  error and retries next hour (`python3 scripts/review_sync.py status` shows it). Once back
  online it pushes; then run `make review-sync`. `labels/publish.py` (PROCESS step 11) needs
  the network to push and open the PR: run it, or `publish.py <stem> --check` to gate only,
  when you are back.
- **What to bring back:** your exported `labels/<stem>.labels.tsv` files and the `labels/review/`
  notes. The listen-through notes live in the review files, not in Audacity, until you fold
  them in.
