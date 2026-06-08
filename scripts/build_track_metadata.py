#!/usr/bin/env python3
"""Generate the authoritative track-metadata.json from the Audacity labels.

This repo is the source of record (see the player repo's DESIGN.md). The Audacity
label exports own the master timeline and Track Title/Artist via
`startNNN: ID: Artist - Title` rows. This script reads those identities and their
resolved master positions and writes `track-metadata.json` at the repo root.

Curated metadata that is NOT in the labels (year, artwork filename, Discogs/
AllMusic links, blurb, and any manual title/artist override) is carried forward
from an optional `--seed` JSON (same schema), so re-generating never loses it.

Self-contained (stdlib only). Run from anywhere:
    python3 scripts/build_track_metadata.py [--seed path] [--dry-run]
"""
import argparse
import json
import os
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LABELS_DIR = REPO / "labels"
OUT_PATH = REPO / "track-metadata.json"
SCHEMA = "netradio.track-metadata.v1"


def parse_float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def normalize_audio_filename(name):
    name = os.path.basename((name or "").strip())
    if not name:
        return None
    stem, ext = os.path.splitext(name)
    if ext.lower() in (".wav", ".au", ".mp3"):
        return stem + ".mp3"
    return name + ".mp3"


def read_label_rows():
    rows = []
    if not LABELS_DIR.is_dir():
        return rows
    for path in sorted(LABELS_DIR.glob("*.labels.tsv")):
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                parts = line.rstrip("\n").split("\t", 2)
                if len(parts) < 3:
                    continue
                seconds = parse_float(parts[0], None)
                if seconds is None:
                    continue
                rows.append({"path": str(path), "seconds": seconds, "text": parts[2].strip()})
    return rows


def parse_file_timeline(label_rows):
    """File master windows from the label sync chain (ported from the player)."""
    starts, ends, current_by_path = {}, {}, {}
    for row in label_rows:
        m = re.search(r"\bfile(?:\s+start)?\s+sync:\s*([^\s]+)\s+(-?\d+(?:\.\d+)?)", row["text"], re.I)
        if not m or abs(row["seconds"]) > 0.01:
            continue
        filename = normalize_audio_filename(m.group(1))
        master_start = parse_float(m.group(2), None)
        if filename and master_start is not None:
            starts[filename] = master_start
            current_by_path.setdefault(row["path"], filename)

    for row in label_rows:
        m = re.search(r"\bfile start\s+([^\s]+\.(?:wav|au|mp3))\b", row["text"], re.I)
        if not m or "file start sync" in row["text"].lower():
            continue
        filename = normalize_audio_filename(m.group(1))
        if not filename or filename in starts:
            continue
        owner = current_by_path.get(row["path"])
        if owner and owner in starts:
            starts[filename] = starts[owner] + row["seconds"]

    for _ in range(6):
        changed = False
        for row in label_rows:
            current = current_by_path.get(row["path"])
            if not current or current not in starts:
                continue
            current_master = starts[current]
            text = row["text"]
            m = re.search(r"\bfile start sync:\s*([^\s]+)\s+(?:-?\d+(?:\.\d+)?|MARK|NEEDMARKINOWNFILE)", text, re.I)
            if m:
                filename = normalize_audio_filename(m.group(1))
                if filename != current:
                    master_start = current_master + row["seconds"]
                    old = starts.get(filename)
                    if filename and (old is None or abs(old - master_start) > 0.001):
                        starts[filename] = master_start
                        changed = True
            m = re.search(r"\bfile end:?\s+([^\s]+\.(?:wav|au|mp3))\b", text, re.I)
            if m:
                filename = normalize_audio_filename(m.group(1))
                master_end = current_master + row["seconds"]
                old = ends.get(filename)
                if filename and (old is None or master_end > old):
                    ends[filename] = master_end
                    changed = True
            elif re.search(r"\bfile end:\s*%s\b" % re.escape(current[:-4]), text, re.I):
                master_end = current_master + row["seconds"]
                old = ends.get(current)
                if old is None or master_end > old:
                    ends[current] = master_end
                    changed = True
        if not changed:
            break
    return {name: {"master_start_seconds": starts[name], "master_end_seconds": ends.get(name)}
            for name in starts}


LABEL_ID_RE = re.compile(r"\bstart0*(\d+)\s*:\s*ID:\s*(.+)$", re.I)


def parse_label_track_id_text(text):
    """`startNNN: ID: Artist - Title` -> (number, artist, title), else None.

    Splits artist/title on the first ' - ' (spaces) so hyphenated names stay
    intact, and strips stray audio-file suffixes (".mp3"/".wav"/".au").
    """
    match = LABEL_ID_RE.search((text or "").strip())
    if not match:
        return None
    number = int(match.group(1))
    body = match.group(2).strip()
    if " - " not in body:
        return None
    artist, title = body.split(" - ", 1)
    title = re.sub(r"\.(?:mp3|wav|au)$", "", title.strip(), flags=re.I)
    return number, artist.strip(), title.strip()


def label_stem(path):
    return re.sub(r"\.labels\.tsv$", "", os.path.basename(path), flags=re.I)


def owning_file_for_label_path(path):
    # Canonical key for timeline matching only (the label sync rows / timeline use
    # this normalized form internally). NOT written to the metadata.
    return normalize_audio_filename(label_stem(path))


def original_audio_name(stem):
    """The ORIGINAL capture filename for a label stem (.wav, or .au for 14Nov).

    Track metadata in this repo is about the original files + tracks; the player
    keeps its own original->transcoded(.mp3) mapping separately.
    """
    if "14Nov" in stem:
        if "." in stem:
            tail = stem.split(".", 1)[1]
            stem = ("d-" + tail) if tail.startswith("14Nov") else tail
        return stem + ".au"
    return stem + ".wav"


def parse_label_track_ids():
    label_rows = read_label_rows()
    timeline = parse_file_timeline(label_rows)
    # File master windows keyed by ORIGINAL capture filename (the .mp3 form is an
    # internal artifact). Used to find every capture a track appears in.
    windows = []
    for mp3_key, info in timeline.items():
        stem = mp3_key[:-4] if mp3_key.lower().endswith(".mp3") else mp3_key
        windows.append((original_audio_name(stem),
                        info.get("master_start_seconds"), info.get("master_end_seconds")))

    def files_for(master, owning_original):
        """All capture files whose master window covers this track, owning first/included."""
        hits = []
        if master is not None:
            for name, start, end in windows:
                if start is None or end is None:
                    continue
                if start - 0.05 <= master <= end + 0.05:
                    hits.append((start, name))
        if owning_original not in [n for _, n in hits]:
            own_start = next((s for n, s, e in windows if n == owning_original and s is not None),
                             master if master is not None else 0.0)
            hits.append((own_start, owning_original))
        seen, ordered = set(), []
        for _start, name in sorted(hits):
            if name not in seen:
                seen.add(name)
                ordered.append(name)
        return ordered

    result, conflicts = {}, []
    for row in label_rows:
        parsed = parse_label_track_id_text(row["text"])
        if not parsed:
            continue
        number, artist, title = parsed
        owning = owning_file_for_label_path(row["path"])
        owning_original = original_audio_name(label_stem(row["path"]))
        start = (timeline.get(owning) or {}).get("master_start_seconds")
        master = start + row["seconds"] if start is not None else None
        record = {"track_number": number, "track_artist": artist, "track_name": title,
                  "source_files": files_for(master, owning_original), "master_seconds": master}
        existing = result.get(number)
        if existing is None:
            result[number] = record
        else:
            if (existing["track_artist"], existing["track_name"]) != (artist, title):
                conflicts.append({"track_number": number, "kept": [existing["track_artist"], existing["track_name"]],
                                  "also": [artist, title], "file": owning})
            if existing["master_seconds"] is None and master is not None:
                result[number] = record
    return result, conflicts


def _track_sort_key(key):
    try:
        return (0, int(key))
    except (TypeError, ValueError):
        return (1, str(key))


def save(data, path):
    tracks = data.get("tracks", {})
    ordered = {}
    for key in sorted(tracks, key=_track_sort_key):
        entry = tracks[key]
        ordered[key] = {k: (dict(sorted(entry[k].items())) if isinstance(entry[k], dict) else entry[k])
                        for k in sorted(entry)}
    out = {"schema": data.get("schema", SCHEMA), "tracks": ordered}
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(out, handle, indent=2, ensure_ascii=False, sort_keys=False)
        handle.write("\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", help="JSON to carry curated fields forward from")
    parser.add_argument("--out", default=str(OUT_PATH))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    data = {"schema": SCHEMA, "tracks": {}}
    if args.seed and os.path.exists(args.seed):
        with open(args.seed, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    tracks = data.setdefault("tracks", {})

    ids, conflicts = parse_label_track_ids()
    added, filled, kept, no_master = [], 0, [], []
    for number in sorted(ids):
        label = ids[number]
        key = str(number)
        entry = tracks.get(key)
        if entry is None:
            entry = {"title": None, "artist": None, "artwork": None, "use_logo": False, "fields": {}}
            tracks[key] = entry
            added.append(number)
        for field, label_key in (("title", "track_name"), ("artist", "track_artist")):
            if not entry.get(field):
                entry[field] = label[label_key]
                filled += 1
            elif entry[field] != label[label_key]:
                kept.append((number, field, entry[field], label[label_key]))
        entry["source_files"] = label["source_files"]  # original capture files (.wav/.au)
        entry.pop("source_file", None)  # supersede the earlier singular field
        if label["master_seconds"] is not None:
            entry["master_seconds"] = round(label["master_seconds"], 3)
        else:
            no_master.append(number)

    print("identified tracks: %d  (added %d, filled title/artist %d)" % (len(ids), len(added), filled))
    if no_master:
        print("  WARN no master position: %s" % sorted(no_master))
    for n, f, have, lab in kept:
        print("  KEPT override track %s %s=%r (label %r)" % (n, f, have, lab))
    for c in conflicts:
        print("  CONFLICT track %s: %s vs %s (%s)" % (c["track_number"], c["kept"], c["also"], c["file"]))

    if args.dry_run:
        print("dry-run: nothing written")
        return 0
    save(data, args.out)
    print("wrote %s (%d entries)" % (args.out, len(tracks)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
