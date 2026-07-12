# Netradio Drum and Bass ISDN - mix analysis

> **What this is:** the **master index** for this repo — the master-timeline +
> track-identification analysis of the 1998 netradio.com Drum & Bass ISDN stream.

## Introduction

In 1998 I discovered Drum & Bass courtesy of a netradio.com station called
something like "Drum & Bass ISDN". I was completely entranced by these dark
intelligent quiet/driving tunes.  The thing was, a lot of music was long DJ
mixes and there were no annotations.  I became obsessed, documented a lot of
it by sounds and clips of lyrics, and wrote some hacky code to record the
RealAudio stream on my Sun workstation.

The station was an almost nine hour loop of music, and I believe I captured
the entirety of it in 70 WAV and AU format files, comprising almost 23 hours
of audio.  I've listened to these files a lot over the last 25 years, and
along the way I found a lot of the original music.  However, over the last few
years I decided to try to identify the whole playlist.

I started this project in 2017, loading the files into [Audacity](https://www.audacityteam.org/),
making notes, piping audio into Shazam and other music ID programs, and
eventually starting to compare the mix with the original tracks to learn more
about start/stop points and rate changes.  I tracked the notes in a text file,
but when I picked this up again I decided a Google Sheet would be a better
help in summarizing and cross-referencing the information.

## What's here

* [tracklog-1998.txt](./tracklog-1998.txt) -- The text file I used in 1998 to try and ensure I'd recorded the entire stream
* [tracklist-2017.txt](./tracklist-2017.txt) -- Notes on stream details, tracklist, and clues to unknown songs
* [audacity](./audacity) -- Audacity version 2.1.x metadata files from 2017.  Audacity 3.3 stores data in one big file,
  so these remain as 2017 artifacts.
* [labels](./labels) -- Audacity 3.x label export files -- notes on file start/stop, track start/stop, sync points with original tracks, etc.
* [logo](./logo) -- netradio.com logo files retrieved from archive.org and other places
* [scripts](./scripts) -- misc. helper scripts mostly oriented around Audacity 2.1.x metadata files
* [STREAM_PROVENANCE.md](./STREAM_PROVENANCE.md) -- how the capture files came to exist (RealAudio loop -> `/dev/audio` dump -> hex -> wav/au), why they overlap and contain skips, and what "master time" actually is
* [scripts/streamalign/](./scripts/streamalign) -- the Stream Alignment Engine; see its [README](./scripts/streamalign/README.md) (status) and [WALKTHROUGH](./scripts/streamalign/WALKTHROUGH.md) (how the functions compose)
* [TASKLIST.md](./TASKLIST.md) -- what's outstanding, and the **known gaps in what already exists** (two queues that should be one; nothing starts the harvester)
* [docs/SCRIPTS.md](./docs/SCRIPTS.md) -- **every script**: purpose, when to run it, which python it needs
* [FINDING_MYSTERY_TRACKS.md](./FINDING_MYSTERY_TRACKS.md) -- **identifying the unnamed tracks**: where to publish the excerpts and ask (Dogs on Acid, r/AtmosphericDnB, Discogs, tuneID), the chroma-matching method that works offline, and every dead end already tried
* [PROCESS.md](./PROCESS.md) -- **how the analysis is actually done**: the per-recording loop (manual + engine), the by-ear technique for seating an original against the mix, and what the engine can/cannot do
* [HOWTO.md](./HOWTO.md) -- howto / FAQ: **which tool to run, when, and why** (notate → build → serve → publish)

## Process — how the analysis is actually done

**Moved to [PROCESS.md](./PROCESS.md).** That is now the single home for the per-recording
loop (pick → place → hint → certify skips → label → export → publish), the by-ear technique
for seating an original against the mix, and an honest account of what the engine can and
cannot do for you.

It is built around one **master timeline** for the whole stream, tracked per capture on the
spreadsheet's **File List** tab via two statuses that come straight from the labels:
**complete** (`file end: … COMPLETE`) and **verified** (`verified <other>` on a
`file [start] sync:` line — the record of how the captures connect).

The reference material the process depends on stays here: the **[label grammar](#label-grammar)**
below, and the publish/import setup that follows.

### One-command publish (`labels/publish.py`)

The last steps of the loop (sort → push → refresh; see [PROCESS.md](./PROCESS.md#11-publish))
collapse into one **hard-gated** command:

```bash
python3 labels/publish.py d019-040          # validate → sort → commit → push → refresh
python3 labels/publish.py d019-040 --check  # validate only (gate), no push
python3 labels/publish.py d019-040 --dry-run
```

It is **all-or-nothing**: it validates every target first and pushes **nothing** if any
fails. The gate (reusing `sort_tsv.py`'s checks) refuses **bad-syntax / unrecognized-grammar**
rows, **unverified** syncs, `file end` missing **COMPLETE**, missing `LABELTRACK` markers, and
files with no start-sync / `file end … COMPLETE` anchor (a half-labelled tail capture can never
reach the sheet). `*.starter.labels.tsv` (seed) and `*.auto.labels.tsv` (engine) are refused.
On success it triggers the sheet refresh by POSTing to the Apps Script Web App at
`NETRADIO_SHEET_WEBHOOK` (its `doPost` runs `GithubImport()`); if that's unset it prints the
manual **Reload Data** reminder.

### Spreadsheet import setup (GitHub token)

`Code.js` authenticates to the GitHub API with a Personal Access Token read from an Apps
Script **script property** named **`GITHUB_PAT`** (not from a sheet cell):

```javascript
var authToken = PropertiesService.getScriptProperties().getProperty('GITHUB_PAT');
```

The source repo **`trjh/netradio_DNB_analysis` is public**, so the token is not strictly
required to read labels — but without it the GitHub API is rate-limited to ~60 requests/hour
(vs ~5,000/hour authenticated), so the import can fail intermittently. Keep a valid token set.

**Set or rotate the token** (the current one **expires June 2027** — replace it before then):

1. **Create a token.** GitHub ▸ *Settings ▸ Developer settings ▸ Personal access tokens ▸
   Fine-grained tokens ▸ Generate new token.* Set an expiration, resource owner `trjh`, and —
   because the repo is public — **Public repositories (read-only)** access is sufficient
   (Metadata: Read). Copy the `github_pat_…` value.
2. **Store it in Script Properties.** Open the spreadsheet's Apps Script project (*Extensions ▸
   Apps Script*) ▸ **Project Settings** (gear icon) ▸ **Script properties** ▸ add/edit the
   property **`GITHUB_PAT`** with the token as its value. Save. No code change is needed.
3. **Verify.** Click **Reload Data** on the **File Analysis** tab; the import should repopulate
   without rate-limit errors.

Storing the token as a script property (rather than in a `SECRETS` tab) keeps it out of the
spreadsheet entirely, so the sheet can be shared without exposing the credential.

## Label grammar

Each Audacity label is `start_time ⇥ end_time ⇥ text`. `sort_tsv.py` and `Code.js` parse the
`text` with the same small set of patterns. Timestamps are **local** to the file; adding the
file's `file start sync` offset yields **master time**.

**Label-track scoping (`LABELTRACK`)**

- `LABELTRACK <name>` — the first (earliest) label of an Audacity label track *names* that
  track, so one export can carry several tracks unambiguously. `sort_tsv.py` reads it before
  sorting and scopes every following label to `<name>` until the next `LABELTRACK`; the marker
  itself is **stripped** from the emitted `.tsv`. `<name>` resolves three ways:
  the **primary stem** (`== <stem>`) → labels pass through verbatim; **another capture stem**
  (e.g. `d356-375`) → re-homed onto that file via `file_<name>:` (the secondary mechanism);
  **anything else** (e.g. `orig069`) → prefix-expanded (`sync: 0` → `orig069 sync: 0`;
  free text → `orig069 note: <text>`). A file that uses `LABELTRACK` is **validated**: every
  label-track block (the file start, and each backwards-timestamp boundary) must carry a marker,
  or `sort_tsv.py` fails before writing. Legacy exports with no `LABELTRACK` markers are sorted
  unchanged. Use `--stem <name>` to set the primary when reading from stdin.

**File-level markers (the connection backbone)**

- `file start: FILE.wav` — *physical* file start / pre-roll marker. **Not** authoritative;
  must never override a sync marker.
- `file start sync: FILE.wav OFFSET [verified OTHERFILE]` — **authoritative master anchor.**
  `OFFSET` is added to every local timestamp to get master time. e.g.
  `file start sync: d336-355.wav 19637.763068 verified d328-342`.
- `file sync: FILE.wav OFFSET verified OTHERFILE` — a sync tying this file to another; often
  authoritative for local offset 0.
- `file end: FILE.wav … COMPLETE` (or `COMPLETED`) — file endpoint; **drives "complete."**
- `verified OTHERFILE` / `verified by OTHERFILE [double-checked]` / `MARK verified by …` /
  `NOT VERIFIED` — **the connection record:** which overlapping capture confirmed this file's
  position. **Drives "verified."**
- `file_OTHER: <label>` — **secondary-file re-homing:** places `<label>` onto a *different*
  wav's track (used where one file's labels also document where a neighbour starts/ends).

**Track identity**

- `startNNN: ID: Artist - Title` — **track start** (`NNN` = track number). Title is split on
  ` - ` into artist / name.
- `ID: Artist - Title` (no `start`) — a track **ID** without starting a new track.

**Sync points (mix ↔ original; drive the speed calc)**

- `track sync: X …` / `trackNNN sync: X …` — mix-side alignment point.
- `origNNN sync: X …` — original-side alignment point.
- `X` is a single token: **`A` and `B` are the paired anchors** used for speed
  `(trackB−trackA)/(origB−origA)`; numeric `0,1,2,…` are rough/secondary syncs.

**Original-track spans**

- `origNNN start: …` / `origNNN end: …` / `origNNN note: …` — where the original starts/ends/notes.
- Shorthand `NNNs…` / `NNNe…` (3 digits + `s`/`e`) — compact orig start/end (e.g. `069s0`,
  `065e10`, `067eB`).

**Generic notes & skips**

- `mix start|end|note: …`, `note[ TAG]: …` — mix markers and free/tagged notes.
- Skips are notes: `note: SKIP back 2.279s (multiple files)`, `file note: SKIP ahead 1.248s` —
  they mark stream discontinuities in the RealAudio capture (see [STREAM_PROVENANCE.md](./STREAM_PROVENANCE.md)).

## TODO

What I'd like to accomplish
* [ ] Automatically back up/export the Google Sheet so the spreadsheet state is versioned and recoverable
* [ ] Determine complete tracklist
* [ ] Build playlist of tracks from the stream (as much as possible) on YouTube, Apple Music, and Soundcloud
* [ ] Compile definitive recording of stream, perhaps in five 1-2 hour chunks
* [ ] Publish recording on YouTube and Soundcloud -- it's not very high quality (16kHz, originally [RealAudio](https://en.wikipedia.org/wiki/RealAudio)) so I doubt I'll be chased for copyright claims, and maybe the original DJ will appear to tell us more about the mix.

It's also slightly tempting to think about remaking the mix with better quality original sources, but that's probably a step too far.

## Links

* [YouTube Playlist](https://www.youtube.com/playlist?list=PLei572m3gA_kAghvCs4L5pbCZjmzi5Hhh)
