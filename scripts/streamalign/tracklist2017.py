"""Read `tracklist-2017.txt`'s Timing section: the 1998/2017 hand notes, as machine data.

This file is the oldest evidence in the project and, for the stretches where captures do not
overlap, it is very nearly the ONLY evidence. It records, per capture:

  * where the file starts on the master clock (`351:14.552  00:00.000 -- START`),
  * each track's start in the file's LOCAL time, with a name
    (`356:53  05:38.556 :: Hypnotising / PFM`),
  * whether the file follows its predecessor directly (`(direct transition from d336-355)`)
    and whether a track carries over (`(continuation of Mystery Track 3)`),
  * and, indented under a track, cue times INSIDE the original recording -- one of which is
    literally annotated `[sync point]`.

Why the engine wants it
-----------------------
Blind search across a 20-minute capture fails on this material: the broadcast repeats, so a
record can match in several places, and the DJ beatmatches, so the match is never exact. The
2017 notes collapse that: they say roughly WHERE each track sits, which turns an unbounded
search into a bounded one. A prior is what makes the search tractable.

The times are hand-noted and approximate -- treat every value here as a HINT to be confirmed,
never as an anchor. It disagrees with the hand labels in places, and where it does, that
disagreement is itself worth surfacing.
"""

import os
import re

from . import audio as _audio
from . import groundtruth as _gt

TRACKLIST = os.path.join(_gt.REPO_ROOT, "tracklist-2017.txt")

# The Timing section runs from the top of the file to the OVERLAP FILES heading; everything
# after it is prose and appendices, in other formats.
_SECTION_END = re.compile(r"^(OVERLAP FILES|Notes from investigating|APPENDIX)\b")

# A block header is an indented bare stem: `\td336-355`, `\tdnb356-375`, `\td107-121b`.
_BLOCK = re.compile(r"^\s+((?:d|dnb)[\d][\w.\-]*)\s*$", re.IGNORECASE)

# `MMM:SS[.sss]` master time, then a local `MM:SS[.sss]`, then the payload.
_MASTER = r"(\d{1,3}):(\d{2}(?:\.\d+)?)"
_LOCAL = r"(\d{1,2}):(\d{2}(?:\.\d+)?)"
_START = re.compile(r"^\s*" + _MASTER + r"\s+" + _LOCAL + r"\s*-{2,}.*\bSTART\b", re.I)
_END = re.compile(r"^\s*" + _MASTER + r"\s+" + _LOCAL + r"\s*-{2,}.*\bEND\b", re.I)
_TRACK = re.compile(r"^\s*" + _MASTER + r"\s+" + _LOCAL + r"\s*::\s*(.+?)\s*$")
_TRANSITION = re.compile(r"\(direct transition from ([\w.\-]+)\)", re.I)
_CONTINUATION = re.compile(r"\(continuation of (.+?)\)", re.I)
# An indented cue inside the ORIGINAL: `1:33 drums  [sync point]`. These are the labeller's
# own by-ear markers in the source recording -- exactly what an A/B anchor is made of.
_ORIG_CUE = re.compile(r"^\s+(\d{1,2}):(\d{2}(?:\.\d+)?)\s+(\S.*?)\s*$")


def _mmss(minutes, seconds):
    return int(minutes) * 60.0 + float(seconds)


def _known_label_stem(stem):
    """Does the COMMITTED evidence know this capture? A `<stem>.labels.tsv` (hand) or
    `<stem>.auto.labels.tsv` (engine) in the labels dir is as authoritative as the audio
    being present -- and unlike the audio, the label files are in the repo. Without this, a
    fresh clone (no capture audio on disk) resolved `dnb356-375` differently from a loaded
    machine, and the whole 2017-notes parse went red (the "not hermetic" backlog item)."""
    for suffix in (".labels.tsv", ".auto.labels.tsv"):
        if os.path.isfile(os.path.join(_gt.LABELS_DIR, stem + suffix)):
            return True
    return False


def normalise_stem(name, audio_dir=None):
    """`dnb356-375` / `d356-375.wav` -> the stem the rest of the engine uses (`d356-375`).

    The 2017 notes are hand-typed and not consistent with the filenames on disk: one block is
    headed `dnb356-375` where the capture is `d356-375.wav`. Resolve against evidence that
    actually exists -- the audio on disk when we have it, else the committed label files --
    rather than guessing a rule.
    """
    stem = _audio.stem_of(name)
    if _audio.find_audio_file(stem, audio_dir) or _known_label_stem(stem):
        return stem
    if stem.lower().startswith("dnb"):
        alt = "d" + stem[3:]
        if _audio.find_audio_file(alt, audio_dir) or _known_label_stem(alt):
            return alt
    return stem


def parse(path=None, audio_dir=None):
    """{stem: {master_start_s, master_end_s, transition_from, continuation, tracks:[...]}}.

    Each track is {local_s, master_s, name, cues:[{at_s, text}]}, where `local_s` is its start
    in the capture's own timeline and `cues` are the hand-noted moments INSIDE that original
    (from the indented lines beneath it).
    """
    path = path or TRACKLIST
    out, cur, cur_track = {}, None, None
    try:
        handle = open(path, "r", encoding="utf-8", errors="replace")
    except OSError:
        return {}
    with handle:
        for line in handle:
            if _SECTION_END.match(line):
                break

            m = _BLOCK.match(line.rstrip("\n"))
            if m:
                cur = normalise_stem(m.group(1), audio_dir)
                cur_track = None
                out.setdefault(cur, {"master_start_s": None, "master_end_s": None,
                                     "transition_from": None, "continuation": None,
                                     "tracks": []})
                continue
            if cur is None:
                continue

            t = _TRANSITION.search(line)
            if t:
                out[cur]["transition_from"] = normalise_stem(t.group(1), audio_dir)
            c = _CONTINUATION.search(line)
            if c:
                out[cur]["continuation"] = c.group(1).strip()

            m = _START.match(line)
            if m and out[cur]["master_start_s"] is None:
                # master start = master time at that line MINUS the local time it names
                # (normally local 0, but be exact rather than assume).
                out[cur]["master_start_s"] = (_mmss(m.group(1), m.group(2))
                                              - _mmss(m.group(3), m.group(4)))
                cur_track = None
                continue
            m = _END.match(line)
            if m:
                out[cur]["master_end_s"] = _mmss(m.group(1), m.group(2))
                cur_track = None
                continue
            m = _TRACK.match(line)
            if m:
                cur_track = {"local_s": _mmss(m.group(3), m.group(4)),
                             "master_s": _mmss(m.group(1), m.group(2)),
                             "name": m.group(5).strip(), "cues": []}
                out[cur]["tracks"].append(cur_track)
                continue

            # An indented `M:SS text` under a track is a cue inside that ORIGINAL. Guard
            # against swallowing the `:: metadata` continuation lines and URLs.
            if cur_track is not None and "::" not in line and "http" not in line:
                m = _ORIG_CUE.match(line.rstrip("\n"))
                if m and int(m.group(1)) < 30:      # a cue inside a track, not a master time
                    # collapse the hand-typed tab runs -- these lines are aligned by eye
                    cur_track["cues"].append({"at_s": _mmss(m.group(1), m.group(2)),
                                              "text": " ".join(m.group(3).split())})
    return out


def for_stem(stem, path=None, audio_dir=None):
    """The 2017 notes for one capture, or None."""
    return parse(path, audio_dir).get(normalise_stem(stem, audio_dir))
