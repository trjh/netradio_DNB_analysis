#!/usr/bin/env python3
"""Generate `stream-files.json`: where each capture file sits on the master timeline.

The Audacity label exports in `labels/*.labels.tsv` place every capture file on one master
timeline (seconds from the start of the reconstructed broadcast). This script reads them, plus
the Timing section of `tracklist-2017.txt` (which captures are PRIMARY, with their approximate
minute ranges), plus `track-metadata.json` (to count the tracks in each capture), and writes
`stream-files.json` at the repo root.

    python3 scripts/stream_files.py              # writes stream-files.json
    python3 scripts/stream_files.py --dry-run    # prints a summary, writes nothing
    python3 scripts/stream_files.py --prune      # also drop captures whose audio is gone
    make stream-data                             # track-metadata.json, then this

It is also the one label-timeline parser in this repo: `build_track_metadata.py` and
`streamalign/groundtruth.py` call `read_label_rows` and `resolve_timeline` here. Both raise
`BuildRefused` rather than return a partial timeline: on a label file that cannot be read, and
on rows that never settle (two rows placing one capture two ways).

Label grammar that places a file:
  - `file start sync: <file> <n>` / `file sync: <file> <n>` at local 0.0 of a label file: the
    named file's offset 0 is master <n>. These seeds win over `file start` rows.
  - `file start sync: <file> <n>` anywhere else: the named file starts at the owning file's
    master start plus this row's timestamp. The trailing <n> is a verification anchor from the
    file it was checked against, NEVER an offset to subtract. Once its owner is placed, such a
    row overrides the named file's own seed when the two disagree.
  - `file start <file>`: the physical start (pre-roll included). Only seeds a file that has no
    sync row.
  - `file end: <file>` (or `file end <file>`): the file's master end; the latest wins.

A capture's duration is the length of its ORIGINAL recording (the `.wav` or `.au` the label
timings were made against), never its MP3, which can run longer by the encoder's padding. The
originals are in NETRADIO_CAPTURES_DIR, which defaults to the parent of NETRADIO_MP3_DIR.
Durations are measured once and committed: a capture that already has a duration in
`stream-files.json` keeps it, and only a capture with none is measured, with ffprobe, from its
original. `--remeasure` measures every capture whose original is there again. The recordings
never change, so a duration measured once stays right, and never re-measuring keeps ffprobe's
last-digit drift out of the file. A capture that needed measuring but has no original keeps its
recorded duration (null when there is none) and is named on stderr. `size` is the MP3's size
when the MP3 is there, else the recorded value.

The committed `stream-files.json` is the capture list: every capture it records is kept, and an
MP3 that appears in NETRADIO_MP3_DIR is added. A capture leaves the list only with `--prune`,
which drops the recorded captures whose audio is not in NETRADIO_MP3_DIR. The build refuses to
write an empty list, or a shorter one without `--prune`, and stops when an existing
`stream-files.json` cannot be read.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
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


def original_path(mp3_name, captures_dir):
    """The path of a capture's original recording in `captures_dir`, or None when it is not
    there. Tries `original_filename()`, then the `dnb-` spelling some 14Nov originals carry
    (`d-14Nov02-a.au` is on disk as `dnb-14Nov02-a.au`)."""
    if not captures_dir:
        return None
    name = original_filename(mp3_name)
    names = [name] + (["dnb-" + name[2:]] if name.startswith("d-") else [])
    for candidate in names:
        path = os.path.join(captures_dir, candidate)
        if os.path.isfile(path):
            return path
    return None


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
        except OSError as exc:
            # An unreadable label file drops every capture it places; stop rather than build a
            # timeline without them, as an unreadable stream-files.json already stops the build.
            raise BuildRefused("cannot read the label file %s (%s): fix or restore it first"
                               % (path, exc))
    return rows


def _bare(key):
    return key[:-4] if key.lower().endswith(".mp3") else key


def resolve_timeline(label_rows, key=normalize_audio_filename, overrides=None):
    """Resolve the label rows to (starts, ends, current_by_path).

    `key` turns a filename written in a label into the dict key (the `.mp3` timeline key by
    default; a caller that keys by bare stem passes its own). `current_by_path` maps each
    label file to the capture whose coordinates its rows are in. When `overrides` is a dict,
    it is filled with `{key: (own_seed, placed)}` for each capture whose own start label a
    cross-file row overrode.
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
    seeds = dict(starts)

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

    # Pass 3: starts and ends written in another file's coordinates. Rounds resolve chains
    # such as d000 -> d019 -> d026; each round settles at least one more link, so a chain needs
    # at most one round per label file, and one more shows nothing moved. Rows still moving
    # after that keep placing a capture two ways, and the build stops naming them.
    rounds = max(6, len(set(current_by_path.values())) + 2)
    for _ in range(rounds):
        changed = set()
        for row in label_rows:
            current = current_by_path.get(row["path"])
            if not current or current not in starts:
                continue
            current_master = starts[current]
            text = row["text"]

            # The trailing number is a verification anchor, not an offset: the named file
            # starts at this row's own master time, overriding its own seed if they disagree.
            # A self row (`file start sync: <own file> <n>`) is the seed itself and places
            # nothing, but the row still goes on to the `file end` checks below.
            m = SYNC_REF_RE.search(text)
            if m:
                name = key(m.group(1))
                if name and name != current:
                    master = current_master + row["seconds"]
                    old = starts.get(name)
                    if old is None or abs(old - master) > 0.001:
                        starts[name] = master
                        changed.add(name)

            m = FILE_END_RE.search(text)
            if m:
                name = key(m.group(1))
                master_end = current_master + row["seconds"]
                old = ends.get(name)
                if name and (old is None or master_end > old):
                    ends[name] = master_end
                    changed.add(name)
            elif re.search(r"\bfile end:\s*%s\b" % re.escape(_bare(current)), text, re.I):
                master_end = current_master + row["seconds"]
                old = ends.get(current)
                if old is None or master_end > old:
                    ends[current] = master_end
                    changed.add(current)
        if not changed:
            break
    else:
        raise BuildRefused("the label rows did not settle after %d rounds: %s still moved in "
                           "the last one -- two rows keep placing the same capture differently"
                           % (rounds, ", ".join(sorted(changed))))
    if overrides is not None:
        for name, seed in seeds.items():
            if abs(starts[name] - seed) > 0.001:
                overrides[name] = (seed, starts[name])
    return starts, ends, current_by_path


def parse_file_timeline(label_rows, overrides=None):
    """({key: {master_start_seconds, master_end_seconds}}, current_by_path). `overrides` is
    filled as `resolve_timeline` fills it."""
    starts, ends, current_by_path = resolve_timeline(label_rows, overrides=overrides)
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


def read_env(path):
    """KEY -> value from a `.env` file, never executed: blank lines and `#` lines skipped, each
    line split on its first `=`, both sides trimmed, then double and single quotes stripped
    from the value's ends. The first line for a key wins. A missing file reads as empty."""
    env = {}
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except OSError:
        pass
    return env


def read_setting(name, env=None):
    """The environment wins; this repo's `.env` fills a gap."""
    env = read_env(REPO / ".env") if env is None else env
    return os.environ.get(name) or env.get(name) or None


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


def ffprobe_works(ffprobe):
    """True when `ffprobe` names a program that answers `-version`."""
    if not ffprobe:
        return False
    try:
        return subprocess.run([ffprobe, "-version"], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=15, check=False).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


class BuildRefused(Exception):
    """The build would lose recorded captures, or cannot read what is recorded."""


def load_previous(path):
    """{filename: row} from an existing stream-files.json; {} when there is none.

    A file that exists but cannot be read is an error, not an empty list: treating it as empty
    would write an empty capture list over every recorded duration."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return {row["filename"]: row for row in json.load(handle)["files"]}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise BuildRefused("cannot read %s (%s): fix or restore it first" % (path, exc))


def build(labels_dir=LABELS_DIR, tracklist_path=TRACKLIST_PATH, metadata_path=METADATA_PATH,
          previous=None, audio_dir=None, ffprobe=None, prune=False, remeasure=False,
          captures_dir=None, report=None):
    """The stream-files document.

    The capture set is every capture already recorded (`previous`) plus every MP3 in
    `audio_dir`; with `prune`, only the MP3s in `audio_dir`. A capture's duration is the one
    recorded in `previous`; only a capture with none (or every capture, with `remeasure`) is
    measured, from its original in `captures_dir`, never from its MP3. Its size comes from
    its MP3 when present, else `previous`.

    When `report` is given, it collects `no_original` (captures that needed measuring but have
    no original), `probe_failed` (captures whose original ffprobe could not measure) and
    `overridden` (`{capture: (own_seed, placed)}` for each capture whose own start label a
    cross-file row overrode).

    Raises BuildRefused for an empty capture set, a prune with no audio directory, and a
    shorter set without `prune`."""
    previous = previous or {}
    local = set()
    if audio_dir and os.path.isdir(audio_dir):
        local = {f for f in os.listdir(audio_dir) if f.lower().endswith(".mp3")}
    elif prune:
        raise BuildRefused("--prune needs NETRADIO_MP3_DIR to name a directory of the audio")
    files = sorted(local if prune else set(previous) | local)
    if not files:
        raise BuildRefused("no captures: nothing recorded and no audio in NETRADIO_MP3_DIR")
    dropped = sorted(set(previous) - set(files))
    if dropped and not prune:
        raise BuildRefused("would drop %d recorded capture(s) without --prune: %s"
                           % (len(dropped), ", ".join(dropped)))

    overridden = {}
    timeline, _ = parse_file_timeline(read_label_rows(labels_dir), overrides=overridden)
    if report is not None and overridden:
        report["overridden"] = overridden
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
        recorded = prev.get("duration_seconds", prev.get("duration"))
        duration, size = None, None
        if filename in local:
            size = os.path.getsize(os.path.join(audio_dir, filename))
        if recorded is None or remeasure:
            original = original_path(filename, captures_dir)
            if original:
                duration = probe_duration(ffprobe, original)
                if duration is None and report is not None:
                    report.setdefault("probe_failed", []).append(filename)
            elif report is not None:
                report.setdefault("no_original", []).append(filename)
        if duration is None:
            duration = recorded
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
    parser.add_argument("--tracklist", default=str(TRACKLIST_PATH))
    parser.add_argument("--metadata", default=str(METADATA_PATH))
    parser.add_argument("--prune", action="store_true",
                        help="drop recorded captures whose audio is not in NETRADIO_MP3_DIR")
    parser.add_argument("--remeasure", action="store_true",
                        help="measure every capture whose original is present again, "
                             "recorded or not")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    audio_dir = read_setting("NETRADIO_MP3_DIR")
    audio_dir = os.path.expanduser(audio_dir) if audio_dir else None
    captures_dir = read_setting("NETRADIO_CAPTURES_DIR")
    if captures_dir:
        captures_dir = os.path.expanduser(captures_dir)
    elif audio_dir:
        captures_dir = os.path.dirname(audio_dir.rstrip(os.sep))
    ffprobe = read_setting("NETRADIO_FFPROBE") or shutil.which("ffprobe")
    report = {}
    if args.remeasure and not ffprobe_works(ffprobe):
        print("REFUSING TO WRITE: --remeasure needs a working ffprobe (NETRADIO_FFPROBE, or "
              "ffprobe on PATH); found %s" % (ffprobe or "none"))
        return 1
    try:
        previous = load_previous(args.out)
        data = build(labels_dir=args.labels_dir, tracklist_path=args.tracklist,
                     metadata_path=args.metadata, previous=previous, audio_dir=audio_dir,
                     ffprobe=ffprobe, prune=args.prune,
                     remeasure=args.remeasure, captures_dir=captures_dir, report=report)
    except BuildRefused as exc:
        print("REFUSING TO WRITE: %s" % exc)
        return 1
    dropped = sorted(set(previous) - {r["filename"] for r in data["files"]})
    if dropped:
        print("pruned %d capture(s) with no audio: %s" % (len(dropped), ", ".join(dropped)))
    placed = sum(1 for r in data["files"] if r["master_start_seconds"] is not None)
    missing = [r["filename"] for r in data["files"] if r["duration_seconds"] is None]
    print("%d captures, %d placed on the timeline, %d primary"
          % (len(data["files"]), placed, len(data["primary_files"])))
    for name, (seed, placed) in sorted(report.get("overridden", {}).items()):
        print("%s: its own start label says %.3f; a cross-file row places it at %.3f, which "
              "wins" % (name, seed, placed))
    if report.get("probe_failed"):
        if ffprobe_works(ffprobe):
            print("ffprobe could not measure the original (recorded duration kept): %s"
                  % ", ".join(report["probe_failed"]), file=sys.stderr)
        else:
            print("not measured, no working ffprobe (NETRADIO_FFPROBE, or ffprobe on PATH; "
                  "found %s; recorded duration kept): %s"
                  % (ffprobe or "none", ", ".join(report["probe_failed"])), file=sys.stderr)
    if report.get("no_original"):
        print("not measured, no original recording in NETRADIO_CAPTURES_DIR (recorded duration "
              "kept): %s" % ", ".join(report["no_original"]), file=sys.stderr)
    if missing:
        print("no duration (none recorded, and no original measured): %s" % ", ".join(missing),
              file=sys.stderr)
    if args.dry_run:
        print("dry-run: nothing written")
        return 0
    save(data, args.out)
    print("wrote %s" % args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
