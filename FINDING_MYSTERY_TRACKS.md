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
| Mystery Track 3 | <https://www.youtube.com/watch?v=jKEt_2jLzYo> — **SOLVED**: Aquarius — *Wave Forms* |
| Mystery Tracks 4 & 5 | <https://soundcloud.com/trjh/sets/track-id-requested> — **MT5 SOLVED**: Jacob's Optical Stairway — *Solar Feelings* (J Majik remix). MT4 still open (the same replier had not heard it either). |
| Mystery Track 4 (2026-07-16) | Reddit round, still open: [r/AtmosphericDnB](https://www.reddit.com/r/AtmosphericDnB/s/A6HV01HyuU) · [r/DnB](https://www.reddit.com/r/DnB/s/Yx5wclnR6F) · [r/WhatsThisSong](https://www.reddit.com/r/WhatsThisSong/s/NvR2FATglT) · [r/Identificationofmusic](https://www.reddit.com/r/Identificationofmusic/s/7VhxjwubSH) |

**Asking works.** Two of the three published mysteries have been named by strangers, from
[one Reddit thread](https://www.reddit.com/r/AtmosphericDnB/comments/16n4u0m/). That is a better
hit rate than every machine method in this document combined — which is why publishing is §1.

> **Lesson — post it clean in each place, don't repost.** The r/DnB entry above was a *repost of
> the r/AtmosphericDnB thread* rather than a fresh, self-contained request. A crosspost reads as
> low-effort, buries the audio, and loses the context (the timeline neighbours, the hook) that
> makes someone bother. Write each post standalone: direct audio link + what's around it +
> whatever vocal/hook you can name (see "What to say when you post").

> **Newly solved ⇒ new work.** A named track still needs its **original acquired** (see
> `PROCESS.md` step 9): without the record we cannot seat sync anchors, recover the mix/original
> rate, or verify the placement. **MT5 (track 74) now needs its original** — it is a candidate for
> the harvester, and the fastest possible win for it.

### Ready to publish

Prepared, loudness-normalised, in `~/Downloads/Netradio/mystery-uploads/`:

| Track | Length | Note |
|---|---|---|
| Mystery Track 6 | 310 s | "drumline change" — **video built**: `Unknown Track 6.mp4` |
| Mystery Track 7 | 23 s | **short** — thin for an ID, but a hook is a hook |
| Mystery Track 10 | 213 s | freshly cut from `d465-484` |

*(Mystery Track 2 was prepared in error and withdrawn — it is already solved. Its clip still sits
in `sources/`, which is exactly the trap §1b describes: **the filename is not the truth**.)*

**The video is built locally** — no website:

```bash
bash scripts/mkvideo.sh "$NETRADIO_SOURCES_DIR/Mystery Track 6.wav" 6
```

It reproduces the format of the Mystery Track 3 upload from ffmpeg primitives: a generated
starfield, blue log-scale frequency bars, the netradio logo, and the title block. The bars are
the right visual for a track-ID post — the shape of the bassline and the drum pattern are
themselves a clue someone may recognise.

**Link to the exact position in the mix.** Every track number in
[`TRACKLIST.md`](./TRACKLIST.md) is now an anchor, so a post can point straight at it:

    https://github.com/trjh/netradio_DNB_analysis/blob/main/TRACKLIST.md#t74

That gives the reader the tracks either side of it — the DJ's neighbours, which are a strong hint
at label and scene.

**Not preparable yet:** Mystery Tracks **8** and **9** (their capture audio isn't on this box),
and **11** (no master span in `track-metadata.json` — it needs labelling before it can be cut).

### Where to ask

Ordered by how likely the people there are to have *owned the record in 1998*. **Best remaining
shots first — the top three are still untried** (MT4's 2026-07-16 round only hit Reddit):

1. **Dogs on Acid** ⭐ *untried* — the D&B forum, and the best bet for this era.
   - [Track ID Megathread](https://www.dogsonacid.com/threads/track-id-megathread.827362/)
   - [90's & 00's Track ID](https://www.dogsonacid.com/threads/90s-00s-track-id-vol-6.830381/)
2. **Discogs Groups** ⭐ *untried* — [track-ID threads](https://www.discogs.com/group/thread/736684)
   sit next to the database that will name the pressing once someone recognises it.
3. **tuneID.com** ⭐ *untried* — a [D&B archive](https://www.tuneid.com/archive/index.php/f-82.html)
   built for exactly this question.
4. **r/AtmosphericDnB** — used, and it *worked*: Mystery Track 3 was named from
   [this thread](https://www.reddit.com/r/AtmosphericDnB/comments/16n4u0m/); MT4 posted again
   2026-07-16. The genre-right Reddit sub — keep going back.
5. **r/DnB** — the big general D&B sub (used 2026-07-16). Far more reach than r/AtmosphericDnB but
   noisier; post it **clean**, not as a crosspost (see the lesson under "Already published").
6. **Music-ID generalists** — **r/WhatsThisSong** and **r/Identificationofmusic** (both used
   2026-07-16), plus **r/NameThatSong** and **WatZatSong**. Lower hit rate for this genre, but
   free and high-traffic; best when there's a vocal or hook to hang the ID on.

### What to say when you post

Give them the things that narrow it, because they are the things you actually know:

- **When**: a 1998 netradio.com D&B ISDN broadcast — so the record is **1998 or earlier**.
- **What's around it**: the tracks *before and after* on the master timeline (from
  `TRACKLIST.md`). A DJ's neighbours are a strong hint at label and scene.
- **Any lyric or vocal hook** you can hear (the 1998/2017 notes already record several).
- **A direct link to the audio** — not an attachment. That's what the uploads above are for.
- That it's from a **continuous DJ mix**, so the intro may be buried under the previous record.

---

## 1b. When you extract a NEW mystery clip — what to run

Say you've just cut Mystery Track 8 out of its capture. Two things make it searchable, and
**neither is optional**:

**1. Save the clip with the exact name `Mystery Track <N>.wav`** into the originals dir
(`$NETRADIO_SOURCES_DIR`). The number in the filename is how the tools find it.

**2. Make sure `track-metadata.json` still calls it a mystery** — its `title` must contain
`Mystery Track <N>`. **This is the authority, not the filename.** A track whose title has been
changed to a real artist/title is *solved*, and the tools will (correctly) stop searching for it.

Then:

```bash
set -a && . ./.env_vars && set +a

# what does the engine now consider unsolved-and-searchable?
PYTHONPATH=scripts .env/bin/python -c \
  "from streamalign import mystery; print([e['number'] for e in mystery.searchable()])"

# 1. search the originals you already have
PYTHONPATH=scripts .venv/bin/python scripts/identify_by_chroma.py --all-mystery

# 2. search the listen queue's downloaded, UNLISTENED tracks (slow — runs for hours)
PYTHONPATH=scripts .venv/bin/python scripts/match_queue.py --out /tmp/queue-match.txt

# 3. a cheap re-check against the commercial catalogues (ACRCloud + AudD). Proven defeated by
#    the codec on the clips tried so far (see §4), so expect nothing — but catalogues grow and a
#    cleaner future clip might land, and it costs a few free requests. Needs the API keys in .env_vars.
python3 scripts/identify_by_api.py --query "Mystery Track 8.wav" --windows 8

# 4. build the video to post
bash scripts/mkvideo.sh "$NETRADIO_SOURCES_DIR/Mystery Track 8.wav" 8
```

**Also worth a hands-on spot-check:** play the clean clip into **Shazam**, **SoundHound**, and
**Google** ("Hum to Search" / Sound Search / Circle to Search) — three engines *independent* of
ACRCloud/AudD, so a different algorithm might survive the 1998 damage where the APIs above didn't
(low odds; see §4). Don't bother with web tools that resell ACRCloud/AudD (e.g. AHA Music) — they
fail identically.

No list to edit, nowhere to register it: **both searches derive their queries from
`track-metadata.json` + the clips on disk**, so a new clip is picked up automatically and a
solved one is dropped automatically.

> **Why this is spelled out.** The tools used to pick their queries by globbing
> `sources/Mystery Track *` — and that directory still holds clips of tracks that have *since
> been identified*. A long-running match job spent ~40% of its work re-answering solved
> questions (Mystery Tracks 2 and 3), and a spurious hit against a solved track would have read
> as a real lead. **The filename is not the truth; `track-metadata.json` is.** That is now
> enforced in one place (`streamalign/mystery.py`) so it cannot drift back.
>
> A related trap, also fixed: signatures were skipped for anything under 45 s — which silently
> excluded **Mystery Track 7** (23 s) from its own search. A short *query* is fine; the minimum
> only ever applied to *candidates*.

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
  **channel/playlist subscriptions**: they are a candidate *stream*.
- **Discogs**, by label and year: the identified tracks reveal which labels this DJ was playing.

---

## 3. Signatures, not a library: why this scales

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

**Be a good citizen.** Rate-limit, back off hard on any refusal, and stop when told to. We do
**not** crawl -- every URL is one a human or a catalogue lookup put in the queue deliberately, and
the signature cache means each track is fetched **once, ever** —
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

### ✗ Commercial fingerprint APIs (ACRCloud + AudD) — **tested 2026-07-15, defeated by the same damage**

The obvious next thought: the big "what song is this" services (ACRCloud ~150M tracks, AudD ~160M)
search catalogues the local chroma pool can't, and they claim noise-robustness. So `scripts/identify_by_api.py`
carves clean interior windows from each mystery clip and submits them to both. The result is
conclusive and negative:

- **Clean controls identify perfectly** — a clean FLAC of Skyjuice – "The Rope-a-Dope" scores
  **100** on ACRCloud and matches on AudD. The pipeline works.
- **Every real mystery (MT4, MT6, MT7) returns nothing** from either engine, across every window.
- **The decisive control:** Mystery Track 5 is *solved* — Jacob's Optical Stairway – "Solar
  Feelings", a catalogued R&S release — but its **stream capture** drew a blank from AudD and one
  garbage hit from ACRCloud (Rami Eid – "In The Shade", a 2020 Latin-pop track, score 34, the
  noise floor). Clean copies match; the stream captures do not.

Same wall as AcoustID: ACRCloud and AudD are *also* spectral-peak fingerprinters, and the
1998 ISDN/EQ damage destroys what they key on. Catalogue size was never the problem.

**Worth a manual try anyway — but only with *independent* engines.** A different fingerprint
algorithm *might* survive the damage where these two didn't (low odds — the degradation is
fundamental — but free to try by playing a clean clip in):

- **Shazam** (Apple's own peak-pairing engine), **SoundHound** (its own engine; also does
  humming), and **Google** (Sound Search / "Hum to Search" / Circle to Search — its own ML
  model, huge catalogue). Three genuinely different algorithms and catalogues.
- **Skip the web "Shazam alternatives" that just resell ACRCloud/AudD** — e.g. **AHA Music is
  built on ACRCloud** — they will fail *identically* to the test above, so they're not a new shot.
- None have a batch/file API worth scripting (Shazam has no developer API at all), so this is a
  hands-on, one-clip-at-a-time spot-check, not part of the automated sweep.

### ✗ TrackSniff — tested 2026-07-16, no match on MT4

<https://tracksniff.com/results/EydDqHszfRI> — a DJ-mix-oriented "what's the track ID" web tool
(upload, or paste a YouTube / SoundCloud / Mixcloud / TikTok URL; five confidence tiers from
Unknown → Verified). It returned **no identification** for Mystery Track 4.

**Same wall, dressed for DJs.** TrackSniff's actual novelty is *mix segmentation* — detecting where
each record starts and ends inside a continuous set — not a recognition primitive that survives
codec damage. Underneath it is still **audio-fingerprint-against-a-commercial-catalogue**, the exact
class the ACRCloud + AudD entry above proves the 1998 ISDN/EQ degradation defeats. Its backend isn't
publicly disclosed (the reviews only say "fingerprints matched against millions of songs" + "AI");
if it resells ACRCloud/AudD it fails *identically*, and even if the fingerprinter is bespoke,
catalogue size was never the bottleneck — the solved-MT5 control shows clean copies match while the
stream capture doesn't. Its one plausible edge, an electronic-music-weighted catalogue, doesn't
change the damage wall.

Net: a confirmed dead end for MT4, and **not** a new scriptable method. Worth at most a one-off
manual paste for MT6/MT7 if you're already on the site — expect the same blank.

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

### ⚠️ Chroma matching MUST search transpositions — or it reports false negatives

**Chroma is invariant to timbre, not to pitch.** Shift a recording by a semitone and all twelve
chroma bins **rotate**, so a naive matcher compares C major against C♯ major, sees nothing in
common, and returns a confident **no match**.

This is not theoretical. **Mystery Track 5 was missed exactly this way.** Tim identified it by
ear as Jacob's Optical Stairway — *Solar Feelings* (J Majik mix); the matcher scored it 0.069
(non-match) and I nearly wrote the ID off. The record was there all along, one semitone away:

| transposition | vs the record | vs a control |
|---|---|---|
| **+0** (what the matcher did) | 0.0689 — *"no match"* | 0.0848 |
| **−1 semitone** | **0.0498 — match** | 0.1137 |

Uploads are frequently pitched a semitone or two -- deliberately, or because a turntable ran
fast -- so **any candidate taken from a stream is likely transposed.** A search that doesn't try all twelve
rotations will silently miss real matches — and a false negative is the worst failure a search
can have, because *it looks exactly like a clean negative.*

Fixed in `streamalign/chroma_match.py`, which all matchers now use. It stays fast via the
**Optimal Transposition Index** — the mean chroma vector encodes the key, so ranking the twelve
rotations costs a 12×12 dot product and only the best few pay for a DTW.

**The lesson, which is bigger than this bug:** the human ear beat the machine, and the machine
was *confidently* wrong. When a person who was there says yes and the tool says no, **suspect
the tool.**

### ✓ Chroma + subsequence-DTW against a candidate pool (transposition-aware)

Works (see §2). Bounded only by what's in the pool.

### ✓ Asking humans

Mystery Track 3 was identified this way. It remains the highest-yield method, which is why §1 is
at the top of this document.
