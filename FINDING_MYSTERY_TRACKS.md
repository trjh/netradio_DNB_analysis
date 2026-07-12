# Finding the Mystery Tracks

> **What this is:** the working guide to identifying the stream's unidentified tracks — what to
> do next (top), and everything already tried, including the dead ends (bottom), so nobody
> repeats them.
> **Fits in:** [README](./README.md) · [PROCESS](./PROCESS.md) (the labelling loop that *creates*
> a Mystery Track) · [`Archive/LESSON_acoustid_stream.md`](./Archive/LESSON_acoustid_stream.md).

A **Mystery Track** is a track placed on the master timeline whose record nobody has named. It
gets a `startNNN: ID: Mystery Track N` label and a slot in `track-metadata.json` like any other
— it just has no artist or title.

**The single most useful thing to know:** machine identification of these is *hard by nature*,
and asking humans works. Every method below is a way of getting a clean excerpt in front of
someone who was there.

---

## 1. Publish the excerpt and ask (this is what works)

### Already published

| Track | Where |
|---|---|
| Mystery Track 3 | <https://www.youtube.com/watch?v=jKEt_2jLzYo> *(since identified: Aquarius — Wave Forms)* |
| Mystery Tracks 4 & 5 | <https://soundcloud.com/trjh/sets/track-id-requested> |

### Ready to publish

Prepared, loudness-normalised, in `~/Downloads/Netradio/mystery-uploads/`:

| Track | Length | Note |
|---|---|---|
| Mystery Track 2 | 331 s | |
| Mystery Track 6 | 310 s | "drumline change" |
| Mystery Track 7 | 23 s | **short** — thin for an ID, but a hook is a hook |
| Mystery Track 10 | 213 s | freshly cut from `d465-484` |

**Not preparable yet:** Mystery Tracks **8** and **9** (their capture audio isn't on this box),
and **11** (no master span in `track-metadata.json` — it needs labelling before it can be cut).

### Where to ask

Ordered by how likely the people there are to have *owned the record in 1998*:

1. **Dogs on Acid** — the D&B forum, and the best bet for this era.
   - [Track ID Megathread](https://www.dogsonacid.com/threads/track-id-megathread.827362/)
   - [90's & 00's Track ID](https://www.dogsonacid.com/threads/90s-00s-track-id-vol-6.830381/)
2. **r/AtmosphericDnB** — already used, and it *worked*: Mystery Track 3 was identified from
   [this thread](https://www.reddit.com/r/AtmosphericDnB/comments/16n4u0m/). Keep going back.
3. **Discogs Groups** — [track-ID threads](https://www.discogs.com/group/thread/736684) sit next
   to the database that will name the pressing once someone recognises it.
4. **tuneID.com** — a [D&B archive](https://www.tuneid.com/archive/index.php/f-82.html) built
   for exactly this question.
5. **r/NameThatSong**, **WatZatSong** — generalists. Lower hit rate for this genre, but free.

### What to say when you post

Give them the things that narrow it, because they are the things you actually know:

- **When**: a 1998 netradio.com D&B ISDN broadcast — so the record is **1998 or earlier**.
- **What's around it**: the tracks *before and after* on the master timeline (from
  `TRACKLIST.md`). A DJ's neighbours are a strong hint at label and scene.
- **Any lyric or vocal hook** you can hear (the 1998/2017 notes already record several).
- **A direct link to the audio** — not an attachment. That's what the uploads above are for.
- That it's from a **continuous DJ mix**, so the intro may be buried under the previous record.

---

## 2. Search a candidate pool by chroma (the method that works offline)

If you can get audio of a *plausible* record, the engine will tell you reliably whether it is
the mystery:

```bash
set -a && . ./.env_vars && set +a
PYTHONPATH=scripts .venv/bin/python scripts/identify_by_chroma.py --all-mystery
PYTHONPATH=scripts .venv/bin/python scripts/identify_by_chroma.py --pool ~/dnb-candidates
```

**Validated:** given 90 s of the *1998 stream* where Dead Calm's *Urban Style* plays, matched
against 78 originals, the true record ranks **#1 (cost 0.0337)** with the runner-up at **0.1017**
— a 3× margin. It is gated so a bland file that matches everything is rejected rather than
reported.

**The catch is the whole game: it can only find what is in the pool.** A Mystery Track is by
definition a record nobody recognised, so it is not among the known originals — run it today and
every one scores at the non-match floor. **The remaining work is not matching, it is acquiring
candidates.**

### Where the candidates come from

- **The listen queue** — 567 unlistened items, 233 already downloaded. Being checked by
  `scripts/match_queue.py` (only the *unlistened* ones: if he'd heard it and it were the mystery,
  it would not be a mystery).
- **YouTube archive channels** of the era — e.g.
  <https://www.youtube.com/@back2theoldskoolera999>. This is why the player's listen queue wants
  **channel/playlist subscriptions**: they are a candidate *firehose*.
- **Discogs**, by label and year: the identified tracks reveal which labels this DJ was playing.

---

## 3. Gathering signatures without downloading the internet

This box has ~40 GB free and the pool we want is far larger. **So don't keep the audio — keep the
signature.**

A chroma signature is a 12×N matrix. At `hop=2048` / 16 kHz that's ~2,300 frames for a 5-minute
track: **~55 KB as float16**, versus ~8 MB for the audio. **A 100,000-track pool is ~5 GB of
signatures** — and the audio never has to touch the disk at all:

```
yt-dlp -o - <url> | ffmpeg -i - -ac 1 -ar 16000 -f wav - | chroma → .npy → discard audio
```

`scripts/match_queue.py` already caches signatures this way (`.chroma-cache/`, content-addressed,
float16). Extending it to *stream* rather than read a local file is a small change, and it is the
only way this scales.

**Be a good citizen.** Rate-limit, back off, respect `robots.txt`, and never bulk-download a
platform just because you can. The signature cache means each track is fetched **once, ever** —
that is the point of keeping it.

---

## 4. What has been tried

### ✗ AcoustID / Chromaprint fingerprinting of the stream — **impossible, proven**

Not "didn't work" — *cannot* work. The same record taken from the 1998 broadcast, aligned to its
own start and pitch-corrected, has a **bitwise fingerprint similarity of 0.511** to its own clean
original. **0.50 is random noise.** The ISDN/RealAudio compression and the DJ's EQ destroy exactly
the fine spectral detail Chromaprint keys on. No sweep of duration, rate or offset recovers
information that isn't there.

Controlled, because a null from an unvalidated instrument is not a finding:
- the **clean** file of that same record matches AcoustID at **0.99**;
- **65 of 89** originals are in AcoustID (73%) — the database covers this genre fine;
- the same records, from the stream, match nothing.

**The catalogue was never the problem. The stream audio is.** Full detail, including the bug that
nearly turned this into a false claim about AcoustID's coverage:
[`Archive/LESSON_acoustid_stream.md`](./Archive/LESSON_acoustid_stream.md).

> **The subtlety worth keeping:** Chromaprint *is* built on chroma. But it **quantises** chroma
> into compact hashes for a fast index, and it is that quantisation the codec destroys. Raw chroma
> + DTW keeps all 12 dimensions and tolerates the degradation. The feature was always right; the
> hashing is what breaks.

### ✗ Blind chroma search for a record inside a capture

`track_mix.locate_original` — right 2 times in 8, and its *most confident* answer was wrong by 25
minutes. The DJ beatmatches (so a fixed-lag correlation drifts) and the broadcast repeats material
(so the answer isn't unique). See
[`Archive/LESSON_locate_original.md`](./Archive/LESSON_locate_original.md). **With a prior it
works** — that's `solo_anchors`.

### ✗ Matching the mysteries against Tim's own library

Pointless by definition: a Mystery Track is one he did *not* recognise, so it isn't in his
collection. (Run anyway as a control — every mystery scored at the non-match floor, as expected.)

### ✗ A public database of raw chroma signatures

There isn't one. AcoustID's 89M fingerprints are open and downloadable, but they are *Chromaprint
hashes* — the thing we've proven useless here. **The signature pool has to be ours.**

### ✓ Chroma + subsequence-DTW against a candidate pool

Works (see §2). Bounded only by what's in the pool.

### ✓ Asking humans

Mystery Track 3 was identified this way. It remains the highest-yield method, which is why §1 is
at the top of this document.
