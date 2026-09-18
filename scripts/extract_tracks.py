#!/usr/bin/env python3
"""Cut every well-defined track OUT of the mix, reassembling across captures where it must.

    . .venv/bin/activate && python scripts/extract_tracks.py
    . .venv/bin/activate && python scripts/extract_tracks.py --dry-run

The output is the `stream_tracks` cache of the cache policy (cache_budget.py; the player plan
PLAN_data_tiering.md §5.11): flac, under $NETRADIO_CACHE_ROOT/stream_tracks (or
NETRADIO_STREAM_TRACKS_CACHE_DIR, or --out), 2 GB, oldest-added first, 14 days. A cut that would
not fit under the cap or past the disk floor is refused with the reason, like an imprecise one.

Why
---
Everything downstream needs a clean cut of a track AS IT PLAYED, and until now everything has
been improvising one -- usually "90 seconds from master_begin", which is the single worst window
available, because that is exactly where the DJ is still blending the previous record in.

The cost of that shortcut is measurable. Dead Calm's "Urban Style", same track, same original,
same matcher:

    hand-picked solo window        0.0337   rank 1 of 78
    naive master_begin + 90s       0.0846   rank 9

The difference is entirely the extract. So the calibration matrix was, in part, measuring my own
sloppy windowing and blaming the matcher for it.

What "well-defined" means, and what it refuses
----------------------------------------------
A track is extractable only if we can say WHERE it is, precisely:

  * it has a master span (`master_begin_seconds` / `master_end_seconds`);
  * the captures covering that span have PRECISE timing -- `d-...` captures do not (their master
    positions are approximate), so a cut taken from one lands in the wrong place;
  * we hold the audio for those captures;
  * and the coverage has no HOLE in it.

Anything else is refused, loudly, with the reason. A silently-wrong extract is worse than a
missing one: it propagates into the calibration, the thresholds, and the search, and it looks
exactly like data.

Reassembly
----------
Captures overlap, and a track can straddle a boundary. Where one capture covers the whole span we
cut it directly. Where it does not, we take each piece from the capture that covers it -- greedily
preferring the capture that can supply the LONGEST continuous run, so we make the fewest joins --
and butt them together on the master clock. Joins are logged, because a join is a place where a
future bug will hide.
"""

import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cache_budget                              # noqa: E402  (the cache policy)
from streamalign import audio as _audio          # noqa: E402
from streamalign import groundtruth as _gt       # noqa: E402
from streamalign import tracklist2017 as _tl     # noqa: E402

MIN_S = 30.0
OUT_RATE, OUT_CHANNELS = 44100, 2
PCM_BYTES_PER_S = OUT_RATE * OUT_CHANNELS * 2    # the planned size: 16-bit PCM, the flac's ceiling

cache_budget.register("stream_tracks", cap_default="2", max_age_default=14, refill="re-extract",
                      is_entry=lambda path: path.lower().endswith((".flac", ".wav")))


def imprecise(stem):
    """`d-...` captures do not have precise timing yet. A cut from one lands in the wrong place,
    and then everything downstream blames the matcher for the clock."""
    return _audio.stem_of(stem).lower().startswith("d-")


def positions():
    starts = {s: v for s, v in _gt.resolve_starts().items() if not imprecise(s)}
    for stem, note in _tl.parse().items():
        if stem not in starts and not imprecise(stem) and note.get("master_start_s") is not None:
            starts[stem] = note["master_start_s"]
    return {s: v for s, v in starts.items() if _audio.find_audio_file(s)}


def windows(stem, start, audio_dir=None):
    dur = _audio.duration_seconds(stem, audio_dir=audio_dir)
    return start, start + dur


def plan(mb, me, places):
    """[(stem, master_from, master_to)] covering [mb, me], or (None, reason).

    Greedy by longest continuous run: at each point take the capture that can carry us furthest
    without a join, because every join is a seam and every seam is somewhere a bug can live.
    """
    pieces, at = [], mb
    guard = 0
    while at < me - 0.05:
        guard += 1
        if guard > 40:
            return None, "could not cover the span in a sane number of pieces"
        best, best_end = None, at
        for stem, (s0, s1) in places.items():
            if s0 <= at < s1:
                reach = min(s1, me)
                if reach > best_end:
                    best, best_end = stem, reach
        if best is None:
            return None, "HOLE in coverage at master %.1fs -- no precise capture has it" % at
        pieces.append((best, at, best_end))
        at = best_end
    return pieces, None


def cut(stem, m_from, m_to, cstart, out_path):
    """One piece of a capture to `out_path`; the container follows its extension (flac for the
    track, wav for a part that is concatenated afterwards)."""
    src = _audio.find_audio_file(stem)
    lo = m_from - cstart
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "%.4f" % lo,
                    "-t", "%.4f" % (m_to - m_from), "-i", src,
                    "-ac", str(OUT_CHANNELS), "-ar", str(OUT_RATE), out_path], check=True)


def safe(name):
    return "".join(c if (c.isalnum() or c in " -_&.,'()") else "_" for c in name).strip()[:90]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=None,
                    help="output directory (default: the stream_tracks cache's directory)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", type=int, action="append")
    args = ap.parse_args()

    meta = json.load(open(os.path.join(_gt.REPO_ROOT, "track-metadata.json")))
    tracks = meta.get("tracks", meta)
    starts = positions()
    places = {s: windows(s, v) for s, v in starts.items()}
    print("# %d capture(s) with PRECISE timing and audio on disk\n" % len(places))

    if args.out:
        os.environ["NETRADIO_STREAM_TRACKS_CACHE_DIR"] = os.path.expanduser(args.out)
    args.out = cache_budget.dir_of("stream_tracks")
    if not args.out:
        sys.exit("extract_tracks: nowhere to write. Set NETRADIO_CACHE_ROOT (or "
                 "NETRADIO_STREAM_TRACKS_CACHE_DIR) in .env, or pass --out.")
    if not args.dry_run:
        if not cache_budget.ensure_dir("stream_tracks"):
            sys.exit("extract_tracks: NETRADIO_CACHE_ROOT names a directory that does not exist; "
                     "create it (or pass --out).")
        cache_budget.run(reason="extract-start", names=("stream_tracks",))   # the age, the cap

    made = skipped = joined = 0
    for num, e in sorted(tracks.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 1e9):
        if not str(num).isdigit():
            continue
        if args.only and int(num) not in args.only:
            continue
        mb, me = e.get("master_begin_seconds"), e.get("master_end_seconds")
        title = "%s - %s" % (e.get("artist") or "?", e.get("title") or "?")

        if mb is None or me is None:
            print("  %3s SKIP  %-42s no master span" % (num, title[:42])); skipped += 1; continue
        if me - mb < MIN_S:
            print("  %3s SKIP  %-42s only %.0fs long" % (num, title[:42], me - mb))
            skipped += 1; continue

        pieces, why = plan(mb, me, places)
        if pieces is None:
            print("  %3s SKIP  %-42s %s" % (num, title[:42], why)); skipped += 1; continue

        name = "%03d - %s.flac" % (int(num), safe(title))
        out = os.path.join(args.out, name)
        tag = "" if len(pieces) == 1 else "  [%d pieces: %s]" % (
            len(pieces), " + ".join(p[0] for p in pieces))
        if len(pieces) > 1:
            joined += 1
        print("  %3s  %-42s %6.0fs from %s%s"
              % (num, title[:42], me - mb, pieces[0][0], tag))
        if args.dry_run:
            continue

        # Make room, then add (the policy's rule): the planned size is the PCM ceiling of the
        # cut, and a refusal names why (past the disk floor, nothing evictable) like any SKIP.
        ok, why = cache_budget.reserve("stream_tracks", int((me - mb) * PCM_BYTES_PER_S), out)
        if not ok:
            print("  %3s SKIP  %-42s cache refused the write (%s)" % (num, title[:42], why))
            skipped += 1
            continue
        if len(pieces) == 1:
            stem, a, b = pieces[0]
            cut(stem, a, b, starts[stem], out)
        else:
            # The concat parts are scratch, not entries. They are PCM .wav (ffmpeg reads the
            # format from the extension, so they cannot carry a `.part` suffix) and they sit in
            # the cache directory, so they go in a DOT-prefixed subdirectory: the policy's walk
            # skips those, and a concurrent run's cap pass can neither count nor evict them.
            pdir = os.path.join(os.path.dirname(out) or ".", ".parts")
            os.makedirs(pdir, exist_ok=True)
            stem_name = os.path.basename(out)
            parts, lst = [], os.path.join(pdir, stem_name + ".txt")
            # The scratch is invisible to the policy, so nothing else will ever reclaim it: every
            # exit path from here removes it, a failed ffmpeg and a Ctrl-C included. A part is
            # PCM, about 176 KB a second, and no run resumes one.
            try:
                for i, (stem, a, b) in enumerate(pieces):
                    p = os.path.join(pdir, "%s.part%d.wav" % (stem_name, i))
                    cut(stem, a, b, starts[stem], p)
                    parts.append(p)
                with open(lst, "w") as fh:
                    for p in parts:
                        fh.write("file '%s'\n" % p.replace("'", "'\\''"))
                # Encoded, not stream-copied: the parts are PCM and the track is flac.
                subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0",
                                "-i", lst, "-c:a", "flac", out], check=True)
            finally:
                for p in parts + [lst]:
                    try:
                        os.unlink(p)
                    except OSError:
                        pass
        cache_budget.commit("stream_tracks", out, reason="extract")
        made += 1

    print("\n# %d extracted (%d needed reassembly across captures), %d refused"
          % (made, joined, skipped))
    if not args.dry_run:
        print("# -> %s" % args.out)
    print("# A refused track is a track we cannot yet say WHERE it is. A silently-wrong extract\n"
          "# would be worse: it propagates into the calibration and looks exactly like data.")


if __name__ == "__main__":
    main()
