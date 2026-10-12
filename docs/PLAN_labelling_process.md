# PLAN — Labelling-process automation

> **What this is:** the design for automating the Audacity labelling process, so hand labelling
> is fast and self-checking.
>
> **Fits in:** [this repo's README](https://github.com/trjh/netradio_DNB_analysis/blob/main/README.md) (its master index).

| | |
|---|---|
| **Status** | Design 2026-06-18; **Proposal A shipped** (A1–A4 on `main`), refined 2026-07-14 by the first real multi-track export (#84) |
| **Code** | `labels/sort_tsv.py`, `sheetscript/Code.js` |
| **Goal** | Make hand labelling in Audacity fast and reliable so verifying the file-to-file links that bridge the tail is quick |

**Proposals A–D are implemented** (A1–A4, on `main`): `LABELTRACK` scoping,
`streamalign starter`, `--live`, and `publish`. The shipped grammar is specified in
[`PROCESS.md`](https://github.com/trjh/netradio_DNB_analysis/blob/main/PROCESS.md) (*Label grammar*) — this doc holds the *design* and the reasoning behind each
locked decision.

## Where this fits in the pipeline

The pipeline has three steps:

- **Notate** — Audacity / tools → `labels/*.tsv`
- **Build** — `build_track_metadata.py` → `track-metadata.json`
- **Serve** — a label reader (reads `track-metadata.json`)

This work makes the **Notate** step faster and self-checking, which directly accelerates the
file-to-file link verification (`verified` / `file_<other>:` markers) that the tail needs.

## Motivation

1. **Grammar gap.** Free-text labels that don't match a keyword pattern are flagged as
   unrecognized and must be fixed by hand. Today the only way to attach a label to an
   original/track/file is to type its qualifier on every label (`orig069 note: …`,
   `orig069 sync: 0`). Repetitive and error-prone.
2. **Forgotten manual steps.** `--live` (compare on-disk export vs. what's live in Audacity)
   is optional and run inconsistently; there's no way to know whether a given `.tsv` reflects
   the latest Audacity state. The tooling should enforce/automate this rather than rely on memory.

## Background: what `file_<other>:` does today

Verified against `labels/sort_tsv.py`:

- A label `file_<NAME>: <inner label>` is matched by `file_([^:]+):\s+(.+)`.
- `<NAME>` is peeled off; the **inner label** is stashed in a per-name bucket (`secondfiles[<NAME>]`).
- After the primary labels are sorted, each bucket is sorted on its own, validated (the
  `file start` / `file end` tag must contain `<NAME>`), and appended to the output.
- **Meaning:** while working inside file A, this is how you record labels that describe a
  neighbour file B — where B starts inside A, B's own `file start sync`, etc.
- **History check (resolved 2026-06-18):** the committed corpus has **zero bare
  `file_<NAME>:` rows** in any `.tsv`. The only `file_` usages are 5 cross-reference *notes*
  (`note: file_d041-064: …` in `d019-040`), which don't trigger the split and never reached
  the sheet. So the secondary form was effectively **never persisted**. **Decision:** don't
  rely on `file_<NAME>:` rows surviving the main `.tsv` — carry-forward content goes to
  **separate files** (Proposal B, `<stem>.starter.labels.tsv`).
- **Corrected 2026-07-14 — the 2026-06-18 check drew the wrong conclusion, and it hid a
  destructive bug** (fixed in #84). The corpus had almost no `file_<NAME>:` rows not
  because the form was unused, but because **`sort_tsv.py` was eating them**: `process_entry()`
  peels the `file_<NAME>: ` prefix to bucket the row, and `assemble_write_lines()` wrote the
  bare inner label back. So sorting a file that carried a neighbour's label track **destroyed
  the link** and dumped the neighbour's labels into the file's own `.tsv` as anonymous rows at the
  *neighbour's* timestamps (in `d356-375`: 35 rows at 2000s+, past its own 1200s `file end`).
  The user had been **cutting neighbour label tracks out by hand before sorting** — the workaround
  that made the form look unused.
- **This directly broke Proposal B.** `streamalign starter` reads `file_<other>:` rows straight
  back out of the committed `<stem>.labels.tsv` (`emit_labels._LINK_RE`) to seed the neighbour.
  The sort was destroying its only input. `d336-355.labels.tsv` line 120 carries exactly such a
  row (`file_d356-375: file start sync: …`) — d356-375's seed — and the unmodified sort stripped
  it.
- **Now:** the prefix is re-attached on write, so sorting is **idempotent** for these rows and
  the starter link survives. The earlier "don't rely on `file_<NAME>:` rows surviving the main
  `.tsv`" decision is **superseded** — they survive, and Proposal B depends on it.

## Proposal A — `LABELTRACK <name>` header per label track

Each Audacity label track gets, as its first (earliest-time) label, a marker `LABELTRACK <name>`.
`sort_tsv.py` reads it **before sorting** (input is sequential) and scopes every following
label to `<name>` until the next `LABELTRACK`. Evidence it works: `d336-355.labels.tsv` shows
the export is per-label-track contiguous blocks, internally time-sorted (one-time check before
relying on it).

**Name handling (three cases):**

| `<name>` is… | Behaviour |
|---|---|
| the primary stem (`== <stem>`) | **No prefix** — raw `file…`/`track…`/`start…` grammar (primary block) |
| another **file** stem (e.g. `d356-375`) | **No prefix**, routed to the secondary mechanism (`file_<other>`) |
| anything else (e.g. `070.labels`) | **Prefix** via the expansion rule below |

**The track name is not the qualifier** *(learned 2026-07-14, implemented — #84)*. A
track called `070.labels`, `069-dig.labels` or `069.vinyl` holds labels *about* `orig070` /
`orig069`. So the qualifier is the **3-digit original in the name**, not the name itself —
`069-dig` and `069.vinyl` are two tracks about the same original and both expand to `orig069`.
Using the stem verbatim produced `070 note: orig070 sync: A`, which parses as nothing.

**Expansion rule (prefix case) — implemented, #84:**

0. If the label already **names its subject** (`orig070 sync: A`, `file end: …`, `069s0`) → emit
   as written. *(The original rule only asked "does it start with `<name> `", so a label already
   carrying its proper qualifier got prefixed a second time and destroyed.)*
1. Else if `"<qualifier> " + label` matches a grammar pattern → use that (`sync: 0` → `orig070 sync: 0`).
2. Else if the label is **keyword-shaped** → **ERROR** (see below).
3. Else → free text → `"<qualifier> note: " + label`, recorded in the run's auto-note summary.

**Free text vs. a mistyped keyword (locked 2026-07-14).** The requirement: *"i
don't want to have to prefix all my little notes with `note:` but i also want to know when i've
made a typo on a significant keyword."* Warning on everything that fails to parse — the original
behaviour — flags every note you'd ever want to jot (68 of them in `d356-375` alone) while a real
typo drowns in the noise. So the classification keys on **shape, not parse-success**: a label that
leads with an entity (`orig…`, `track…`, `file…`, `mix…`), a verb **and its colon** (`sync:`,
`start:`, `end:`, `note:`, `ID:`), or the compact `NNNs`/`NNNe` form was *trying* to be a keyword,
so failing to parse is an **error that stops the write**. Everything else is free text, silently
auto-noted, and listed in a summary at the end of the run.

- `origNNN start:` / `end:` **keep their colon** (decided against relaxing the grammar): a bare
  `orig070 start` is keyword-shaped and therefore an error, not a note.
- **Scope check.** Inside `LABELTRACK 071`, every label is about 071 — so a label whose *head*
  names another original (`orig017 sync: A`) is an error. This is the one typo class no grammar
  check can catch: `orig017 sync: A` parses perfectly, and did pass silently. It reads the head
  only; a note's body may mention any original.

**Decisions (locked):** the `LABELTRACK` line does **not** survive to the `.tsv`; `--live`
**must** apply the expansion before comparing (note `GetInfo: Type=Labels` returns the track
*number*, not its name, so the name must come from the in-track `LABELTRACK` label).

**Validation — flag a missing `LABELTRACK`:** `sort_tsv.py` should require one at the **start
of the file**, and one **wherever a line's timestamp is earlier than the previous line's** (a
backwards jump signals a new label-track block that began without a header). Flag each missing
marker so it can be fixed in Audacity.

## Proposal B — auto-emit time-adjusted starter files

**Shipped** as `streamalign starter` (`scripts/streamalign/emit_labels.py:emit_starter`). Note it
reads its `file_<other>:` links **out of the committed `<stem>.labels.tsv`** — which is why the
sort must not eat them (see the corrected background above; #84).

Automate "carry labels forward". When a file records where a neighbour begins inside it,
emit a pre-positioned starter set for that neighbour.

- The **link is the `file_<other>:` entry's local timestamp** (no new token). e.g.
  `d336-355` line 120: `file_d356-375: file start sync: d356-375.wav … 1203.135` ⇒ d356-375
  begins at local 1203.135 inside d336-355.
- Take this file's labels at/after that local time, subtract the offset (math already in
  `--adjust`), and write `<other>.starter.labels.tsv`.
- **Derive/store `--adjust`** instead of hand-copying it from the spreadsheet: the master
  offset already lives in the `file start sync: FILE OFFSET` label (or a small stored map) —
  close that manual loop so the value is looked up, not typed. *(Confirm the source-of-truth
  when we build it.)*
- **`*.starter.labels.tsv` is seed-only:** it may be committed, but it is **excluded from the
  sheet import (Code.js) and from solve/build processing** — it only pre-positions labels for a
  new file in Audacity.
- Naming fits the reserved scheme: hand = `<stem>.labels.tsv`, engine = `<stem>.auto.labels.tsv`,
  seed = `<stem>.starter.labels.tsv` (lowest priority; overwrite freely).
- Open: how far forward to carry; whether to drop labels purely about *this* file.

## Proposal C — `--live`: fix and automate

Today `--live` reads labels from Audacity via `pipeclient` (`GetInfo: Type=Labels`), sorts,
and compares against the on-disk `.tsv`. It is currently **not working** and run
inconsistently. **Step 1 of implementing this is to research the current Audacity 3.7.8
scripting API** — the existing pipe code predates 3.7.8 and there may be better APIs now;
don't rewrite `--live` until that's checked. Desired end state:

- **(a) Fix `--live`.** Suspects: `mod-script-pipe` enabled in Audacity, the pipe paths in
  `scripts/pipeclient.py`, and the reply-parse / `floatcmp` comparison.
- **(b) Auto-run live when Audacity is open.** On any `sort_tsv.py` run, if Audacity has the
  file loaded, compare to confirm the on-disk export is current. Apply the `LABELTRACK`
  expansion on both sides before comparing.
- **(c) Fail loud.** If Audacity is up but live can't be reached, complain, suggest the fix,
  and exit non-zero — never silently proceed against a possibly-stale export.

## Proposal D — one-command publish + auto sheet refresh

- **Local:** a `publish` wrapper (Makefile target / script) that runs `sort_tsv.py` then
  `git add/commit/push` of the `.tsv`, **gated on the grammar check** (refuse to push on
  unrecognized-grammar / missing-`verified` / missing-`COMPLETE` warnings).
- **Cloud:** eliminate the "Reload Data" click with an **event-driven GitHub Action** — on
  push to this repo it calls the Apps Script Web App (`doPost` → `GithubImport()`) so the
  sheet refreshes within seconds. **No time-driven trigger** (when this work falls idle a timer
  would fire thousands of no-op runs). This keeps the **repo → sheet** view current; the sheet
  remains a view only.
- **End state:** finish your label tracks → one command → the sheet *and* File List status
  update themselves.

## Proposal E — one command per capture (built, #84)

The five hand-run commands per file (`hints`, `align`, `skip-clips`, `sort_tsv`, `starter`)
collapse to one. (`skip-clips` is now retired, in `Archive/skip-clips/`.) Two realisations made it cheap:

- **`streamalign hints` already subsumes `align` and skip detection** — it cross-correlates
  every overlapping neighbour (the offset `align` prints) and emits skip candidates as
  `note QUESTION: possible SKIP …` rows. So the engine step is a single `hints` call.
- **`sort_tsv.py` is the natural hub.** On a successful sort it already seeds the starters
  (Proposal B, now automatic); it now also **guesses the next file** (`graph.next_stem`: the
  `file_<next>:` hand link, else the 1998/2017 notes, else the filename range — returning the
  reason) and offers to run `hints` on it. `--nextfile` overrides, `--no-next` skips, `--next`
  runs unattended.

**The one guard that matters:** the next-file prompt appears only on a TTY. `publish.py` runs
`sort_tsv.py` as a subprocess with no terminal, so an unguarded prompt would hang the push
forever; off a TTY it prints the command and returns (and `publish` passes `--no-next` besides).

**Naming/hygiene fixed alongside:** `sort_tsv.py` warns whenever it writes a bare `<stem>.tsv`
(invisible to solve/build/publish/sheet); `*.hints.tsv` and `*.starter.labels.tsv` are now
gitignored scratch; and the sheet importer was tightened to `*.labels.tsv` (+ `remainder.tsv`)
mirroring `groundtruth.is_pipeline_label_file` — it had been swallowing hints and mis-named
`.tsv` files as if they were hand labels.

## Prior art (live Audacity scripting)

`scripts/alignfinder.py` + `scripts/pipeclient.py` already drive Audacity over the
mod-script-pipe (interactive label read/write-back). That's the basis for the `--live`
fix and for a tighter Audacity ⇄ engine loop (push auto labels into an open project, pull
corrections back out).

## Next steps

In order:

1. **Research the current Audacity 3.7.8 scripting API** (the existing pipe code predates it) — gates all the `--live` work below.
2. Implement `LABELTRACK <name>` (three name cases + expansion; not persisted to `.tsv`) **+ missing-`LABELTRACK` validation** (required at file start and at every backwards-timestamp boundary).
3. One-time check that Audacity exports label tracks as contiguous per-track blocks.
4. Implement `.starter.labels.tsv` emission from `file_<other>:` links — **separate files, excluded from import/processing**; derive/store the `--adjust` value.
5. Fix `--live` (per the API research) + auto-run when Audacity is open + fail-loud guard.
6. Live-read **write** mode for `sort_tsv.py` (Audacity → `.tsv` directly, no manual export).
7. `publish` wrapper (sort + commit + push) gated on the grammar check.
8. **GitHub Action → Apps Script Web App webhook** for the repo → sheet refresh (no timer).
9. *(Done, #84 — Proposal E)* One command per capture: `sort_tsv.py` seeds the starters and preps the next file (`graph.next_stem` + `streamalign hints`), TTY-guarded so `publish` can't hang; sheet import tightened to the pipeline predicate.
