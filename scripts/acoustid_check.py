#!/usr/bin/env python3
"""Verify the ORIGINALS against AcoustID: confirm what each source file actually is.

    set -a && . ./.env_vars && set +a          # needs ACOUSTID_DEV_API_KEY
    PYTHONPATH=scripts .env/bin/python scripts/acoustid_check.py            # all of sources/
    PYTHONPATH=scripts .env/bin/python scripts/acoustid_check.py --mismatch # only disagreements
    PYTHONPATH=scripts .env/bin/python scripts/acoustid_check.py --file 013-*.mp3

It fingerprints each file in `NETRADIO_SOURCES_DIR` the way AcoustID actually wants it -- the
fingerprint from the START of the recording, paired with its FULL duration -- and reports what
AcoustID says the file is. Where that disagrees with the filename, it says so: a mislabelled
source file silently poisons every downstream alignment and ID that trusts it.

Found on the first run: `013-DJ Addiction - Senses.mp3` is really Blame's "J-Walkin'" (the same
record as `021`), and `022-Castillo - Junkle I.flac` is by *Callisto*, not "Castillo".

## What this deliberately does NOT do

**It cannot identify the Mystery Tracks, and neither can anything else built on AcoustID.**
Fingerprinting the *stream* is impossible, not merely hard: the same record, taken from the 1998
broadcast, aligned to its own start and pitch-corrected, shares a bitwise fingerprint similarity
of **0.511** with its clean original -- and 0.50 is random noise. The ISDN/RealAudio compression
and the DJ's EQ throw away exactly the spectral detail Chromaprint keys on. There is no signal
to find, so no amount of duration/rate/offset sweeping can recover it.

This is measured, with controls: the clean file of that same record matches at 0.99, and 65 of
89 originals here are in AcoustID. The database covers this genre fine. The stream audio is the
problem. See `Archive/LESSON_acoustid_stream.md`.

Read-only. It never submits fingerprints -- that is irreversible and public, and not a script's
decision to make.
"""

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request

LOOKUP = "https://api.acoustid.org/v2/lookup"
THROTTLE_S = 0.35                 # AcoustID asks for <= 3 requests/second
AUDIO_EXTS = ("mp3", "flac", "m4a", "opus", "wav", "wv", "aif", "aiff", "mp4", "ogg")

_NOISE = re.compile(r"[^a-z0-9]+")


def _words(text):
    stop = {"the", "a", "an", "original", "mix", "remix", "feat", "ft", "vs", "and", "version"}
    return {w for w in _NOISE.split((text or "").lower()) if w and w not in stop}


def fingerprint(path):
    """(full_duration, fingerprint) -- fpcalc over the WHOLE file.

    This is the only form AcoustID's index accepts: the fingerprint is taken from the START of
    the recording and the duration is the recording's FULL length. An excerpt from the middle,
    or the head with an excerpt's duration, matches nothing at all -- both were tried.
    """
    out = subprocess.run(["fpcalc", "-json", path], capture_output=True, text=True)
    if out.returncode != 0:
        return None
    try:
        data = json.loads(out.stdout)
        return data["duration"], data["fingerprint"]
    except (ValueError, KeyError):
        return None


def lookup(duration, fp, key):
    query = urllib.parse.urlencode({"client": key, "meta": "recordings",
                                    "duration": int(round(duration)), "fingerprint": fp})
    try:
        with urllib.request.urlopen(LOOKUP + "?" + query, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        return {"error": str(exc)}
    if data.get("status") != "ok":
        return {"error": (data.get("error") or {}).get("message", "lookup failed")}
    hits = []
    for result in data.get("results", []):
        for rec in result.get("recordings", []) or []:
            hits.append({"score": result.get("score", 0.0),
                         "title": rec.get("title") or "",
                         "artists": ", ".join(a.get("name", "")
                                              for a in rec.get("artists") or [])})
    return {"hits": sorted(hits, key=lambda h: -h["score"])}


def disagrees(filename, hit):
    """Does AcoustID's answer contradict the filename? (word-set overlap, order-agnostic)"""
    stem = os.path.splitext(filename)[0]
    stem = re.sub(r"^\d{3}-", "", stem)               # drop the NNN- track prefix
    said = _words(hit["artists"]) | _words(hit["title"])
    named = _words(stem)
    if not said or not named:
        return False
    # Filenames carry junk (youtube ids, durations). Ask only whether ANY of what AcoustID
    # says appears in the name -- a real disagreement shares nothing at all.
    return not (said & named)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", action="append", help="only these (glob, repeatable)")
    ap.add_argument("--mismatch", action="store_true",
                    help="only report files whose name disagrees with what they ARE")
    ap.add_argument("--min-score", type=float, default=0.5)
    args = ap.parse_args(argv)

    key = os.environ.get("ACOUSTID_DEV_API_KEY")
    if not key:
        sys.exit("ACOUSTID_DEV_API_KEY is unset. It belongs in .env_vars (gitignored -- this "
                 "repo is PUBLIC), then: set -a && . ./.env_vars && set +a")
    sources = os.environ.get("NETRADIO_SOURCES_DIR")
    if not sources or not os.path.isdir(sources):
        sys.exit("NETRADIO_SOURCES_DIR is unset or missing (see .env_vars.example)")

    names = sorted(f for f in os.listdir(sources)
                   if f.rsplit(".", 1)[-1].lower() in AUDIO_EXTS)
    if args.file:
        names = [f for f in names if any(fnmatch.fnmatch(f, p) for p in args.file)]

    found = missing = bad = 0
    for name in names:
        fp = fingerprint(os.path.join(sources, name))
        if not fp:
            print("  ????  %-52s (could not fingerprint)" % name[:52])
            continue
        res = lookup(fp[0], fp[1], key)
        time.sleep(THROTTLE_S)
        if res.get("error"):
            print("  ERR   %-52s %s" % (name[:52], res["error"]))
            continue
        hits = [h for h in res["hits"] if h["score"] >= args.min_score]
        if not hits:
            missing += 1
            if not args.mismatch:
                print("  --    %-52s (not in AcoustID)" % name[:52])
            continue
        found += 1
        top = hits[0]
        if disagrees(name, top):
            bad += 1
            print("  ⚠ MISLABELLED  %s" % name)
            print("       AcoustID says: %s - %s  (%.2f)" % (top["artists"], top["title"],
                                                             top["score"]))
        elif not args.mismatch:
            print("  ok    %-52s %.2f  %s - %s" % (name[:52], top["score"],
                                                   top["artists"], top["title"]))

    print("\n# %d identified, %d not in AcoustID, %d MISLABELLED (of %d)"
          % (found, missing, bad, len(names)))
    if bad:
        print("# A mislabelled source poisons every alignment and ID that trusts it. Fix these.")


if __name__ == "__main__":
    sys.exit(main())
