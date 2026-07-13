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

import numpy as np

N_PITCH = 12
DEFAULT_TRIES = 3          # how many OTI-ranked rotations to actually DTW


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


# Frames -> seconds. The chroma hop is 2048 at 16kHz, so a frame is 0.128s.
HOP = 2048
SR = 16000


def match(query, candidate, tries=DEFAULT_TRIES):
    """Best (cost, semitones, at_seconds) of finding `query` inside `candidate`.

    `semitones` is how far the QUERY had to be rotated to meet the candidate. A non-zero value
    is a fact worth surfacing, not an implementation detail: the two recordings are in different
    keys, so one has been pitched -- which tells you something real about the copy you hold.

    `at_seconds` is WHERE in the candidate the match begins. Subsequence DTW has always known
    this -- the warp path's first frame is the offset -- and not returning it was a waste: a
    candidate can be a 90-minute DJ set, and "the record is in there somewhere" is a much poorer
    answer than "it starts at 47:12". You can go and listen to the right minute.
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
            # wp runs backwards; its LAST row is the start of the matched region.
            start_frame = int(wp[-1][1])
            best_cost, best_shift = cost, k
            best_at = start_frame * HOP / float(SR)
    return best_cost, best_shift, best_at


def describe_at(seconds):
    """`2831.4` -> `47:11`. A timestamp you can scrub to."""
    if seconds is None:
        return ""
    s = int(round(seconds))
    return "%d:%02d" % (s // 60, s % 60)


def describe_shift(semitones):
    """`+11` is really `-1`: say it the way a musician would, or nobody will read it."""
    if not semitones:
        return "same key"
    signed = semitones - N_PITCH if semitones > 6 else semitones
    return "%+d semitone%s (the copy is pitched)" % (signed, "" if abs(signed) == 1 else "s")
