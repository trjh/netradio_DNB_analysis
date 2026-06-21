#!/usr/bin/env python3
"""Render the identified stream tracklist (``track-metadata.json``) as ``TRACKLIST.md``.

This is the **public** tracklist of the mix: each identified track in master-timeline order,
with cover artwork and a clickable service logo, **both linking to the track's full page**
(its Apple Music / Spotify / YouTube release).

The artwork + release URLs live **in ``track-metadata.json`` itself** (no side-car cache):
each linked track carries a ``full_page_url`` (its chosen release link) and an external
``artwork_url`` (YouTube → ytimg; Apple/Spotify → the page's ``og:image``). This script
resolves any that are missing (network, concurrent) and writes them back into
``track-metadata.json``, then renders ``TRACKLIST.md`` from those stored fields. Once populated
it is fully offline; ``--refresh`` re-resolves everything, ``--no-resolve`` skips the network.

``track-metadata.json`` is canonical in this (analysis) repo; the player mirrors it. No image
files are committed — only URLs.
"""

import argparse
import html
import json
import os
import re
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
META = os.path.join(REPO, "track-metadata.json")
OUT = os.path.join(REPO, "TRACKLIST.md")

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) netradio-tracklist/1.0"
LOGO_FALLBACK = "https://raw.githubusercontent.com/trjh/netradio_DNB_analysis/main/logo/logo.jpg"

# service key -> (display label, badge colour, simple-icons slug)
SERVICES = {
    "apple":   ("AppleMusic", "FA243C", "applemusic"),
    "spotify": ("Spotify",    "1DB954", "spotify"),
    "youtube": ("YouTube",    "FF0000", "youtube"),
    "other":   ("Link",       "8FA3B0", None),
}
# track/album `fields` link keys, in preference order, mapped to a service key.
LINK_FIELDS = (("apple", "apple"), ("apple-music", "apple"), ("spotify", "spotify"),
               ("youtube", "youtube"))

_OG = re.compile(
    r'<meta[^>]+(?:property|name)=["\']og:image(?::secure_url)?["\'][^>]+content=["\']([^"\']+)', re.I)
_OG_REV = re.compile(
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']og:image', re.I)
_YT = re.compile(r"(?:v=|/vi/|youtu\.be/|/embed/|/shorts/)([A-Za-z0-9_-]{11})")


def pick_link(track, albums):
    """(service_key, url) for a track's best release link, or None. Prefers a track-level link;
    falls back to the track's album's link."""
    fields = track.get("fields") or {}
    for key, svc in LINK_FIELDS:
        if fields.get(key):
            return svc, fields[key]
    alb = albums.get(track.get("album")) if track.get("album") else None
    if alb:
        afields = alb.get("fields") or {}
        for key, svc in LINK_FIELDS:
            if afields.get(key):
                return svc, afields[key]
    return None


def service_of(url):
    if "music.apple.com" in url:
        return "apple"
    if "spotify.com" in url:
        return "spotify"
    if "youtu" in url:
        return "youtube"
    return "other"


def _fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=8) as resp:   # urllib follows 301/302 by default
        return resp.read(2_000_000).decode("utf-8", "replace")


def resolve_artwork(service, url):
    """External cover-image URL for a release link, or None. YouTube is derived; everything
    else scrapes the page's og:image. Best-effort (network)."""
    if service == "youtube":
        m = _YT.search(url)
        return "https://i.ytimg.com/vi/%s/hqdefault.jpg" % m.group(1) if m else None
    try:
        body = _fetch(url)
    except Exception:   # noqa: BLE001 — best-effort
        return None
    m = _OG.search(body) or _OG_REV.search(body)
    return m.group(1).strip() if m else None


def enrich(meta, resolve=True, refresh=False):
    """Populate each linked track's `full_page_url` + `artwork_url` IN track-metadata.json.
    Resolves missing artwork over the network (concurrently). Returns True if anything changed."""
    tracks, albums = meta["tracks"], meta.get("albums") or {}
    changed = False
    to_resolve = {}
    for tid, track in tracks.items():
        link = pick_link(track, albums)
        if not link:
            continue
        _svc, url = link
        if track.get("full_page_url") != url:
            track["full_page_url"] = url
            changed = True
        if resolve and (refresh or "artwork_url" not in track):
            to_resolve[tid] = (service_of(url), url)
    if to_resolve:
        def work(item):
            tid, (svc, url) = item
            return tid, resolve_artwork(svc, url)
        with ThreadPoolExecutor(max_workers=12) as pool:
            for tid, art in pool.map(work, list(to_resolve.items())):
                if tracks[tid].get("artwork_url") != art:
                    tracks[tid]["artwork_url"] = art
                    changed = True
    return changed


def md_text(value):
    text = html.unescape(value or "")
    text = text.replace("|", "\\|").replace("[", "(").replace("]", ")")
    return re.sub(r"\s+", " ", text).strip()


def fmt_time(seconds):
    if seconds is None:
        return ""
    s = int(round(seconds))
    return "%d:%02d:%02d" % (s // 3600, (s % 3600) // 60, s % 60) if s >= 3600 \
        else "%d:%02d" % (s // 60, s % 60)


def badge(service, page):
    label, colour, slug = SERVICES.get(service, SERVICES["other"])
    url = "https://img.shields.io/badge/%s-%s" % (label, colour)
    if slug:
        url += "?logo=%s&logoColor=white" % slug
    img = "![%s](%s)" % (label, url)
    return "[%s](%s)" % (img, page) if page else img


def row(track):
    page = track.get("full_page_url")
    art = track.get("artwork_url") or LOGO_FALLBACK
    cover_img = '<img src="%s" width="110" alt="">' % art
    cover = "[%s](%s)" % (cover_img, page) if page else cover_img

    title = md_text(track.get("title") or "(untitled)")
    artist = md_text(track.get("artist"))
    album = md_text((track.get("album") or "").replace("-", " ")) if track.get("album") else ""
    label = ("**%s**" % title) + (" — " + artist if artist else "")
    from_cell = ("from " + badge(service_of(page), page)) if page else "_(no release link)_"
    if album:
        from_cell += " · _%s_" % album
    return "| %s | %s | %s<br>%s |" % (fmt_time(track.get("master_begin_seconds")), cover, label, from_cell)


def render(meta):
    tracks = meta["tracks"]
    ordered = sorted(tracks.values(),
                     key=lambda t: t.get("master_begin_seconds") if
                     t.get("master_begin_seconds") is not None else 1e18)
    arted = sum(1 for t in ordered if t.get("artwork_url"))
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# Netradio DNB — Tracklist",
        "",
        "> The identified tracks of the mix, in master-timeline order. Auto-generated from "
        "`track-metadata.json` by `scripts/render_tracklist.py` — do not edit by hand. "
        "Cover art and the service logo link to each track's release page.",
        "",
        "**%d tracks** · %d with cover art · generated %s" % (len(ordered), arted, now),
        "",
        "| Time | Cover | Track |",
        "|------|-------|-------|",
    ]
    lines += [row(t) for t in ordered]
    lines.append("")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Render TRACKLIST.md from track-metadata.json.")
    parser.add_argument("--meta", default=META)
    parser.add_argument("--out", default=OUT)
    parser.add_argument("--refresh", action="store_true", help="re-resolve every artwork_url")
    parser.add_argument("--no-resolve", dest="resolve", action="store_false",
                        help="render only; never touch the network or track-metadata.json")
    args = parser.parse_args(argv)

    with open(args.meta, "r", encoding="utf-8") as handle:
        meta = json.load(handle)

    if args.resolve and enrich(meta, resolve=True, refresh=args.refresh):
        # Match the canonical writer: indent=2, ensure_ascii=False, sort_keys=False — but the
        # builder keeps each track/album's OWN keys alphabetical, so sort those (only) before
        # writing, leaving the top-level (schema/albums/tracks) and collection order untouched.
        sort_inner = lambda d: {k: d[k] for k in sorted(d)}
        out = dict(meta)
        out["tracks"] = {tid: sort_inner(t) for tid, t in meta["tracks"].items()}
        if "albums" in meta:
            out["albums"] = {aid: sort_inner(a) for aid, a in meta["albums"].items()}
        with open(args.meta, "w", encoding="utf-8") as handle:
            json.dump(out, handle, indent=2, ensure_ascii=False, sort_keys=False)
            handle.write("\n")
        print("updated %s with artwork_url / full_page_url" % args.meta, file=sys.stderr)

    with open(args.out, "w", encoding="utf-8") as handle:
        handle.write(render(meta))
    arted = sum(1 for t in meta["tracks"].values() if t.get("artwork_url"))
    print("wrote %s — %d tracks, %d with external artwork"
          % (args.out, len(meta["tracks"]), arted), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
