#!/usr/bin/env python3
"""Render the identified stream tracklist (``track-metadata.json``) as ``TRACKLIST.md``.

This is the **public** tracklist of the mix: each identified track in master-timeline order,
with cover artwork and a clickable service logo, **both linking to the track's full page**
(its Apple Music / Spotify / YouTube release). Artwork URLs are **external** — resolved from
each track's release link (YouTube → ytimg; Apple/Spotify → the page's ``og:image``) — so the
markdown renders on GitHub without committing any image files. Resolved URLs are cached in
``tracklist_artwork.json`` so reruns are offline (pass ``--refresh`` to re-resolve).

Source of truth is the canonical ``track-metadata.json`` in this (analysis) repo; the player
mirrors it. See the Makefiles for the cross-repo sync.
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
CACHE = os.path.join(REPO, "tracklist_artwork.json")
OUT = os.path.join(REPO, "TRACKLIST.md")

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) netradio-tracklist/1.0"
LOGO_FALLBACK = "https://raw.githubusercontent.com/trjh/netradio_DNB_analysis/main/logo/logo.jpg"

# A track/album link field -> (service key, display label, badge colour, simple-icons slug).
SERVICES = {
    "apple":       ("apple",   "AppleMusic", "FA243C", "applemusic"),
    "apple-music": ("apple",   "AppleMusic", "FA243C", "applemusic"),
    "spotify":     ("spotify", "Spotify",    "1DB954", "spotify"),
    "youtube":     ("youtube", "YouTube",    "FF0000", "youtube"),
}
LINK_PREFERENCE = ("apple", "apple-music", "spotify", "youtube")

_OG = re.compile(
    r'<meta[^>]+(?:property|name)=["\']og:image(?::secure_url)?["\'][^>]+content=["\']([^"\']+)', re.I)
_OG_REV = re.compile(
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']og:image', re.I)
_YT = re.compile(r"(?:v=|/vi/|youtu\.be/|/embed/|/shorts/)([A-Za-z0-9_-]{11})")


def pick_link(track, albums):
    """(service_key, label, colour, slug, url) for a track's best release link, or None.
    Prefers a track-level link; falls back to the track's album's link."""
    fields = track.get("fields") or {}
    for key in LINK_PREFERENCE:
        if fields.get(key):
            return (*SERVICES[key], fields[key])
    alb = albums.get(track.get("album")) if track.get("album") else None
    if alb:
        afields = alb.get("fields") or {}
        for key in LINK_PREFERENCE:
            if afields.get(key):
                return (*SERVICES[key], afields[key])
    return None


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


def build_cache(tracks, albums, old_cache, refresh=False):
    """{id: {full_page_url, service, label, colour, slug, artwork_url}} — resolves missing
    artwork over the network CONCURRENTLY (bounded pool), reusing `old_cache` unless refresh."""
    cache, to_resolve = {}, {}
    for tid, track in tracks.items():
        link = pick_link(track, albums)
        if not link:
            continue
        service, label, colour, slug, url = link
        prev = old_cache.get(tid) or {}
        reuse = (not refresh) and prev.get("full_page_url") == url and "artwork_url" in prev
        cache[tid] = {"full_page_url": url, "service": service, "label": label,
                      "colour": colour, "slug": slug,
                      "artwork_url": prev.get("artwork_url") if reuse else None}
        if not reuse:
            to_resolve[tid] = (service, url)
    if to_resolve:
        def work(item):
            tid, (service, url) = item
            return tid, resolve_artwork(service, url)
        with ThreadPoolExecutor(max_workers=12) as pool:
            for tid, art in pool.map(work, list(to_resolve.items())):
                cache[tid]["artwork_url"] = art
    return cache


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


def badge(label, colour, slug, page):
    url = "https://img.shields.io/badge/%s-%s?logo=%s&logoColor=white" % (label, colour, slug)
    img = "![%s](%s)" % (label, url)
    return "[%s](%s)" % (img, page) if page else img


def row(track, info):
    page = info.get("full_page_url") if info else None
    art = (info.get("artwork_url") if info else None) or LOGO_FALLBACK
    cover_img = '<img src="%s" width="110" alt="">' % art
    cover = "[%s](%s)" % (cover_img, page) if page else cover_img

    title = md_text(track.get("title") or "(untitled)")
    artist = md_text(track.get("artist"))
    album = md_text((track.get("album") or "").replace("-", " ")) if track.get("album") else ""
    label = ("**%s**" % title) + (" — " + artist if artist else "")
    from_cell = ("from " + badge(info["label"], info["colour"], info["slug"], page)) if info \
        else "_(no release link)_"
    if album:
        from_cell += " · _%s_" % album
    when = fmt_time(track.get("master_begin_seconds"))
    return "| %s | %s | %s<br>%s |" % (when, cover, label, from_cell)


def render(meta, cache):
    tracks = meta["tracks"]
    ordered = sorted(tracks.items(),
                     key=lambda kv: (kv[1].get("master_begin_seconds") if
                                     kv[1].get("master_begin_seconds") is not None else 1e18))
    linked = sum(1 for _, t in ordered if cache.get(_) and cache[_].get("artwork_url"))
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# Netradio DNB — Tracklist",
        "",
        "> The identified tracks of the mix, in master-timeline order. Auto-generated from "
        "`track-metadata.json` by `scripts/render_tracklist.py` — do not edit by hand. "
        "Cover art and the service logo link to each track's release page.",
        "",
        "**%d tracks** · %d with cover art · generated %s" % (len(ordered), linked, now),
        "",
        "| Time | Cover | Track |",
        "|------|-------|-------|",
    ]
    lines += [row(t, cache.get(tid)) for tid, t in ordered]
    lines.append("")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Render TRACKLIST.md from track-metadata.json.")
    parser.add_argument("--meta", default=META)
    parser.add_argument("--out", default=OUT)
    parser.add_argument("--cache", default=CACHE)
    parser.add_argument("--refresh", action="store_true", help="re-resolve all artwork URLs")
    args = parser.parse_args(argv)

    with open(args.meta, "r", encoding="utf-8") as handle:
        meta = json.load(handle)
    try:
        with open(args.cache, "r", encoding="utf-8") as handle:
            old_cache = json.load(handle)
    except (OSError, ValueError):
        old_cache = {}

    cache = build_cache(meta["tracks"], meta.get("albums") or {}, old_cache, refresh=args.refresh)
    with open(args.cache, "w", encoding="utf-8") as handle:
        json.dump(cache, handle, indent=2, ensure_ascii=False, sort_keys=True)
        handle.write("\n")
    with open(args.out, "w", encoding="utf-8") as handle:
        handle.write(render(meta, cache))

    got = sum(1 for v in cache.values() if v.get("artwork_url"))
    print("wrote %s — %d tracks, %d linked, %d with external artwork"
          % (args.out, len(meta["tracks"]), len(cache), got), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
