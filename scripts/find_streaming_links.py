#!/usr/bin/env python3
"""Find accurate Apple Music (and, with creds, Spotify) links for tracks.

Searches the iTunes Search API for each track that lacks an `apple` field and
accepts a result only when the **artist matches** and the **title matches**
(remix/version qualifiers respected) — obscure 1998 D&B tracks attract wrong
matches, so we'd rather leave a track blank than link the wrong recording.

Spotify has no unauthenticated search. Either set `SPOTIFY_CLIENT_ID`/
`SPOTIFY_CLIENT_SECRET` (not yet implemented), or feed a curated link list via
`--spotify-json data/spotify-links.json` — those are applied the same strict way:
high-confidence only, and only when the curator's claimed artist/title still
agrees with our label-authoritative artist/title. Mismatches are held for review.

Prints a report (matched url, or NO MATCH with the closest candidate) and writes
track-metadata.json. Usage:
    python3 scripts/find_streaming_links.py [--apply] [--country IE]
    python3 scripts/find_streaming_links.py --skip-apple --spotify-json data/spotify-links.json --apply
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


def split_matched(text):
    """Split a curated 'Artist - Title' match string on the first ' - '."""
    parts = (text or "").split(" - ", 1)
    return parts[0].strip(), (parts[1].strip() if len(parts) > 1 else "")


def import_spotify_json(tracks, path):
    """Apply curated Spotify links from a JSON list the same strict way as Apple.

    Spotify has no unauthenticated search, so links are curated offline (e.g. by a
    browsing agent) into a JSON list of
        {"track_number", "spotify", "matched": "Artist - Title", "confidence"}.
    We trust nothing blindly: a link is applied only when it is a real
    open.spotify.com/track URL, the curator marked it `high` confidence, the field
    isn't already set, and the curator's claimed Artist/Title still agrees with our
    label-authoritative artist/title (artist_ok + title_ok). Everything else is
    left blank and reported for human review — better blank than the wrong song.
    """
    items = json.load(open(path, encoding="utf-8"))
    applied, review = 0, []
    for e in sorted(items, key=lambda x: int(x.get("track_number", 0))):
        number = str(e.get("track_number"))
        url = (e.get("spotify") or "").strip()
        conf = (e.get("confidence") or "").lower()
        entry = tracks.get(number)
        ma, mt = split_matched(e.get("matched", ""))
        reasons = []
        if entry is None:
            reasons.append("no such track")
        if not url.startswith("https://open.spotify.com/track/"):
            reasons.append("not a spotify track url")
        if conf != "high":
            reasons.append("confidence=%s" % (conf or "?"))
        if entry is not None and entry.get("fields", {}).get("spotify"):
            reasons.append("already set")
        if entry is not None:
            oa, ot = entry.get("artist") or "", entry.get("title") or ""
            if not artist_ok(oa, ma):
                reasons.append("artist mismatch (%s vs %s)" % (oa, ma))
            if not title_ok(ot, mt):
                reasons.append("title mismatch (%s vs %s)" % (ot, mt))
        if reasons:
            review.append((number, e.get("matched", ""), "; ".join(reasons)))
            print("  HOLD %3s %s  [%s]" % (number, e.get("matched", ""), "; ".join(reasons)))
            continue
        entry.setdefault("fields", {})["spotify"] = url
        applied += 1
        print("  OK   %3s %s  ->  %s" % (number, e.get("matched", ""), url))
    print("\nspotify(curated): applied=%d  held-for-review=%d" % (applied, len(review)))
    return applied, review


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write track-metadata.json")
    parser.add_argument("--country", default="IE")
    parser.add_argument("--sleep", type=float, default=0.3)
    parser.add_argument("--spotify-json", help="curated Spotify links JSON to import (strict)")
    parser.add_argument("--skip-apple", action="store_true", help="skip the iTunes search pass")
    args = parser.parse_args()

    data = json.load(open(OUT, encoding="utf-8"))
    tracks = data["tracks"]
    if not args.skip_apple:
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

    if args.spotify_json:
        import_spotify_json(tracks, args.spotify_json)
    elif not (os.environ.get("SPOTIFY_CLIENT_ID") and os.environ.get("SPOTIFY_CLIENT_SECRET")):
        print("spotify: SKIPPED (no curated --spotify-json and no SPOTIFY_CLIENT_ID/SECRET)")

    if args.apply:
        gen.save(data, str(OUT))
        print("wrote %s" % OUT)
    else:
        print("(dry-run; pass --apply to write)")


if __name__ == "__main__":
    raise SystemExit(main())
