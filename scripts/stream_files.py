#!/usr/bin/env python3
"""Generate `stream-files.json`: where each capture file sits on the master timeline.

The Audacity label exports in `labels/*.labels.tsv` place every capture file on one master
timeline (seconds from the start of the reconstructed broadcast). This script reads them, plus
the Timing section of `tracklist-2017.txt` (which captures are PRIMARY, with their approximate
minute ranges), plus `track-metadata.json` (to count the tracks in each capture), and writes
`stream-files.json` at the repo root.

    python3 scripts/stream_files.py              # writes stream-files.json
    python3 scripts/stream_files.py --dry-run    # prints a summary, writes nothing
    make stream-data                             # track-metadata.json, then this

It is also the one label-timeline parser in this repo: `build_track_metadata.py` and
`streamalign/groundtruth.py` call `read_label_rows` and `resolve_timeline` here.

Label grammar that places a file:
  - `file start sync: <file> <n>` / `file sync: <file> <n>` at local 0.0 of a label file: the
    named file's offset 0 is master <n>. These seeds always win.
  - `file start sync: <file> <n>` anywhere else: the named file starts at the owning file's
    master start plus this row's timestamp. The trailing <n> is a verification anchor from the
    file it was checked against, NEVER an offset to subtract.
  - `file start <file>`: the physical start (pre-roll included). Only seeds a file that has no
    sync row.
  - `file end: <file>` (or `file end <file>`): the file's master end; the latest wins.

Durations are measured with ffprobe from the audio in NETRADIO_MP3_DIR when it is there; a
capture whose audio is absent keeps the duration (and size) already recorded in
`stream-files.json`. The recordings never change, so a duration measured once stays right.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LABELS_DIR = REPO / "labels"
TRACKLIST_PATH = REPO / "tracklist-2017.txt"
METADATA_PATH = REPO / "track-metadata.json"
OUT_PATH = REPO / "stream-files.json"
SCHEMA = "netradio.stream-files.v1"

# `file [start] sync: NAME MASTER` — a seed when the row sits at local 0.0 of its label file.
SYNC_SEED_RE = re.compile(r"\bfile(?:\s+start)?\s+sync:\s*([^\s]+)\s+(-?\d+(?:\.\d+)?)", re.I)
# `file start sync: NAME ANCHOR` anywhere: NAME starts at owner master + row seconds.
# MARK / NEEDMARKINOWNFILE stand in for an anchor not yet written.
SYNC_REF_RE = re.compile(
    r"\bfile start sync:\s*([^\s]+)\s+(?:-?\d+(?:\.\d+)?|MARK|NEEDMARKINOWNFILE)", re.I)
FILE_START_RE = re.compile(r"\bfile start\s+([^\s]+\.(?:wav|au|mp3))\b", re.I)
FILE_END_RE = re.compile(r"\bfile end:?\s+([^\s]+\.(?:wav|au|mp3))\b", re.I)


def parse_float(value, default=None):
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def fmt_time(seconds):
    if seconds is None:
        return ""
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return "%d:%02d:%02d" % (h, m, s)
    return "%d:%02d" % (m, s)


def normalize_audio_filename(name):
    """A capture's timeline key: its basename with the audio extension made `.mp3`."""
    name = os.path.basename((name or "").strip())
    if not name:
        return None
    stem, ext = os.path.splitext(name)
    if ext.lower() in (".wav", ".au", ".mp3"):
        return stem + ".mp3"
    return name + ".mp3"


def original_filename(mp3_name):
    """The original capture's filename for a timeline key: `.au` for the 14Nov captures,
    else `.wav`. Some keys carry a sortable prefix (`d201-221.14Nov10-b.mp3`)."""
    stem = mp3_name[:-4] if mp3_name.lower().endswith(".mp3") else mp3_name
    if "14Nov" in stem:
        if "." in stem:
            tail = stem.split(".", 1)[1]
            stem = ("d-" + tail) if tail.startswith("14Nov") else tail
        return stem + ".au"
    return stem + ".wav"


def is_pipeline_label_file(name):
    """True for the label files the pipeline reads: hand `<stem>.labels.tsv` and engine
    `<stem>.auto.labels.tsv`. Seed-only `<stem>.starter.labels.tsv` only pre-positions labels
    in Audacity and is never read."""
    return name.endswith(".labels.tsv") and not name.endswith(".starter.labels.tsv")


def read_label_rows(labels_dir=LABELS_DIR):
    """Every timestamped row of every pipeline label file in `labels_dir`, in file order.

    Each row is {path, seconds, end, text}; `end` is the Audacity region-end column (equal to
    `seconds` for a point label)."""
    rows = []
    labels_dir = str(labels_dir)
    if not os.path.isdir(labels_dir):
        return rows
    for fn in sorted(n for n in os.listdir(labels_dir) if is_pipeline_label_file(n)):
        path = os.path.join(labels_dir, fn)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    parts = line.rstrip("\n").split("\t", 2)
                    if len(parts) < 3:
                        continue
                    seconds = parse_float(parts[0], None)
                    if seconds is None:
                        continue
                    rows.append({"path": path, "seconds": seconds,
                                 "end": parse_float(parts[1], None), "text": parts[2].strip()})
        except OSError:
            pass
    return rows


def _bare(key):
    return key[:-4] if key.lower().endswith(".mp3") else key


def resolve_timeline(label_rows, key=normalize_audio_filename):
    """Resolve the label rows to (starts, ends, current_by_path).

    `key` turns a filename written in a label into the dict key (the `.mp3` timeline key by
    default; a caller that keys by bare stem passes its own). `current_by_path` maps each
    label file to the capture whose coordinates its rows are in.
    """
    starts, ends, current_by_path = {}, {}, {}

    # Pass 1: seeds. A sync row at local 0.0 says the named file's offset 0 is master <n>.
    for row in label_rows:
        m = SYNC_SEED_RE.search(row["text"])
        if not m or abs(row["seconds"]) > 0.01:
            continue
        name = key(m.group(1))
        master = parse_float(m.group(2), None)
        if name and master is not None:
            starts[name] = master
            current_by_path.setdefault(row["path"], name)

    # Pass 2: `file start NAME` (physical start, pre-roll included) seeds only a file that
    # has no sync row.
    for row in label_rows:
        m = FILE_START_RE.search(row["text"])
        if not m or "file start sync" in row["text"].lower():
            continue
        name = key(m.group(1))
        if not name or name in starts:
            continue
        owner = current_by_path.get(row["path"])
        if owner and owner in starts:
            starts[name] = starts[owner] + row["seconds"]

    # Pass 3: starts and ends written in another file's coordinates. A few rounds resolve
    # chains such as d000 -> d019 -> d026.
    for _ in range(6):
        changed = False
        for row in label_rows:
            current = current_by_path.get(row["path"])
            if not current or current not in starts:
                continue
            current_master = starts[current]
            text = row["text"]

            # The trailing number is a verification anchor, not an offset: the named file
            # starts at this row's own master time.
            m = SYNC_REF_RE.search(text)
            if m:
                name = key(m.group(1))
                if name and name != current:
                    master = current_master + row["seconds"]
                    old = starts.get(name)
                    if old is None or abs(old - master) > 0.001:
                        starts[name] = master
                        changed = True

            m = FILE_END_RE.search(text)
            if m:
                name = key(m.group(1))
                master_end = current_master + row["seconds"]
                old = ends.get(name)
                if name and (old is None or master_end > old):
                    ends[name] = master_end
                    changed = True
            elif re.search(r"\bfile end:\s*%s\b" % re.escape(_bare(current)), text, re.I):
                master_end = current_master + row["seconds"]
                old = ends.get(current)
                if old is None or master_end > old:
                    ends[current] = master_end
                    changed = True
        if not changed:
            break
    return starts, ends, current_by_path


def parse_file_timeline(label_rows):
    """({key: {master_start_seconds, master_end_seconds}}, current_by_path)."""
    starts, ends, current_by_path = resolve_timeline(label_rows)
    timeline = {name: {"master_start_seconds": starts[name], "master_end_seconds": ends.get(name)}
                for name in starts}
    return timeline, current_by_path


def parse_file_range(filename):
    """The minute range a capture's name hints at (`d019-040` -> (19, 40)); a hint only."""
    stem = filename[:-4] if filename.lower().endswith(".mp3") else filename
    match = re.search(r"d(-?\d+)-(\d+)", stem)
    if not match:
        match = re.search(r"d(\d+)", stem)
        if not match:
            return None
        num = int(match.group(1))
        return num, num
    start, end = int(match.group(1)), int(match.group(2))
    return min(start, end), max(start, end)


def parse_tracklist_range_from_line(line, filename):
    """A Timing line's minute range: an explicit `(actual 087-108)` first, then the first
    standalone range after the filename, then the filename's own hint."""
    match = re.search(r"actual\s+(-?\d+)\s*-\s*(\d+)", line, re.I)
    if not match:
        after_name = line.split(filename[:-4], 1)[-1] if filename[:-4] in line else line
        match = re.search(r"\b(-?\d{1,3})\s*-\s*(\d{1,3})\b", after_name)
    if not match:
        return parse_file_range(filename)
    a, b = int(match.group(1)), int(match.group(2))
    return min(a, b), max(a, b)


def read_primary_specs(tracklist_path=TRACKLIST_PATH):
    """The PRIMARY captures from the Timing section of tracklist-2017.txt, in order, each with
    its approximate range in master seconds."""
    specs, seen = [], set()
    try:
        with open(tracklist_path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if "definitive track listing" in line.lower():
                    break
                if not re.search(r"\bPRIMARY\b\??", line, re.I):
                    continue
                match = re.search(r"\b(d[-\w.]*)(?:\.(?:wav|au|mp3))?\b", line, re.I)
                if not match:
                    continue
                filename = normalize_audio_filename(match.group(1))
                if not filename or filename in seen:
                    continue
                minutes = parse_tracklist_range_from_line(line, filename)
                specs.append({
                    "filename": filename,
                    "range_start_seconds": None if not minutes else minutes[0] * 60.0,
                    "range_end_seconds": None if not minutes else minutes[1] * 60.0,
                })
                seen.add(filename)
    except OSError:
        pass
    return specs


def resolve_capture_name(filename, files):
    """`filename` itself when it is a known capture, else the capture with the same original."""
    if filename in files:
        return filename
    original = original_filename(filename).lower()
    for candidate in files:
        if original_filename(candidate).lower() == original:
            return candidate
    return filename


def read_tracks(metadata_path=METADATA_PATH):
    """Track intervals from track-metadata.json: [{number, start, end, note_file}], sorted.

    The end is the generated segment end when it is past the start, else the next track's
    start, else start + 1 s."""
    try:
        with open(metadata_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    rows = []
    for key, entry in (data.get("tracks") or {}).items():
        try:
            number = int(str(key).strip())
        except ValueError:
            continue
        start = parse_float(entry.get("master_begin_seconds", entry.get("master_seconds")), None)
        if start is None:
            continue
        source_files = entry.get("source_files") or []
        rows.append({"number": number, "start": start,
                     "explicit_end": parse_float(entry.get("master_end_seconds"), None),
                     "note_file": normalize_audio_filename(source_files[0]) if source_files else None})
    rows.sort(key=lambda t: (t["start"], t["number"]))
    for index, track in enumerate(rows):
        nxt = rows[index + 1]["start"] if index + 1 < len(rows) else None
        end = track.pop("explicit_end")
        if end is None or end <= track["start"]:
            end = nxt
        if end is None or end <= track["start"]:
            end = track["start"] + 1.0
        track["end"] = end
    return rows


def track_count(tracks, start, end, filename):
    """How many tracks overlap the capture's window; a capture with no start counts the
    tracks whose first source file it is."""
    if start is None:
        return sum(1 for t in tracks if t["note_file"] == filename)
    count = 0
    for t in tracks:
        effective_end = end if end is not None else t["end"]
        if t["end"] > start + 0.001 and t["start"] <= effective_end + 0.001:
            count += 1
    return count


def read_setting(name):
    """The environment wins; this repo's `.env` fills a gap (never executed, names only)."""
    if os.environ.get(name):
        return os.environ[name]
    try:
        with open(REPO / ".env", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    if k.strip() == name:
                        return v.strip().strip('"').strip("'") or None
    except OSError:
        pass
    return None


def probe_duration(ffprobe, path):
    if not ffprobe:
        return None
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15)
        return parse_float(result.stdout.strip(), None)
    except (OSError, subprocess.SubprocessError):
        return None


def load_previous(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return {row["filename"]: row for row in json.load(handle).get("files") or []}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def build(labels_dir=LABELS_DIR, tracklist_path=TRACKLIST_PATH, metadata_path=METADATA_PATH,
          previous=None, audio_dir=None, ffprobe=None):
    """The stream-files document.

    The capture set is every capture already recorded (`previous`) plus every MP3 in
    `audio_dir`. Each capture's duration and size come from its audio when present, else
    from `previous`."""
    previous = previous or {}
    local = set()
    if audio_dir and os.path.isdir(audio_dir):
        local = {f for f in os.listdir(audio_dir) if f.lower().endswith(".mp3")}
    files = sorted(set(previous) | local)

    timeline, _ = parse_file_timeline(read_label_rows(labels_dir))
    specs = [dict(s, filename=resolve_capture_name(s["filename"], files))
             for s in read_primary_specs(tracklist_path)]
    specs = [s for s in specs if s["filename"] in files]
    spec_by_name = {s["filename"]: s for s in specs}
    tracks = read_tracks(metadata_path)

    rows = []
    for filename in files:
        info = timeline.get(filename) or {}
        start, end = info.get("master_start_seconds"), info.get("master_end_seconds")
        prev = previous.get(filename) or {}
        duration, size = None, None
        if filename in local:
            path = os.path.join(audio_dir, filename)
            size = os.path.getsize(path)
            duration = probe_duration(ffprobe, path)
        if duration is None:
            duration = prev.get("duration_seconds", prev.get("duration"))
        if size is None:
            size = prev.get("size")
        spec = spec_by_name.get(filename)
        rows.append({
            "filename": filename,
            "mp3_filename": filename,
            "original_filename": original_filename(filename),
            "master_start_seconds": start,
            "master_end_seconds": end,
            "is_primary": spec is not None,
            "timing_range_start_seconds": spec["range_start_seconds"] if spec else None,
            "timing_range_end_seconds": spec["range_end_seconds"] if spec else None,
            "duration_seconds": duration,
            "duration": duration,
            "duration_label": fmt_time(duration),
            "size": size,
            "track_count": track_count(tracks, start, end, filename),
            "listing_order": None,
        })
    rows.sort(key=lambda r: (
        0 if r["is_primary"] else 1,
        r["master_start_seconds"] is None,
        r["master_start_seconds"] if r["master_start_seconds"] is not None else float("inf"),
        r["filename"],
    ))
    for index, row in enumerate(rows, 1):
        row["listing_order"] = index
    return {"schema": SCHEMA, "primary_files": [s["filename"] for s in specs], "files": rows}


def save(data, path):
    tmp = "%s.tmp" % path
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default=str(OUT_PATH))
    parser.add_argument("--labels-dir", default=str(LABELS_DIR))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    audio_dir = read_setting("NETRADIO_MP3_DIR")
    audio_dir = os.path.expanduser(audio_dir) if audio_dir else None
    ffprobe = read_setting("NETRADIO_FFPROBE") or shutil.which("ffprobe")
    data = build(labels_dir=args.labels_dir, previous=load_previous(args.out),
                 audio_dir=audio_dir, ffprobe=ffprobe)
    placed = sum(1 for r in data["files"] if r["master_start_seconds"] is not None)
    missing = [r["filename"] for r in data["files"] if r["duration_seconds"] is None]
    print("%d captures, %d placed on the timeline, %d primary"
          % (len(data["files"]), placed, len(data["primary_files"])))
    if missing:
        print("no duration (audio absent, none recorded): %s" % ", ".join(missing))
    if args.dry_run:
        print("dry-run: nothing written")
        return 0
    save(data, args.out)
    print("wrote %s" % args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
