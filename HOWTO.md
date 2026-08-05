# HOWTO / FAQ — which tool, when, and why

> **What this is:** a plain-language, question→answer reference for *when to run each tool*
> in this repo, so the pipeline order is reproducible and nothing sits unused. It consolidates
> the player repo's `ROADMAP.md` §7 "Tool run-triggers (F4)" into the analysis repo, next to
> the code.
> **For the process itself** — the per-recording loop, the by-ear technique, and what the
> engine can and cannot do — see **[PROCESS.md](./PROCESS.md)**. This doc answers *"which tool
> now?"*; that one answers *"how is this done?"*.
> For *how* a tool works internally, see its own `--help`, the
> [label grammar](./PROCESS.md#label-grammar), and `scripts/streamalign/`
> [README](./scripts/streamalign/README.md) / [WALKTHROUGH](./scripts/streamalign/WALKTHROUGH.md).

## The pipeline in one breath

Every tool sits in one of three stages, plus a **publish** loop that pushes labels to the
sheet:

1. **Notate** — by hand in Audacity (and the labelling tools) → `labels/*.tsv`. The master
   timeline + track IDs + sync points live here. Engine output is always
   `<stem>.auto.labels.tsv` and **never** overwrites your hand `<stem>.labels.tsv`; seed files
   are `<stem>.starter.labels.tsv` / `<stem>.start-unprocessed.labels.tsv` (lowest priority).
2. **Build** — `scripts/build_track_metadata.py` turns the labels into the authoritative
   **`track-metadata.json`**.
3. **Serve** — `track-metadata.json` drives the player.

**Publish** (`labels/publish.py`) takes finished labels → GitHub → the Google Sheet view.

> **The one rule that answers "does this update `track-metadata.json`?":** *only the **build**
> step does.* Everything in **Notate** below produces **labels, clips, or scores for you to
> review** — none of those commands write `track-metadata.json`. When you've changed labels and
> want the player to see it, run `build_track_metadata.py` (the Build section). That's the single
> writer of the JSON.

The golden rule of the file naming: **hand** = `<stem>.labels.tsv` (yours, authoritative),
**engine** = `<stem>.auto.labels.tsv` (regenerable), **seed** = a starter in one of two states:
`<stem>.start-unprocessed.labels.tsv` (just emitted from a not-yet-published owner — **don't
load it yet**) → `<stem>.starter.labels.tsv` (**ready** to load; `publish` promotes it once the
owner is published). Both seed states are throwaway and excluded from import/solve/build.

> ✅ **The G5 labelling tooling is now on `main`** — `LABELTRACK` scoping in `sort_tsv.py`
> (A1), the `streamalign starter` seed emitter (A2), `sort_tsv.py --live` (A3), and
> `labels/publish.py` (A4) of `docs/ROADMAP_stream_analysis.md`. The manual paths below are kept
> only as a fallback.

---

## Notate — labelling in Audacity

**Q:** I just finished hand-labelling a capture in Audacity — what do I run?

**A:** Export the label track (**File ▸ Export ▸ Export Labels** → `<stem>.labels.txt`), then
sort + sanity-check it:

```bash
python3 labels/sort_tsv.py labels/<stem>.labels.txt           # rename .txt→.tsv, sort, validate
python3 labels/sort_tsv.py labels/<stem>.labels.tsv --test    # dry-run: just report grammar/notices
```

**What you get:** a sorted, validated on-disk `<stem>.labels.tsv` (the `.txt` is renamed to
`.tsv`), plus printed **notices** for anything off — a sync line missing `verified`, a
`file end:` missing `COMPLETE`, or bad grammar. It also splits any `file_OTHER:` rows out onto
the neighbour's file. **It is the only txt→tsv tool**, and it touches **only that one label
file** — not `track-metadata.json`. **What to do next:** fix anything it flagged, then either
keep labelling or run the **build** step to regenerate the JSON.

**Q:** I'm putting several label tracks in one Audacity export — how do I keep them apart?

**A:** Give each label track a `LABELTRACK <name>` marker as its first (earliest) label.
`sort_tsv.py` scopes every following label to `<name>` and strips the marker. `<name>` resolves
three ways: the **primary stem** → labels pass through verbatim; **another capture stem** (e.g.
`d356-375`) → re-homed onto that file (`file_<name>:`); **anything else** (`orig069`) →
prefix-expanded (`sync: 0` → `orig069 sync: 0`). A file that uses `LABELTRACK` is validated — a
block missing its marker is an error before anything is written.

**Q:** I just finished analysing one file and want to move on to the next. While analysing this
file I captured some overlapping labels for where the *next* file begins — how do I pass those
forward, so the next file's analysis starts from them instead of a blank slate?

**A:** Run the **starter emitter** over the file you just finished (the one whose labels record
where its neighbour begins, via the `file_<other>:` link):

```bash
PYTHONPATH=scripts python3 -m streamalign starter <owner-stem>   # writes <other>.start-unprocessed.labels.tsv seed(s)
```

**What you get:** a `<other>.start-unprocessed.labels.tsv` for the *next* capture — the owner's
labels at/after the link, time-shifted onto the next file's local timeline, with a derived
`file start sync` anchor. The **`start-unprocessed`** name is deliberate: the seed came from a
file you haven't *published* yet, so it's still provisional — **don't load it yet**.

**How do I know when a seed is ready to load?** By its name. **`publish`** finalizes it: when you
publish the owner file (see [Publish](#publish--labels--github--the-google-sheet)), publish
re-runs `streamalign starter <owner> --ready` against the now-locked labels, which rewrites the
seed as **`<other>.starter.labels.tsv`** (and removes the `.start-unprocessed` twin). So when you
open the next capture: a `.starter.labels.tsv` is **ready** to load and confirm/nudge in Audacity;
a `.start-unprocessed.labels.tsv` means *publish the owner first*. (This is the accelerator for
carrying labels forward across any sequential hand-verification.)

**Why a separate command — why doesn't `sort_tsv.py` just emit it?** Because they're different
jobs. `sort_tsv.py` only ever sorts/validates/scopes **one file's own** exported labels — it
never moves labels *between* files. Pre-seeding **projects** a finished capture's labels onto a
*different* file's timeline, which needs that file's master-timeline offset. The `starter`
command computes that offset and writes the seed; the manual fallback is to copy the labels
across yourself and rebase with `sort_tsv.py --adjust <seconds>` (you supply the offset by hand).

**And the seed itself is never published** — both states are throwaway, excluded from the sheet
import, the solve, and the build. Publishing the *owner's finished labels* is what **promotes**
the seed to ready (above); the two happen in one `publish` run.

**Q:** Is my on-disk `.tsv` in sync with what's live in Audacity?

**A:**

```bash
python3 labels/sort_tsv.py labels/<stem>.labels.tsv --live    # diff sorted .tsv vs live labels
```

**What you get:** a printed diff of the sorted on-disk `.tsv` against the labels currently live
in the Audacity session — so you can catch edits you made in Audacity but never re-exported.
Read-only; nothing is written. (Depends on Audacity's `mod-script-pipe` being enabled — see
`scripts/pipeclient.py`.)

**Q:** I want to (re)place captures on the master timeline, or check the engine against my hand
work — what do I run, and how do I read the result?

**A:**

```bash
PYTHONPATH=scripts python3 -m streamalign groundtruth     # dump resolved hand master-starts
PYTHONPATH=scripts python3 -m streamalign validate        # align every hand-verified pair and score
PYTHONPATH=scripts python3 -m streamalign align <a> <b>   # align two specific captures
```

**What each prints** (all **read-only** — none of them touch `track-metadata.json`):
- **`groundtruth`** — each file's resolved hand master-start in seconds (and whether its audio
  is found). This is the "answer key" your hand labels define.
- **`validate`** — for every hand-verified pair, cut equal-length slices tiled across the whole
  labeled overlap and correlate each: does the audio confirm the labels *everywhere in the
  overlap*? Each pair lands in one of three buckets — **confirmed** (every chunk matches:
  residual ≈ 0, high `conf`), **suspect** (real overlap but some chunk doesn't match — flagged
  `<-- SUSPECT`, `conf` is the weakest chunk, `resid_ms` the largest drift), or **adjacent**
  (labels place them end-to-end with no overlap — nothing to compare, listed separately, *not*
  an error). Ends with a `graded / confirmed / suspect / adjacent / skipped` summary. Two
  suspect signatures to read: **low `conf` throughout** = a genuinely mis-placed file (or no real
  overlap); **high `conf` but a clean constant `resid_ms` after some point** = a *skip within the
  overlap* (the file-start label is fine — `skips.py`/P2 characterises it). Grading the overlap
  directly, rather than re-deriving the offset from scratch, is what keeps very different-length
  pairs from tripping the coarse large-lag decode.
- **`align <a> <b>`** — the offset, confidence, and error-vs-ground-truth for one specific pair.

**How it helps:** these tell you *whether the computed placements are trustworthy* — i.e. does
the engine reproduce your hand alignments. They change no data; once you trust them, those
placements feed the auto-label emit (A5) and, after a build, the JSON. Re-run after you add or
verify labels.

**Q:** There are unconfirmed skips (stream discontinuities) — what do these commands actually do?

**A:**

```bash
PYTHONPATH=scripts python3 -m streamalign skip-clips                  # detect skips over verified overlaps + clips
PYTHONPATH=scripts python3 -m streamalign skip-confirm <id>           # confirm → owner's hand .labels.tsv
PYTHONPATH=scripts python3 -m streamalign skip-reject  <id> --note …  # reject → labels/skip-rejections.tsv
```

- **`skip-clips`** — finds candidate skips over your verified overlaps and **writes short audio
  clips** (+ a manifest) into the clips dir (`$NETRADIO_CLIPS_DIR`, else `clips_out/`), so you
  can listen to each suspected discontinuity in Audacity. Nothing else changes.
- **`skip-confirm <id>`** — you decided clip `<id>` is a real skip → it records that skip into
  the owning capture's **hand `<stem>.labels.tsv`**.
- **`skip-reject <id> --note …`** — not a skip → records the rejection in
  `labels/skip-rejections.tsv` so the engine stops re-proposing it.

**How it helps:** it keeps wrong auto-skips out of the engine's placements. These edit **labels**
(or the rejections file), **not** `track-metadata.json` — if a confirmed skip changed a file's
span, rebuild the JSON afterwards.

**Q:** Seat an original track inside a capture — where do the paired sync points come from?

**A:** Two passes ([PROCESS.md step 9](./PROCESS.md#9-align-the-originals)):

```bash
PYTHONPATH=scripts .venv/bin/python -m streamalign match-hints <stem> <NNN>   # Pass 1: propose
PYTHONPATH=scripts .venv/bin/python -m streamalign match-hints <stem> --all   # batch: every overlapping track with an original
```

**What you get:** a paired hints file per track — `<stem>.origNNN.match.hints.tsv` +
`origNNN.<stem>.match.hints.tsv` (gitignored hints, **never** labels): `track sync:` /
`origNNN sync:` anchor pairs carrying ` verified confidence n/10`, proposed
`origNNN start:`/`end:` rows (marked `? confidence n/10`), and `note QUESTION:` rows wherever
the engine cannot tell. **What to do next (Pass 2):** verify/hand-tune every point in the
companion player project's **/align** inspector (live subtraction null test, snap-to-best,
PHAT|MATCH engines) — its export writes the adjusted rows; import them into Audacity as their
own label track, fold what you accept into your hand labels, and `sort_tsv.py` as usual.
Sonic Visualiser is **not** part of this: MATCH runs headless inside Pass 1 (via
`sonic-annotator`; no `sonic-annotator` on PATH → pass `--csv` with a pre-exported
`match:a_b` CSV), and SV+MATCH survives only as an optional manual cross-check.

**Q:** Map an original track's speed/offset onto the mix.

**A:**

```bash
PYTHONPATH=scripts .venv/bin/python -m streamalign track-mix --tracks <NNN>   # original↔mix rate (G2; needs librosa)
```

**What you get:** a printed report of each original's measured rate/offset vs the mix (add
`--json <path>` to dump the full results to a file). It **reads** `track-metadata.json` and your
labels but writes **neither** — emitting a confirmed alignment is gated behind your by-ear
confirm. (The rate axis is still a research line — plain correlation doesn't lock; see
`docs/PLAN_track_mix.md` in the player.) Needs `.venv/bin/python` for librosa.

---

## Build — labels → `track-metadata.json`

**Q:** I changed labels (IDs, timings, syncs) — how do I rebuild the metadata the player reads?

**A:**

```bash
python3 scripts/build_track_metadata.py            # writes track-metadata.json at the repo root
python3 scripts/build_track_metadata.py --dry-run  # preview without writing
```

**This is the one tool that writes `track-metadata.json`.** Run it after any label change that
affects track identity, position, or span. Curated fields (year, artwork, links, manual
overrides) are carried forward from the previous JSON, so re-generating never loses them.
`*.auto.labels.tsv` are read; `*.starter.labels.tsv` are not. **What to do next:** the player
serves the new JSON directly; if you're syncing repos, that's what `make sync` propagates.

**Q:** Refresh the missing-originals / sourcing inventory.

**A:**

```bash
python3 scripts/g4_missing_sources.py              # gap inventory + sourcing leads
```

**What you get:** an up-to-date list of which original tracks are still missing, with sourcing
leads. Run it after `sources_local/` changes or to refresh the gap list. Related:
`find_streaming_links.py` and `merge_track_sources.py` for streaming-link discovery/merge.

---

## Publish — labels → GitHub → the Google Sheet

**Q:** My labels are finished and verified — how do I publish them and refresh the sheet?

**A:** The runnable path today (the README Process steps 9–11):

```bash
python3 labels/sort_tsv.py labels/<stem>.labels.txt          # sort + validate (review the notices!)
git add labels/<stem>.labels.tsv
git commit -m "labels: update <stem>" && git push            # commit + push to trjh/netradio_DNB_analysis
```

then click **Reload Data** on the **File Analysis** tab (runs `GithubImport()` in
`sheetscript/Code.js`). The **File List** complete/verified columns then update from sheet
formulas — no script touches that tab.

**Q:** Isn't there a one-command version?

**A:**

```bash
python3 labels/publish.py <stem>           # validate → sort → commit → push → finalize seeds → refresh
python3 labels/publish.py <stem> --check   # gate only, no push
python3 labels/publish.py <stem> --dry-run # show what would happen
```

`publish.py` is **all-or-nothing** and hard-gated: it refuses to push bad-syntax rows,
unverified syncs, `file end` missing `COMPLETE`, missing `LABELTRACK` markers, files with no
start-sync / `file end … COMPLETE` anchor, and seed/engine (`*.starter` / `*.start-unprocessed`
/ `*.auto`) files — so a half-labelled capture can never reach the sheet. **After the push it
finalizes starter seeds:** it runs `streamalign starter <stem> --ready`, promoting any
`<other>.start-unprocessed.labels.tsv` the published file produced to the ready
`<other>.starter.labels.tsv` — so the next capture's seed is marked loadable. On success it
auto-refreshes by POSTing to the Apps Script Web App at `NETRADIO_SHEET_WEBHOOK` (whose `doPost`
runs `GithubImport()`), else it prints the **Reload Data** reminder.

---

## Quick reference

| I want to… | Run | Stage | Writes |
|---|---|---|---|
| sort + validate a labelled capture | `sort_tsv.py <stem>.labels.txt` | notate | that `.tsv` |
| scope multiple label tracks in one export | `LABELTRACK <name>` markers + `sort_tsv.py` | notate | that `.tsv` |
| pass labels forward to seed the next capture | `streamalign starter <owner>` | notate | `<other>.start-unprocessed.labels.tsv` (→ `.starter` on publish) |
| diff on-disk vs live Audacity labels | `sort_tsv.py <stem>.labels.tsv --live` | notate | — (read-only) |
| re-place captures / score the engine | `streamalign groundtruth \| validate \| align` | notate | — (read-only) |
| place the unlabelled tail (loop-wrap anchor) | `streamalign tail-solve` (`--emit` to write labels) | notate | — (read-only; `--emit` → `<stem>.auto.labels.tsv`) |
| resolve skips | `streamalign skip-clips \| skip-confirm \| skip-reject` | notate | clips / hand `.labels.tsv` / rejections |
| seat an original in a capture (paired sync points) | `streamalign match-hints` → verify in the player's `/align` | notate | `*.match.hints.tsv` (gitignored) |
| original↔mix rate | `streamalign track-mix` | notate | — (read-only) |
| **rebuild `track-metadata.json`** | `build_track_metadata.py` | **build** | **`track-metadata.json`** |
| refresh missing-originals inventory | `g4_missing_sources.py` | build/sourcing | inventory |
| publish finished labels + refresh sheet | `publish.py <stem>` · manual: `sort_tsv` + `git commit`/`push` + Reload Data | publish | git + sheet |
