# Netradio Drum and Bass ISDN - mix analysis

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


## Process (the old, manual + scripted Audacity workflow)

This is the established workflow as actually practised: a manual labelling pass in
Audacity, scripted cleanup, and a GitHub → Google Sheet import. It is built around a
single **master timeline** for the whole netradio DNB stream. (A newer, automated
stream-alignment engine lives under `scripts/streamalign/`; it will be documented
here separately later.)

The whole loop is tracked on the spreadsheet's **File List** tab, which carries a
**complete** and a **verified** status per capture file:

- **complete** — you listened to and labelled the file all the way to its end. In the
  labels this is the `file end: FILE.wav … COMPLETE` marker.
- **verified** — the file's position/offset was cross-checked against an overlapping
  capture. In the labels this is the `verified OTHERFILE` tag on a `file [start] sync:`
  line. *This tag is the record of how the source files connect to each other.*

### Steps

1. **Pick the next file.** Use the **File List** tab's complete/verified columns (plus
   [tracklist-2017.txt](./tracklist-2017.txt) and any remainder notes) to choose the next
   capture to analyze.
2. **Open the capture in Audacity.** Create/use a label track.
3. **Carry labels forward.** Bring in useful labels from the previous/overlapping file so
   the new file starts with known track IDs, file boundaries, and sync context.
4. **Overlay adjacent captures.** Load overlapping stream captures, find sync points, and
   label file starts, file ends, **verified** syncs, skips, and spans of sync.
5. **Overlay originals where known.** Add the original track/source recording when
   available; label `origNNN start/end/note` points.
6. **Label paired sync points.** Add stream-vs-original pairs — `track sync: A`/`B` and
   `origNNN sync: A`/`B`. The **A** and **B** points are the ones used for the speed calc.
7. **Speed/slow values are computed downstream.** [sheetscript/Code.js](./sheetscript/Code.js)
   computes the sheet's speed value once it has all four points:
   ```javascript
   (trackB - trackA) / (origB - origA)
   ```
   [scripts/alignfinder.py](./scripts/alignfinder.py) can also help find alignment points and
   prints diagnostic speed comparisons.
8. **Export labels from Audacity.** Select the label track → **File ▸ Export ▸ Export
   Labels** (older Audacity: **File ▸ Export Other ▸ Export Labels**; bound to `⇧⌘;`). This
   writes `<stem>.labels.txt` into [labels](./labels) — a plain `start⇥end⇥label` file.
9. **Convert + sort + sanity-check with `sort_tsv.py`.** Hand it the exported `.txt`;
   [labels/sort_tsv.py](./labels/sort_tsv.py) **renames `<stem>.labels.txt` → `<stem>.labels.tsv`**
   (via `shutil.move`, leaving a `.bak`), then sorts (`file start` rows first, then by
   timestamp, then by label), splits `file_OTHER:` secondary-file entries onto their own
   files, validates the [label grammar](#label-grammar), and flags any sync line missing a
   `verified` tag or any `file end:` missing `COMPLETE`. There is **no separate txt→tsv
   tool — this script is it.**
   ```bash
   cd labels
   python3 sort_tsv.py d019-040.labels.txt --test   # dry run: just report grammar/notices
   python3 sort_tsv.py d019-040.labels.txt          # rename .txt→.tsv, sort, write back
   python3 sort_tsv.py d019-040.labels.tsv --live    # diff sorted .tsv vs labels live in Audacity
   python3 sort_tsv.py d019-040.labels.tsv --adjust  # rebase timestamps to first "file start"
   ```
10. **Push the label file to GitHub.** Commit/push the new or updated `<stem>.labels.tsv`
    to the repo the sheet reads from: **`trjh/netradio_DNB_analysis`** (hard-coded in
    `Code.js`), *not* a local working copy.
11. **Click "Reload Data" on the File Analysis tab.** That button runs `GithubImport()` in
    [sheetscript/Code.js](./sheetscript/Code.js): it lists `…/repos/trjh/netradio_DNB_analysis/contents/labels`,
    downloads every `*.tsv`, parses each (`ParseTSV`), computes normalized rows + speed
    values, and writes them into the active **File Analysis** sheet from row 2. It reads its
    GitHub token from the **`GITHUB_PAT` script property** (see [Spreadsheet import setup](#spreadsheet-import-setup-github-token)).
12. **File List status updates from File Analysis via spreadsheet formulas.** No script
    touches the File List tab — its **complete**/**verified** columns are sheet **formulas**
    that scan the File Analysis rows per `.wav` for a `file end … COMPLETE` row and a
    `file … verified …` row. Because this logic lives only in the sheet, the spreadsheet
    backup must capture formulas, not just values.
13. **Back up the spreadsheet & validate downstream.** Keep dated exports (the local CSV
    `Netradio DNB ISDN Analysis - Tracklist - preskipfix.csv` feeds the player and other
    tools). Run timeline/player smoke tests after label or sheet changes, especially around
    file transitions and overlapping captures. *(Automated, formula-preserving spreadsheet
    backup is still a major TODO.)*

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

See [AGENT.md](./AGENT.md) for a file-by-file repo guide, more label-grammar notes, a
git-history summary, and operational cautions for future agents.

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
