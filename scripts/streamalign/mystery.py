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


def current(sources_dir=None, metadata_path=None):
    """[{track, number, title, clip}] for every UNSOLVED mystery, clip only where we have one.

    `track` is the master track number (e.g. 68), `number` the mystery's own number (4).
    `clip` is the extracted audio if it exists in `sources_dir`, else None -- a mystery with
    no clip is still a mystery, it just cannot be searched or published yet.
    """
    sources_dir = sources_dir or os.environ.get("NETRADIO_SOURCES_DIR") or ""
    clips = {}
    if os.path.isdir(sources_dir):
        # Lossless beats lossy, and .wav beats .wv only by convention (both are lossless;
        # ffmpeg decodes WavPack natively, so decode was never the problem). `.wv` earned its
        # place here the hard way: Mystery Track 4's clip was wavpack-compacted and became
        # INVISIBLE to the whole search -- the harvester ran for days looking for everything
        # except the thing it was missing a clip for.
        rank = {".wav": 0, ".wv": 1, ".flac": 2, ".m4a": 3, ".mp3": 4}
        best = {}
        for name in os.listdir(sources_dir):
            stem, ext = os.path.splitext(name)
            m = _MYSTERY_RE.match(stem)
            if m and ext.lower() in rank:
                key = int(m.group(1))
                if key not in best or rank[ext.lower()] < best[key]:
                    best[key] = rank[ext.lower()]
                    clips[key] = os.path.join(sources_dir, name)

    out = []
    for num, entry in _metadata(metadata_path).items():
        if not str(num).isdigit():
            continue
        m = _MYSTERY_RE.search(entry.get("title") or "")
        if not m:
            continue                      # title no longer says "Mystery Track" -> solved
        number = int(m.group(1))
        out.append({"track": int(num), "number": number,
                    "title": entry.get("title"), "clip": clips.get(number),
                    "master_begin_seconds": entry.get("master_begin_seconds"),
                    "master_end_seconds": entry.get("master_end_seconds")})
    return sorted(out, key=lambda e: e["number"])


def searchable(sources_dir=None, metadata_path=None):
    """The unsolved mysteries we actually have audio for -- what a matcher should query."""
    return [e for e in current(sources_dir, metadata_path) if e["clip"]]
