#!/usr/bin/env python3
"""Merge the Google Sheet "track sources" tab into track-metadata.json.

The sheet is a *view* (per the authority model); this brings its accumulated
per-track metadata into the repo's authoritative track-metadata.json:

- `apple` / `spotify` / `youtube` — streaming links (URLs only; "n/a"/blank skipped)
- `release` — the release/album name from the Discogs column (page-title strings,
  lightly cleaned), distinct from a `discogs` release URL

Track number/title/artist/master/source_files stay label-authoritative and are NOT
changed; title/artist mismatches between the sheet and the labels are reported.

Usage: python3 scripts/merge_track_sources.py [--csv data/track-sources.csv] [--dry-run]
"""
import argparse
import csv
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import build_track_metadata as gen  # noqa: E402  (reuse save() / numeric ordering)

OUT = REPO / "track-metadata.json"
DEFAULT_CSV = REPO / "data" / "track-sources.csv"


def is_url(value):
    return bool(re.match(r"https?://", (value or "").strip(), re.I))


def clean_release(value):
    value = (value or "").strip()
    if not value or value.lower() == "n/a":
        return ""
    value = value.split(" | ")[0]                       # drop "| Releases | Discogs"
    value = re.sub(r"\s*[-–]\s*Discogs.*$", "", value, flags=re.I)  # drop "- Discogs…"
    return value.strip()


def load_json(path):
    import json
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", default=str(DEFAULT_CSV))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    data = load_json(OUT)
    tracks = data["tracks"]
    rows = list(csv.DictReader(open(args.csv, encoding="utf-8-sig")))

    added, mismatches, missing = {}, [], []
    for row in rows:
        number = (row.get("Track Number") or "").strip()
        if not number:
            continue
        entry = tracks.get(number)
        if entry is None:
            missing.append(number)
            continue
        fields = entry.setdefault("fields", {})

        updates = {}
        for col, key in (("Apple", "apple"), ("Spotify", "spotify"), ("YouTube", "youtube")):
            value = (row.get(col) or "").strip()
            if is_url(value):
                updates[key] = value
        release = clean_release(row.get("Discogs"))
        if release:
            updates["release"] = release

        for key, value in updates.items():
            if fields.get(key) != value:
                fields[key] = value
                added.setdefault(key, []).append(number)

        # Report (don't apply) title/artist divergence from the labels.
        sheet_artist = (row.get("Track Artist") or "").strip()
        sheet_title = (row.get("Track Name") or "").strip()
        if sheet_artist and entry.get("artist") and sheet_artist != entry["artist"]:
            mismatches.append((number, "artist", entry["artist"], sheet_artist))
        if sheet_title and entry.get("title") and sheet_title.lower() != (entry["title"] or "").lower():
            mismatches.append((number, "title", entry["title"], sheet_title))

    for key, nums in sorted(added.items()):
        print("  %-8s set on %d tracks: %s" % (key, len(nums), ",".join(nums)))
    if missing:
        print("  WARN sheet rows with no matching track entry: %s" % missing)
    for n, field, have, sheet in mismatches:
        print("  note track %s %s: labels=%r sheet=%r (kept labels)" % (n, field, have, sheet))

    if args.dry_run:
        print("dry-run: nothing written")
        return 0
    gen.save(data, str(OUT))
    print("wrote %s" % OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
