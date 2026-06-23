#!/usr/bin/env python3
"""Enrich track-metadata.json with cover art + streaming links (Tim, 2026-06-22).

For every track we try, in priority order, to fill **cover art** (the main goal) and
**links** toward having two of {apple, spotify, discogs}:

  1. Apple Music — the iTunes Search API (strict artist+title match, reusing
     find_streaming_links' matchers so an obscure 1998 D&B track never links the wrong
     recording). From the accepted result we take the album art (`artworkUrl100`) and store
     a normal `artwork_url` (600x600, for TRACKLIST.md) and a hi-res `artwork_hires`
     (1200x1200, for the local player) — the "change NxN" trick Tim found. Tracks that
     already have an Apple link get their cover upgraded via an iTunes *lookup* of the id.
  2. Discogs — the public search API (no token; rate-limited, conservative match). Adds a
     `discogs` link (and a cover only if Discogs has a real one).
  Spotify has no unauthenticated search → reported as a gap (needs SPOTIFY creds or a curated
  list, like find_streaming_links).

NEVER overwrites an existing link; cover art prefers Apple. Strict matching throughout —
"rather blank than wrong". Writes track-metadata.json (format-preserving) and prints a report.

    python3 scripts/enrich_covers_links.py            # dry-run report
    python3 scripts/enrich_covers_links.py --apply     # write track-metadata.json
"""

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import find_streaming_links as fsl   # noqa: E402  — canon / artist_ok / title_ok
OUT = REPO / "track-metadata.json"

UA = "netradio-meta/1.0 (+listen-queue metadata enrichment)"


def curl_json(url, ua=None, timeout=10):
    cmd = ["curl", "-4", "-sS", "--max-time", str(timeout)]   # -4: dodge this LAN's black-holed IPv6
    if ua:
        cmd += ["-A", ua]
    cmd.append(url)
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5).stdout
        return json.loads(out)
    except Exception:   # noqa: BLE001 — best-effort
        return None


# ---- Apple / iTunes -----------------------------------------------------------------
def itunes_search(term, country="US", limit=6):
    u = "https://itunes.apple.com/search?" + urllib.parse.urlencode(
        {"term": term, "entity": "song", "country": country, "limit": str(limit)})
    time.sleep(0.4)
    return (curl_json(u) or {}).get("results", [])


def itunes_lookup(apple_id, country="US"):
    u = "https://itunes.apple.com/lookup?id=%s&country=%s" % (apple_id, country)
    time.sleep(0.4)
    return (curl_json(u) or {}).get("results", [])


def covers_from_art100(art):
    """(normal 600x600, hi-res 1200x1200) from an artworkUrl100, or (None, None)."""
    if not art or "100x100bb" not in art:
        return None, None
    return art.replace("100x100bb", "600x600bb"), art.replace("100x100bb", "1200x1200bb")


COUNTRIES = ["US", "GB", "IE"]   # search several catalogs — much of this 1998 EU D&B is GB/IE-only


def find_apple(artist, title):
    """Full iTunes result for a confident artist+title match across catalogs, or None."""
    for country in COUNTRIES:
        for r in itunes_search("%s %s" % (artist, title), country):
            if not (r.get("trackViewUrl") or r.get("collectionViewUrl")):
                continue
            if fsl.artist_ok(artist, r.get("artistName", "")) and fsl.title_ok(title, r.get("trackName", "")):
                return r
    return None


def album_country_id(url):
    """(country, album_id) from a music.apple.com link — the country matters for /lookup."""
    m = re.search(r"music\.apple\.com/([a-z]{2})/album/[^/]+/(\d+)", url or "")
    if m:
        return m.group(1).upper(), m.group(2)
    m = re.search(r"/album/[^/]+/(\d+)", url or "")
    return ("US", m.group(1)) if m else (None, None)


# ---- Discogs (public search, conservative) ------------------------------------------
def discogs_search(artist, want, want_is_album):
    """Public Discogs release search → {url, cover} for a confident match, or None.

    Discogs releases are identified by their *release* title, so for an album track we match
    the album name; for a single we match the track title. Artist must match; the release
    title must equal the wanted name (strict). Rate-limited (no token)."""
    time.sleep(2.5)
    u = "https://api.discogs.com/database/search?" + urllib.parse.urlencode(
        {"q": "%s %s" % (artist, want), "type": "release", "per_page": "10"})
    for r in (curl_json(u, ua=UA, timeout=12) or {}).get("results", []):
        rtitle = r.get("title", "")               # "Artist - Release Title"
        ra, _, rt = rtitle.partition(" - ")
        if not (fsl.artist_ok(artist, ra) and fsl.title_ok(want, rt)):
            continue
        cover = r.get("cover_image") or ""
        if "spacer" in cover or not cover.startswith("http"):
            cover = ""
        return {"url": "https://www.discogs.com/release/%s" % r.get("id"), "cover": cover or None}
    return None


# ---- main ---------------------------------------------------------------------------
def link_keys(track, albums):
    f = track.get("fields") or {}
    present = set(k for k in ("apple", "apple-music", "spotify", "discogs") if f.get(k))
    alb = albums.get(track.get("album")) if track.get("album") else None
    if alb:
        present |= set(k for k in ("apple", "apple-music", "spotify", "discogs")
                       if (alb.get("fields") or {}).get(k))
    return present


def write_meta(meta):
    def sort_inner(d):
        d = {k: d[k] for k in sorted(d)}
        if isinstance(d.get("fields"), dict):     # keep the per-service link block alphabetical too
            d["fields"] = {k: d["fields"][k] for k in sorted(d["fields"])}
        return d
    out = dict(meta)
    out["tracks"] = {tid: sort_inner(t) for tid, t in meta["tracks"].items()}
    if "albums" in meta:
        out["albums"] = {aid: sort_inner(a) for aid, a in meta["albums"].items()}
    with open(OUT, "w", encoding="utf-8") as h:
        json.dump(out, h, indent=2, ensure_ascii=False, sort_keys=False)
        h.write("\n")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Enrich track-metadata.json covers + links.")
    ap.add_argument("--apply", action="store_true", help="write track-metadata.json (default: dry-run)")
    ap.add_argument("--country", default="US")
    ap.add_argument("--limit", type=int, default=None, help="only the first N tracks (testing)")
    args = ap.parse_args(argv)

    meta = json.load(open(OUT, encoding="utf-8"))
    tracks, albums = meta["tracks"], meta.get("albums", {})
    items = list(tracks.items())[: args.limit] if args.limit else list(tracks.items())

    n_apple, n_cover, n_hi, n_discogs, gaps = 0, 0, 0, 0, []
    for tid, t in items:
        artist, title, album = t.get("artist"), t.get("title"), t.get("album")
        f = t.setdefault("fields", {})
        present = link_keys(t, albums)
        tag = "%-3s %s — %s" % (tid, artist or "?", title or t.get("url"))

        # 1) Apple cover (+ link). Use an existing apple id when present, else search.
        art100 = None
        apple_url = f.get("apple") or f.get("apple-music")
        ac, aid = album_country_id(apple_url)
        if apple_url and aid:
            res = itunes_lookup(aid, ac)
            art100 = res[0].get("artworkUrl100") if res else None
        elif not apple_url and artist and title:
            r = find_apple(artist, title)
            if r:
                f["apple"] = r.get("trackViewUrl") or r.get("collectionViewUrl")
                art100 = r.get("artworkUrl100")
                present.add("apple"); n_apple += 1
                print("  +apple   %s" % tag)
        if art100:
            norm, hi = covers_from_art100(art100)
            if norm:
                t["artwork_url"] = norm; n_cover += 1
            if hi:
                t["artwork_hires"] = hi; n_hi += 1
            print("  +cover   %s" % tag)

        # 2) Discogs link, conservatively, only while short of two links.
        if "discogs" not in present and len(present) < 2 and artist and (album or title):
            dg = discogs_search(artist, album or title, bool(album))
            if dg:
                f["discogs"] = dg["url"]; present.add("discogs"); n_discogs += 1
                print("  +discogs %s  -> %s" % (tag, dg["url"]))
                if "artwork_url" not in t and dg["cover"]:
                    t["artwork_url"] = dg["cover"]; n_cover += 1

        if len(present) < 2:
            gaps.append(tag)

    print("\n=== summary ===")
    print("apple links +%d · covers set +%d (hi-res +%d) · discogs +%d" % (n_apple, n_cover, n_hi, n_discogs))
    print("still < 2 links (need Spotify creds / Amazon / wider search): %d" % len(gaps))
    for g in gaps:
        print("  GAP %s" % g)

    if args.apply:
        write_meta(meta)
        print("\nwrote %s" % OUT)
    else:
        print("\n(dry-run — re-run with --apply to write)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
