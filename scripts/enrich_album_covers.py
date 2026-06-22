#!/usr/bin/env python3
"""Move cover art to the ALBUM level (Tim, 2026-06-22).

The first enrichment put `artwork_url`/`artwork_hires` on individual tracks, so tracks that
share an album ended up with *different* covers (each matched its own single) — inconsistent in
TRACKLIST.md. Covers belong on the **album**; a track with an album inherits it.

For each album, resolve ONE cover, priority:
  1. the album's own Apple link  -> iTunes /lookup  (authoritative)
  2. an Apple *album* search (artist+title, or title-only for "Various")  -> strict-ish match
  3. MusicBrainz release-group -> Cover Art Archive front art
  4. a Discogs search cover_image (album title), if real
  5. the album's tracks already agree on one cover  -> promote it
Then store `artwork_url` (600/front-500) + `artwork_hires` (1200/front-1200) on the album and
**delete the per-track covers for every track that has an album** (it now inherits). Album-less
tracks (singles) keep their track-level cover. Additive otherwise; "rather blank than wrong".

    python3 scripts/enrich_album_covers.py [--apply]
"""

import argparse
import collections
import json
import re
import sys
import urllib.parse
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import find_streaming_links as fsl                                   # noqa: E402
from enrich_covers_links import (curl_json, itunes_lookup, covers_from_art100,   # noqa: E402
                                 album_country_id, write_meta, UA, COUNTRIES)
from enrich_musicbrainz import caa_has_front                          # noqa: E402
import time                                                          # noqa: E402
OUT = REPO / "track-metadata.json"

VARIOUS = ("various", "various artists", "va", "v/a", "")


def strip_album_title(title):
    """Drop trailing format/year parentheticals: 'Earth Volume Two (1996, CD)' -> 'Earth Volume Two'."""
    return re.sub(r"\s*[\(\[][^\)\]]*[\)\]]\s*$", "", title or "").strip() or (title or "")


def itunes_album_search(term, country, limit=8):
    u = "https://itunes.apple.com/search?" + urllib.parse.urlencode(
        {"term": term, "entity": "album", "country": country, "limit": str(limit)})
    time.sleep(0.4)
    return (curl_json(u) or {}).get("results", [])


def apple_album_cover(artist, title):
    base = strip_album_title(title)
    various = fsl.canon(artist) in VARIOUS
    term = base if various else "%s %s" % (artist, base)
    for c in COUNTRIES:
        for r in itunes_album_search(term, c):
            if fsl.canon(r.get("collectionName", "")) != fsl.canon(base):
                continue
            if various or fsl.artist_ok(artist, r.get("artistName", "")):
                return r.get("artworkUrl100")
    return None


def mb_album_cover(artist, title):
    base = strip_album_title(title)
    q = 'releasegroup:"%s"' % base.replace('"', "")
    if fsl.canon(artist) not in VARIOUS:
        q = 'artist:"%s" AND %s' % (artist.replace('"', ""), q)
    u = "https://musicbrainz.org/ws/2/release?" + urllib.parse.urlencode(
        {"query": q, "fmt": "json", "limit": "8"})
    time.sleep(1.1)
    for rel in (curl_json(u) or {}).get("releases", []):
        if fsl.canon(rel.get("title", "")) != fsl.canon(base):
            continue
        if caa_has_front(rel.get("id")):
            b = "https://coverartarchive.org/release/%s/front-" % rel["id"]
            return b + "500", b + "1200"
    return None


def discogs_album_cover(title):
    u = "https://api.discogs.com/database/search?" + urllib.parse.urlencode(
        {"q": strip_album_title(title), "type": "release", "per_page": "8"})
    time.sleep(2.5)
    for r in (curl_json(u, ua=UA, timeout=12) or {}).get("results", []):
        cov = r.get("cover_image") or ""
        if cov.startswith("http") and "spacer" not in cov:
            return cov
    return None


def resolve_album_cover(aid, alb, album_tracks):
    """(norm, hires) for an album, or (None, None). Also reports the source used."""
    f = alb.get("fields") or {}
    artist, title = alb.get("artist") or "", alb.get("title") or aid

    # 1) album's own Apple link
    apple = f.get("apple") or f.get("apple-music")
    ac, aaid = album_country_id(apple)
    if apple and aaid:
        res = itunes_lookup(aaid, ac)
        if res and res[0].get("artworkUrl100"):
            return (*covers_from_art100(res[0]["artworkUrl100"]), "apple-link")
    # 2) Apple album search
    art = apple_album_cover(artist, title)
    if art:
        return (*covers_from_art100(art), "apple-search")
    # 3) MusicBrainz / Cover Art Archive
    mb = mb_album_cover(artist, title)
    if mb:
        return (*mb, "musicbrainz")
    # 4) Discogs search cover
    dc = discogs_album_cover(title)
    if dc:
        return (dc, dc, "discogs")
    # 5) promote the most common existing track cover (consistency beats per-track variance)
    covers = collections.Counter(t.get("artwork_url") for t in album_tracks if t.get("artwork_url"))
    if covers:
        c = covers.most_common(1)[0][0]
        hi = next((t.get("artwork_hires") for t in album_tracks if t.get("artwork_url") == c), None)
        src = "promote-track" if len(covers) == 1 else "promote-majority"
        return c, hi, src
    return None, None, "none"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)

    meta = json.load(open(OUT, encoding="utf-8"))
    tracks, albums = meta["tracks"], meta.get("albums", {})
    by_album = collections.defaultdict(list)
    for t in tracks.values():
        if t.get("album"):
            by_album[t["album"]].append(t)

    set_n, none_n = 0, 0
    for aid, alb in albums.items():
        if aid not in by_album:
            continue
        norm, hi, src = resolve_album_cover(aid, alb, by_album[aid])
        if norm:
            alb["artwork_url"] = norm
            if hi:
                alb["artwork_hires"] = hi
            set_n += 1
            print("  album %-44s <- %s" % (aid[:44], src))
        else:
            none_n += 1
            print("  album %-44s -- NO COVER (tracks will show the logo)" % aid[:44])

    # tracks with an album inherit the album cover -> drop their per-track covers
    cleared = 0
    for t in tracks.values():
        if t.get("album"):
            if t.pop("artwork_url", None) is not None or t.pop("artwork_hires", None) is not None:
                cleared += 1

    print("\n=== %d album covers set, %d albums coverless; cleared per-track covers on %d album-tracks ===" %
          (set_n, none_n, cleared))
    if args.apply:
        write_meta(meta)
        print("wrote %s" % OUT)
    else:
        print("(dry-run)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
