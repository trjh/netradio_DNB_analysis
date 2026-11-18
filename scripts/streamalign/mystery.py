"""Which tracks are STILL mysteries -- from `track-metadata.json`, never from filenames.

This exists because of a real bug. The tools used to pick their queries by globbing
`sources/Mystery Track *` -- and that directory still holds clips of tracks that have SINCE
BEEN IDENTIFIED (Mystery Tracks 2 and 3 among them; 3 is Aquarius - "Wave Forms"). So a
long-running match job spent ~40% of its work re-answering solved questions, and a spurious
hit against a solved track would have read as a real lead.

**The filename is not the truth. `track-metadata.json` is.** A track is a mystery if, and only
if, its title still says so. Everything that needs the mystery list goes through here.

(The same lesson, in another key: `013-DJ Addiction - Senses.mp3` is really Blame's "J-Walkin'".
Filenames lie.)
"""

import json
import os
import re
import sys

import sources_resolve

from . import groundtruth as _gt

_MYSTERY_RE = re.compile(r"mystery\s+track\s+(\d+)", re.IGNORECASE)


def _metadata(path=None):
    path = path or os.path.join(_gt.REPO_ROOT, "track-metadata.json")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data.get("tracks", data)


def clips(sources_dir=None):
    """({mystery number: path}, {mystery number: [names]}) for the clips in the originals
    directory, by the one resolver (`sources_resolve`, 2026-10-28): the uncompressed file,
    else the lossless master, else the flac, else a lossy one. A mystery whose winning rung
    holds two files is refused, never guessed: it lands in the second map, naming both, and
    has no clip until one is removed. Local files only: a clip is never pulled.

    `.wv` earned its place the hard way: Mystery Track 4's clip was wavpack-compacted and
    became INVISIBLE to the whole search -- the harvester ran for days looking for everything
    except the thing it was missing a clip for. One rule for every reader of an original is
    what keeps that from recurring."""
    src = sources_dir or sources_resolve.sources_dir()
    found, refused = {}, {}
    for key, names in sources_resolve.group(sources_resolve.local_names(src)).items():
        if key[0] != "mystery":
            continue
        try:
            found[key[1]] = os.path.join(src, sources_resolve.choose(
                names, "Mystery Track %d" % key[1]))
        except sources_resolve.Ambiguous as exc:
            refused[key[1]] = exc.names
    return found, refused


def current(sources_dir=None, metadata_path=None):
    """[{track, number, title, clip, clips_refused}] for every UNSOLVED mystery, clip only
    where we have one.

    `track` is the master track number (e.g. 68), `number` the mystery's own number (4).
    `clip` is the extracted audio if the originals directory holds one, else None -- a mystery
    with no clip is still a mystery, it just cannot be searched or published yet.
    `clips_refused` names the two files when the resolver refused to pick between them
    (`clips`), else it is absent.
    """
    found, refused = clips(sources_dir)

    out = []
    for num, entry in _metadata(metadata_path).items():
        if not str(num).isdigit():
            continue
        m = _MYSTERY_RE.search(entry.get("title") or "")
        if not m:
            continue                      # title no longer says "Mystery Track" -> solved
        number = int(m.group(1))
        row = {"track": int(num), "number": number,
               "title": entry.get("title"), "clip": found.get(number),
               "master_begin_seconds": entry.get("master_begin_seconds"),
               "master_end_seconds": entry.get("master_end_seconds")}
        if number in refused:
            row["clips_refused"] = refused[number]
        out.append(row)
    return sorted(out, key=lambda e: e["number"])


def searchable(sources_dir=None, metadata_path=None):
    """The unsolved mysteries we actually have audio for -- what a matcher should query.

    A mystery whose clip the resolver refused is left out, never in silence: a line on stderr
    names both files, so every caller says why it is not searched."""
    out = []
    for e in current(sources_dir, metadata_path):
        if e.get("clips_refused"):
            print("Mystery Track %d not searched: two clips, keep one: %s"
                  % (e["number"], ", ".join(e["clips_refused"])), file=sys.stderr)
        if e["clip"]:
            out.append(e)
    return out
