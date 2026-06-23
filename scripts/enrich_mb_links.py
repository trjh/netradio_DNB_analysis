#!/usr/bin/env python3
"""Add a MusicBrainz link to every album (release-group) and every album-less track (recording).

Stored as `fields.musicbrainz`. Strict artist+title match (reuses find_streaming_links'
matchers); additive only; free/no-token; rate-limited ~1 req/s with a descriptive UA. Albums
first (their tracks inherit the album's link at render time).

    python3 scripts/enrich_mb_links.py [--apply]
"""

import argparse
import json
import sys
import time
import urllib.parse
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import find_streaming_links as fsl                               # noqa: E402
from enrich_musicbrainz import curl, mb_recordings              # noqa: E402
from enrich_album_covers import strip_album_title, VARIOUS      # noqa: E402
from enrich_covers_links import write_meta                      # noqa: E402
OUT = REPO / "track-metadata.json"


def recording_url(artist, title):
    for rec in mb_recordings(artist, title):
        if fsl.artist_ok(artist, rec.get("artist-credit-phrase", "")) and \
                fsl.title_ok(title, rec.get("title", "")):
            return "https://musicbrainz.org/recording/%s" % rec["id"]
    return None


def release_group_url(artist, title):
    base = strip_album_title(title)
    q = 'releasegroup:"%s"' % base.replace('"', "")
    if fsl.canon(artist) not in VARIOUS:
        q = 'artist:"%s" AND %s' % (artist.replace('"', ""), q)
    u = "https://musicbrainz.org/ws/2/release-group?" + urllib.parse.urlencode(
        {"query": q, "fmt": "json", "limit": "8"})
    time.sleep(1.1)
    for rg in (curl(u) or {}).get("release-groups", []):
        if fsl.canon(rg.get("title", "")) != fsl.canon(base):
            continue
        if fsl.canon(artist) in VARIOUS or fsl.artist_ok(artist, rg.get("artist-credit-phrase", "")):
            return "https://musicbrainz.org/release-group/%s" % rg["id"]
    return None


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)

    meta = json.load(open(OUT, encoding="utf-8"))
    tracks, albums = meta["tracks"], meta.get("albums", {})

    n_alb, n_trk = 0, 0
    print("=== albums (release-group) ===")
    for aid, a in albums.items():
        f = a.setdefault("fields", {})
        if f.get("musicbrainz"):
            continue
        url = release_group_url(a.get("artist") or "", a.get("title") or aid)
        if url:
            f["musicbrainz"] = url; n_alb += 1
            print("  +mb %-44s %s" % (aid[:44], url))

    print("=== album-less tracks (recording) ===")
    for tid, t in tracks.items():
        if t.get("album") or not (t.get("artist") and t.get("title")) or "Mystery Track" in (t.get("title") or ""):
            continue
        f = t.setdefault("fields", {})
        if f.get("musicbrainz"):
            continue
        url = recording_url(t["artist"], t["title"])
        if url:
            f["musicbrainz"] = url; n_trk += 1
            print("  +mb %-3s %s — %s" % (tid, t["artist"], t["title"]))

    print("\n=== musicbrainz: +%d albums, +%d album-less tracks ===" % (n_alb, n_trk))
    if args.apply:
        write_meta(meta); print("wrote %s" % OUT)
    else:
        print("(dry-run)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
