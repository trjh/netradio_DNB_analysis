#!/usr/bin/env python3
"""Identify the Mystery Tracks by fingerprinting the mix where they play ALONE (G3).

    PYTHONPATH=scripts .env/bin/python scripts/identify_mystery.py            # all of them
    PYTHONPATH=scripts .env/bin/python scripts/identify_mystery.py --track 68
    PYTHONPATH=scripts .env/bin/python scripts/identify_mystery.py --dry-run  # no network

Why this can work at all
------------------------
Fingerprinting a DJ mix normally fails for two reasons: the audio is **blended** (two records
at once) and **pitched** (the DJ beatmatches, so the record is not at its own speed). Both
wreck a fingerprint. We can blunt each:

* **Blending happens at the EDGES.** A DJ mixes a record in and mixes it out; in between it
  plays alone. So we sample the MIDDLE of the track's span and skip the ends. (Tim's read of
  the material, and the reason to try at all: the mystery tracks are not blended throughout.)
  Several excerpts are taken, not one -- if a DJ talks over the middle of one, another is clean.
* **Pitch.** Every rate measured on this stream sits within ~1.3% of 1.0 (see PROCESS.md's
  addendum: the DJ sets a pitch per record and leaves it), and Chromaprint tolerates a percent
  or two. So no correction is applied by default -- but `--rate-sweep` will re-fingerprint each
  excerpt across a small band of speeds if the plain lookup finds nothing.

What we CANNOT do, and why the obvious ideas fail
-------------------------------------------------
* We cannot use `solo_anchors` to find the solo passages: it locates where *a given original*
  plays alone, and for a mystery track there IS no original. That is circular.
* We cannot pitch-correct exactly: the rate is `mix_bpm / original_bpm`, and the original's
  tempo is precisely what we do not know. Hence the sweep rather than a correction.
* Matching against Tim's own library is pointless: a mystery track is, by definition, one he
  did not recognise. It is not in there.

This tool only ever READS from AcoustID (`lookup`). It never submits. Submitting fingerprints
of this material to a public database is an irreversible, outward-facing act and is not
something a script should decide to do.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from streamalign import audio as _audio          # noqa: E402
from streamalign import groundtruth as _gt       # noqa: E402
from streamalign import track_mix as _tm         # noqa: E402

LOOKUP = "https://api.acoustid.org/v2/lookup"
META = "recordings releasegroups"

# Skip this fraction of the span at each end: that is where the DJ blends. Everything between
# is the record playing (we hope) by itself.
EDGE_SKIP = 0.20
EXCERPT_S = 60.0            # AcoustID wants >= ~30s; 60 is comfortable
EXCERPTS = 3                # sample a few places -- one bad excerpt should not sink the track
RATE_SWEEP = (0.98, 0.99, 1.0, 1.01, 1.02)
THROTTLE_S = 0.4            # AcoustID asks for <= 3 requests/second


def mystery_tracks(meta):
    out = []
    for num, entry in (meta or {}).items():
        if not str(num).isdigit():
            continue
        if "mystery" in (entry.get("title") or "").lower():
            out.append((int(num), entry))
    return sorted(out)


def excerpt_windows(begin_s, end_s, count=EXCERPTS, length=EXCERPT_S):
    """Where inside a track's span to sample, avoiding the blended edges."""
    span = end_s - begin_s
    if span <= 0:
        return []
    inner_lo = begin_s + span * EDGE_SKIP
    inner_hi = end_s - span * EDGE_SKIP
    usable = inner_hi - inner_lo
    if usable < 15.0:                        # too short to be worth it -- take the middle
        mid = 0.5 * (begin_s + end_s)
        return [(max(begin_s, mid - length / 2), min(end_s, mid + length / 2))]
    length = min(length, usable)
    if count == 1 or usable <= length:
        mid = 0.5 * (inner_lo + inner_hi)
        return [(mid - length / 2, mid + length / 2)]
    step = (usable - length) / float(count - 1)
    return [(inner_lo + i * step, inner_lo + i * step + length) for i in range(count)]


def find_capture(entry, begin, end, starts):
    """(stem, master_start) of a capture containing this span, or (None, None).

    Prefers the track's own `source_files` (what `align_track` trusts). Falls back to scanning
    the placed captures for one whose window CONTAINS the span -- which matters here: the
    mystery tracks we most want to identify sit in files that are not hand-labelled yet, so
    they have no `source_files` at all, and refusing to look would rule out exactly the tracks
    this tool exists for.
    """
    cap, cstart = _tm._select_capture(entry.get("source_files") or [], begin, end, starts)
    if cap and _audio.find_audio_file(cap):
        return cap, cstart

    # The captures the mystery tracks live in are mostly NOT hand-placed yet, so they are
    # absent from resolve_starts() and the scan below would find nothing. The 1998/2017 notes
    # place them (approximately, but well within the ~20% margin we skip at each edge anyway),
    # so fall back to those. Hand placements still win where they exist.
    from streamalign import tracklist2017 as _tl
    positions = dict(starts)
    for stem, note in _tl.parse().items():
        if stem not in positions and note.get("master_start_s") is not None:
            positions[stem] = note["master_start_s"]

    best, best_slack = None, None
    for stem, ms in positions.items():
        if not _audio.find_audio_file(stem):
            continue
        me = ms + _audio.duration_seconds(stem)
        if ms <= begin and end <= me:
            slack = min(begin - ms, me - end)      # prefer the capture that brackets it best
            if best_slack is None or slack > best_slack:
                best, best_slack = (stem, ms), slack
    return best if best else (None, None)


def cut(capture_path, start_s, end_s, rate=1.0):
    """Extract [start,end] of a capture to a temp wav, optionally re-speeding it."""
    handle = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    handle.close()
    cmd = ["ffmpeg", "-v", "error", "-y", "-ss", "%.3f" % start_s,
           "-t", "%.3f" % (end_s - start_s), "-i", capture_path]
    if abs(rate - 1.0) > 1e-6:
        # Undo the DJ's pitch: if the mix played it at `rate`, dividing the tempo by `rate`
        # restores the record's own speed. atempo keeps the pitch sane over this small band.
        cmd += ["-filter:a", "atempo=%.6f" % (1.0 / rate)]
    cmd += ["-ac", "1", "-ar", "44100", handle.name]
    if subprocess.run(cmd, capture_output=True).returncode != 0:
        os.unlink(handle.name)
        return None
    return handle.name


def fingerprint(path):
    out = subprocess.run(["fpcalc", "-json", path], capture_output=True, text=True)
    if out.returncode != 0:
        return None
    try:
        data = json.loads(out.stdout)
        return data["duration"], data["fingerprint"]
    except (ValueError, KeyError):
        return None


def lookup(duration, fp, key):
    query = urllib.parse.urlencode({"client": key, "meta": META,
                                    "duration": int(round(duration)), "fingerprint": fp})
    try:
        with urllib.request.urlopen(LOOKUP + "?" + query, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception as exc:                                  # network, rate limit, whatever
        return {"error": str(exc)}
    if data.get("status") != "ok":
        return {"error": (data.get("error") or {}).get("message", "lookup failed")}
    hits = []
    for result in data.get("results", []):
        for rec in result.get("recordings", []) or []:
            hits.append({
                "score": result.get("score", 0.0),
                "title": rec.get("title"),
                "artists": ", ".join(a.get("name", "") for a in rec.get("artists", []) or []),
                "id": rec.get("id"),
            })
    return {"hits": sorted(hits, key=lambda h: -h["score"])}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--track", type=int, action="append",
                    help="only this Mystery Track number (repeatable)")
    ap.add_argument("--meta", default="track-metadata.json")
    ap.add_argument("--dry-run", action="store_true",
                    help="cut + fingerprint, but do not call AcoustID (no network)")
    ap.add_argument("--rate-sweep", action="store_true",
                    help="if a plain lookup finds nothing, retry across +/-2%% of speed")
    ap.add_argument("--min-score", type=float, default=0.5,
                    help="ignore AcoustID hits below this score (default 0.5)")
    args = ap.parse_args(argv)

    key = os.environ.get("ACOUSTID_DEV_API_KEY")
    if not key and not args.dry_run:
        sys.exit("ACOUSTID_DEV_API_KEY is unset. Put it in .env_vars (gitignored -- this repo "
                 "is public), then: set -a && . ./.env_vars && set +a")

    with open(args.meta) as handle:
        data = json.load(handle)
    meta = data.get("tracks", data)
    starts = _gt.resolve_starts()

    wanted = mystery_tracks(meta)
    if args.track:
        wanted = [(n, e) for n, e in wanted if n in args.track]
    if not wanted:
        sys.exit("no Mystery Tracks matched")

    print("# fingerprinting the MIDDLE of each mystery track (the edges are where the DJ "
          "blends)\n")
    for num, entry in wanted:
        begin, end = entry.get("master_begin_seconds"), entry.get("master_end_seconds")
        title = entry.get("title") or "?"
        if begin is None or end is None:
            print("%-3d %-34s SKIP: no master span in track-metadata.json" % (num, title[:34]))
            continue
        cap, cstart = find_capture(entry, begin, end, starts)
        path = _audio.find_audio_file(cap) if cap else None
        if not path:
            print("%-3d %-34s SKIP: no capture audio for its span" % (num, title[:34]))
            continue

        print("%-3d %s" % (num, title))
        print("    span %.0f-%.0fs of %s" % (begin - cstart, end - cstart, cap))
        best = []
        rates = RATE_SWEEP if args.rate_sweep else (1.0,)
        for lo, hi in excerpt_windows(begin - cstart, end - cstart):
            for rate in rates:
                clip = cut(path, lo, hi, rate)
                if not clip:
                    continue
                fp = fingerprint(clip)
                os.unlink(clip)
                if not fp:
                    continue
                if args.dry_run:
                    print("    %5.0f-%5.0fs rate %.2f -> fingerprinted (%.0fs), no lookup"
                          % (lo, hi, rate, fp[0]))
                    continue
                res = lookup(fp[0], fp[1], key)
                time.sleep(THROTTLE_S)
                if res.get("error"):
                    print("    %5.0f-%5.0fs rate %.2f -> lookup error: %s"
                          % (lo, hi, rate, res["error"]))
                    continue
                hits = [h for h in res["hits"] if h["score"] >= args.min_score]
                if hits:
                    best.extend(hits)
                    print("    %5.0f-%5.0fs rate %.2f -> %d hit(s)" % (lo, hi, rate, len(hits)))
                    break
                print("    %5.0f-%5.0fs rate %.2f -> no match" % (lo, hi, rate))
            if best and not args.rate_sweep:
                break
        if best:
            seen, uniq = set(), []
            for h in sorted(best, key=lambda h: -h["score"]):
                if h["id"] in seen:
                    continue
                seen.add(h["id"])
                uniq.append(h)
            print("    ==> CANDIDATES:")
            for h in uniq[:5]:
                print("        %.2f  %s - %s" % (h["score"], h["artists"] or "?",
                                                 h["title"] or "?"))
        elif not args.dry_run:
            print("    ==> no identification. Still a mystery.")
        print()

    print("# NOTE: this only ever READS from AcoustID. Submitting fingerprints back is an\n"
          "# irreversible public act and is deliberately not automated here.")


if __name__ == "__main__":
    sys.exit(main())
