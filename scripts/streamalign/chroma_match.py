"""Chroma matching that is not fooled by a transposed copy.

The bug this exists to fix
--------------------------
Chroma is invariant to TIMBRE, not to PITCH. Shift a recording by a semitone and all twelve
chroma bins ROTATE -- so a matcher that compares them directly is holding C major against
C# major and correctly concluding they are unrelated. It returns a confident FALSE NEGATIVE.

That is not hypothetical. Mystery Track 5 is Jacob's Optical Stairway - "Solar Feelings"
(J Majik mix): Tim identified it by ear, and the matcher said no (cost 0.069, non-match). The
record was there all along, a semitone away:

    shift   vs the record   vs a control
      +0         0.0689        0.0848      <- what the matcher used to do. Wrong.
     +11         0.0498        0.1137      <- a match

Uploads get pitch-nudged routinely (to dodge Content ID, or a turntable ran fast), so this is
not an edge case -- it is the common case for anything sourced from YouTube. Any search that
does not try all twelve rotations will quietly miss real matches and report nothing, which is
the worst failure a search can have: it looks exactly like a clean negative.

How it stays fast
-----------------
Trying all 12 rotations through DTW is 12x the work. Instead, use the **Optimal Transposition
Index**: the average chroma vector of a piece is a fingerprint of its key, so rotating the
query's MEAN against the candidate's MEAN (a 12x12 dot product -- free) ranks the plausible
transpositions. Only the best few then pay for a DTW. Verified on the MT5 case: OTI puts the
true rotation (+11) first.
"""

import os

import numpy as np

N_PITCH = 12
# How many OTI-ranked rotations to actually DTW. 12 = exhaustive, the default since the
# 2026-07-25 experiment (see docs/CALIBRATION.md §tries): on this codec-damaged audio the
# cheap OTI ranking picked a suboptimal key in 41% of pairs (median cost inflation 0.0038,
# max 0.0397) — no leader changed that day, but the false-negative risk is real and a full
# corpus double-scan cost only ~88 min, so the 4x DTW price is affordable at fetch rate.
DEFAULT_TRIES = int(os.environ.get("NETRADIO_CHROMA_TRIES", "12") or 12)


def transposition_order(query, candidate):
    """Rotations of `query`, most plausible first, by Optimal Transposition Index.

    The mean chroma vector says what key a piece is in. Rotating the query's mean against the
    candidate's and taking the dot product scores each of the 12 transpositions for pennies --
    so DTW, which is expensive, only ever runs on the ones worth trying.
    """
    q = np.asarray(query, dtype="float64").mean(axis=1)
    c = np.asarray(candidate, dtype="float64").mean(axis=1)
    q = q / (np.linalg.norm(q) + 1e-9)
    c = c / (np.linalg.norm(c) + 1e-9)
    scores = [(float(np.dot(np.roll(q, k), c)), k) for k in range(N_PITCH)]
    scores.sort(reverse=True)
    return [k for _s, k in scores]


# Frames -> seconds: chroma hop 2048 at 16 kHz -> 0.128 s per frame.
_HOP, _SR = 2048, 16000


def match(query, candidate, tries=DEFAULT_TRIES):
    """Best (cost, semitones, at_seconds) of finding `query` inside `candidate`.

    `semitones` is how far the QUERY had to be rotated to meet the candidate -- non-zero means one
    copy is pitched, which is a fact about the copy, not an implementation detail.

    `at_seconds` is WHERE in the candidate the match begins. Subsequence DTW has always known this
    (the warp path's first frame is the offset); returning it turns "the record is in this
    90-minute set somewhere" into a timestamp you can scrub to -- and tells the harvester which
    30 seconds to keep as an excerpt.
    """
    import librosa
    query = np.asarray(query)
    candidate = np.asarray(candidate)
    if candidate.shape[1] < query.shape[1]:
        return None, None, None

    best_cost, best_shift, best_at = None, None, None
    for k in transposition_order(query, candidate)[:max(1, tries)]:
        rolled = np.roll(query, k, axis=0)
        d, wp = librosa.sequence.dtw(X=rolled, Y=candidate, subseq=True, metric="cosine")
        cost = float(d[-1, wp[0][1]]) / len(wp)
        if best_cost is None or cost < best_cost:
            best_cost, best_shift = cost, k
            best_at = int(wp[-1][1]) * _HOP / float(_SR)   # wp runs backwards; last row = start
    return best_cost, best_shift, best_at


def describe_at(seconds):
    """`2831.4` -> `47:11`."""
    if seconds is None:
        return "?"
    s = int(round(seconds))
    return "%d:%02d" % (s // 60, s % 60)


def describe_shift(semitones):
    """`+11` is really `-1`: say it the way a musician would, or nobody will read it."""
    if not semitones:
        return "same key"
    signed = semitones - N_PITCH if semitones > 6 else semitones
    return "%+d semitone%s (the copy is pitched)" % (signed, "" if abs(signed) == 1 else "s")
