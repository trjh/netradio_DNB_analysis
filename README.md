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


## Process

The analysis process is a manual+scripted Audacity workflow built around a single master timeline for the whole netradio DNB stream.

1. **Start from the notes.** Use [tracklist-2017.txt](./tracklist-2017.txt), the current label TSVs, and any remainder/progress notes to choose the next stream audio file to analyze.
2. **Open the stream file in Audacity.** Load the stream capture tile and create/use a label track.
3. **Carry labels forward.** Bring in useful labels from the previous or overlapping stream file so the new file starts with known track IDs, file boundaries, and sync context.
4. **Overlay adjacent stream files.** Load overlapping stream captures and find sync points between them. Label file starts, file ends, verified sync points, skips, and spans of sync.
5. **Add original/source audio where known.** Overlay the original track/source recording when available. Label source start/end/note points.
6. **Label paired sync points.** Add stream-vs-original sync pairs such as `track sync: A`, `track sync: B`, `origNNN sync: A`, and `origNNN sync: B`.
7. **Calculate speed/slow values.** The Google Apps Script importer in [sheetscript/Code.js](./sheetscript/Code.js) computes the sheet speed value when it has all four sync points:
   ```javascript
   (trackB - trackA) / (origB - origA)
   ```
   [scripts/alignfinder.py](./scripts/alignfinder.py) can also help find alignment points and prints diagnostic speed comparisons.
8. **Export labels.** Export Audacity labels to [labels](./labels) as `*.labels.tsv`.
9. **Sort and sanity-check labels.** Use [labels/sort_tsv.py](./labels/sort_tsv.py) to sort labels, check recognized label grammar, split secondary-file entries, and compare live Audacity labels when needed:
   ```bash
   cd labels
   python3 sort_tsv.py d019-040.labels.tsv --test
   python3 sort_tsv.py d019-040.labels.tsv
   python3 sort_tsv.py d019-040.labels.tsv --live
   ```
10. **Import labels into the sheet.** The Apps Script in [sheetscript/Code.js](./sheetscript/Code.js) reads label TSVs from GitHub, parses them, computes normalized rows and speed values, and writes them into the active Google Sheet.
11. **Export/back up the spreadsheet.** The local CSV export (`Netradio DNB ISDN Analysis - Tracklist - preskipfix.csv`) is consumed by downstream tools, including the player. Automatic spreadsheet backup is still a major TODO: set up a repeatable export from Google Sheets/Drive or Apps Script, avoid secrets, and keep dated backups.
12. **Validate downstream.** Run timeline/player smoke tests after label or sheet changes, especially around file transitions and overlapping captures.

See [AGENT.md](./AGENT.md) for a file-by-file repo guide, label grammar notes, git-history summary, and operational cautions for future agents.

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
