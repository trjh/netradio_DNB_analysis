#!/usr/bin/env python3
"""Render the identified stream tracklist (``track-metadata.json``) as ``TRACKLIST.md``.

Pure renderer — no network. Covers + links come from ``track-metadata.json``, populated by the
enrichment scripts (``enrich_covers_links`` / ``enrich_musicbrainz`` / ``enrich_album_covers`` /
``enrich_mb_links``). **Album-first:** a track with an album inherits the album's cover and the
album's info links; the *listen* link prefers a track-specific link and falls back to the album.

Each row shows, in master-timeline order:
  * the cover (album cover, or the track's own for singles), linked to the listen page;
  * **Title** — Artist;
  * a **listen** badge — Apple Music → else Spotify → else YouTube;
  * **info** badges — Discogs and MusicBrainz (album-first);
  * the album name.
"""

import argparse
import html
import json
import os
import re
import sys
from datetime import datetime, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
META = os.path.join(REPO, "track-metadata.json")
OUT = os.path.join(REPO, "TRACKLIST.md")
LOGO_FALLBACK = "https://raw.githubusercontent.com/trjh/netradio_DNB_analysis/main/logo/logo.jpg"

# Listen services, in priority order: (fields key, label, badge colour, simple-icons slug).
LISTEN = [("apple", "AppleMusic", "FA243C", "applemusic"),
          ("apple-music", "AppleMusic", "FA243C", "applemusic"),
          ("spotify", "Spotify", "1DB954", "spotify"),
          ("youtube", "YouTube", "FF0000", "youtube")]
# Info/reference links (album-first): (fields key, label, colour, slug).
INFO = [("discogs", "Discogs", "333333", "discogs"),
        ("musicbrainz", "MusicBrainz", "BA478F", "musicbrainz")]


def _album(track, albums):
    return albums.get(track.get("album")) if track.get("album") else None


# Sentinel values that mean "checked, no such link exists" (e.g. the Net Radio promos carry
# discogs: "n/a"). They are truthy strings, so without this they'd render a badge linking to a
# bogus href like `](n/a)`. Treat them as absent so the badge is dropped entirely.
_NO_LINK = {"", "n/a", "na", "none", "null", "-", "tbd", "?"}


def _link(value):
    if not value:
        return None
    v = str(value).strip()
    return None if v.lower() in _NO_LINK else v


def listen_field(track, albums, key):
    """Track-specific link first (precise for the song), else the album's."""
    v = _link((track.get("fields") or {}).get(key))
    if v:
        return v
    alb = _album(track, albums)
    return _link((alb.get("fields") or {}).get(key)) if alb else None


def info_field(track, albums, key):
    """Album-first (release info belongs to the album), else the track's."""
    alb = _album(track, albums)
    av = _link((alb.get("fields") or {}).get(key)) if alb else None
    return av or _link((track.get("fields") or {}).get(key))


def cover_url(track, albums):
    # Album-first: a track on an album shows the album's cover when it has one, so every track on a
    # compilation (e.g. Ultra Mix Drum & Bass) shows the same release art rather than a grab-bag of
    # per-track single covers. Falls back to the track's own art (singles / album has no cover).
    alb = _album(track, albums)
    if alb and alb.get("artwork_url"):
        return alb["artwork_url"]
    return track.get("artwork_url")


def listen_link(track, albums):
    for key, label, colour, slug in LISTEN:
        url = listen_field(track, albums, key)
        if url:
            return label, colour, slug, url
    return None


def badge(label, colour, slug, url):
    src = "https://img.shields.io/badge/%s-%s" % (label, colour)
    if slug:
        src += "?logo=%s&logoColor=white" % slug
    img = "![%s](%s)" % (label, src)
    return "[%s](%s)" % (img, url) if url else img


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


def clean_album_name(album):
    """The album's display name, minus the discogs-style description. We show '(name) — (year)'
    on its own line, so drop any trailing format/year parenthetical ('… (1997, CD2, CD)') and a
    leading 'Artist - ' duplication (the artist is already on the title line)."""
    title = (album.get("title") or "").strip()
    name = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]\s*$", "", title).strip() or title
    artist = (album.get("artist") or "").strip()
    if artist:
        for dash in (" - ", " – ", " — "):
            prefix = artist + dash
            if name.lower().startswith(prefix.lower()):
                name = name[len(prefix):].strip()
                break
    return name


def album_year(album):
    """Release year: explicit fields.year, else a 19xx/20xx found in the title."""
    year = (album.get("fields") or {}).get("year")
    if year:
        return str(year)
    match = re.search(r"\b(?:19|20)\d{2}\b", album.get("title") or "")
    return match.group(0) if match else None


def row(track, albums):
    ll = listen_link(track, albums)
    page = ll[3] if ll else None
    art = cover_url(track, albums) or LOGO_FALLBACK
    cover_img = '<img src="%s" width="110" alt="">' % art
    cover = "[%s](%s)" % (cover_img, page) if page else cover_img

    # line 1: **Title — Artist**
    title = md_text(track.get("title") or "(untitled)")
    artist = md_text(track.get("artist"))
    head = ("**%s — %s**" % (title, artist)) if artist else ("**%s**" % title)

    # line 2: the links — listen first, then info; no ▶/ⓘ markers (the badges speak for themselves)
    links = [badge(*ll)] if ll else []
    links += [badge(lab, col, slug, info_field(track, albums, key))
              for key, lab, col, slug in INFO if info_field(track, albums, key)]
    links_line = " ".join(links) if links else "—"

    # line 3: _Album — Year_ (clean name; album-less singles get no third line)
    alb = _album(track, albums)
    cell = head + "<br>" + links_line
    if alb:
        name = md_text(clean_album_name(alb))
        year = album_year(alb)
        cell += "<br>_%s_" % (("%s — %s" % (name, year)) if year else name)

    # The "#" cell carries an anchor so a track can be DEEP-LINKED from anywhere -- e.g. a
    # track-ID post can point at the exact position in the mix:
    #   .../blob/main/TRACKLIST.md#t74
    # That is the whole reason the track number is surfaced at all: on its own it means nothing
    # to a reader, but it is the only stable handle this table has.
    num = track.get("number") or track.get("track") or ""
    anchor = ('<a id="t%s"></a>**%s**' % (num, num)) if num else ""
    return "| %s | %s | %s | %s |" % (
        anchor, fmt_time(track.get("master_begin_seconds")), cover, cell)


def render(meta):
    tracks, albums = meta["tracks"], meta.get("albums", {})
    for key, entry in tracks.items():          # the dict key IS the track number
        entry.setdefault("number", key)
    ordered = sorted(tracks.values(),
                     key=lambda t: t.get("master_begin_seconds") if
                     t.get("master_begin_seconds") is not None else 1e18)
    arted = sum(1 for t in ordered if cover_url(t, albums))
    listenable = sum(1 for t in ordered if listen_link(t, albums))
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# Netradio DNB — Tracklist",
        "",
        "> The identified tracks of the mix, in master-timeline order. Auto-generated from "
        "`track-metadata.json` by `scripts/render_tracklist.py` — do not edit by hand. "
        "Each track shows a listen link (Apple Music / Spotify / YouTube) and reference links "
        "(Discogs / MusicBrainz). Cover art + links are album-first (a track inherits its album's). "
          "**Each track number is a link target** — `TRACKLIST.md#t74` jumps straight to track 74, "
          "so a track-ID post can point at the exact position in the mix.",
        "",
        "**%d tracks** · %d with cover art · %d with a listen link · generated %s"
        % (len(ordered), arted, listenable, now),
        "",
        "| # | Time | Cover | Track |",
        "|---|------|-------|-------|",
    ]
    lines += [row(t, albums) for t in ordered]
    lines.append("")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Render TRACKLIST.md from track-metadata.json (pure).")
    parser.add_argument("--meta", default=META)
    parser.add_argument("--out", default=OUT)
    # the renderer is now always pure (covers/links come from the enrichment scripts); these
    # are accepted-and-ignored so existing callers (tracklist_sync.sh, the Makefile) don't break.
    parser.add_argument("--no-resolve", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--refresh", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    with open(args.meta, "r", encoding="utf-8") as handle:
        meta = json.load(handle)
    with open(args.out, "w", encoding="utf-8") as handle:
        handle.write(render(meta))
    print("wrote %s — %d tracks" % (args.out, len(meta["tracks"])), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
