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
SCHEMA = "netradio.track-metadata.v2"


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
                # parts[1] is the Audacity region END column; for point labels it
                # equals parts[0], for region labels (e.g. a `startNNN:` whose span
                # runs to the track's mix-end) it is later.
                rows.append({"path": str(path), "seconds": seconds,
                             "end": parse_float(parts[1], None), "text": parts[2].strip()})
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
    timeline = {name: {"master_start_seconds": starts[name], "master_end_seconds": ends.get(name)}
                for name in starts}
    return timeline, current_by_path


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


def original_from_mp3(mp3_name):
    """Original capture filename for a normalized .mp3 timeline key."""
    stem = mp3_name[:-4] if mp3_name.lower().endswith(".mp3") else mp3_name
    return original_audio_name(stem)


ID_ROW_RE = re.compile(r"^ID0*(\d+):", re.I)  # bare `IDNNN:` continuation marker

# Track-end markers (all anchored at the start of the label text so that
# forward-reference notes like `note d122-144: mix end: 027` — which describe a
# DIFFERENT file — are excluded; only a row that IS the end marker matches).
ORIG_END_RE = re.compile(r"^orig0*(\d+)\s+end:", re.I)     # `origNNN end: A`
MIX_END_RE = re.compile(r"^mix\s+end:\s*0*(\d+)\b", re.I)  # `mix end: NNN`


def compute_track_ends(label_rows, timeline, current_by_path):
    """Per-track master END time, derived from the labels only.

    A track's end is the LATEST master timestamp of any of its end markers:
      - `origNNN end: <x>`  — end of original source NNN (a point label);
      - `mix end: NNN`       — where track NNN's mix finished (a point label);
      - `startNNN: ID: …`    — when that start row is a *region* label, its end
                               column (col 1) is the track's mix-end directly.
    Each marker's local offset is mapped to master time through the owning file's
    window, exactly like the start rows. Returns {number: master_end_seconds}.
    """
    ends = {}

    def master_of(path, local):
        owning = current_by_path.get(path) or owning_file_for_label_path(path)
        start = (timeline.get(owning) or {}).get("master_start_seconds")
        return start + local if start is not None else None

    for row in label_rows:
        text = row["text"]
        markers = []  # (track_number, local_offset_seconds)
        m = ORIG_END_RE.match(text)
        if m:
            markers.append((int(m.group(1)), row["seconds"]))
        m = MIX_END_RE.match(text)
        if m:
            markers.append((int(m.group(1)), row["seconds"]))
        start = parse_label_track_id_text(text)
        region_end = row.get("end")
        if start and region_end is not None and region_end > row["seconds"] + 0.001:
            markers.append((start[0], region_end))
        for number, local in markers:
            master = master_of(row["path"], local)
            if master is None:
                continue
            if number not in ends or master > ends[number]:
                ends[number] = master
    return ends


# --- First-pass (tracklist-2017) tail -------------------------------------------
# labels/remainder.tsv is produced by scripts/remainderlist.pl from the ~2017
# tracklist-2017.txt and covers the part of the broadcast the second-pass Audacity
# labelling hasn't reached yet (tracks ~67-91, incl. the "Mystery Track N" segments).
# Two things differ from the real *.labels.tsv rows: the timestamp is an ABSOLUTE
# master second (NOT a local offset to resolve through the file-sync timeline), and
# the body is `Title / Artist` (slash, artist LAST) instead of `Artist - Title`.
REMAINDER_FILE = LABELS_DIR / "remainder.tsv"
MYSTERY_RE = re.compile(r"^\s*Mystery Track\b", re.I)


def parse_remainder_id_text(text):
    """`start0NN: ID: Title / Artist` (remainder.tsv) -> (number, artist, title).

    Splits on the LAST '/' (artist last); artist may be empty/absent (the Mystery
    Tracks have no '/'). Returns None if it isn't a start-ID row. Caveat: a title
    containing a literal '/' would mis-split, but the first-pass tail has none.
    """
    match = LABEL_ID_RE.search((text or "").strip())
    if not match:
        return None
    number = int(match.group(1))
    body = match.group(2).strip()
    if "/" in body:
        title, _, artist = body.rpartition("/")
        title, artist = title.strip(), (artist.strip() or None)
    else:
        title, artist = body, None
    return number, artist, title


def parse_remainder():
    """Rough first-pass records from labels/remainder.tsv (empty list if absent)."""
    records = []
    if not REMAINDER_FILE.is_file():
        return records
    with open(REMAINDER_FILE, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            parts = line.rstrip("\n").split("\t", 2)
            if len(parts) < 3:
                continue
            master = parse_float(parts[0], None)
            parsed = parse_remainder_id_text(parts[2])
            if master is None or not parsed:
                continue
            number, artist, title = parsed
            records.append({"track_number": number, "track_artist": artist,
                            "track_name": title, "master_begin_seconds": master,
                            "is_mystery": bool(MYSTERY_RE.match(title or ""))})
    return records


def drop_labelled(rough_records, frontier):
    """Drop rough rows the second-pass labels already cover (master at/before the
    labelling frontier), so the player updates tidily as labelling extends into the
    tail. Assumes the second pass advances contiguously from the broadcast start —
    it does, the timeline is reconstructed continuously."""
    return [r for r in rough_records if r["master_begin_seconds"] > frontier]


# "Try this first" capture file per master-time span for the unlabelled tail, taken
# from the tracklist-2017 "definitive track listing" file START/END markers (each a
# clean ~20-min primary-file block; the filename minute-range is otherwise only a
# hint). Rough tracks aren't labelled yet, so they carry no overlap-derived
# source_files — this is the file the player should reach for to play them.
# [primary_file, span_end_master_seconds]; the span starts where the previous ends.
TAIL_PRIMARY = [
    ("d336-355.wav", 21074.552),   # ...–351:14.552
    ("d356-375.wav", 22274.552),   # 351:14.552–371:14.552
    ("d376-395.wav", 23474.552),   # 371:14.552–391:14.552
    ("d396-415.wav", 24674.552),   # 391:14.552–411:14.552
    ("d416-435.wav", 25874.552),   # 411:14.552–431:14.552
    ("d436-455.wav", 27074.552),   # 431:14.552–451:14.552
    ("d456-470.wav", 27950.455),   # 451:14.552–465:50.455
]


def tail_primary_for(master_seconds):
    """The primary capture file whose master span contains master_seconds, or None."""
    if master_seconds is None:
        return None
    for name, span_end in TAIL_PRIMARY:
        if master_seconds < span_end:
            return name
    return None


def _anchor_word(word):
    word = re.sub(r"[^0-9A-Za-z]", "", word or "")
    return word[:1].upper() + word[1:] if word else ""


def anchor_id(title, artist, used):
    """Stable per-track id: first title word + first artist word, each capitalised
    (e.g. 'Promo1Net', 'YoureJamie'). On collision, append further words (remaining
    title words, then remaining artist words) until unambiguous; a numeric suffix is
    the last resort. `used` is the set of ids already taken (mutated). The id is a
    function of the LABEL/rough title+artist only, so it is stable across regenerates
    and lets curated metadata follow a track whose number/timestamp later changes."""
    title_words = [w for w in (_anchor_word(x) for x in (title or "").split()) if w]
    artist_words = [w for w in (_anchor_word(x) for x in (artist or "").split()) if w]
    parts = ([title_words[0]] if title_words else []) + ([artist_words[0]] if artist_words else [])
    extra = title_words[1:] + artist_words[1:]
    anchor = "".join(parts)
    i = 0
    while (not anchor or anchor in used) and i < len(extra):
        parts.append(extra[i])
        anchor = "".join(parts)
        i += 1
    if not anchor:
        anchor = "Track"
    if anchor in used:
        base, n = anchor, 2
        while "%s%d" % (base, n) in used:
            n += 1
        anchor = "%s%d" % (base, n)
    used.add(anchor)
    return anchor


def parse_label_track_ids():
    label_rows = read_label_rows()
    timeline, current_by_path = parse_file_timeline(label_rows)
    track_ends = compute_track_ends(label_rows, timeline, current_by_path)

    def owning_original_for(path):
        owning = current_by_path.get(path) or owning_file_for_label_path(path)
        return original_from_mp3(owning), owning

    # File master windows keyed by ORIGINAL capture filename (the .mp3 form is an
    # internal artifact).  start_of maps original name -> master_start.
    windows, start_of = [], {}
    for mp3_key, info in timeline.items():
        name = original_from_mp3(mp3_key)
        s, e = info.get("master_start_seconds"), info.get("master_end_seconds")
        windows.append((name, s, e))
        start_of[name] = s

    # Every label file that explicitly names a track — both `startNNN: ID:` and the
    # bare `IDNNN:` continuation rows — is a capture the track appears in. (The
    # `note X: IDNNN:` forward-references are about file X, not this one, and are
    # excluded because ID_ROW_RE anchors at the start of the text.)
    appearances = {}
    for row in label_rows:
        id_match = ID_ROW_RE.match(row["text"])
        start_match = parse_label_track_id_text(row["text"])
        if id_match:
            number = int(id_match.group(1))
        elif start_match:
            number = start_match[0]
        else:
            continue
        appearances.setdefault(number, set()).add(owning_original_for(row["path"])[0])

    # Pass 1: the canonical start row per track (number/title/artist/master).
    result, conflicts = {}, []
    for row in label_rows:
        parsed = parse_label_track_id_text(row["text"])
        if not parsed:
            continue
        number, artist, title = parsed
        owning_original, owning = owning_original_for(row["path"])
        start = (timeline.get(owning) or {}).get("master_start_seconds")
        master = start + row["seconds"] if start is not None else None
        record = {"track_number": number, "track_artist": artist, "track_name": title,
                  "owning_original": owning_original, "master_begin_seconds": master}
        existing = result.get(number)
        if existing is None:
            result[number] = record
        elif (existing["track_artist"], existing["track_name"]) != (artist, title):
            conflicts.append({"track_number": number, "kept": [existing["track_artist"], existing["track_name"]],
                              "also": [artist, title], "file": owning})
            if existing["master_begin_seconds"] is None and master is not None:
                result[number] = record
        elif existing["master_begin_seconds"] is None and master is not None:
            result[number] = record

    # Pass 2: source_files = explicit appearances (start + ID rows) UNION every
    # capture whose window overlaps the track's [master, next-track master] span.
    ordered = sorted(result.values(),
                     key=lambda r: (r["master_begin_seconds"] is None, r["master_begin_seconds"] or 0.0, r["track_number"]))
    for index, record in enumerate(ordered):
        master = record["master_begin_seconds"]
        nxt = ordered[index + 1]["master_begin_seconds"] if index + 1 < len(ordered) else None
        end = nxt if (nxt is not None and master is not None and nxt > master) else master
        files = set(appearances.get(record["track_number"], set()))
        files.add(record.pop("owning_original"))
        if master is not None and end is not None:
            for name, s, e in windows:
                if s is None or e is None:
                    continue
                if s - 0.05 <= end and e + 0.05 >= master:  # window overlaps [master, end]
                    files.add(name)
        record["source_files"] = sorted(files, key=lambda n: (start_of.get(n) is None, start_of.get(n) or 0.0, n))
        # Definitive, NON-OVERLAPPING segment end: the label-derived end CLAMPED to
        # the next track's start, so master_end_seconds[n] is never past the next
        # track's begin. The player switches track info at one unambiguous boundary;
        # a genuine labelled gap (end < next begin) stays a gap → a future
        # "Unidentified"/Mystery segment. Clamping also neutralises duplicate-capture
        # phantom ends (e.g. #27, which over-ran by ~9 min). The track's TRUE musical
        # extent — which legitimately overlaps the next track — is a separate future
        # "individual play" start/stop field, not this one.
        end = track_ends.get(record["track_number"])
        if end is not None and master is not None and end > master:
            record["master_end_seconds"] = min(end, nxt) if nxt is not None else end
        elif nxt is not None:
            # No label end-marker yet (e.g. the 30 s promos): the segment is
            # treated as contiguous and runs to the next track's begin, so every
            # track the player shows has a definitive end. A real end < nxt only
            # appears once the labels carry one (then a gap/Mystery segment opens).
            record["master_end_seconds"] = nxt
        else:
            record["master_end_seconds"] = None   # only the last track has no next

    return result, conflicts


def _track_sort_key(key):
    try:
        return (0, int(key))
    except (TypeError, ValueError):
        return (1, str(key))


def _ordered(entries):
    out = {}
    for key in sorted(entries, key=_track_sort_key):
        entry = entries[key]
        out[key] = {k: (dict(sorted(entry[k].items())) if isinstance(entry[k], dict) else entry[k])
                    for k in sorted(entry)}
    return out


def save(data, path):
    out = {"schema": data.get("schema", SCHEMA)}
    # Preserve the schema-v2 album records (album-shared metadata + per-track
    # `album` refs) — the player's curation flows back through --seed, so dropping
    # `albums` here would lose every album cover/year/link on a regenerate.
    if "albums" in data:
        out["albums"] = _ordered(data.get("albums") or {})
    out["tracks"] = _ordered(data.get("tracks", {}))
    for key in data:  # carry forward any other top-level keys, untouched
        out.setdefault(key, data[key])
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
    seed_tracks = data.get("tracks") or {}

    # 1. Assemble every generated track record (second-pass precise + first-pass
    #    rough), tagged with its source, generated title/artist, and raw label end.
    ids, conflicts = parse_label_track_ids()
    records = []
    for number in sorted(ids):
        lab = ids[number]
        records.append({"number": number, "g_title": lab["track_name"], "g_artist": lab["track_artist"],
                        "master_begin": lab["master_begin_seconds"], "label_end": lab.get("master_end_seconds"),
                        "source": "precise", "source_files": lab["source_files"], "kind": None})
    # First-pass tail: rows the second-pass labels don't cover yet (tracks ~67-91 +
    # Mystery Tracks). Each gets its "try this first" primary capture file.
    frontier = max((max(r.get("master_begin_seconds") or 0.0, r.get("master_end_seconds") or 0.0)
                    for r in ids.values()), default=0.0)
    rough = drop_labelled(parse_remainder(), frontier)
    for rec in rough:
        primary = tail_primary_for(rec["master_begin_seconds"])
        records.append({"number": rec["track_number"], "g_title": rec["track_name"],
                        "g_artist": rec["track_artist"], "master_begin": rec["master_begin_seconds"],
                        "label_end": None, "source": "rough",
                        "source_files": [primary] if primary else [],
                        "kind": "mystery" if rec["is_mystery"] else None})

    # 2. Stable anchor id per record (first title word + first artist word; see
    #    anchor_id). Curated metadata follows the anchor, so a track keeps its
    #    artwork/links when its number or timestamp later changes (e.g. a rough
    #    track becoming precise, or a Mystery splitting/renumbering).
    used = set()
    for rec in sorted(records, key=lambda r: r["number"]):
        rec["anchor"] = anchor_id(rec["g_title"], rec["g_artist"], used)

    # 3. Index the seed by anchor (its own stored anchor if it has one, else
    #    recomputed) so curated fields transfer by identity, not by track number.
    seed_by_anchor, used_seed = {}, set()
    for k in sorted(seed_tracks, key=_track_sort_key):
        seed_entry = seed_tracks[k]
        anchor = seed_entry.get("anchor") or anchor_id(seed_entry.get("title"), seed_entry.get("artist"), used_seed)
        used_seed.add(anchor)
        seed_by_anchor.setdefault(anchor, seed_entry)

    # 4. Build the output: carry every curated field forward from the anchor-matched
    #    seed entry (number is only a fallback), then overlay the generated fields.
    out_tracks, added, kept, no_master, consumed = {}, [], [], [], set()
    for rec in records:
        curated = seed_by_anchor.get(rec["anchor"]) or seed_tracks.get(str(rec["number"]))
        if curated is not None:
            consumed.add(id(curated))
        else:
            added.append(rec["number"])
        entry = dict(curated) if curated else {}
        entry.setdefault("artwork", None)
        entry.setdefault("use_logo", False)
        entry.setdefault("fields", {})
        for field, gval in (("title", rec["g_title"]), ("artist", rec["g_artist"])):
            if not entry.get(field):
                if gval:
                    entry[field] = gval
            elif gval and entry[field] != gval:
                kept.append((rec["number"], field, entry[field], gval))  # curated override wins
        entry["source"] = rec["source"]
        entry["source_files"] = rec["source_files"]
        entry["anchor"] = rec["anchor"]
        if rec["kind"]:
            entry["kind"] = rec["kind"]
        else:
            entry.pop("kind", None)
        entry.pop("master_seconds", None)   # the old ambiguous field name
        entry.pop("source_file", None)      # the earlier singular field
        if rec["master_begin"] is not None:
            entry["master_begin_seconds"] = round(rec["master_begin"], 3)
        else:
            entry.pop("master_begin_seconds", None)
            no_master.append(rec["number"])
        entry["_label_end"] = rec["label_end"]   # resolved into master_end below
        out_tracks[str(rec["number"])] = entry

    # 5. Definitive, non-overlapping segment ends across the COMBINED 1..N timeline:
    #    a label end is clamped to the next begin; a track with no label end runs to
    #    the next begin (contiguous); only the global-last track is open-ended.
    ordered = sorted((k for k, e in out_tracks.items() if e.get("master_begin_seconds") is not None),
                     key=lambda k: out_tracks[k]["master_begin_seconds"])
    for i, k in enumerate(ordered):
        entry = out_tracks[k]
        nxt = out_tracks[ordered[i + 1]]["master_begin_seconds"] if i + 1 < len(ordered) else None
        label_end = entry.pop("_label_end", None)
        if label_end is not None:
            entry["master_end_seconds"] = round(min(label_end, nxt) if nxt is not None else label_end, 3)
        elif nxt is not None:
            entry["master_end_seconds"] = round(nxt, 3)
        else:
            entry.pop("master_end_seconds", None)
    for entry in out_tracks.values():
        entry.pop("_label_end", None)

    # 6. Keep any curated-only seed track that no generated record claimed.
    for k, seed_entry in seed_tracks.items():
        if id(seed_entry) not in consumed and k not in out_tracks:
            out_tracks[k] = seed_entry
    data["tracks"] = tracks = out_tracks

    precise_n = sum(1 for e in out_tracks.values() if e.get("source") == "precise")
    rough_n = sum(1 for e in out_tracks.values() if e.get("source") == "rough")
    mystery_n = sum(1 for e in out_tracks.values() if e.get("kind") == "mystery")
    print("tracks: %d  (precise %d, rough %d, mystery %d; new %d, frontier=%.0fs)"
          % (len(out_tracks), precise_n, rough_n, mystery_n, len(added), frontier))
    if no_master:
        print("  WARN no master position: %s" % sorted(no_master))
    for n, f, have, gen in kept:
        print("  KEPT override track %s %s=%r (label %r)" % (n, f, have, gen))
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
