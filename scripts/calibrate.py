#!/usr/bin/env python3
"""Score every KNOWN stream track against every KNOWN original. The calibration matrix.

    PYTHONPATH=scripts .venv/bin/python scripts/calibrate.py --out /tmp/calibration.md

Why this exists
---------------
Every threshold the matchers use -- MATCH at 0.050, KEEP at 0.075, "must beat the runner-up by
0.6x" -- was calibrated on **one** confirmed positive (Dead Calm's "Urban Style": 0.0337, #1 of
78). One positive does not characterise a failure mode, and trusting it has already cost us:

  * Mystery Track 5 was a CONFIDENT FALSE NEGATIVE (0.069, "no match") because chroma is not
    pitch-invariant and the copy was a semitone off. Tim's ear caught it; the tool did not.
  * Mystery Track 7 produced five CONFIDENT FALSE POSITIVES, all within 0.0007 of each other,
    because a 23s query drives every cost down.

Both were found by accident. This finds them on purpose.

What it does
------------
For every track where we hold BOTH the mix (its master span inside a capture we have) and the
original, it computes:

  * the TRUE-POSITIVE cost -- that track's mix against its own original;
  * the TRUE-NEGATIVE costs -- the same mix against every OTHER original;

and reports the two distributions. That is the only honest basis for a threshold: a gate is only
as good as the gap between the populations it separates, and if they overlap, no threshold works
and the tool must say "I don't know" instead of guessing.

It also names every track whose own original does NOT come first. Those are the false negatives
we do not yet know about -- and a false negative is the worst failure a search can have, because
it looks exactly like a clean negative.
"""

import argparse
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from streamalign import audio as _audio          # noqa: E402
from streamalign import chroma_match as _cm      # noqa: E402
from streamalign import groundtruth as _gt       # noqa: E402
from streamalign import track_mix as _tm         # noqa: E402
from streamalign import tracklist2017 as _tl     # noqa: E402

HOP = 2048
QUERY_S = 90.0
# Where inside the track's extract to sample. NOT the start: that is where the DJ is still
# blending the previous record in, so the first minute is often two records at once. Measured:
# Urban Style scored 0.0337 from a solo window and 0.0846 from a master_begin window -- same
# track, same original, same matcher. The window WAS the bug.
EDGE_SKIP = 0.25


def chroma(y):
    import librosa
    c = librosa.feature.chroma_cqt(y=np.asarray(y, dtype="float32"),
                                   sr=_audio.SR, hop_length=HOP) + 1e-6
    return librosa.util.normalize(c, norm=2, axis=0)


def imprecise(stem):
    """Captures named `d-...` do NOT have precise timing yet (Tim). Their master positions are
    approximate, so an extract taken from one lands in the wrong place -- and then scores as a
    failed match against the RIGHT original, which looks exactly like a broken matcher.

    That is not hypothetical: every Jamie Myerson track "failed" calibration until this was
    applied. The matcher was fine. The clock was not. A calibration built on imprecise timing
    measures the timing, not the matcher.
    """
    return stem.lower().startswith("d-")


def positions():
    """Every capture we can PRECISELY place: hand labels first, the 1998/2017 notes as fallback."""
    starts = {s: v for s, v in _gt.resolve_starts().items() if not imprecise(s)}
    for stem, note in _tl.parse().items():
        if stem not in starts and not imprecise(stem) and note.get("master_start_s") is not None:
            starts[stem] = note["master_start_s"]
    return starts


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="-")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    src = os.environ.get("NETRADIO_SOURCES_DIR")
    if not src or not os.path.isdir(src):
        sys.exit("NETRADIO_SOURCES_DIR is unset (see .env_vars.example)")
    out = sys.stdout if args.out == "-" else open(args.out, "w", buffering=1)

    meta = json.load(open(os.path.join(_gt.REPO_ROOT, "track-metadata.json")))
    tracks = meta.get("tracks", meta)
    starts = positions()

    # --- who can we actually test? we need BOTH the mix and the original -------------------
    cases = []
    for num, entry in tracks.items():
        if not str(num).isdigit():
            continue
        mb, me = entry.get("master_begin_seconds"), entry.get("master_end_seconds")
        if mb is None or me is None or me - mb < 45:
            continue
        orig = _tm.find_original(int(num), src)
        if not orig:
            continue
        # THE TRACK'S OWN source_files decide which capture it lives in. Captures OVERLAP, so
        # "the first capture whose window contains the span" picks the wrong one and extracts 90s
        # of entirely different music -- which then scores as a failed match against the right
        # original. That bug made all five Jamie Myerson tracks look like matcher failures when
        # the matcher was fine; track 7 was being read out of d-25-000b when it lives in d000-018.
        # This is the third time capture-selection has bitten this project. Use the trusted path.
        cap = None
        srcs = [f for f in (entry.get("source_files") or []) if not imprecise(_audio.stem_of(f))]
        stem, cstart = _tm._select_capture(srcs, mb, me, starts)
        if stem and _audio.find_audio_file(stem):
            cap = (stem, cstart)
        else:                                   # only then fall back to a scan
            for stem, ms in starts.items():
                if not _audio.find_audio_file(stem):
                    continue
                if ms <= mb and me <= ms + _audio.duration_seconds(stem):
                    cap = (stem, ms)
                    break
        # a clean extract, if extract_tracks.py has made one
        ex = None
        exdir = os.path.expanduser(os.environ.get("NETRADIO_TRACKS_DIR",
                                                  "~/media/netradio-tracks"))
        if os.path.isdir(exdir):
            for f in os.listdir(exdir):
                if f.startswith("%03d - " % int(num)):
                    ex = os.path.join(exdir, f)
                    break
        if cap or ex:
            cases.append({"num": int(num), "orig": orig,
                          "cap": cap[0] if cap else None,
                          "cstart": cap[1] if cap else 0,
                          "extract": ex, "mb": mb, "me": me,
                          "name": "%s - %s" % (entry.get("artist") or "?",
                                               entry.get("title") or "?")})
    cases.sort(key=lambda c: c["num"])
    if args.limit:
        cases = cases[:args.limit]

    print("""# CALIBRATION — how well does the matcher actually work?

> **What this is:** the measured accuracy of the chroma matcher, against every track where we
> hold **both** the mix and the original. Auto-generated by `scripts/calibrate.py` — do not edit
> by hand; re-run it instead.
> **Read first:** [FINDING_MYSTERY_TRACKS](../FINDING_MYSTERY_TRACKS.md) (what we are trying to
> do and why chroma) · [docs/SCRIPTS](./SCRIPTS.md) (the tools).

## Why this exists

Every threshold the matcher uses was originally calibrated on **one** confirmed positive. One
positive does not characterise a failure mode, and trusting it cost us twice: a **confident false
negative** (Mystery Track 5 — the record was there, a semitone away, and the matcher said no) and
a batch of **confident false positives** (a 23-second query drives every cost down until five
unrelated tracks tie for first). Both were found by accident. This finds them on purpose, and it
is the **regression test** for the matching stack: change `chroma_match.py`, re-run this, and if
the hit-rate drops the change is wrong.

## How the numbers are made

For each track where we hold both sides:

1. Take a **90-second window from the middle of the track's clean extract** (`extract_tracks.py`).
   The middle, not the start — the start is where the DJ is still blending the previous record in,
   and a window there measures the blend, not the record.
2. Reduce it to **chroma**: 12 pitch classes per frame, which throws away timbre and EQ and keeps
   the harmony. That is what survives a lossy 1998 broadcast.
3. Compare it against **every** original we hold, using subsequence-DTW (the record plays at the
   DJ's speed, not its own, so a rigid alignment drifts) and trying **all twelve transpositions**
   (chroma is blind to timbre but *not* to pitch — a semitone shift rotates all twelve bins).
4. Record the **cost** against its *own* original, the cost of the best *rival*, and the **rank**
   its own original achieved.

## Reading the table

| Column | Meaning |
|---|---|
| **own original** | the cost against the record it actually is. **Lower is better — it is a distance, not a score.** |
| **best rival** | the best cost from somebody *else's* original |
| **gap** | rival − own. **Positive is good**: the right answer beat the field. |
| **rank** | where its own original placed. **1 is what we want.** |
| **shift** | how far it had to be transposed to line up. Non-zero means one copy is pitched — a fact about the copy, not an error. |

**The headline is `rank`, not `cost`.** The two populations *overlap*, so no cost threshold can
separate them; what identifies a record is that it beats the field, not that it clears a bar.

""", file=out)
    print("# %d track(s) where we hold BOTH the mix and the original\n" % len(cases), file=out)
    if not cases:
        return

    # every original becomes a candidate, so each mix is scored against all of them
    pool = {}
    for c in cases:
        pool[c["num"]] = chroma(_audio.load_audio(c["orig"]))

    print("| track | own original | best rival | gap | rank | shift |", file=out)
    print("|---|---|---|---|---|---|", file=out)

    tp, tn, misses = [], [], []
    for c in cases:
        # Prefer the clean extract (extract_tracks.py) over a window carved out of a capture: it
        # is already reassembled across capture boundaries where the track straddles one.
        y = None
        if c.get("extract"):
            y = _audio.load_audio(c["extract"])
        else:
            capa = _audio.load_audio(c["cap"])
            lo = c["mb"] - c["cstart"]
            y = capa[int(lo * _audio.SR):int((c["me"] - c["cstart"]) * _audio.SR)]
        if len(y) < 45 * _audio.SR:
            continue
        # Sample from the MIDDLE. The edges are the blend.
        lo_i = int(len(y) * EDGE_SKIP)
        mix = y[lo_i:lo_i + int(QUERY_S * _audio.SR)]
        if len(mix) < 30 * _audio.SR:
            mix = y[:int(QUERY_S * _audio.SR)]
        q = chroma(mix)

        scored = []
        for num, cand in pool.items():
            cost, shift = _cm.match(q, cand)
            if cost is not None:
                scored.append((cost, num, shift))
        scored.sort()
        if not scored:
            continue

        own = next((s for s in scored if s[1] == c["num"]), None)
        if own is None:
            continue
        rank = [s[1] for s in scored].index(c["num"]) + 1
        rival = next((s[0] for s in scored if s[1] != c["num"]), 1.0)

        tp.append(own[0])
        tn.extend(s[0] for s in scored if s[1] != c["num"])
        hit = rank == 1
        if not hit:
            misses.append((c["num"], c["name"], own[0], rank, scored[0][1]))

        print("| %d %s | %.4f | %.4f | %+.4f | %s | %s |"
              % (c["num"], c["name"][:34], own[0], rival, rival - own[0],
                 "**1**" if hit else "%d ✗" % rank,
                 _cm.describe_shift(own[2]) if own[2] else "—"), file=out)

    def stats(v, label):
        if not v:
            return
        a = np.array(v)
        print("- **%s** (n=%d): min %.4f · median %.4f · mean %.4f · max %.4f"
              % (label, len(a), a.min(), np.median(a), a.mean(), a.max()), file=out)

    print("\n## The two populations\n", file=out)
    stats(tp, "TRUE match (a track vs its own original)")
    stats(tn, "NON-match (a track vs somebody else's original)")

    if tp and tn:
        a, b = np.array(tp), np.array(tn)
        print("\n- true-match worst: **%.4f** · non-match best: **%.4f**" % (a.max(), b.min()),
              file=out)
        if a.max() < b.min():
            print("- **The populations are SEPARATE.** A threshold between them is honest. "
                  "Suggested MATCH gate: **%.4f** (midpoint)." % ((a.max() + b.min()) / 2),
                  file=out)
        else:
            print("- **The populations OVERLAP.** No threshold separates them cleanly, so a "
                  "single cost cannot be trusted on its own -- which is exactly why the gate "
                  "also demands the winner beat its runner-up. Rank, not cost, is the reliable "
                  "signal.", file=out)
        print("- current gates: MATCH 0.0500 · KEEP 0.0750", file=out)

    print("\n## Ranked first by its own original: %d of %d\n"
          % (len(tp) - len(misses), len(tp)), file=out)
    if misses:
        print("**These are the false negatives we did not know about.** A search that misses "
              "them reports a clean negative and nobody is any the wiser:\n", file=out)
        for num, name, cost, rank, winner in misses:
            print("- **%d %s** — own original ranked **%d** (cost %.4f); track %d won instead."
                  % (num, name, rank, cost, winner), file=out)
    else:
        print("Every track's own original came first. No hidden false negatives in this set.",
              file=out)


if __name__ == "__main__":
    main()
