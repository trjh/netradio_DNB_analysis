# HOWTO / FAQ — which tool, when, and why

> **What this is:** a plain-language, question→answer reference for *when to run each tool*
> in this repo, so the pipeline order is reproducible and nothing sits unused. It consolidates
> the player repo's `ROADMAP.md` §7 "Tool run-triggers (F4)" into the analysis repo, next to
> the code. For *how* a tool works internally, see its own `--help`, the
> [README](./README.md) (Process + Label grammar), and `scripts/streamalign/`
> [README](./scripts/streamalign/README.md) / [WALKTHROUGH](./scripts/streamalign/WALKTHROUGH.md).

## The pipeline in one breath

Every tool sits in one of three stages, plus a **publish** loop that pushes labels to the
sheet:

1. **Notate** — by hand in Audacity (and the labelling tools) → `labels/*.tsv`. The master
   timeline + track IDs + sync points live here. Engine output is always
   `<stem>.auto.labels.tsv` and **never** overwrites your hand `<stem>.labels.tsv`; seed files
   are `<stem>.starter.labels.tsv` (lowest priority).
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
**engine** = `<stem>.auto.labels.tsv` (regenerable), **seed** = `<stem>.starter.labels.tsv`
(throwaway pre-positioning, excluded from import/solve/build).

> ⚠️ **Items tagged `(G5 — not yet on main)` below are not runnable on `main` yet.** The G5
> labelling tooling — `LABELTRACK` scoping in `sort_tsv.py`, the `streamalign starter` seed
> emitter, and `labels/publish.py` — ships with the G5 PRs (chunks A1/A2/A4 of
> `docs/ROADMAP_stream_analysis.md`). Each is marked inline with the **manual path to use until
> it lands**. Everything else here already works on `main`.

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
*(G5 — not yet on main; until then, export one label track at a time.)*

**A:** Give each label track a `LABELTRACK <name>` marker as its first (earliest) label.
`sort_tsv.py` scopes every following label to `<name>` and strips the marker. `<name>` resolves
three ways: the **primary stem** → labels pass through verbatim; **another capture stem** (e.g.
`d356-375`) → re-homed onto that file (`file_<name>:`); **anything else** (`orig069`) →
prefix-expanded (`sync: 0` → `orig069 sync: 0`). A file that uses `LABELTRACK` is validated — a
block missing its marker is an error before anything is written.

**Q:** I just finished analysing one file and want to move on to the next. While analysing this
file I captured some overlapping labels for where the *next* file begins — how do I pass those
forward, so the next file's analysis starts from them instead of a blank slate?
*(G5 — not yet on main; until then, carry the labels across by hand and rebase them with
`sort_tsv.py --adjust <seconds>`.)*

**A:** Run the **starter emitter** over the file you just finished (the one whose labels record
where its neighbour begins, via the `file_<other>:` link):

```bash
PYTHONPATH=scripts python3 -m streamalign starter <owner-stem>   # writes <other>.starter.labels.tsv seed(s)
```

**What you get:** a `<other>.starter.labels.tsv` for the *next* capture — the owner's labels
at/after the link, time-shifted onto the next file's local timeline, with a derived
`file start sync` anchor. **What to do with it:** open it in Audacity over the next capture and
confirm/nudge the seeded labels instead of starting from nothing. (This is the accelerator for
the headline tail-contiguity hand-verification.)

**Why a separate command — why doesn't `sort_tsv.py` just emit it?** Because they're different
jobs. `sort_tsv.py` only ever sorts/validates/scopes **one file's own** exported labels — it
never moves labels *between* files. Pre-seeding **projects** a finished capture's labels onto a
*different* file's timeline, which needs that file's master-timeline offset. The `starter`
command computes that offset and writes the seed; the manual fallback is to copy the labels
across yourself and rebase with `sort_tsv.py --adjust <seconds>` (you supply the offset by hand).

**And this is *not* the publish step.** Starters are throwaway seeds — excluded from the sheet
import, the solve, and the build. Publishing *finished* labels to the sheet is a separate thing,
see [Publish](#publish--labels--github--the-google-sheet) below.

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
- **`validate`** — for every hand-verified overlapping pair, the engine's alignment error vs
  your hand answer: a table of `err_ms` / `err_samples` / `confidence`, then a summary
  (median/max error, how many are within tolerance). Rows past tolerance are flagged `<-- check`.
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

**Q:** Isn't there a one-command version? *(G5 — not yet on main; lands with chunk A4.)*

**A:**

```bash
python3 labels/publish.py <stem>           # validate → sort → commit → push → refresh
python3 labels/publish.py <stem> --check   # gate only, no push
python3 labels/publish.py <stem> --dry-run # show what would happen
```

`publish.py` is **all-or-nothing** and hard-gated: it refuses to push bad-syntax rows,
unverified syncs, `file end` missing `COMPLETE`, missing `LABELTRACK` markers, files with no
start-sync / `file end … COMPLETE` anchor, and seed/engine (`*.starter`/`*.auto`) files — so a
half-labelled capture can never reach the sheet. On success it auto-refreshes by POSTing to the
Apps Script Web App at `NETRADIO_SHEET_WEBHOOK` (whose `doPost` runs `GithubImport()`), else it
prints the **Reload Data** reminder.

---

## Quick reference

| I want to… | Run | Stage | Writes |
|---|---|---|---|
| sort + validate a labelled capture | `sort_tsv.py <stem>.labels.txt` | notate | that `.tsv` |
| scope multiple label tracks in one export | `LABELTRACK <name>` markers + `sort_tsv.py` *(G5)* | notate | that `.tsv` |
| pass labels forward to seed the next capture | `streamalign starter <owner>` *(G5)* | notate | `<other>.starter.labels.tsv` |
| diff on-disk vs live Audacity labels | `sort_tsv.py <stem>.labels.tsv --live` | notate | — (read-only) |
| re-place captures / score the engine | `streamalign groundtruth \| validate \| align` | notate | — (read-only) |
| resolve skips | `streamalign skip-clips \| skip-confirm \| skip-reject` | notate | clips / hand `.labels.tsv` / rejections |
| original↔mix rate | `streamalign track-mix` | notate | — (read-only) |
| **rebuild `track-metadata.json`** | `build_track_metadata.py` | **build** | **`track-metadata.json`** |
| refresh missing-originals inventory | `g4_missing_sources.py` | build/sourcing | inventory |
| publish finished labels + refresh sheet | `publish.py <stem>` *(G5)* · manual: `sort_tsv` + `git commit`/`push` + Reload Data | publish | git + sheet |

*(G5)* = ships with the G5 PRs (A1/A2/A4); not on `main` yet — see the inline note for the manual path.
