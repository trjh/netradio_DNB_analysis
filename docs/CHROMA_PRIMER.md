# Chroma primer — why harmony survives what fingerprints don't

> **What this is:** the conceptual explainer for the chroma matching that powers
> `identify_by_chroma.py` and the harvester — what a chromagram is, how a mystery clip is
> found inside a candidate, why DJ-blended edges don't break it, and what limits a
> mix-in-mix match.
>
> **Fits in:** [README](../README.md) →
> [FINDING_MYSTERY_TRACKS.md](../FINDING_MYSTERY_TRACKS.md) (the playbook) ·
> [docs/CALIBRATION.md](./CALIBRATION.md) (where the numbers come from) ·
> [PROCESS.md](../PROCESS.md) §8 (using it in the loop).

## What chroma is

Take the audio, frame by frame (~1/8 s each at our settings), and fold the whole spectrum
onto the 12 pitch classes — every C from every octave into one bin, every C♯ into the
next, and so on. You get a 12-row strip over time: not what the record *sounds* like, but
which notes its harmony is standing on, moment by moment. Each frame is normalized, so
only the *shape* of the harmony matters, not level.

That's the whole trick: EQ, the ISDN codec's mangling, resampling, even a different rip of
the same master all change timbre and fine spectral detail enormously while barely moving
*which pitch classes carry the energy*. It's why chroma survives our 1998 audio when
fingerprints — which key on exactly that fine detail — score 0.511 ≈ coin-flip against
their own record (see
[`Archive/LESSON_acoustid_stream.md`](../Archive/LESSON_acoustid_stream.md)).

## How matching finds a mystery

The mystery clip becomes a 12×N strip; each candidate becomes a 12×M strip. The matcher
slides the query strip along the candidate's timeline and scores the disagreement at every
offset; transpositions (a candidate uploaded a semitone off, or pitched hard, is just the
same strip rotated) are handled by cheaply ranking all 12 rotations and fully testing the
best few — three by default — with the expensive aligner. The answer is the best
(offset, key) — and the *gate* is what makes it trustworthy. Typical costs: a true match
centres around ~0.004–0.03, unrelated records around ~0.095 — but the populations
**overlap** (calibration measured true matches up to 0.097 and non-matches down to 0.038),
so no absolute threshold separates them. **Rank plus a decisive margin over the runner-up
is the reliable signal**, with absolute cost only a supporting hint
([CALIBRATION.md](./CALIBRATION.md)).

## Why the DJ-blended edges don't kill it

During a blend the broadcast's chroma is the *sum* of two records' harmonies — those
frames match nothing cleanly. But a DJ blend is edges, not middle: for most of the clip
the record plays alone (or dominates), and the sliding search finds the offset where that
clean core locks on; the blended head and tail just add a bit of cost.

This is exactly the clip-length lesson: MT7's 23 seconds was nearly all edge, so *every*
candidate scored similarly low and five "confident" ties appeared — hence the 60-second
floor (`MIN_QUERY_S`). Enough solo core, and the core outvotes the edges.

## Pure copy vs. inside a long DJ mix

Against a **pure copy**, the query simply aligns somewhere inside the track.

Against a **long DJ mix**, the same sliding search runs over the whole hour — and it can
work, with two real handicaps:

- only the region where the record plays alone *in both mixes* can line up (our copy is
  blended with our DJ's neighbours, theirs with theirs), so the usable overlap shrinks;
- beatmatching means the two copies run at slightly different rates (measured: within
  ~1.3% of 1.0 — see the speed-drift addendum in [PROCESS.md](../PROCESS.md)), which
  slowly slides the frames out of register across a long alignment — a 1% difference is a
  whole frame of drift every ~15 seconds.

So mix-in-mix hits tend to be shorter-window, weaker-margin leads — which is why the
harvester treats everything as a *lead* for a human ear, never a verdict.
