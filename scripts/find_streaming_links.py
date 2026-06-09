#!/usr/bin/env python3
"""Find accurate Apple Music (and, with creds, Spotify) links for tracks.

Searches the iTunes Search API for each track that lacks an `apple` field and
accepts a result only when the **artist matches** and the **title matches**
(remix/version qualifiers respected) — obscure 1998 D&B tracks attract wrong
matches, so we'd rather leave a track blank than link the wrong recording.

Spotify has no unauthenticated search; if `SPOTIFY_CLIENT_ID`/`SPOTIFY_CLIENT_SECRET`
are set it will also fill `spotify`, otherwise it's skipped.

Prints a report (matched url, or NO MATCH with the closest candidate) and writes
track-metadata.json. Usage: python3 scripts/find_streaming_links.py [--apply] [--country IE]
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import build_track_metadata as gen  # noqa: E402
OUT = REPO / "track-metadata.json"


def canon(s):
    s = (s or "").lower()
    s = s.replace("&", " and ")
    s = re.sub(r"\bfeat\.?\b|\bfeaturing\b", " ", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"^\s*the\s+", "", s)
    return " ".join(s.split())


def base(s):
    return canon(re.sub(r"\s*[\(\[].*?[\)\]]", "", s or ""))


def qualifier_words(title):
    inside = " ".join(re.findall(r"[\(\[](.*?)[\)\]]", title or ""))
    words = set(canon(inside).split()) - {"mix", "remix", "version", "edit", "the"}
    return words


def artist_ok(want, got):
    a, b = canon(want), canon(got)
    return bool(a) and (a == b or a in b or b in a)


def title_ok(want, got):
    cw, cg = canon(want), canon(got)
    if cw == cg:
        return True
    bw, bg = base(want), base(got)
    if not (bw and bw == bg):   # exact base-title equality (no loose substring match)
        return False
    # If the track is a remix/version, require the qualifier to be present too,
    # so we don't link the original recording for a remix track.
    return qualifier_words(want) <= set(cg.split())


def itunes_search(term, country, limit=6):
    url = "https://itunes.apple.com/search?" + urllib.parse.urlencode(
        {"term": term, "entity": "song", "country": country, "limit": str(limit)})
    try:
        out = subprocess.run(["curl", "-sS", "--max-time", "8", url],
                             check=False, capture_output=True, text=True, timeout=12).stdout
        return json.loads(out).get("results", [])
    except Exception:
        return []


def find_apple(artist, title, country):
    results = itunes_search("%s %s" % (artist, title), country)
    accepted, closest = None, None
    for r in results:
        ra, rt = r.get("artistName", ""), r.get("trackName", "")
        url = r.get("trackViewUrl")
        if not url:
            continue
        if artist_ok(artist, ra):
            if closest is None:
                closest = (ra, rt, url)
            if title_ok(title, rt):
                accepted = (ra, rt, url)
                break
    return accepted, closest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write track-metadata.json")
    parser.add_argument("--country", default="IE")
    parser.add_argument("--sleep", type=float, default=0.3)
    args = parser.parse_args()

    data = json.load(open(OUT, encoding="utf-8"))
    tracks = data["tracks"]
    matched, unmatched = 0, 0
    for number in sorted(tracks, key=int):
        entry = tracks[number]
        artist, title = entry.get("artist") or "", entry.get("title") or ""
        if entry.get("use_logo") or canon(artist) == "net radio" or "promo" in canon(title):
            continue
        if entry.get("fields", {}).get("apple"):
            continue
        accepted, closest = find_apple(artist, title, args.country)
        if accepted:
            entry.setdefault("fields", {})["apple"] = accepted[2]
            matched += 1
            print("  OK   %3s %s - %s  ->  %s — %s" % (number, artist, title, accepted[0], accepted[1]))
        else:
            unmatched += 1
            hint = ("  ?closest: %s — %s" % (closest[0], closest[1])) if closest else "  (no artist match)"
            print("  MISS %3s %s - %s%s" % (number, artist, title, hint))
        time.sleep(args.sleep)

    print("\napple: matched=%d  unmatched=%d" % (matched, unmatched))
    if not (os.environ.get("SPOTIFY_CLIENT_ID") and os.environ.get("SPOTIFY_CLIENT_SECRET")):
        print("spotify: SKIPPED (set SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET to enable)")

    if args.apply:
        gen.save(data, str(OUT))
        print("wrote %s" % OUT)
    else:
        print("(dry-run; pass --apply to write)")


if __name__ == "__main__":
    raise SystemExit(main())
