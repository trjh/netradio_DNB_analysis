"""Whole-file review audio for DP-22 (`PLAN_align_tool.md` §6.8) — resolution only.

The align server's two Range-supported audio GETs resolve their files here. This
module is deliberately **DSP-free**: no venv, no worker, and — since the decision of
2026-08-11 — **no transcoder**: the §3.3 security contract extended to
plain file serving:

- **identifiers, never paths**: the client names a capture STEM (validated against
  `align_ids.list_streams()`) or an original NUMBER (validated against the
  sources-dir listing, the same namespace `list_originals()` reads); this module maps
  them to paths itself, realpath-containment-checked under the configured roots;
  a capture only the audio bucket holds is pulled whole, checked, before it is served
  (`align_ids.ensure_stream`);
- **hard caps**: any file over `MAX_REVIEW_BYTES` (~600 MB) is refused with 413 —
  a review pair is a song, not an archive;
- **originals by the one resolver** (`sources_resolve`, 2026-10-28): the stem's file is the
  uncompressed one, else the lossless master, else the flac, else a lossy one. What is served
  is its browser- and AVFoundation-playable form: the pick itself when it is `mp3`/`m4a`/
  `flac`; else its `.flac` sibling, from this disk or pulled whole from the audio bucket's
  `sources/` into the `sources_flac` cache, checked by its SHA-256 metadata before it is
  renamed into place. An original this disk does not hold at all is pulled the same way. A
  pick with no playable form to be had (no bucket, no such object) is refused with 503;
  there is no transcoder on this route (decision made 2026-08-11);
- **two versions need a recorded choice**: when two files share the winning rung under one
  number, the route serves the one recorded in `source-versions.json` (2026-11-18); with none
  recorded it answers 409 naming both and saying what to add, and never picks one.

The served route exposes the resolved filename in an `X-Review-Source` header
(percent-encoded there: header values are latin-1 on the wire).

Every public function returns `(http_status, result)`: on 200 `result` is
`{"path", "ctype", "source"}` for the route to Range-serve (`rangeserve.serve`);
otherwise it is a JSON-able error body.
"""

import os

import align_ids
import align_inspect
import sources_resolve

MAX_REVIEW_BYTES = 600 * 1024 * 1024   # the §3.3-style cap: refuse anything larger

# what a browser and AVFoundation both play as-is (§6.8 tier b — deliberately NOT
# every browser-playable format: opus/ogg fail AVFoundation, wav/aif go via flac)
PLAYABLE_TYPES = {".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".flac": "audio/flac"}


def _ok(path, ctype, source=None):
    """The 200 shape — after the one cap every served file must pass."""
    if os.path.getsize(path) > MAX_REVIEW_BYTES:
        return 413, {"error": "file exceeds the review-audio size cap"}
    return 200, {"path": path, "ctype": ctype,
                 "source": source or os.path.basename(path)}


def stream_delivery(stem, mp3_dir):
    """GET /api/align/audio/stream/<stem> → the capture MP3, identifier-validated.

    `stem` must appear in `align_ids.list_streams()` (which itself applies the stem
    pattern and the containment check, and names the audio bucket's MP3s too) — never a
    client-supplied path. A stem only the bucket holds is pulled once
    (`align_ids.ensure_stream`; a failed pull answers with its own status); the path is then
    rebuilt here and containment-checked again (defence in depth, the /audio idiom)."""
    if not stem or stem not in align_ids.list_streams(mp3_dir):
        return 404, {"error": "unknown stem"}
    failed = align_ids.ensure_stream(stem, mp3_dir)
    if failed is not None:
        return failed[0], {"error": failed[1]}
    path = os.path.join(mp3_dir, stem + ".mp3")
    if not align_inspect._contained(path, mp3_dir) or not os.path.isfile(path):
        return 404, {"error": "unknown stem"}
    return _ok(path, "audio/mpeg")


def orig_delivery(num):
    """GET /api/align/audio/orig/<NNN> -> the original's playable form, by the one resolver.

    Everything about the request is the validated number; there is no client input beyond
    it, and nothing here ever spawns a process. A pull is one lock per file name: a second
    request for the same original waits for the first pull, then finds the file."""
    src_dir = align_inspect.sources_dir()
    if not src_dir:
        return 404, {"error": "unknown orig"}
    key = ("track", int(num))
    names = sources_resolve.group(sources_resolve.local_names(src_dir)).get(key, [])
    try:
        pulled = sources_resolve.pull_recorded(key, names, src_dir)
        chosen = (os.path.basename(pulled) if pulled
                  else sources_resolve.choose(names, "track %03d" % num, key))
        if chosen is None:
            path, failed = sources_resolve.pull_stem(key, src_dir)
            if path is None:
                if failed[0] == 404:
                    return 404, {"error": "unknown orig"}
                return failed[0], {"error": failed[1]}
            chosen = os.path.basename(path)
    except sources_resolve.Unresolved as exc:
        return 409, {"error": str(exc), "files": exc.names}

    base, ext = os.path.splitext(chosen)
    ext = ext.lower()
    if ext in PLAYABLE_TYPES:
        return _ok(os.path.join(src_dir, chosen), PLAYABLE_TYPES[ext])
    # not playable as it is: its flac sibling, here or pulled from the bucket
    path, failed = sources_resolve.path_of(base + ".flac", src_dir)
    if path is not None and os.path.getsize(path) > 0:
        return _ok(path, "audio/flac")
    return 503, {"error": "no browser-playable form of %s (%s): make a FLAC copy beside "
                          "the original" % (chosen, failed[1] if failed else "its FLAC copy "
                                            "is empty")}
