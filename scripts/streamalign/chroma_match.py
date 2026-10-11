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

import math
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


# --- asking with sliding windows ------------------------------------------------------------
#
# A query is a stretch of broadcast, and a broadcast plays a record's opening mixed under the
# record before it. A query cut from the first two minutes therefore asks with the least
# reliable part of the record: on the hand-labelled captures the clear part of an original
# starts 20 s in at the median and up to 105 s in, and in a test on eight labelled originals a
# query from the first 120 s found 1 of 8 while 120 s windows sliding along the clip found
# 8 of 8. So a long query is asked as windows: WINDOW_S long, a new one every STEP_S, the last
# one placed to end where the query ends, and the lowest cost over all windows wins. The step
# was decided 2026-09-06: 15 s placed a record best (median error 0.66 s against the labels);
# 30 s found the same records; 5 s found nothing more and placed loop-built records worse.

WINDOW_S = 120.0
STEP_S = 15.0
FPS = _SR / float(_HOP)                 # chroma frames per second (7.8125)


def window_starts(n_frames, window_s=WINDOW_S, step_s=STEP_S):
    """The first frame of every window over a query `n_frames` long.

    Windows start at 0 and every `step_s` after it (each start rounded to the nearest frame,
    so the spacing does not drift), and the last window is placed to end at the query's end.
    A query no longer than one window is one window: the whole query.
    """
    width = int(round(window_s * FPS))
    if n_frames <= width:
        return [0]
    last = n_frames - width
    starts, k = [], 0
    while True:
        s = int(round(k * step_s * FPS))
        if s >= last:
            break
        starts.append(s)
        k += 1
    starts.append(last)
    return starts


def windows(query, window_s=WINDOW_S, step_s=STEP_S):
    """`(offset_seconds, chroma)` for every window of `query` (views, not copies)."""
    query = np.asarray(query)
    width = int(round(window_s * FPS))
    return [(s / FPS, query[:, s:s + width])
            for s in window_starts(query.shape[1], window_s, step_s)]


def match_sliding(query, candidate, window_s=WINDOW_S, step_s=STEP_S):
    """Best (cost, semitones, at_seconds, offset_seconds) over the windows of `query`.

    `at_seconds` is where in the CANDIDATE the winning window fits; `offset_seconds` is where
    in the QUERY that window starts. The windows are scored one after another, so memory
    stays at one window's DTW whatever the query's length. A query that is not a chroma
    matrix (a stand-in) is passed to `match` whole, at offset 0.
    """
    if getattr(query, "ndim", None) != 2:
        cost, shift, at = match(query, candidate)
        return cost, shift, at, 0.0
    best = (None, None, None, 0.0)
    for off, w in windows(query, window_s, step_s):
        cost, shift, at = match(w, candidate)
        if cost is not None and (best[0] is None or cost < best[0]):
            best = (cost, shift, at, off)
    return best


def window_at(query, offset_s, window_s=WINDOW_S):
    """The window of `query` that starts `offset_s` seconds in (the one `match_sliding`
    reported), or the query itself when it is not a chroma matrix."""
    if getattr(query, "ndim", None) != 2:
        return query
    s = int(round(offset_s * FPS))
    return query[:, s:s + int(round(window_s * FPS))]


# --- the deciding pass: a band-limited path and the peak-to-sidelobe ratio --------------------
#
# The free subsequence DTW above may hold a candidate frame against many query frames, so on a
# long candidate it often finds a cheap fit that means nothing: the query parked on a few
# smooth frames. Its cost alone cannot tell such a fit from a real one. The deciding pass
# changes two things:
#
#   * the warp path may only take the steps (1,1), (6,7) and (7,6), so it runs between 6/7 and
#     7/6 of real time -- the range of a DJ's pitch control -- and can neither hold still nor
#     crawl;
#   * the winner is judged by its peak-to-sidelobe ratio (PSR): the best end cost anywhere in
#     the candidate divided by the best end cost at least one query length away from it, over
#     all the rotations tried. A real fit is a sharp minimum; a bland query fits about as well
#     everywhere, and its ratio sits near 1 however low its cost.
#
# Each step is weighted by the query frames it consumes (1, 6 or 7), so an accumulated end cost
# divided by the query's frame count is a per-frame cost, comparable across end points without
# tracing a path back from each one.

BAND_STEPS = np.array([[1, 1], [6, 7], [7, 6]])
BAND_WEIGHTS = np.array([1.0, 6.0, 7.0])


def match_band(query, candidate, tries=N_PITCH):
    """Best (cost, semitones, at_seconds, psr) of `query` inside `candidate`, band-limited.

    `psr` is None when the candidate leaves no end point a full query length from the winner
    (a candidate shorter than about two query lengths): there is no sidelobe to compare with,
    and the caller must decide without one. Everything is None when the query is not a chroma
    matrix or the candidate is too short to hold it at the fastest allowed rate.
    """
    import librosa
    if getattr(query, "ndim", None) != 2:
        return None, None, None, None
    query = np.asarray(query)
    candidate = np.asarray(candidate)
    n = query.shape[1]
    if n < 2 or candidate.shape[1] < int(math.ceil(n * 6 / 7.0)) + 1:
        return None, None, None, None
    rotations = (range(N_PITCH) if tries >= N_PITCH
                 else transposition_order(query, candidate)[:max(1, tries)])
    best = None                       # (cost, shift, start frame, end frame)
    ends = []
    for k in rotations:
        D, wp = librosa.sequence.dtw(X=np.roll(query, k, axis=0), Y=candidate, subseq=True,
                                     metric="cosine", step_sizes_sigma=BAND_STEPS,
                                     weights_mul=BAND_WEIGHTS)
        per_frame = np.asarray(D[-1, :], dtype="float64") / float(n)
        ends.append(per_frame)
        cost = float(per_frame[wp[0][1]])
        if np.isfinite(cost) and (best is None or cost < best[0]):
            best = (cost, k, int(wp[-1][1]), int(wp[0][1]))
    if best is None:
        return None, None, None, None
    j = best[3]
    idx = np.arange(candidate.shape[1])
    far = np.abs(idx - j) >= n
    side = None
    for e in ends:
        v = e[far]
        v = v[np.isfinite(v)]
        if v.size:
            m = float(v.min())
            side = m if side is None else min(side, m)
    psr = side / best[0] if (side is not None and best[0] > 0) else None
    return best[0], best[1], best[2] / FPS, psr
