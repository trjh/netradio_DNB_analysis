#!/usr/bin/env python3
"""Cut every well-defined track OUT of the mix, reassembling across captures where it must.

    . .venv/bin/activate && python scripts/extract_tracks.py --dry-run
    . .venv/bin/activate && python scripts/extract_tracks.py

The cuts land in the `stream_tracks` cache (below), as FLAC: the codec is picked from the
output's extension, exactly as before -- only the extension changed, from the .wav the tool
first wrote.

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

import cache_budget                              # noqa: E402  (the machine's one cache policy)
from streamalign import audio as _audio          # noqa: E402
from streamalign import groundtruth as _gt       # noqa: E402
from streamalign import tracklist2017 as _tl     # noqa: E402

MIN_S = 30.0

# --- the tracks cache, on the machine's one cache policy ------------------------------------
#
# Every cut lands in the `stream_tracks` cache: the tracks as they played, reassembled across
# captures -- derived from captures this repo already holds, and re-cut in minutes. The policy
# bounds it: cap NETRADIO_STREAM_TRACKS_CACHE_GB (default 2 GB -- the flac set is a little over
# that, so the oldest re-cuts rotate out and come back by re-cut), age
# NETRADIO_STREAM_TRACKS_CACHE_MAX_AGE_DAYS (default 14 -- a calibration run is a day's work,
# a re-cut is minutes), directory NETRADIO_STREAM_TRACKS_CACHE_DIR (default
# $NETRADIO_CACHE_ROOT/stream_tracks). While NETRADIO_CACHE_ROOT is unset there is no default
# directory at all: pass --out, or set the root in .env.
STREAM_TRACKS_CACHE = "stream_tracks"
STREAM_TRACKS_CACHE_GB = 2
STREAM_TRACKS_CACHE_MAX_AGE_DAYS = 14


def tracks_dir():
    """The tracks cache's directory: NETRADIO_STREAM_TRACKS_CACHE_DIR, else
    $NETRADIO_CACHE_ROOT/stream_tracks, else None (no --out default)."""
    d = os.environ.get("NETRADIO_STREAM_TRACKS_CACHE_DIR", "").strip()
    if d:
        return os.path.expanduser(d)
    root = cache_budget.root()
    return os.path.join(root, "stream_tracks") if root else None


def register_cache():
    """Put the tracks cache on the one cache policy, reading the environment now. Returns the
    record, or None while the policy is dark (NETRADIO_CACHE_ROOT unset) or the directory is
    refused."""
    # rank 8: of the caches sharing the policy's floor, the tracks give up entries after the
    # decoded captures and signatures -- each one comes back by a re-cut. The literal name,
    # not the constant above, so env_check.py's code scan sees the registration and counts
    # its variable family as read.
    return cache_budget.register("stream_tracks",
                                 cap=int(STREAM_TRACKS_CACHE_GB * cache_budget.GB),
                                 max_age=STREAM_TRACKS_CACHE_MAX_AGE_DAYS,
                                 refill="re-extract", rank=8)


register_cache()                  # at import: the wrapper sources .env before any import


def _on_policy(out_path):
    """True when a cut landing at `out_path` lands inside the registered tracks cache, so its
    write goes through the policy. A --out outside the cache is the operator's own directory:
    written as before, not accounted."""
    d = cache_budget.dir_of(STREAM_TRACKS_CACHE)
    if not d or not out_path:
        return False
    a, b = os.path.realpath(out_path), os.path.realpath(d)
    return os.path.commonpath([a, b]) == b


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


def cut(stem, m_from, m_to, cstart, out_path, account=True):
    """Cut [m_from, m_to) of the master out of `stem` into `out_path`, as flac where the name
    says flac (ffmpeg picks the codec from the extension; the argv names none).

    Through the cache policy when the cut lands inside the registered tracks cache: `reserve`
    first (the cut's length is not known until ffmpeg has run, so an unplanned one -- admitted
    while the cache is under its cap) and `commit` after. A refusal (the disk past its floor,
    the cap with nothing evictable) skips the cut and returns False.

    `account=False` names this writer's own scratch: the reassembly's part files, written and
    unlinked within the same pass, never committed -- exempt from the policy's door like any
    writer's own half-made file."""
    src = _audio.find_audio_file(stem)
    lo = m_from - cstart
    policy = account and _on_policy(out_path)
    if policy and not cache_budget.reserve(STREAM_TRACKS_CACHE, None):
        return False
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "%.4f" % lo,
                    "-t", "%.4f" % (m_to - m_from), "-i", src,
                    "-ac", "2", "-ar", "44100", out_path], check=True)
    if policy:
        cache_budget.commit(STREAM_TRACKS_CACHE, out_path)
    return True


def safe(name):
    return "".join(c if (c.isalnum() or c in " -_&.,'()") else "_" for c in name).strip()[:90]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=None,
                    help="where the cuts land (default: the stream_tracks cache -- "
                         "NETRUDIO_STREAM_TRACKS_CACHE_DIR, else $NETRADIO_CACHE_ROOT/stream_tracks)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", type=int, action="append")
    args = ap.parse_args()
    args.out = args.out or tracks_dir()
    if not args.out:
        sys.exit("no tracks directory: set NETRADIO_CACHE_ROOT (or NETRADIO_STREAM_TRACKS_CACHE_DIR) "
                 "in .env -- see .env.example -- or pass --out")

    meta = json.load(open(os.path.join(_gt.REPO_ROOT, "track-metadata.json")))
    tracks = meta.get("tracks", meta)
    starts = positions()
    places = {s: windows(s, v) for s, v in starts.items()}
    print("# %d capture(s) with PRECISE timing and audio on disk\n" % len(places))

    if not args.dry_run:
        os.makedirs(args.out, exist_ok=True)

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

        if len(pieces) == 1:
            stem, a, b = pieces[0]
            if not cut(stem, a, b, starts[stem], out):
                print("  %3s SKIP  %-42s the cache policy refused the room (the disk is past "
                      "its floor)" % (num, title[:42]))
                skipped += 1
                continue
        else:
            # The reassembly writes its part files as this writer's own scratch -- never
            # committed, unlinked in this same pass -- and lands the assembled track under the
            # same gate a direct cut uses: one reserve before the final file, one commit after.
            parts = []
            for i, (stem, a, b) in enumerate(pieces):
                p = out + ".part%d.flac" % i
                cut(stem, a, b, starts[stem], p, account=False)
                parts.append(p)
            lst = out + ".txt"
            with open(lst, "w") as fh:
                for p in parts:
                    fh.write("file '%s'\n" % p.replace("'", "'\\''"))
            policy = _on_policy(out)
            if policy and not cache_budget.reserve(STREAM_TRACKS_CACHE, None):
                for p in parts + [lst]:
                    os.unlink(p)
                print("  %3s SKIP  %-42s the cache policy refused the room (the disk is past "
                      "its floor)" % (num, title[:42]))
                skipped += 1
                continue
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0",
                            "-i", lst, "-c", "copy", out], check=True)
            if policy:
                cache_budget.commit(STREAM_TRACKS_CACHE, out)
            for p in parts + [lst]:
                os.unlink(p)
        made += 1

    print("\n# %d extracted (%d needed reassembly across captures), %d refused"
          % (made, joined, skipped))
    if not args.dry_run:
        print("# -> %s" % args.out)
    print("# A refused track is a track we cannot yet say WHERE it is. A silently-wrong extract\n"
          "# would be worse: it propagates into the calibration and looks exactly like data.")


if __name__ == "__main__":
    main()
