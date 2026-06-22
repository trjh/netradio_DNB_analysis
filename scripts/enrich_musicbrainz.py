#!/usr/bin/env python3
"""Second-pass enrichment for tracks still missing cover art: MusicBrainz + Cover Art Archive.

Free, no token. For every track that still has no `artwork_url`, search MusicBrainz for the
recording (strict artist+title match, reusing find_streaming_links' matchers), walk its releases,
and take the first release that has art in the Cover Art Archive — storing `artwork_url`
(front-500) and `artwork_hires` (front-1200). "Rather blank than wrong"; additive only.

Rate-limited to ~1 req/s with a descriptive User-Agent (MusicBrainz requirement). Run AFTER
enrich_covers_links.py (the Apple/Discogs pass).

    python3 scripts/enrich_musicbrainz.py            # dry-run report
    python3 scripts/enrich_musicbrainz.py --apply     # write track-metadata.json
"""

import argparse
import json
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import find_streaming_links as fsl                 # noqa: E402
from enrich_covers_links import write_meta         # noqa: E402  — same format-preserving writer
OUT = REPO / "track-metadata.json"

UA = "netradio-metadata/1.0 (https://github.com/trjh/netradio_DNB_analysis; enrichment)"


def curl(url, accept_json=True, head=False, timeout=12):
    cmd = ["curl", "-4", "-sS", "--max-time", str(timeout), "-A", UA]
    if head:
        cmd += ["-o", "/dev/null", "-w", "%{http_code}", "-I", "-L"]
    cmd.append(url)
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5).stdout
        return out if head else json.loads(out)
    except Exception:   # noqa: BLE001
        return None


def mb_recordings(artist, title):
    q = 'artist:"%s" AND recording:"%s"' % (artist.replace('"', ""), title.replace('"', ""))
    u = "https://musicbrainz.org/ws/2/recording?" + urllib.parse.urlencode(
        {"query": q, "fmt": "json", "limit": "8"})
    time.sleep(1.1)   # MusicBrainz: ~1 req/s
    return (curl(u) or {}).get("recordings", [])


def caa_has_front(mbid):
    """True if the Cover Art Archive has front art for this release MBID."""
    time.sleep(0.4)
    code = curl("https://coverartarchive.org/release/%s/front-250" % mbid, head=True, timeout=12)
    return code and code.strip().endswith("200")


def find_cover(artist, title):
    """(front-500, front-1200) from the first strictly-matched MB recording whose release has
    Cover Art Archive art, or (None, None)."""
    for rec in mb_recordings(artist, title):
        ra = " ".join(c.get("name", "") for c in (
            x.get("artist") for x in rec.get("artist-credit", []) if isinstance(x, dict)) if c) \
            or rec.get("artist-credit-phrase", "")
        if not (fsl.artist_ok(artist, ra) and fsl.title_ok(title, rec.get("title", ""))):
            continue
        for rel in rec.get("releases", [])[:6]:
            mbid = rel.get("id")
            if mbid and caa_has_front(mbid):
                base = "https://coverartarchive.org/release/%s/front-" % mbid
                return base + "500", base + "1200"
    return None, None


def main(argv=None):
    ap = argparse.ArgumentParser(description="MusicBrainz/CAA cover fallback for gap tracks.")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args(argv)

    meta = json.load(open(OUT, encoding="utf-8"))
    tracks = meta["tracks"]
    gaps = [(tid, t) for tid, t in tracks.items()
            if not t.get("artwork_url") and t.get("artist") and t.get("title")
            and "Mystery Track" not in (t.get("title") or "")]
    if args.limit:
        gaps = gaps[: args.limit]
    print("checking %d cover-less tracks via MusicBrainz/CAA…\n" % len(gaps))

    found = 0
    for tid, t in gaps:
        norm, hi = find_cover(t["artist"], t["title"])
        tag = "%-3s %s — %s" % (tid, t["artist"], t["title"])
        if norm:
            t["artwork_url"], t["artwork_hires"] = norm, hi
            found += 1
            print("  +cover %s" % tag)
        else:
            print("  ----- %s" % tag)

    print("\n=== %d/%d covers found via MusicBrainz/CAA ===" % (found, len(gaps)))
    if args.apply and found:
        write_meta(meta)
        print("wrote %s" % OUT)
    elif not args.apply:
        print("(dry-run — re-run with --apply)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
