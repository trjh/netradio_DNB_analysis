# Lesson — AcoustID cannot identify anything from the 1998 stream. Proven, not assumed.

**Do not rebuild this.** Fingerprinting the mix to identify the Mystery Tracks is not a hard
problem or a tuning problem. It is impossible, and the reason is measurable.

---

## The finding

Take **Dead Calm — Urban Style**, a track we have both ways: as a clean file, and playing in
the 1998 stream. Align the stream audio to the record's own start (we know where, from the
solo anchors), and undo the DJ's pitch (we know the rate: 0.9908). Now fingerprint both and
compare the raw Chromaprint subfingerprints bit for bit:

| | |
|---|---|
| Clean original, first 120 s | 948 subfingerprints |
| **The same record, in the stream**, aligned and tempo-corrected | 939 subfingerprints |
| **Bitwise similarity** | **0.511** |

**0.50 is random noise.** AcoustID needs roughly >0.85 to call a match.

So the stream's rendition of a record shares **essentially no fingerprint information** with the
record itself — even when perfectly aligned and pitch-corrected. There is no signal to find.
Sweeping the duration, the start offset or the rate cannot recover information that is not
there.

The cause is the 1998 broadcast chain: a heavily-compressed, band-limited ISDN/RealAudio
stream, plus the DJ's EQ and filtering. Chromaprint keys on exactly the spectral detail that
chain throws away.

## The control that makes this trustworthy

A negative result from an unvalidated pipeline is not a finding, it is a guess — and this one
was nearly reported as a finding while the pipeline was broken. So:

- **Positive control:** the *clean* file of that same record matches AcoustID at **0.99**.
- **Coverage control:** **65 of 89** originals in `sources/` are in AcoustID (73%). The
  database covers this genre and era perfectly well.
- **Negative:** the same records, taken from the stream, match nothing.

The catalogue is not the problem. The stream audio is.

## The bug that hid it, and nearly produced a false finding

The first version fingerprinted an excerpt from the **middle** of a track's span (reasoning that
a DJ blends at the edges, so the middle is where the record plays alone). That reasoning is
sound and it is still how you would pick a clean passage — but it cannot work with AcoustID,
because **AcoustID's index is keyed on a fingerprint taken from the START of a recording, paired
with the recording's FULL duration.** Measured, on a known track:

| fingerprint from | duration sent | result |
|---|---|---|
| head | full | **match** |
| head (first 120 s only) | full | **match** |
| middle | excerpt's own | no match |
| middle | full | no match |
| head | excerpt's own | no match |

Both conditions are mandatory, and the duration tolerance is narrow (roughly +0 to +7 s).

While that bug was live, the tool reported "no match" for **everything** — including clean
originals whose names we already knew. It would have been very easy, and completely wrong, to
write that up as *"AcoustID doesn't cover obscure 1998 D&B"*. It covers 73% of it.

**The lesson is not about AcoustID.** It is that a null result is only evidence if the
instrument has been shown to produce a positive. Find a known-good case and confirm the tool
finds it, before believing anything the tool fails to find.

## What survived

The fingerprinting is genuinely useful pointed at the **originals** rather than the stream —
see `scripts/acoustid_check.py`. Run over `sources/` it confirmed 65 of them and caught two
mislabelled files:

- `013-DJ Addiction - Senses.mp3` **is** `Blame — J-Walkin'` — the same record as `021`.
- `022-Castillo - Junkle I.flac` **is** `Callisto — Junkle I` (artist misspelled).

## So how DO the Mystery Tracks get identified?

By ear, and by the evidence around them — which is how every track here was identified so far:

- the 1998/2017 notes' own clues (lyrics heard, a YouTube timestamp, a Discogs link);
- the `solo_anchors` passages, which give you the cleanest available audio of the record to
  listen to, even though a machine cannot fingerprint it;
- and asking someone who knows the era.

The engine's job here is to hand you the best 30 seconds to listen to. It cannot name the record.
