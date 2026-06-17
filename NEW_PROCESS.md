# NEW_PROCESS.md — proposed improvements to the labelling → sheet workflow

- **Status:** Draft / design notes, started 2026-06-17 (under review with Tim)
- **Scope:** Proposed changes to the *old* process documented in [README.md](./README.md). Nothing here is implemented yet.
- **Goal:** Reduce manual steps and remove "did I remember to run X?" failure modes, and close known grammar gaps in [labels/sort_tsv.py](./labels/sort_tsv.py).

This doc collects design decisions made in conversation so the README can stay a clean
record of the *current* process. Items marked **ROADMAP** are candidate work items.

---

## Motivation

1. **Grammar gap.** Free-text labels that don't fit a keyword pattern are flagged as
   unrecognized and must be fixed by hand. Today the only way to attach a label to an
   original/track/file is to type its qualifier on every label (`orig069 note: …`,
   `orig069 sync: 0`, …). That's repetitive and error-prone.
2. **Forgotten manual steps.** `--live` (compare on-disk export vs. what's live in Audacity)
   is optional and run inconsistently. Years on, there's no way to know whether a given
   `.tsv` reflects the latest Audacity state. We want the tooling to enforce/automate this
   rather than rely on memory.

---

## Background: what `file_<other>:` does today

Verified against `sort_tsv.py`:

- A label written `file_<NAME>: <inner label>` is matched by `file_([^:]+):\s+(.+)`.
- `<NAME>` is peeled off; the **inner label** is stashed in a per-name bucket
  (`secondfiles[<NAME>]`).
- After the primary labels are sorted, each bucket is processed on its own: sorted as a
  separate block, validated (the `file start` / `file end` tag must contain `<NAME>`), and
  appended to the output.
- **Meaning:** while working inside file A, this is how you record labels that actually
  describe a neighbour file B — where B starts inside A, B's own `file start sync`, etc. —
  keeping them grouped and checked under B.
- **OPEN ITEM:** the code path appears to *strip* the `file_<NAME>:` prefix on output. If so,
  the file-association is carried only by block position, which would not round-trip cleanly.
  Confirm and decide whether the prefix should be preserved. **(ROADMAP)**

---

## Proposal A — `LABELTRACK <name>` header per label track

### Idea

Each Audacity label track gets, as its first (earliest-time) label, a marker:

```
LABELTRACK <name>
```

`sort_tsv.py` reads this **before sorting** (input is processed sequentially) and applies
`<name>` to every subsequent label on that track until the next `LABELTRACK`.

### Why it works

`d336-355.labels.tsv` shows Audacity exports label tracks as **contiguous, per-track,
internally time-sorted blocks** (orig069 lines 1–14, orig067 15–34, orig066 35–45,
orig065 46–72, …) — *not* globally time-sorted. So a `LABELTRACK` placed at t≈0 of a track
lands first in that track's block, and a sequential reader can scope the block correctly.
**(One-time check: confirm this holds in the current Audacity version before relying on it.
ROADMAP.)**

### Name handling (the three cases)

| `<name>` is… | Behaviour |
|---|---|
| the primary stem (`<name> == <stem>`) | **No prefix.** Labels keep raw `file …` / `track …` / `start…` grammar (primary block). |
| another **file** stem (e.g. `d356-375`) | **No prefix**, routed to the secondary-file mechanism (`file_<other>`), i.e. its own block, validated against that name. |
| anything else (e.g. `orig069`) | **Prefix** with the expansion rule below. |

### Expansion rule (for the "prefix" case)

For each label under `LABELTRACK <name>`:

1. If the label already starts with `<name> ` → leave it unchanged (idempotent — existing
   fully-qualified files still pass).
2. Else if `"<name> " + label` matches a grammar pattern → use that
   (`sync: 0` → `orig069 sync: 0`).
3. Else → `"<name> note: " + label`
   (`light percussion starts` → `orig069 note: light percussion starts`).

### Decisions (locked)

- **The `LABELTRACK` line does NOT survive to the `.tsv`.** It's consumed during processing;
  the `.tsv` is the flattened, fully-qualified form. Audacity remains the source of truth for
  track structure.
- `--live` **must** read and apply `LABELTRACK` rules *before* comparing, or every expanded
  label reads as a spurious diff. Note `GetInfo: Type=Labels` returns the track *number*, not
  its name, so the name must come from the in-track `LABELTRACK` label itself.

---

## Proposal B — auto-emit time-adjusted starter files

### Idea

Automate "carry labels forward" (README step 3). When a file records where a neighbour begins
inside it, emit a pre-positioned starter label set for that neighbour.

- The **link is the `file_<other>:` entry's local timestamp** — no new token needed.
  (Example: `d336-355` line 120, `file_d356-375: file start sync: d356-375.wav … 1203.135`
  says d356-375 begins at local 1203.135 inside d336-355.)
- Take this file's labels at/after that local time, subtract the offset (the math already
  exists in `--adjust`), and write `<other>.starter.labels.tsv`.

### Naming (fits the existing reserved scheme)

| File | Source | Priority |
|---|---|---|
| `<stem>.labels.tsv` | hand-made / confirmed | highest |
| `<stem>.auto.labels.tsv` | streamalign engine | middle |
| `<stem>.starter.labels.tsv` | **new** — carried-forward seed | lowest (overwrite freely) |

### Decisions to nail down **(ROADMAP)**

- Canonical link field: confirmed = the `file_<other>:` local timestamp (no `link=` token).
- How far forward to carry: to end-of-file, or to the neighbour's known overlap end?
- Whether to drop labels purely about *this* file (e.g. `file end: <stem> COMPLETE`) from the
  starter.

---

## `--live`: fix and automate **(ROADMAP)**

Today `--live` reads labels from Audacity via `pipeclient` (`GetInfo: Type=Labels`), sorts
them, and compares against the on-disk `.tsv` (the comparison rounds to the less-precise side).
It is currently **not working** and is run inconsistently. Desired end state, in priority order:

- **(a) Fix `--live`.** Suspects to check first: `mod-script-pipe` enabled in Audacity
  (Preferences ▸ Modules), the pipe paths in `scripts/pipeclient.py`, and the reply-parsing /
  `floatcmp` comparison logic.
- **(b) Auto-run live when Audacity is running.** On any `sort_tsv.py` run, if Audacity is up
  with the relevant file loaded, automatically run the live comparison to confirm the on-disk
  export is current — i.e. catch "edited in Audacity but forgot to re-export." Must apply the
  `LABELTRACK` expansion (Proposal A) on both sides before comparing.
- **(c) Fail loud.** If Audacity is running and live can't be reached / doesn't work, complain,
  suggest the fix, and exit non-zero — never silently proceed against a possibly-stale export.

This is the core "minimize manual steps / don't rely on memory" change.

---

## Automating the post-labelling pipeline

The irreducible manual work is **listening + setting up the label tracks**. Everything after
that — export → sort → push → sheet refresh → File List status — is mechanical and can collapse
to roughly *one command and zero clicks*. There are two independent halves.

### Local half (Audacity → `.tsv` → GitHub)

- **Pull labels straight from Audacity — kill the manual export.** Extend `--live` into a
  *write* mode: one command reads the live label state, applies the `LABELTRACK` expansion,
  sorts, and writes the canonical `.tsv`. Removes the `File ▸ Export` step and structurally
  removes the "did I forget to re-export?" stale-file risk. Depends on the pipe fix. **(ROADMAP)**
- **A single `publish` wrapper with a validation gate.** A Makefile target / small script —
  `make publish FILE=d336-355` — that runs `sort_tsv.py` then `git add/commit/push` of the
  `.tsv`, **gated on the grammar check** (refuse to push if there are unrecognized-grammar /
  missing-`verified` / missing-`COMPLETE` warnings), so errors are caught before the sheet, not
  after. **(ROADMAP)**

### Cloud half (GitHub → sheet — eliminate the button)

- **Time-driven Apps Script trigger (low effort).** An installable trigger runs
  `GithubImport()` every ~10 min (or hourly); pushing to GitHub becomes sufficient and the
  sheet catches up on its own. Keep the "Reload Data" button as a custom menu-item fallback.
  ~5 min to set up, no new infrastructure. **(ROADMAP)**
- **Event-driven webhook (most polished, more setup).** A GitHub Action on push to the labels
  repo calls the Apps Script deployed as a Web App (`doPost`), which runs `GithubImport()` →
  sheet updates within seconds. Needs a Web App deployment + shared secret (Action repo-secret
  ↔ script property) + the Action. **(ROADMAP, optional)**

### Suggested order

1. Fix the pipe (roadmap #5) — unblocks everything live.
2. `publish` wrapper + validation gate — immediate, purely local.
3. Time-driven trigger — removes the button click.
4. (Later, if you want instant) the webhook.

**End state:** finish your label tracks → run one command → the sheet *and* File List status
update themselves. Note that the carry-forward seed (Proposal B, README step 3 "load
time-adjusted labels track") and this publish flow are the two ends of the same automated loop;
all cloud options work with the existing public repo + `GITHUB_PAT` setup, no auth changes.

---

## Roadmap candidates (collected)

1. **Confirm `file_<other>:` output behaviour** (prefix stripped vs preserved) and fix round-trip.
2. **Implement `LABELTRACK <name>`** parsing + the three name cases + expansion rule (drop the marker from output).
3. **One-time check** that Audacity exports label tracks as contiguous per-track blocks.
4. **Implement `.starter.labels.tsv` emission** from `file_<other>:` links.
5. **Fix `--live`** (mod-script-pipe / pipe paths / comparison).
6. **Auto-run `--live`** when Audacity is running; apply `LABELTRACK` expansion on both sides.
7. **Fail-loud guard** when Audacity is up but live is unavailable.
8. **Add a live-read *write* mode** to `sort_tsv.py` (Audacity → `.tsv` directly, no manual export).
9. **`publish` wrapper** (sort + commit + push) gated on the grammar check.
10. **Time-driven Apps Script trigger** for `GithubImport()` (+ menu-item fallback).
11. **(Optional) GitHub Action → Apps Script Web App webhook** for instant sheet refresh.
