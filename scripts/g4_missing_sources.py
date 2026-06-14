"""G4 — inventory original-audio gaps + carry the sourcing leads (pass 1).

Cross-reference the identified tracklist (`track-metadata.json`) against the local
originals folder (`sources_local/`) and classify every track's source:

- **have**        — a real audio file is present (known audio extension, non-empty).
- **placeholder** — a stub stands in for the missing original: a `.null` file or any
                    zero-byte file (e.g. `014-… Stay (The Midnight Rockers Remix).null`,
                    the track Tim still wants to buy).
- **missing**     — no file at all for that track number.

For each gap we also surface the acquisition leads already recorded in the track
metadata (`fields`: discogs / spotify / release / year / allmusic), so G4 pass 2
(sourcing) starts from real pointers rather than a blank search.

This is inventory only — it never downloads or buys anything (Tim's call). Run:

    python3 scripts/g4_missing_sources.py --meta track-metadata.json \\
        --sources sources_local [--json out.json]
"""

import argparse
import json
import os
import re

# Extensions we treat as a usable original. (Matches streamalign.track_mix.find_original
# plus aac; container video like mp4/webm is NOT counted — those are not clean sources.)
AUDIO_EXTS = ("mp3", "flac", "m4a", "opus", "wav", "aif", "aiff", "aac")
# Lead fields worth carrying into sourcing, in priority order.
LEAD_FIELDS = ("discogs", "spotify", "release", "year", "allmusic", "bandcamp")


def _source_files_by_track(sources_dir):
    """{track_num: [filenames]} for every `NNN-*` file in the originals dir."""
    out = {}
    if not os.path.isdir(sources_dir):
        return out
    for fn in sorted(os.listdir(sources_dir)):
        if len(fn) >= 4 and fn[:3].isdigit() and fn[3] == "-":
            out.setdefault(int(fn[:3]), []).append(fn)
    return out


def _tokens(text):
    """Significant (len ≥ 4) lowercased alphanumeric words, for loose name matching."""
    return {w for w in re.split(r"[^a-z0-9]+", (text or "").lower()) if len(w) >= 4}


def _name_matches(filename, artist, title):
    """Does `filename` plausibly belong to this track (shares a significant word)?

    Files follow `NNN-Artist - Title.ext`, so the real original shares words with the
    track's artist/title. This rejects an unrelated file that merely shares the `NNN-`
    prefix (e.g. track 14's `014-LeRadioClub - Dj Mix Nagra.m4a`, a DJ mix, not
    Me'Shell NdegéOcello's "Stay") which would otherwise masquerade as the original.
    """
    stem = filename[4:] if len(filename) > 4 and filename[3] == "-" else filename
    stem = stem.rsplit(".", 1)[0]
    return bool(_tokens(stem) & (_tokens(artist) | _tokens(title)))


def _classify(filenames, sources_dir, artist=None, title=None):
    """Classify a track's candidate files → (status, chosen_file, ext, size_bytes).

    A usable original is an audio-extension file whose name matches the track's
    artist/title (largest such wins, so a full flac beats a clip). A `.null`/zero-byte
    stub is a placeholder. Audio files that share only the `NNN-` prefix but don't
    match the track are ignored (not counted as "have"). When there is a single audio
    file and no placeholder competing, it's trusted even without a name match (handles
    a cryptically-named original with no ambiguity to resolve).
    """
    audio, matched, placeholder = [], [], None
    for fn in filenames:
        try:
            size = os.path.getsize(os.path.join(sources_dir, fn))
        except OSError:
            size = 0
        ext = fn.rsplit(".", 1)[-1].lower() if "." in fn else ""
        if ext == "null" or size == 0:
            if placeholder is None or _name_matches(fn, artist, title):
                placeholder = (fn, ext, size)
        elif ext in AUDIO_EXTS:
            audio.append((fn, ext, size))
            if _name_matches(fn, artist, title):
                matched.append((fn, ext, size))
    if matched:
        return ("have",) + max(matched, key=lambda t: t[2])
    if len(audio) == 1 and placeholder is None:
        return ("have",) + audio[0]
    if placeholder:
        return ("placeholder",) + placeholder
    return "missing", None, None, None


def _leads(entry):
    fields = entry.get("fields") or {}
    return {k: fields[k] for k in LEAD_FIELDS if fields.get(k)}


def _is_identified(artist, title):
    """A gap is sourceable now only if the track is actually identified. Unidentified
    spans ("Mystery Track N", or no artist and no real title) need G3 first, not G4."""
    if title and title.strip().lower().startswith("mystery track"):
        return False
    return bool((artist or "").strip()) or bool((title or "").strip())


def inventory(meta_path, sources_dir):
    """Build the per-track source inventory. Returns {tracks: [...], summary: {...}}."""
    with open(meta_path, "r", encoding="utf-8") as handle:
        meta = json.load(handle)
    tracks = meta.get("tracks", meta)
    by_track = _source_files_by_track(sources_dir)
    rows = []
    for key in sorted(tracks, key=lambda k: int(k)):
        num = int(key)
        e = tracks[key]
        status, fn, ext, size = _classify(by_track.get(num, []), sources_dir,
                                          artist=e.get("artist"), title=e.get("title"))
        row = {
            "track": num,
            "artist": e.get("artist"),
            "title": e.get("title"),
            "status": status,
            "file": fn,
            "ext": ext,
            "size_bytes": size,
        }
        if status != "have":
            row["identified"] = _is_identified(e.get("artist"), e.get("title"))
            row["leads"] = _leads(e)
        rows.append(row)
    counts = {"have": 0, "placeholder": 0, "missing": 0}
    for r in rows:
        counts[r["status"]] += 1
    gaps = [r for r in rows if r["status"] != "have"]
    sourceable = [r["track"] for r in gaps if r["identified"]]
    needs_g3 = [r["track"] for r in gaps if not r["identified"]]
    return {"tracks": rows, "summary": counts,
            "gaps": [r["track"] for r in gaps],
            "sourceable": sourceable, "needs_g3": needs_g3}


def _fmt_size(n):
    if n is None:
        return "-"
    if n >= 1 << 20:
        return "%.1fM" % (n / (1 << 20))
    if n >= 1 << 10:
        return "%.0fK" % (n / (1 << 10))
    return "%dB" % n


def main(argv=None):
    p = argparse.ArgumentParser(prog="g4_missing_sources", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--meta", default="track-metadata.json")
    p.add_argument("--sources", default="sources_local")
    p.add_argument("--json", default=None, help="write full inventory JSON here")
    p.add_argument("--gaps-only", action="store_true", help="print only the gaps")
    args = p.parse_args(argv)
    # Fail fast on a bad path: a missing/typo'd --sources (or an unmounted originals
    # folder) would otherwise classify EVERY track as missing — a credible but false
    # "buy everything" worklist. `inventory()` stays permissive for library/test use.
    if not os.path.isfile(args.meta):
        p.error("metadata file not found: %s" % args.meta)
    if not os.path.isdir(args.sources):
        p.error("sources directory not found: %s (every track would look missing)"
                % args.sources)
    inv = inventory(args.meta, args.sources)
    print("%4s %-9s %7s  %-26s %s" % ("trk", "status", "size", "artist", "title"))
    for r in inv["tracks"]:
        if args.gaps_only and r["status"] == "have":
            continue
        print("%4d %-9s %7s  %-26.26s %-.40s"
              % (r["track"], r["status"], _fmt_size(r["size_bytes"]),
                 r["artist"] or "", r["title"] or ""))
    s = inv["summary"]
    print("\nhave=%d  placeholder=%d  missing=%d  (gaps=%d of %d)"
          % (s["have"], s["placeholder"], s["missing"],
             len(inv["gaps"]), len(inv["tracks"])))
    print("sourceable now (identified, need acquisition): %d %s"
          % (len(inv["sourceable"]), inv["sourceable"]))
    print("unidentified gaps (need G3 first): %d %s"
          % (len(inv["needs_g3"]), inv["needs_g3"]))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(inv, handle, indent=2)
        print("wrote %s" % args.json)


if __name__ == "__main__":
    main()
