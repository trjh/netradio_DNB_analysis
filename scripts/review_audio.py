"""Whole-file review audio for DP-22 (`PLAN_align_tool.md` §6.8) — resolution only.

The align server's two Range-supported audio GETs resolve their files here. This
module is deliberately **DSP-free**: no venv, no worker, and — since the decision of
2026-08-11 — **no transcoder**: the §3.3 security contract extended to
plain file serving:

- **identifiers, never paths**: the client names a capture STEM (validated against
  `align_inspect.list_streams()`) or an original NUMBER (validated against the
  sources-dir listing, the same namespace `list_originals()` reads); this module maps
  them to paths itself, realpath-containment-checked under the configured roots;
- **hard caps**: any file over `MAX_REVIEW_BYTES` (~600 MB) is refused with 413 —
  a review pair is a song, not an archive;
- **flac-first resolution** (the DP-23 mainline) for originals, in §6.8's order:
  (a) a `.flac` sibling of the chosen master → served directly; (b) the listed file
  itself is browser/AVFoundation-playable (`mp3`/`m4a`/`flac`) → served directly;
  (c) **wv-only → REFUSED** (503 naming the DP-23 sync). There is no on-demand
  transcode, no cache, no lock, and no ffmpeg dependency on this route — the
  earlier fallback tier was removed per §6.8 step 3 (decision made 2026-08-11): DP-23's
  transcode-on-sync makes the flac sibling the mainline, so the honest answer to a
  missing sibling is "make the FLAC copy", not a second transcoder.

When several source files share one number (a known ambiguity), the pick is
deterministic: **first in sorted directory order** (regular files only — a stray
directory named like an original is never a candidate) — the DSP boundary passes
only the number on to the venv, so there is no DSP-side choice to mirror. The
served route exposes the resolved filename in an `X-Review-Source` header
(percent-encoded there: header values are latin-1 on the wire).

Every public function returns `(http_status, result)`: on 200 `result` is
`{"path", "ctype", "source"}` for the route to Range-serve (`rangeserve.serve`);
otherwise it is a JSON-able error body.
"""

import os

import align_inspect

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

    `stem` must appear in `list_streams()` (which itself applies `_STEM_RE` and the
    containment check) — never a client-supplied path; the path is then rebuilt here
    and containment-checked again (defence in depth, the /audio idiom)."""
    if not stem or stem not in align_inspect.list_streams(mp3_dir):
        return 404, {"error": "unknown stem"}
    path = os.path.join(mp3_dir, stem + ".mp3")
    if not align_inspect._contained(path, mp3_dir) or not os.path.isfile(path):
        return 404, {"error": "unknown stem"}
    return _ok(path, "audio/mpeg")


def _candidates(num):
    """Sorted source filenames carrying number `num` — the originals namespace,
    read exactly the way `align_inspect.list_originals()` reads it."""
    src = align_inspect.sources_dir()
    if not src or not os.path.isdir(src):
        return []
    out = []
    for fn in sorted(os.listdir(src)):
        m = align_inspect._ORIG_FILE_RE.match(fn)
        full = os.path.join(src, fn)
        # os.path.isfile: a stray DIRECTORY named like an original must never be
        # picked — it would pass every later check and then break mid-response
        if m and int(m.group(1)) == num and os.path.isfile(full) \
                and align_inspect._contained(full, src):
            out.append(fn)
    return out


def orig_delivery(num):
    """GET /api/align/audio/orig/<NNN> → the original, resolved in §6.8's order.

    Everything about the request is the validated number; there is no client
    input beyond it, and nothing here ever spawns a process."""
    cands = _candidates(num)
    if not cands:
        return 404, {"error": "unknown orig"}
    src_dir = align_inspect.sources_dir()
    chosen = cands[0]          # deterministic pick among same-number files (docstring)
    master = os.path.join(src_dir, chosen)
    base, ext = os.path.splitext(chosen)
    ext = ext.lower()

    # tier (a): a flac sibling exists (the DP-23 mainline) → serve it directly
    if ext != ".flac":
        sibling = os.path.join(src_dir, base + ".flac")
        if os.path.isfile(sibling) and os.path.getsize(sibling) > 0 \
                and align_inspect._contained(sibling, src_dir):
            return _ok(sibling, "audio/flac")

    # tier (b): the listed file itself is browser/AVFoundation-playable
    if ext in PLAYABLE_TYPES:
        return _ok(master, PLAYABLE_TYPES[ext])

    # tier (c): wv-only → refusal (§6.8 step 3, decision made 2026-08-11).
    # The earlier on-demand transcode fallback is deliberately gone: no ffmpeg,
    # no cache, no lock — the flac sibling is the mainline and the sync creates it.
    return 503, {"error": "wv-only: no browser-playable form of %s — run the "
                          "DP-23 flac sync to produce the FLAC sibling beside it"
                          % chosen}
