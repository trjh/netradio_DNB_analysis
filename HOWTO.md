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

**Q: I just finished hand-labelling a capture in Audacity — what do I run?**
Export the label track (**File ▸ Export ▸ Export Labels** → `<stem>.labels.txt`), then sort +
sanity-check it:

```bash
python3 labels/sort_tsv.py labels/<stem>.labels.txt           # rename .txt→.tsv, sort, validate
python3 labels/sort_tsv.py labels/<stem>.labels.tsv --test    # dry-run: just report grammar/notices
```

`sort_tsv.py` sorts (`file start` first, then time, then label), splits `file_OTHER:` rows onto
the neighbour's file, and flags any sync line missing `verified` or any `file end:` missing
`COMPLETE`. **It is the only txt→tsv tool.**

**Q: I'm putting several label tracks in one Audacity export — how do I keep them apart?**
**`(G5 — not yet on main; until then, export one label track at a time.)`**
Give each label track a `LABELTRACK <name>` marker as its first (earliest) label. `sort_tsv.py`
scopes every following label to `<name>` and strips the marker. `<name>` resolves three ways:
the **primary stem** → labels pass through verbatim; **another capture stem** (e.g. `d356-375`)
→ re-homed onto that file (`file_<name>:`); **anything else** (`orig069`) → prefix-expanded
(`sync: 0` → `orig069 sync: 0`). A file that uses `LABELTRACK` is validated — a block missing
its marker is an error before anything is written.

**Q: I placed a tail capture and want to pre-seed the *next* capture's labels.**
**`(G5 — not yet on main; until then, carry labels forward by hand and rebase with sort_tsv.py --adjust.)`**
Run the starter emitter over the capture whose labels record where its neighbour begins
(`file_<other>:` link). The engine runs with `PYTHONPATH=scripts` (and `.venv/bin/python` for
the librosa-backed `track-mix`):

```bash
PYTHONPATH=scripts python3 -m streamalign starter <owner-stem>   # writes <other>.starter.labels.tsv seed(s)
```

It carries the owner's labels at/after the link, shifted onto the neighbour's timeline, with a
derived `file start sync` anchor. Seeds are **seed-only**: confirm/nudge them in Audacity; they
are excluded from the sheet import, the solve, and the build. (Speeds up the tail-contiguity
hand-verification — the headline blocker.)

**Q: Is my on-disk `.tsv` in sync with what's live in Audacity?**
```bash
python3 labels/sort_tsv.py labels/<stem>.labels.tsv --live    # diff sorted .tsv vs live labels
```
(Depends on Audacity's `mod-script-pipe` being enabled — see `scripts/pipeclient.py`.)

**Q: Re-place every capture on the master timeline / check the engine against my hand work?**
```bash
PYTHONPATH=scripts python3 -m streamalign groundtruth     # dump resolved hand master-starts
PYTHONPATH=scripts python3 -m streamalign validate        # align every hand-verified pair and score
PYTHONPATH=scripts python3 -m streamalign align <a> <b>   # align two specific captures
```
Run after you add or verify labels, or to refresh placements. (Feeds the auto-label emit, A5.)

**Q: There are unconfirmed skips (stream discontinuities) to resolve.**
```bash
PYTHONPATH=scripts python3 -m streamalign skip-clips                  # detect skips over verified overlaps + clips
PYTHONPATH=scripts python3 -m streamalign skip-confirm <id>           # confirm → skipper's hand .labels.tsv
PYTHONPATH=scripts python3 -m streamalign skip-reject  <id> --note …  # reject → labels/skip-rejections.tsv
```
Skips are now reviewed in **Audacity** (more context than the player); these commands record the
decisions and keep wrong auto-skips out of the engine output.

**Q: Map an original track's speed/offset onto the mix.**
```bash
PYTHONPATH=scripts .venv/bin/python -m streamalign track-mix --tracks <NNN>   # original↔mix rate (G2; needs librosa)
```
Emission of confirmed alignments is gated by your by-ear confirm. (The rate axis itself is the
A7 research line — plain correlation does not lock; see `docs/PLAN_track_mix.md` in the player.)

---

## Build — labels → `track-metadata.json`

**Q: I changed labels (IDs, timings, syncs) — how do I rebuild the metadata the player reads?**
```bash
python3 scripts/build_track_metadata.py            # writes track-metadata.json at the repo root
python3 scripts/build_track_metadata.py --dry-run  # preview without writing
```
Run after any label change that affects track identity, position, or span. Curated fields
(year, artwork, links, manual overrides) are carried forward from the previous JSON, so
re-generating never loses them. `*.auto.labels.tsv` are read; `*.starter.labels.tsv` are not.

**Q: Refresh the missing-originals / sourcing inventory.**
```bash
python3 scripts/g4_missing_sources.py              # gap inventory + sourcing leads
```
Run after `sources_local/` changes or to refresh the gap list. Related: `find_streaming_links.py`
and `merge_track_sources.py` for streaming-link discovery/merge.

---

## Publish — labels → GitHub → the Google Sheet

**Q: My labels are finished and verified — how do I publish them and refresh the sheet?**
The runnable path today (the README Process steps 9–11):
```bash
python3 labels/sort_tsv.py labels/<stem>.labels.txt          # sort + validate (review the notices!)
git add labels/<stem>.labels.tsv
git commit -m "labels: update <stem>" && git push            # commit + push to trjh/netradio_DNB_analysis
```
then click **Reload Data** on the **File Analysis** tab (runs `GithubImport()` in
`sheetscript/Code.js`). The **File List** complete/verified columns then update from sheet
formulas — no script touches that tab.

**Q: Isn't there a one-command version?** **`(G5 — not yet on main; lands with chunk A4.)`**
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

| I want to… | Run | Stage |
|---|---|---|
| sort + validate a labelled capture | `sort_tsv.py <stem>.labels.txt` | notate |
| scope multiple label tracks in one export | `LABELTRACK <name>` markers + `sort_tsv.py` *(G5)* | notate |
| pre-seed the next capture's labels | `streamalign starter <owner>` *(G5)* | notate |
| diff on-disk vs live Audacity labels | `sort_tsv.py <stem>.labels.tsv --live` | notate |
| re-place captures / score the engine | `streamalign groundtruth \| validate \| align` | notate |
| resolve skips | `streamalign skip-clips \| skip-confirm \| skip-reject` | notate |
| original↔mix rate | `streamalign track-mix` | notate |
| rebuild `track-metadata.json` | `build_track_metadata.py` | build |
| refresh missing-originals inventory | `g4_missing_sources.py` | build/sourcing |
| publish finished labels + refresh sheet | `publish.py <stem>` *(G5)* · manual: `sort_tsv` + `git commit`/`push` + Reload Data | publish |

*(G5)* = ships with the G5 PRs (A1/A2/A4); not on `main` yet — see the inline note for the manual path.
