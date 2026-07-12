#!/usr/bin/env python3
"""Identify a record from the 1998 stream by CHROMA matching against a candidate pool.

    set -a && . ./.env_vars && set +a
    PYTHONPATH=scripts .venv/bin/python scripts/identify_by_chroma.py --query "Mystery Track 7.wav"
    PYTHONPATH=scripts .venv/bin/python scripts/identify_by_chroma.py --all-mystery
    PYTHONPATH=scripts .venv/bin/python scripts/identify_by_chroma.py --pool ~/dnb-candidates

This is the method that WORKS on this material. AcoustID does not, and cannot -- see
`Archive/LESSON_acoustid_stream.md`: the same record taken from the 1998 broadcast has a
bitwise Chromaprint similarity of 0.511 to its own clean original, and 0.50 is random noise.
The ISDN/RealAudio compression and the DJ's EQ destroy exactly the fine spectral detail a
fingerprint keys on.

**Chroma survives what fingerprints do not.** Collapsing audio to 12 pitch classes throws away
timbre, EQ and codec artefacts, and keeps the harmony -- so a record still matches itself
through a lossy 1998 broadcast. That is not a hope, it is the same machinery that already
recovers the mix/original rate to four decimal places (`track_mix.solo_anchors`).

Validated end-to-end: given 90s of the STREAM where Dead Calm's "Urban Style" plays, matched
against 78 originals, the true record ranks **#1 at cost 0.0337**, with the runner-up at
**0.1017** -- a 3x margin.

The catch, and it is the whole game
-----------------------------------
This can only identify what is IN THE POOL. The Mystery Tracks are, by definition, records
nobody recognised -- so they are not among the originals on disk, and running this against them
finds nothing (measured: every mystery track scores 0.058-0.10, i.e. the non-match floor).

**So the work is not matching. It is ACQUIRING CANDIDATES.** Point `--pool` at a directory of
plausible era-appropriate records -- 1997/98 drum & bass, the labels and artists this DJ was
playing, leads from `tracklist-2017.txt` -- and this will tell you, reliably, whether the
mystery is among them.

The gate
--------
A match is only reported when it is BOTH absolutely good and clearly better than its rivals.
A candidate that matches everything is matching nothing -- and some do: short or harmonically
bland files sit near the top for every query. `--show-all` prints the ranking regardless.

Calibrated on a single confirmed positive (Urban Style: 0.0337 vs 0.1017). That is thin. Treat
a reported match as a strong lead to CONFIRM BY EAR, never as an answer.
"""

import argparse
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from streamalign import audio as _audio     # noqa: E402

AUDIO_EXTS = ("mp3", "flac", "m4a", "opus", "wav", "wv", "aif", "aiff", "ogg")
HOP = 2048
QUERY_S = 120.0

# A real match sat at 0.034 and the best wrong answer at 0.102. Demand a match be clearly
# inside the former: good in absolute terms AND decisively ahead of the runner-up.
MAX_COST = 0.050
MAX_RATIO = 0.60        # best must beat second-best by at least this factor


def chroma(samples, sr=None):
    import librosa
    sr = sr or _audio.SR
    c = librosa.feature.chroma_cqt(y=np.asarray(samples, dtype="float32"),
                                   sr=sr, hop_length=HOP) + 1e-6
    return librosa.util.normalize(c, norm=2, axis=0)


def cost(query_chroma, cand_chroma):
    """Mean per-frame subsequence-DTW cost of finding the query inside the candidate.

    Subsequence DTW, because the query is an excerpt of the record, not the whole of it -- and
    DTW rather than a fixed lag because the DJ beatmatches, so the record plays at the mix's
    speed and drifts out of any rigid alignment within a minute.
    """
    import librosa
    if cand_chroma.shape[1] < query_chroma.shape[1]:
        return None
    d, wp = librosa.sequence.dtw(X=query_chroma, Y=cand_chroma, subseq=True, metric="cosine")
    return float(d[-1, wp[0][1]]) / len(wp)


def load_pool(pool_dir, exclude_mystery=True):
    out = []
    for name in sorted(os.listdir(pool_dir)):
        if name.rsplit(".", 1)[-1].lower() not in AUDIO_EXTS:
            continue
        if exclude_mystery and name.lower().startswith("mystery"):
            continue            # these are clips OF the stream -- they self-match at ~0.002
        try:
            samples = _audio.load_audio(os.path.join(pool_dir, name))
        except Exception:
            continue
        if len(samples) < 60 * _audio.SR:
            continue
        out.append((name, chroma(samples)))
    return out


def identify(query_path, pool, top=5):
    samples = _audio.load_audio(query_path)[:int(QUERY_S * _audio.SR)]
    q = chroma(samples)
    scored = []
    for name, c in pool:
        value = cost(q, c)
        if value is not None:
            scored.append((value, name))
    scored.sort()
    if not scored:
        return None, []
    best = scored[0]
    runner = scored[1][0] if len(scored) > 1 else 1.0
    verdict = None
    if best[0] <= MAX_COST and best[0] <= MAX_RATIO * runner:
        verdict = {"name": best[1], "cost": best[0], "runner_up": runner}
    return verdict, scored[:top]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--query", action="append", help="audio file to identify (repeatable)")
    ap.add_argument("--all-mystery", action="store_true",
                    help="identify every `Mystery Track*` clip in the sources dir")
    ap.add_argument("--pool", default=None,
                    help="directory of candidate records (default: NETRADIO_SOURCES_DIR)")
    ap.add_argument("--show-all", action="store_true", help="print the ranking even when gated")
    args = ap.parse_args(argv)

    sources = os.environ.get("NETRADIO_SOURCES_DIR")
    pool_dir = args.pool or sources
    if not pool_dir or not os.path.isdir(pool_dir):
        sys.exit("no pool: set NETRADIO_SOURCES_DIR in .env_vars, or pass --pool")

    queries = list(args.query or [])
    if args.all_mystery:
        if not sources:
            sys.exit("--all-mystery needs NETRADIO_SOURCES_DIR")
        # From track-metadata.json, not a filename glob -- sources/ still holds clips of
        # mysteries that have since been solved, and re-querying those is worse than useless.
        from streamalign import mystery as _mystery
        for entry in _mystery.searchable(sources):
            queries.append(entry["clip"])
    if not queries:
        sys.exit("nothing to identify: pass --query or --all-mystery")

    print("# chroma subsequence-DTW against %s" % pool_dir)
    pool = load_pool(pool_dir)
    print("# pool: %d candidate record(s)\n" % len(pool))
    if not pool:
        sys.exit("the pool is empty")

    for q in queries:
        path = q if os.path.exists(q) else os.path.join(sources or "", q)
        if not os.path.exists(path):
            print("  %s: not found\n" % q)
            continue
        verdict, ranking = identify(path, pool)
        print("  %s" % os.path.basename(path))
        if verdict:
            print("    ==> MATCH: %s" % verdict["name"])
            print("        cost %.4f vs next-best %.4f -- confirm by ear before believing it."
                  % (verdict["cost"], verdict["runner_up"]))
        else:
            print("    ==> no match in this pool.")
            if ranking:
                print("        (nearest was %.4f -- a true match scores ~0.034; the non-match "
                      "floor is ~0.10)" % ranking[0][0])
        if args.show_all or not verdict:
            for value, name in ranking:
                print("        %.4f  %s" % (value, name[:56]))
        print()

    print("# This can only find what is IN THE POOL. The Mystery Tracks are not among the\n"
          "# known originals -- that is what makes them mysteries. Grow the pool with\n"
          "# era-appropriate candidates (--pool) and run it again.")


if __name__ == "__main__":
    sys.exit(main())
