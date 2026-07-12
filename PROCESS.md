# PROCESS — taking one capture from raw audio to published

> **What this is:** the single home for *how the analysis is actually done* — the manual
> Audacity pass, the by-ear technique that makes it work, and exactly where the engine helps.
> **Fits in:** [README](./README.md) (what this repo is, and the [label grammar](./README.md#label-grammar))
> · [HOWTO](./HOWTO.md) (which tool to run right now) · [`scripts/streamalign/`](./scripts/streamalign/) (the engine).

The manual process is the backbone and is **not** being replaced. The engine's job is to do
the measuring, the arithmetic and the cross-checking — the things a human is slow and
unreliable at — and to **ask** rather than assert when it cannot tell. Ear and judgement stay
where they are.

---

## The shape of the work

Every capture file has to be placed on one **master timeline** for the whole broadcast, and
every track identified along it. Progress is tracked per capture on the spreadsheet's
**File List** tab, with two statuses that come straight from the labels:

| Status | What it means | The label that sets it |
|---|---|---|
| **complete** | you listened to and labelled the file to its end | `file end: <stem>.wav … COMPLETE` |
| **verified** | its position was cross-checked against an *overlapping* capture | `verified <other>` on a `file [start] sync:` line |

**Verified is the record of how the captures connect to each other**, and it is only possible
where two captures share audio.

### Two kinds of neighbour — this decides what the engine can do for you

- **Overlapping** captures share audio. The engine can cross-correlate them, so it can
  *measure* the offset, *find* skips, and cross-check a placement. Most of the stream is
  like this.
- **Exactly-joined** captures follow one another directly and share **no** audio. There is
  nothing to correlate: the engine cannot measure an offset or detect a skip, and the next
  file's position can only be **carried forward** from a hand link in the previous one
  (`file_<next>:`). Anything it says about such a file is derived, not measured — and it will
  tell you so.

---

## The per-recording loop

Steps 8–9 only apply to files containing unidentified or original-bearing tracks.

### 0. Pick the next file

Work **outward from already-placed neighbours** — a file is ready when it overlaps (or joins)
one already on the master clock. The chain starts from the anchor `d000-018 = 0`. Use the
**File List** complete/verified columns plus [`tracklist-2017.txt`](./tracklist-2017.txt).

### 1. Open it, carrying the previous file's work forward

Rather than starting from a blank project, reuse the previous capture's Audacity project so
its labels come with you:

1. Open the previous capture's `.aup3` — the one whose labels link to your new file.
2. **Save As** the new name (e.g. `d356-375.aup3`).
3. Remove the old master track and its labels.
4. **Select All** (`⌘A`), then **Tracks ▸ Keep Tracks Synchronized** (Sync-Lock) **on**.
5. Click at the start of the new base track and drag the selection to zero.
6. **Select ▸ Track ▸ In All Tracks** (`⇧⌘K`), then **Delete** (`⌘K`).
7. **Select All** again and turn Sync-Lock back **off**.

The carried labels now sit on the new file's timeline. Open an already-placed **overlapping
neighbour** too, if there is one.

### 2. Ask the engine what it thinks (hints)

Before labelling, get the engine's opinion as a **separate label track** you can accept,
ignore, or argue with:

```bash
PYTHONPATH=scripts python3 -m streamalign hints <stem>
# -> labels/<stem>.hints.tsv    Audacity: File ▸ Import ▸ Labels
```

It proposes a `file start sync:` and `file end:`, the measured offset to each overlapping
neighbour, skip candidates — and, wherever it cannot corroborate something, an explicit
`note QUESTION:` explaining *why*. Every row carries its confidence spelled out
(`confidence 9.8/10`) and is marked `HINT`.

**Hints never touch your labels.** They are written to `<stem>.hints.tsv`, which is not a
`.labels.tsv` and is invisible to the solve, the build and the sheet. They only ever *add*:
import the track, copy across what you accept, delete the rest.

### 3. Place the file

For an **overlapping** neighbour, measure the offset:

```bash
PYTHONPATH=scripts python3 -m streamalign align <placed-neighbour> <this-file>
```

It prints the offset in seconds and a 0–1 confidence, and `master_start(this) =
master_start(neighbour) + offset`. **Verify by ear.** If a second placed neighbour also
overlaps, measure that too — redundant edges cross-check the placement (`… validate` grades
them all later).

For an **exactly-joined** neighbour there is nothing to measure: the placement comes from the
previous file's `file_<next>:` link, and is only as good as that hand label.

### 4. Certify the skips

Skips are **load-bearing**: `master_end = local_length + master_start + Σ(skips inside)`. A
missed skip shifts everything placed downstream, so find them before chaining onward.

```bash
PYTHONPATH=scripts python3 -m streamalign skip-clips     # detect + render review clips
PYTHONPATH=scripts python3 -m streamalign skip-confirm <id>   # -> writes into your hand labels
PYTHONPATH=scripts python3 -m streamalign skip-reject  <id>   # -> engine stops re-proposing it
```

Listen to each clip in the clip-review player and rule on it. Skip detection walks an
**overlap**, so on an exactly-joined file it has nothing to work with — those skips are yours
to find by ear.

### 5. Hand-label in Audacity

With the master start and the skips known, lay the label track (full spec: the
[label grammar](./README.md#label-grammar)):

- `file start sync: <stem>.wav <master_start> verified <neighbour>` at local **0.0**
- `startNNN: ID: <Artist> - <Title>` at each track boundary
- `file note: skip ahead|back <N>s` at each confirmed skip
- `track sync: A`/`B` + `origNNN sync: A`/`B` where an original lines up (see below)
- `file end: <stem>.wav COMPLETE` at the end
- `LABELTRACK <name>` as the first label of a track, if exporting several tracks at once

### 6. Export, sort, sanity-check

**File ▸ Export ▸ Export Labels** (`⇧⌘;`) → `labels/<stem>.labels.txt`, then:

```bash
python3 labels/sort_tsv.py labels/<stem>.labels.txt          # rename .txt→.tsv, sort, validate
python3 labels/sort_tsv.py labels/<stem>.labels.tsv --test   # dry run: report notices only
python3 labels/sort_tsv.py labels/<stem>.labels.tsv --live   # diff on-disk vs Audacity's live labels
python3 labels/sort_tsv.py labels/<stem>.labels.tsv --adjust # rebase timestamps to the first file start
```

This **is** the txt→tsv tool — there is no other. It sorts, splits `file_OTHER:` rows onto the
neighbour's file, validates the grammar, and flags a sync line missing `verified` or a
`file end:` missing `COMPLETE`.

### 7. Seed the next file

If you captured where the **next** file begins (a `file_<next>:` link), carry it forward so
the next file doesn't start blank:

```bash
PYTHONPATH=scripts python3 -m streamalign starter <this-stem>
```

### 8. Identify the tracks

For an unidentified span: AcoustID-fingerprint it, or match it against your originals by ear.
Add `ID: <Artist> - <Title>` to the label. An unnamed but placed track is `Mystery Track N`.

### 9. Align the originals

**Don't chase the track's start and end** — in a DJ mix records are *blended*, so there is no
frame at which one "begins", and picking one is subjective. What you actually need is the
opposite: moments where the record plays **alone**, which are objective and are exactly what
an A/B anchor is made of.

The engine will propose those (`solo_anchors`, surfaced through `streamalign hints`, and
bounded by the [2017 notes](./tracklist-2017.txt)). Each candidate gives **both** times — an
instant in the mix *and* the matching instant in the original — i.e. a ready-made
`track sync:` / `origNNN sync:` pair. Seat one early (`A`) and one late (`B`); confirm both by
ear using the technique below.

Then grade the result:

```bash
make align-env      # once: the librosa venv (python3.13) + NETRADIO_SOURCES_DIR in .env_vars
make align-check
.venv/bin/python -m streamalign track-mix --meta track-metadata.json --sources <originals-dir>
```

`track-mix` recovers the mix/original rate + offset per track (chroma + DTW) and reports
whether it is reliable enough to trust.

> **Sanity check that costs nothing:** a DJ pitches a record by a *few percent*. If your two
> anchors imply a rate far from 1.0, they are not both on the record — the engine gates its own
> candidates on exactly this, and it is how it catches its own bad matches.

### 10. Build + validate

```bash
python3 scripts/build_track_metadata.py                  # labels → track-metadata.json
PYTHONPATH=scripts python3 -m streamalign validate       # engine vs hand: error table
```

`build_track_metadata.py` is the **only** writer of `track-metadata.json`. Nothing in the
notating steps above writes it.

### 11. Publish

```bash
python3 labels/publish.py <stem> --check   # gate only, no push
python3 labels/publish.py <stem>           # gate → sort → commit → push → refresh the sheet
```

Hard-gated: it refuses unverified syncs, a missing `COMPLETE`, or bad grammar, and it is
all-or-nothing across the files you give it. Then **loop to step 0.**

---

## Aligning an original to the mix (by ear + by eye)

Steps 5 and 9 say to overlay the original and drop paired sync points. This is how you
actually *find* the seat.

Up front: **the mix and the original are rarely at the same clock rate** — the DJ beatmatches
— so a point that seats perfectly at one end drifts by the other. That drift is *captured* by
the **A/B anchor pair**, not fought.

**Pick the cue.**

- Anchor on a sharp **transient**, never a sustained sound: a consonant plosive, a word onset,
  a snare or rim, a vinyl click, a one-shot FX. The phrase you remember only tells you *which*
  transient to seat on.
- Prefer an **aperiodic** cue. The break loops, so a purely rhythmic match can be a whole bar
  out; a non-repeating event pins the *absolute* position.
- For vocals, switch the track to **Spectrogram** — consonants show as clean vertical energy
  bursts that line up by eye where the waveform is an ambiguous blob.

**Hear the seat.**

- Hard-pan the mix left and the original right on headphones. Off by a hair you hear a
  **flam** leaning to one ear; as you close in it collapses to a single centred hit, and flams
  the *other* way if you overshoot. Where the flam vanishes is good to a few samples.
- Sharper still, a **phase null**: `Effect ▸ Invert` the original and listen to the sum. What
  the two share (bass, break) audibly thins into a null at the seat — a *deepening
  cancellation* is easier to hear than a tightening echo. It won't null to silence (the mix
  has other layers). Un-invert when done.

**See the seat.** At sample zoom Audacity draws individual sample dots. Match the **timing**
of peaks and zero-crossings, **not** their heights — the mix copy is EQ'd and compressed
differently, so amplitudes won't agree. Coarse-align on the phrase → zoom to the transient →
zoom to samples.

**Nudge precisely.**

- **Sync-Lock Tracks** moves the original's audio and its `origNNN` labels as a unit. Turn
  **Snap off** or every edit quantizes. Set the **Selection Toolbar** to **samples**.
- Drag with Time-Shift zoomed to samples — one screen pixel is then a fraction of a sample.
- Audacity has no numeric "nudge by N samples". For an exact amount, edit the **head of the
  original only**: `Generate ▸ Silence` (typed duration) pushes it later; select `0`→`N` at
  the head and delete to pull it earlier. If the mix is in the Sync-Lock group it ripples too
  — drop Sync-Lock for that one edit.

**Capture two anchors, not one.** Seat a cue near the start (`track sync: A` on the mix,
`origNNN sync: A` on the original) and another near the end (`… B`). If the intro seats
perfectly but the outro is a few ms out, that *is* the clock-rate difference — and the A/B
pair is exactly what lets the speed calculation solve it:

```
speed = (trackB − trackA) / (origB − origA)
```

computed downstream in [`sheetscript/Code.js`](./sheetscript/Code.js), instead of you chasing
a moving target by hand.

---

## What the engine cannot do

Worth knowing, so you don't wait for help that isn't coming:

- **No overlap, no measurement.** Cross-correlation is the engine's only evidence. On an
  exactly-joined file it cannot propose a sync point or detect a skip *at all* — it will say
  so in a question rather than pretend.
- **It cannot search for an original *blind*.** Given a whole capture and no idea where a
  record sits, `locate_original` is wrong more often than right here (measured: 2 hits in 8,
  and its *most confident* answer was wrong by 25 minutes). Two reasons, neither fixable by
  tuning: the DJ beatmatches, so a fixed-lag correlation drifts out of alignment within a
  minute; and the broadcast repeats material, so the answer isn't even unique. It is
  deliberately **not** offered as a hint. See
  [`Archive/LESSON_locate_original.md`](./Archive/LESSON_locate_original.md).
  **With a prior it works** — see `solo_anchors` above. Bounding the search is the whole
  difference.
- **It cannot tell you where a track starts or ends.** Nor can anyone: in a DJ mix records are
  *blended*, so there is no frame at which one "begins". Start/end is a judgement call and
  stays yours. What the engine can offer instead is the moments where a record plays **alone**
  — which is what an anchor actually needs.
- **It does not decide.** Everything in the notating steps produces labels, clips, scores or
  questions **for you to review**. Only `build_track_metadata.py` writes the authoritative
  JSON.

---

## File-naming: who owns what

| File | Who writes it | Authority |
|---|---|---|
| `<stem>.labels.tsv` | **you**, by hand | authoritative; nothing else may overwrite it |
| `<stem>.auto.labels.tsv` | the engine (`tail-solve --emit`) | regenerable; consumed by solve/build |
| `<stem>.starter.labels.tsv` | `streamalign starter` | seed only; excluded from import/solve/build |
| `<stem>.hints.tsv` | `streamalign hints` | **suggestions + questions**; invisible to everything, yours to accept or delete |
