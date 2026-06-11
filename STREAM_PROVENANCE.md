# Stream provenance — where these files came from, and what "master time" means

This explains how the netradio DNB capture files came to exist, why they overlap,
why they contain skips, and — importantly for the [Stream Alignment Engine](#the-reconstruction-problem)
— what the project's "master timeline" actually *is*. Tim wrote most of this down
here for the first time on 2026-06-11; the [README](./README.md) has the shorter
origin story.

## The chain of custody

The audio in this repo is the end product of a long pipeline, and every stage
shaped it:

1. **A DJ built a playlist** for the netradio.com "Drum & Bass ISDN" station and
   **mixed the tracks together** into one continuous ~9-hour set.
2. **That mix was turned into a file (or files)** in the late 1990s — the exact
   encoding/segmentation on netradio's side is unknown.
3. **It was broadcast on a loop** over the internet via RealAudio/RealPlayer. The
   listener did **not** choose a position in the stream — you clicked the station
   and heard *whatever the "broadcast" was playing at that moment*. It just looped.
4. **Tim played the stream on his Sun workstation** and used a syscall tracer
   (`truss`/`strace`-style) to **dump the bytes being written to `/dev/audio`**.
   These came out as very large text files. *(The original capture code still
   exists and can be added here if useful.)*
5. **Perl code extracted the hex from those dumps into binary audio data.**
6. The binary was **sometimes converted to WAV, sometimes left in the original Sun
   audio (`.au`) format** — which is why the captures are a mix of `.wav` and `.au`.

The result: **~70 capture files, ~23 hours of audio**, that together cover an
almost-9-hour loop — so there is heavy redundancy (the same broadcast content
recorded many times, at different points in the loop).

## What a capture file *is*: a window onto the loop

Because the broadcast was a loop you joined at an arbitrary point, each capture
file is just a **window** onto that loop — a slice starting wherever Tim happened
to begin recording, running until he stopped. Different windows overlap heavily,
and between two windows the **same broadcast audio is recorded twice**. That
redundancy is the whole basis for alignment: where two windows overlap, they are
(barring capture artifacts) the *same samples*, so one window can be positioned
relative to another by finding the offset at which their overlap matches.

## What "master time" means

"Master time" / `master_seconds` / `master_begin_seconds` is **not broadcast
wall-clock time** and not a netradio-side timecode. It is **the timing of the DJ's
original continuous mix** — the single ~9-hour programme timeline — *reconstructed*
from the overlapping capture windows, and possibly slightly distorted by the
capture pipeline above.

Concretely, the master timeline is a **construction**:

- It is anchored at `d000-018 = master 0.0` (the `SELF-INIT` file-start sync).
- Every other file is placed by aligning its overlap with an already-placed
  neighbour, chaining outward from the anchor.
- So the master timeline is literally the **stitched-together, skip-corrected
  sequence of capture segments along the primary chain**. Its accuracy *is* the
  accuracy of that alignment.

There is no external reference to anchor to — the windows are all we have, and the
goal of the analysis is to **reconstruct the DJ's mix timeline from them**.

## Why there are "skips" — and why they're load-bearing

The capture pipeline (a looping RealAudio stream traced off `/dev/audio`) was not
sample-perfect. Individual captures contain **skips**: points where the recorded
audio jumps **forward** (missing a stretch of the broadcast) or **back** (repeating
a stretch). Skips are **per-capture** — a glitch in one window is generally not in
another — which is exactly why overlapping windows can be used to *find* them: where
two otherwise-identical windows suddenly diverge, one of them skipped.

Skips are not a footnote; they are central to the reconstruction:

- A file's mapping from its **local time** to **master time** is *piecewise*:
  slope 1, with a `+N` jump at each "skip ahead N seconds" and a `−N` jump at each
  "skip back N seconds". So a file's **precise master end** is
  `local_length + master_start + Σ(skips inside it)` — a file with a significant
  skip ends at a very different master time than its length alone would suggest.
- Because files are placed **by chaining** (each new file locks onto an
  already-placed neighbour's content), an **unaccounted skip propagates** its error
  to every file placed downstream of it.
- Therefore the alignment engine must **find every skip first**; there is no honest
  "rough master-start" that can be refined later. Skip count per file is also a
  useful **quality signal** for choosing the cleanest covering set of captures.

## The reconstruction problem (the Stream Alignment Engine)

The above is why the headline analysis effort is to **reconstruct the master
timeline by audio alignment**: starting from the anchor file, find the precise,
skip-resolved placement of every capture window on the master timeline, validate it
against the ~45 file-alignments Tim has already done by hand (in `labels/*.labels.tsv`
as `file start sync:` rows), and then emit alignment labels for the windows not yet
done by hand. Between two capture windows the relationship is expected to be a pure
time offset (no clock drift, no polarity inversion — those artifacts in the hand
labels were *original-track-vs-stream* comparisons, not stream-vs-stream); that
assumption is to be **verified, not assumed**. See the player repo's `TASKLIST.md`
("Stream Alignment Engine") and `scripts/alignfinder.py` (the earlier interactive,
pairwise prototype) for the working plan.
