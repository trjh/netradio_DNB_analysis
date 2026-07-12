# Lesson — `locate_original`: the blind search that didn't work, and why

**Status:** the code is live in `scripts/streamalign/track_mix.py` and is **deliberately not
offered as a hint**. This is the record of why, so nobody rebuilds it hoping for a better day.

Its replacement is `solo_anchors` (same module), which works. The difference between them is
the whole lesson.

---

## What it does

`locate_original(orig, capture)` searches a whole capture for an original recording and returns
where it thinks it starts. It is the *inverse* of `align_track`, which only **grades** a
placement the labeller has already made.

That inversion is the appealing part: it needs no overlapping capture, so it would work on the
exactly-joined files where the alignment engine is otherwise blind and has nothing to correlate.

## How badly it fails

Scored against the 8 tracks whose true position is known from `track-metadata.json` (with the
capture chosen the way the trusted `align_track` chooses it — via the track's own
`source_files`):

| | |
|---|---|
| Within 5 s of the truth | **2 of 8** |
| Its single most confident answer (track 2, margin 0.69 — the highest in the set) | **wrong by 25 minutes** |

That last row is the one that matters. Because its *best* answer is among its *worst*, no
confidence or margin threshold separates the hits from the misses — **it cannot be gated into
safety.** A tool that is unreliable but knows it can still be useful; this one is unreliable and
confident.

## Why it fails — two reasons, neither a tuning problem

1. **The DJ beatmatches.** The record plays at the *mix's* speed, not its own. Over a five-minute
   track even a 1% rate difference drifts ~3 s, so a fixed-lag correlation walks out of
   alignment within a minute. (Subsequence DTW, which warps and should have handled exactly
   this, scored no better — see below.)
2. **The broadcast repeats material.** The same record genuinely recurs, so "where does it play"
   has more than one true answer, and the search has no way to prefer one.

Neither is fixed by a better threshold, a longer window, or a sharper feature. They are
properties of the material.

## The bug that hid inside the failure

The first version reported **9/10 confidence while being wrong by 11 minutes**. That was a real
bug, and worth understanding on its own:

**Chroma is non-negative.** Cosine similarity between raw chroma frames therefore sits around
0.8 for *any* two pieces of music — the true peak is a ripple on a large DC pedestal, and the
`argmax` lands wherever the capture happens to be loudest. Centring each pitch class over time
(subtracting its mean) makes a random lag score ~0 and turns the score into a real Pearson
correlation in [−1, 1].

After the fix it was still wrong — but it now *reported* itself as low-confidence (0.6–1.2/10,
margins ~0.01). **That is the difference between a bug and a limit.** The bug was fixed; the
limit remains.

## What replaced it, and the actual lesson

Two changes, both of which came from asking a better question rather than writing better code.

### 1. Ask for solo moments, not start/end

A track's **start and end in a DJ mix are subjective** — records are blended, so there is no
frame at which one "begins". Chasing that boundary was chasing something that does not exist.

The moments where a record plays **alone** *are* objective — and they are exactly what an A/B
sync anchor needs: a single instant identifiable in both the mix and the source. `solo_anchors`
looks for those instead, and returns *both* times (mix and original), i.e. a ready-made
`track sync:` / `origNNN sync:` pair.

### 2. Give the search a prior

Blind over 20 minutes of a repeating broadcast, the answer is ambiguous — that ambiguity is
what defeated `locate_original`. **Bounded**, the problem is tractable.

The bound comes from evidence that already existed and was sitting unused: `tracklist-2017.txt`
says roughly where each track sits (see `scripts/streamalign/tracklist2017.py`). The oldest,
least "technical" artefact in the project turned out to be the thing that made the newest
technique work.

**Result:** with a prior, anchor pairs recover the known mix/original rate for **5 of 6** tracks
offered, four of them to four decimal places.

### 3. Let the failures confess

A DJ pitches a record by a few percent, not tens of percent. So an anchor pair implying a rate
of **0.30** or **0.10** has not found the record — it has matched noise — and the absurd rate is
how it says so. `RATE_PLAUSIBLE` gates on that, and it caught both of the bad cases.

This is the gate `locate_original` never had: a check that is **independent of the thing being
measured**. Confidence scores come from the same machinery that produced the answer, so a
confidently wrong answer looks exactly like a confidently right one. A *physical* constraint
does not.

---

## If you come back to this

- Don't re-run the blind search hoping the material changed. It didn't.
- Do widen the prior's coverage: every track `tracklist-2017.txt` places is a track
  `solo_anchors` can be pointed at.
- The originals must be reachable (`NETRADIO_SOURCES_DIR`) and `find_original` must recognise
  the extension — it silently missed **every WavPack (`.wv`)** original until 2026-07, reporting
  "no original" where it meant "unsupported extension". Most of the library is `.wv`.
