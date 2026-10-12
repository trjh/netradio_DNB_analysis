"""DP-22 §6.6/§6.7: the review-note writer behind `POST /api/align/review-note`.

The whole-song review mode (docs/PLAN_align_tool.md §6) lets the operator place a
free-text note on the scrolling waveform; this module turns that click into one
appended row of the §6.6 label-compatible grammar in the per-pair file
`labels/review/<stem>.origNNN.review.tsv` inside this checkout
(the DP-24 layout — `PLAN_labels_layout.md`;
`align_inspect.repo_root()`). Everything about that write lives here:

- **identifiers, never paths** (the §3.3 contract): the client names a capture STEM
  and an original NUMBER, both validated against the server's own listings
  (`align_inspect.list_streams()` / `list_originals()`); the target path is derived
  HERE from the validated identifiers and realpath-containment-checked under
  `<checkout>/labels/review/`. Times are hard-capped, the rate bounded by `RATE_RANGE`, the
  anchor list bounded like `MAX_OVERVIEW_POINTS`, the free text sanitized
  (tabs/newlines/control chars become spaces; length-capped) so a note can never
  break the TSV shape — and the mapping model itself must be CANONICAL (unique
  anchor instants, §6.2 mode cardinality, the fitted-mode rate verified against the
  least-squares slope) with the note's `orig_t` agreeing with the §6.2/§6.3 mapping
  the body's `{mode, rate, pairs}` imply (recomputed server-side,
  `expected_orig_t`): a row whose numbers its own session could not deterministically
  reproduce is rejected, never durably acked.
- **the §6.6 grammar**: every appended row carries the durable per-file session key
  `s<K>` — allocated under the append lock by scanning the file's existing
  `note REVIEW session` rows — so a note stays tied to its mapping (its session row
  + that session's anchor rows) no matter how the rows are later re-sorted. A new
  session row + one anchor row per selected pair are emitted whenever the supplied
  mode / rate / selection differs from the LAST session's context; otherwise the
  note reuses the current key. REVIEW rows are prose note rows: they never carry
  either `verified` token form and no parser may read them as sync rows.
- **crash-safe**: write + flush + `fsync` (and a directory fsync for a new file) happen BEFORE
  the 200 — the durable append is the acknowledgement; the `review_sync.py` commit
  is downstream batching (this module nudges it via the route).
- **review files are NOT hand labels** (§6.11): they live under `labels/review/`, never
  among the authoritative root `labels/*.labels.tsv`, and never
  `labels/`, and reach the hand labels only through the human fold.

The route itself (`align_server.py`) sits behind the same `_gate` as every align
route — Basic Auth when configured, absent entirely in read-only mode.
"""

import os
import re
import threading
import time

import align_inspect

MODES = ("constant-rate", "re-anchor", "assumed-rate")
MAX_TEXT_LEN = 500
MAX_ANCHORS = align_inspect.MAX_OVERVIEW_POINTS
# §6.6 promises every notation's orig_s is re-derivable from its session's mode/rate/
# anchors alone. The server therefore requires the mapping MODEL itself to be
# canonical — anchor stream instants unique at emitted precision (equal-time anchors
# would make the re-anchor segments order-dependent, and order does not survive
# sort_tsv.py), mode cardinality per §6.2 (assumed-rate = exactly one pair; the
# fitted modes = at least two), the supplied rate equal to the least-squares slope
# over the pairs for the fitted modes — and then recomputes the mapping and REJECTS
# an orig_t that disagrees: an irreproducible row must never be durably acked.
# For the fitted modes the rate is not the client's to choose AT ALL: the canonical
# rate is the least-squares slope over the pairs at the session row's own 6 dp
# representation — a supplied rate is accepted only when it rounds to exactly that
# value, and the canonical value is what gets validated against, persisted, and
# re-derived from (so the stored session IS the fit, bit-for-bit at emitted
# precision). assumed-rate stays free-but-bounded (§6.2: one pair gives nothing to
# fit). MAPPING_TOLERANCE_S absorbs client-side display rounding on orig_t (6 dp on
# rate over a whole-song span is sub-ms); anything past it is a bug or garbage.
MAPPING_TOLERANCE_S = 0.010

_ANCHOR_K_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,15}$")
_SESSION_ROW_RE = re.compile(
    r"^\d+\.\d{6}\t\d+\.\d{6}\tnote REVIEW session s(\d+): (\S+) rate ([0-9.]+) "
    r"from (\d+) pairs? \d{4}-\d{2}-\d{2}\s*$")
_ANCHOR_ROW_RE = re.compile(
    r"^(\d+\.\d{6})\t\d+\.\d{6}\tnote REVIEW anchor s(\d+): (\S+) orig(\d{3}) "
    r"([0-9.]+)\s*$")

_append_lock = threading.Lock()


def _today():
    """The session row's date stamp (a seam so tests can pin bytes)."""
    return time.strftime("%Y-%m-%d")


def fsync_dir(d):
    """fsync a directory so a rename or create inside it survives power loss (the directory
    entry, not just the file contents, must reach the disk). Best-effort: some filesystems
    reject a directory fsync (EINVAL) — the write still holds, the metadata flush just cannot
    be forced."""
    try:
        fd = os.open(d, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def review_dir():
    repo = align_inspect.repo_root()
    return os.path.join(repo, "labels", "review") if repo else None


def enabled():
    """The write path's capability: a checkout to append into. The DSP
    venv is NOT required — appending TSV is not compute."""
    repo = align_inspect.repo_root()
    return bool(repo and os.path.isdir(repo))


def pair_filename(stem, orig):
    return "%s.orig%03d.review.tsv" % (stem, int(orig))


def sanitize_text(text):
    """Free text → one safe TSV cell: every control char (tabs and newlines
    included) becomes a space, then strip + length-cap."""
    out = "".join(" " if (ord(c) < 32 or ord(c) == 127) else c for c in str(text))
    return out.strip()[:MAX_TEXT_LEN]


def validate(params, mp3_dir):
    """(clean, None) or (None, error). §3.3-style: identifiers checked against the
    server's own listings, every number hard-capped, text sanitized."""
    if not isinstance(params, dict):
        return None, "request body must be a JSON object"
    stem = str(params.get("stem") or "")
    if not align_inspect._STEM_RE.match(stem):
        return None, "bad stem"
    if stem not in align_inspect.list_streams(mp3_dir):
        return None, "unknown stem"
    try:
        orig = int(params.get("orig"))
    except (TypeError, ValueError):
        return None, "bad orig"
    if not any(o["num"] == orig for o in align_inspect.list_originals()):
        return None, "unknown orig"

    def _num(key, value, lo, hi):
        try:
            v = float(value)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("bad %s" % key)
        if not (lo <= v <= hi):
            raise ValueError("%s out of range" % key)
        return v

    lo, hi = align_inspect.RATE_RANGE
    try:
        clean = {
            "stem": stem,
            "orig": orig,
            "stream_t": _num("stream_t", params.get("stream_t"), 0.0,
                             align_inspect.MAX_T_S),
            "orig_t": _num("orig_t", params.get("orig_t"), 0.0,
                           align_inspect.MAX_T_S),
            "rate": _num("rate", params.get("rate"), lo, hi),
        }
    except ValueError as exc:
        return None, str(exc)
    mode = str(params.get("mode") or "")
    if mode not in MODES:
        return None, "bad mode"
    clean["mode"] = mode
    text = sanitize_text(params.get("text") or "")
    if not text:
        return None, "empty text"
    clean["text"] = text
    raw_pairs = params.get("pairs")
    if not isinstance(raw_pairs, list) or not 1 <= len(raw_pairs) <= MAX_ANCHORS:
        return None, "bad pairs"
    pairs = []
    for p in raw_pairs:
        if not isinstance(p, dict):
            return None, "bad pairs"
        k = str(p.get("k") or "")
        if not _ANCHOR_K_RE.match(k):
            return None, "bad pair key"
        try:
            pairs.append({
                "k": k,
                "stream_s": _num("pair stream_s", p.get("stream_s"), 0.0,
                                 align_inspect.MAX_T_S),
                "orig_s": _num("pair orig_s", p.get("orig_s"), 0.0,
                               align_inspect.MAX_T_S),
            })
        except ValueError as exc:
            return None, str(exc)
    clean["pairs"] = pairs
    if len({"%.6f" % p["stream_s"] for p in pairs}) != len(pairs):
        # equal-time anchors persist as indistinguishable rows: the re-anchor
        # schedule would depend on request order, which sort_tsv.py destroys
        return None, "duplicate anchor stream_s"
    if mode == "assumed-rate":
        if len(pairs) != 1:
            return None, "assumed-rate takes exactly 1 pair"
    else:
        if len(pairs) < 2:
            return None, "%s needs at least 2 pairs" % mode
        fitted = _lsq_slope(pairs)
        if fitted is None:
            return None, "degenerate anchor span"
        canonical = float("%.6f" % fitted)
        if "%.6f" % clean["rate"] != "%.6f" % canonical:
            return None, ("rate must be the least-squares fit over the pairs "
                          "(%.6f)" % canonical)
        if not (lo <= canonical <= hi):
            return None, "rate out of range"
        # the canonical value — the session row's exact 6 dp representation — is
        # what gets validated against and persisted, never the client float
        clean["rate"] = canonical
    expected = expected_orig_t(mode, clean["rate"], pairs, clean["stream_t"])
    if abs(clean["orig_t"] - expected) > MAPPING_TOLERANCE_S:
        return None, ("orig_t inconsistent with the anchor mapping "
                      "(expected %.6f)" % expected)
    return clean, None


def _lsq_slope(pairs):
    """The least-squares slope of orig_s over stream_s — §6.2's fitted rate (the
    exact two-point line for two pairs). None when the anchor span is degenerate
    (no variance to fit over)."""
    n = len(pairs)
    sm = sum(p["stream_s"] for p in pairs) / n
    om = sum(p["orig_s"] for p in pairs) / n
    den = sum((p["stream_s"] - sm) ** 2 for p in pairs)
    if den <= 1e-9:
        return None
    return sum((p["stream_s"] - sm) * (p["orig_s"] - om) for p in pairs) / den


def expected_orig_t(mode, rate, pairs, stream_t):
    """The §6.2/§6.3 mapping's original-native instant for `stream_t` — exactly what
    the session's rows must be able to reproduce.

    constant-rate: the fixed-slope least-squares line through the anchors
    (`a = mean(orig_i − r·stream_i)`, the LSQ intercept given slope r — the fit the
    client displays). re-anchor / assumed-rate: governed by the last anchor at or
    before `stream_t` (else the first): `orig_i + r·(t − stream_i)` (§6.3; with one
    anchor all three modes coincide). Clamped to [0, MAX_T_S]: §6.4's stream-solo
    margins map to instants before the original's clock zero, where the client
    clamps too."""
    if mode == "constant-rate":
        a = sum(p["orig_s"] - rate * p["stream_s"] for p in pairs) / len(pairs)
        exp = a + rate * stream_t
    else:
        ordered = sorted(pairs, key=lambda p: p["stream_s"])
        gov = ordered[0]
        for p in ordered:
            if p["stream_s"] <= stream_t:
                gov = p
        exp = gov["orig_s"] + rate * (stream_t - gov["stream_s"])
    return min(max(exp, 0.0), align_inspect.MAX_T_S)


# --- the §6.6 grammar -----------------------------------------------------------------

def _context_of(clean):
    """The session context a request implies: (mode, rate@6dp, the anchor set).
    Anchors compare at emitted precision, stream-ordered — exactly what the
    session's anchor rows record."""
    anchors = tuple(sorted(
        ("%.6f" % p["stream_s"], p["k"], "%.6f" % p["orig_s"]) for p in clean["pairs"]))
    return (clean["mode"], "%.6f" % clean["rate"], anchors)


def _last_context(lines, orig):
    """(max_session_key, that session's (mode, rate, anchors)) from the file's rows,
    or (0, None) for a fresh file. Keyed by s<K>, never by row order: the rows are
    expressly allowed to ride sort_tsv.py, so append order proves nothing."""
    sessions = {}
    anchors = {}
    for line in lines:
        m = _SESSION_ROW_RE.match(line)
        if m:
            sessions[int(m.group(1))] = (m.group(2), m.group(3))
            continue
        # FAIL CLOSED on a session-shaped row the grammar can't parse: counting it
        # as "no session" would restart keys at s1 and merge two mappings under one
        # key — the exact property the file exists to preserve. (A note's free text
        # can't trigger this: it sits after "| " and tabs are sanitized out of it.)
        cols = line.split("\t")
        if len(cols) == 3 and cols[2].startswith("note REVIEW session "):
            raise OSError("unparseable session row in the review file: %r"
                          % line[:120])
        m = _ANCHOR_ROW_RE.match(line)
        if m and int(m.group(4)) == int(orig):
            anchors.setdefault(int(m.group(2)), []).append(
                (m.group(1), m.group(3), m.group(5)))
    if not sessions:
        return 0, None
    last = max(sessions)
    mode, rate = sessions[last]
    return last, (mode, rate, tuple(sorted(anchors.get(last, []))))


def session_rows(key, clean, date):
    """The session row + one anchor row per selected pair (§6.6 context rows)."""
    n = len(clean["pairs"])
    rows = ["0.000000\t0.000000\tnote REVIEW session s%d: %s rate %.6f from %d %s %s"
            % (key, clean["mode"], clean["rate"], n,
               "pair" if n == 1 else "pairs", date)]
    for p in sorted(clean["pairs"], key=lambda p: p["stream_s"]):
        rows.append("%.6f\t%.6f\tnote REVIEW anchor s%d: %s orig%03d %.6f"
                    % (p["stream_s"], p["stream_s"], key, p["k"], clean["orig"],
                       p["orig_s"]))
    return rows


def note_row(key, clean):
    return "%.6f\t%.6f\tnote REVIEW s%d: orig%03d %.6f | %s" % (
        clean["stream_t"], clean["stream_t"], key, clean["orig"], clean["orig_t"],
        clean["text"])


def append_note(clean):
    """Append the §6.6 rows for one validated note; fsync before returning.

    Under the module lock: read the pair file, allocate (or reuse) the session key
    by scanning its session rows, emit session + anchor rows only when the mapping
    context changed, then the note row — one durable append, fsynced before the
    caller acks. Returns {"ok", "session", "file", "rows"}.
    """
    rdir = review_dir()
    if rdir is None:
        raise OSError("no checkout configured")
    with _append_lock:
        new_dir = not os.path.isdir(rdir)
        os.makedirs(rdir, exist_ok=True)
        fname = pair_filename(clean["stem"], clean["orig"])
        path = os.path.join(rdir, fname)
        if not align_inspect._contained(path, rdir):
            raise OSError("review path escapes the review dir")
        new_file = not os.path.isfile(path)
        lines = []
        if not new_file:
            with open(path, "r", encoding="utf-8") as handle:
                lines = handle.read().splitlines()
        last_key, last_ctx = _last_context(lines, clean["orig"])
        rows = []
        if last_ctx == _context_of(clean) and last_key > 0:
            key = last_key
        else:
            key = last_key + 1
            rows.extend(session_rows(key, clean, _today()))
        rows.append(note_row(key, clean))
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("".join(r + "\n" for r in rows))
            handle.flush()
            os.fsync(handle.fileno())
        if new_file:
            # a first note also creates the FILE's directory entry (and possibly
            # labels/review/ itself): fsync the parent dirs too, or a power loss right
            # after the 200 could drop the acked note
            fsync_dir(rdir)
            if new_dir:
                fsync_dir(os.path.dirname(rdir))
        return {"ok": True, "session": "s%d" % key, "file": fname,
                "rows": len(rows)}


def handle_review_note(params, mp3_dir):
    """The route body → (payload, http_status). Dark (404) without a checkout
    directory; 400 on validation; the append is fsynced before the 200."""
    if not enabled():
        return {"error": "review notes not configured "
                         "(no checkout directory to write labels/review/ into)"}, 404
    clean, err = validate(params, mp3_dir)
    if err:
        return {"error": err}, 400
    try:
        return append_note(clean), 200
    except OSError as exc:
        return {"error": "append failed", "detail": str(exc)[:200]}, 500
