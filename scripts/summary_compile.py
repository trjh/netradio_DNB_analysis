"""DP-24 §3: the summary compile — `labels/automated/` + `labels/review/` → `labels/summary/`.

The export button's job (docs/PLAN_labels_layout.md §3; supersedes the DP-25 write-back
sketch). **Compile, not edit:** read every automated file and every review file in this
checkout's `labels/` (`align_inspect.repo_root()`), emit exactly one importable Audacity label
set per audio file, touch nothing else:

- `labels/summary/<stem>.summary.tsv` (stream-clocked): every automated row for that
  capture — all its `<stem>.origNNN.match.hints.tsv` pair files merged, plus its
  `<stem>.hints.tsv` — and every `<stem>.origNNN.review.tsv` row, verbatim, time-sorted;
  including the converter's per-anchor **seat rows** (`origNNN start: k confidence n/10`,
  the native clip seat `a_k − b_native_k` — requirement and correction of 2026-08-17: where
  the record, imported at its own rate, is dragged so it lines up at anchor k; NOT the
  rate-corrected zero). The converter omits a seat that falls before the capture's first
  sample (TIMELINE_GUIDE), so a capture can cover an original with NO seat row at all — the
  compile fills exactly that gap: one `origNNN start: k confidence n/10` row in the same
  grammar at the pair's best-confidence anchor (tie → earliest), plus an `origNNN note:` row
  saying so. When the converter emitted any seat row for the pair, the compile adds none.
  Track 3 of the six-track session.
- `labels/summary/origNNN.summary.tsv` (original-clocked): that original's
  `origNNN.<stem>.match.hints.tsv` rows from EVERY capture it appears in, merged and
  time-sorted, plus every review row about it converted to the record's clock — each review
  row already carries `origNNN <orig_s>` in its text (the §6.6 grammar), so the conversion
  is a read, not arithmetic. Track 6.

Rows are copied **verbatim** — the compile never rewrites a ` verified` token of either
family (`docs/TIMELINE_GUIDE.md`), never renumbers a marker (`track sync: k` in the stem
summary ↔ `origNNN sync: k` in the original summary stay identical by construction), and
drops nothing: `note QUESTION:` rows and every other automated line ride through (decision
made 2026-08-11). The only rows it ADDS are the gap-filling seat rows above. Time columns are
re-emitted at 6 dp. A torn final line (no trailing newline — a review append in flight) is
dropped and counted, never copied; the review directory is read under `review_notes`'
append lock so an in-process append cannot interleave (the CLI and the server are separate
processes — both are idempotent and rename-atomic, so the worst case is a redundant write).

Derived-only and idempotent: safe to run at any time, any number of times. Each file is
written whole (temp + fsync + rename, then the directory fsynced) so a half-landed summary
is not a state the directory can be in; an unchanged file is left untouched; a
`*.summary.tsv` the compile did not produce is stale derived output and is removed.
Identifiers come from the file names in the two controlled directories, validated against
the same stem/orig shapes the inspector uses (a stem shaped like an original's tag is
refused — its summary name would collide), and every path is containment-checked inside its
own subdirectory — a symlink out of `automated/` reaches nothing, not even the hand labels
one level up. No request names a path (the §3.3 contract; the route body carries nothing).

Dark (404) without a `labels/` directory; the DSP venv is NOT required — this is parsing
and arithmetic. `review_sync` already commits `labels/summary/*.summary.tsv` (its PATHSPEC),
so the route nudges it the way the review-note route does.
"""

import os
import re
import sys
import tempfile
import threading

import align_inspect
import review_notes
from review_notes import fsync_dir

_STEM = r"([A-Za-z0-9][A-Za-z0-9._-]{0,63})"
_PAIR_STREAM_RE = re.compile(r"^" + _STEM + r"\.orig(\d{3})\.match\.hints\.tsv$")
_PAIR_ORIG_RE = re.compile(r"^orig(\d{3})\." + _STEM + r"\.match\.hints\.tsv$")
_STEM_HINTS_RE = re.compile(r"^" + _STEM + r"\.hints\.tsv$")
_REVIEW_RE = re.compile(r"^" + _STEM + r"\.orig(\d{3})\.review\.tsv$")
_SUMMARY_RE = re.compile(r"^(?:" + _STEM + r"|orig\d{3})\.summary\.tsv$")

# review rows → the original's clock: every §6.6 row names `origNNN <orig_s>` itself
_REVIEW_NOTE_RE = re.compile(r"^note REVIEW s\d+: orig(\d{3}) ([0-9]+\.[0-9]+) \| ")
_REVIEW_ANCHOR_RE = re.compile(
    r"^note REVIEW anchor s\d+: \S+ orig(\d{3}) ([0-9]+\.[0-9]+)\s*$")
_REVIEW_SESSION_RE = re.compile(r"^note REVIEW session s\d+: ")
_ORIG_START_RE = re.compile(r"^orig(\d{3}) start:")
_ORIG_TAG_RE = re.compile(r"^orig\d{3}$")     # a stem may not look like an original's tag

_compile_lock = threading.Lock()


def labels_dir():
    repo = align_inspect.repo_root()
    return os.path.join(repo, "labels") if repo else None


def summary_dir():
    ldir = labels_dir()
    return os.path.join(ldir, "summary") if ldir else None


def enabled():
    """The compile's capability: a checkout with a labels/ directory."""
    ldir = labels_dir()
    return bool(ldir and os.path.isdir(ldir))


def _read_rows(path):
    """Verbatim rows: (rows, torn). A line that is not a 3-column label row is skipped
    (the same tolerance as align_inspect._read_tsv — hints are suggestions). A final
    line with no trailing newline is an append in flight: dropped, reported as torn."""
    rows = []
    with open(path, "r", encoding="utf-8") as handle:
        lines = handle.read().split("\n")
    torn = bool(lines[-1])          # text after the last newline
    for line in lines[:-1]:
        parts = line.split("\t")
        if len(parts) >= 3:
            try:
                rows.append((float(parts[0]), float(parts[1]), parts[2]))
            except ValueError:
                continue
    return rows, torn


def _sort_key(row):
    """sort_tsv.py's discipline: `file start` rows first, then time-major, then text."""
    t0, t1, text = row
    return (0 if text.startswith("file start") else 1, t0, t1, text)


def _render(rows):
    return "".join("%.6f\t%.6f\t%s\n" % row for row in sorted(rows, key=_sort_key))


def _listing(dirpath):
    """The files of one controlled directory, containment-checked INSIDE that directory
    (a symlink pointing anywhere else — the hand labels one level up included — is
    simply not listed)."""
    if not os.path.isdir(dirpath):
        return []
    out = []
    for fn in sorted(os.listdir(dirpath)):
        path = os.path.join(dirpath, fn)
        if os.path.isfile(path) and align_inspect._contained(path, dirpath):
            out.append((fn, path))
    return out


def _seat_rows(orig, srows, orows):
    """The gap-filler: when the pair's stream file carries NO `origNNN start:` row for
    this original (the converter omits seats before the capture's first sample), one
    seat row in the converter's own grammar at the best-confidence anchor present on
    both sides (tie → earliest), plus a note row saying where it came from. [] when the
    converter already emitted a seat, or when no marker pairs."""
    for _, _, text in srows:
        m = _ORIG_START_RE.match(text)
        if m and int(m.group(1)) == orig:
            return []
    streams = {}                    # marker -> (stream_s, confidence)
    for t, _, text in srows:
        m = align_inspect._TRACK_SYNC_RE.match(text)
        if m and m.group(1) not in streams:
            streams[m.group(1)] = (t, float(m.group(2)))
    best = None                     # (-confidence, stream_s, native_s, marker)
    for t, _, text in orows:
        m = align_inspect._ORIG_SYNC_RE.match(text)
        if m and int(m.group(1)) == orig and m.group(2) in streams:
            a, conf = streams[m.group(2)]
            cand = (-conf, a, t, m.group(2))
            if best is None or cand < best:
                best = cand
    if best is None:
        return []
    conf, a, b, k = -best[0], best[1], best[2], best[3]
    seat = a - b
    return [
        (seat, seat, "orig%03d start: %s confidence %.1f/10" % (orig, k, conf)),
        (seat, seat, "orig%03d note: seat compiled by the summary compile at anchor %s "
                     "(stream %.6f = native %.6f); the converter emitted no start row for "
                     "this pair%s" % (orig, k, a, b,
                                      "" if seat >= 0 else
                                      " (every anchor seats it before the capture's first "
                                      "sample)")),
    ]


def _review_to_orig(rows, orig):
    """Review rows on the original's clock: the position each row's own text carries.
    Returns (converted_rows, unconverted_count) — a row the grammar cannot place on
    the record's clock stays in the stem summary only (nothing is dropped there)."""
    out, unconverted = [], 0
    for t0, t1, text in rows:
        m = _REVIEW_NOTE_RE.match(text) or _REVIEW_ANCHOR_RE.match(text)
        if m:
            if int(m.group(1)) == orig:
                t = float(m.group(2))
                out.append((t, t, text))
            else:
                unconverted += 1
        elif _REVIEW_SESSION_RE.match(text):
            out.append((0.0, 0.0, text))       # a context row: at zero on both clocks
        else:
            unconverted += 1
    return out, unconverted


def gather(ldir):
    """Read both input directories → {"stems": {stem: rows}, "origs": {orig: rows},
    "unconverted": n, "torn": [files]}. Pure: no writes."""
    stems, origs = {}, {}
    pairs = {}          # (stem, orig) -> {"s": rows, "o": rows}
    unconverted, torn = 0, []

    def read(fn, path):
        rows, is_torn = _read_rows(path)
        if is_torn:
            torn.append(fn)
        return rows

    for fn, path in _listing(os.path.join(ldir, "automated")):
        m = _PAIR_STREAM_RE.match(fn)
        if m and not _ORIG_TAG_RE.match(m.group(1)):
            rows = read(fn, path)
            stems.setdefault(m.group(1), []).extend(rows)
            pairs.setdefault((m.group(1), int(m.group(2))), {})["s"] = rows
            continue
        m = _PAIR_ORIG_RE.match(fn)
        if m and not _ORIG_TAG_RE.match(m.group(2)):
            rows = read(fn, path)
            origs.setdefault(int(m.group(1)), []).extend(rows)
            pairs.setdefault((m.group(2), int(m.group(1))), {})["o"] = rows
            continue
        m = _STEM_HINTS_RE.match(fn)
        if m and not _ORIG_TAG_RE.match(m.group(1)):
            stems.setdefault(m.group(1), []).extend(read(fn, path))
    for (stem, orig), sides in sorted(pairs.items()):
        if "s" in sides and "o" in sides:
            stems.setdefault(stem, []).extend(_seat_rows(orig, sides["s"], sides["o"]))
    # the review files are appended to by review_notes (same process for the route):
    # read them under its lock so a multi-row append cannot interleave with the read
    with review_notes._append_lock:
        review = [(fn, path, read(fn, path))
                  for fn, path in _listing(os.path.join(ldir, "review"))
                  if _REVIEW_RE.match(fn)]
    for fn, path, rows in review:
        m = _REVIEW_RE.match(fn)
        stem, orig = m.group(1), int(m.group(2))
        if _ORIG_TAG_RE.match(stem):
            continue
        stems.setdefault(stem, []).extend(rows)
        converted, n = _review_to_orig(rows, orig)
        unconverted += n
        if converted:
            origs.setdefault(orig, []).extend(converted)
    return {"stems": stems, "origs": origs, "unconverted": unconverted, "torn": torn}


def _umask():
    cur = os.umask(0)
    os.umask(cur)
    return cur


def _write_whole(path, content, sdir):
    """Temp + fsync + rename in the summary dir: the file is either the old bytes or
    the new bytes, never a prefix. Returns True when the bytes changed."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            if handle.read() == content:
                return False
    except (OSError, ValueError):
        pass          # a summary we cannot read (or decode) is derived output: overwrite it
    fd, tmp = tempfile.mkstemp(prefix=".summary-", suffix=".tmp", dir=sdir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o666 & ~_umask())          # mkstemp gives 0600; summaries are shared
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return True


def compile_summaries(dry_run=False):
    """The compile. Returns the report; raises OSError when unconfigured."""
    ldir = labels_dir()
    if not ldir or not os.path.isdir(ldir):
        raise OSError("no labels directory in the checkout")
    sdir = os.path.join(ldir, "summary")
    if not any(os.path.isdir(os.path.join(ldir, d)) for d in ("automated", "review")):
        # A checkout mid-sync or on a pre-DP-24 commit has neither input directory. Treating
        # that as "no inputs" would delete EVERY summary and hand review_sync the deletions
        # to commit — a configuration error, never a compile result.
        raise OSError("no labels/automated or labels/review to compile from "
                      "(checkout mid-pull or pre-DP-24?)")
    with _compile_lock:
        found = gather(ldir)
        outputs = []
        for stem in sorted(found["stems"]):
            outputs.append(("stem", stem, "%s.summary.tsv" % stem, found["stems"][stem]))
        for orig in sorted(found["origs"]):
            outputs.append(("orig", "orig%03d" % orig, "orig%03d.summary.tsv" % orig,
                            found["origs"][orig]))
        report = {"ok": True, "dry_run": bool(dry_run), "dir": "labels/summary",
                  "files": [], "removed": [], "unconverted": found["unconverted"],
                  "torn": found["torn"]}
        produced = set()
        new_dir = not os.path.isdir(sdir)
        if not dry_run:
            os.makedirs(sdir, exist_ok=True)
        for kind, ident, fname, rows in outputs:
            path = os.path.join(sdir, fname)
            if not align_inspect._contained(path, sdir):
                report["files"].append({"kind": kind, "id": ident, "file": fname,
                                        "rows": len(rows), "changed": None,
                                        "error": "path escapes labels/summary — skipped"})
                continue
            produced.add(fname)
            changed = None
            if not dry_run:
                changed = _write_whole(path, _render(rows), sdir)
            report["files"].append({"kind": kind, "id": ident, "file": fname,
                                    "rows": len(rows), "changed": changed})
        for fn, path in _listing(sdir):
            if _SUMMARY_RE.match(fn) and fn not in produced:
                report["removed"].append(fn)
                if not dry_run:
                    os.unlink(path)
        if not dry_run and (report["removed"]
                            or any(f["changed"] for f in report["files"])):
            fsync_dir(sdir)
            if new_dir:
                fsync_dir(ldir)
        return report


def handle_compile(params):
    """The route body → (payload, http_status). Dark without a labels/ directory;
    the body carries no parameters (there is nothing to name — the compile is whole)."""
    if not enabled():
        return {"error": "summary compile not configured "
                         "(no labels/ directory in the checkout)"}, 404
    if params is not None and not isinstance(params, dict):
        return {"error": "request body must be a JSON object"}, 400
    dry_run = bool((params or {}).get("dry_run"))
    try:
        return compile_summaries(dry_run=dry_run), 200
    except (OSError, ValueError) as exc:
        # ValueError covers a non-UTF-8 byte in an input (UnicodeDecodeError); the
        # message never carries the checkout's absolute path onto the wire
        return {"error": "compile failed: " + _scrub(str(exc))}, 500


def _scrub(message):
    ldir = labels_dir()
    return message.replace(ldir, "<labels>") if ldir else message


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    dry = "--dry-run" in argv
    import envfile                       # the repo's one `.env` reader
    envfile.load(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
    payload, status = handle_compile({"dry_run": dry})
    if status != 200:
        print("ERROR: %s" % payload.get("error"), file=sys.stderr)
        return 1
    for f in payload["files"]:
        state = "dry-run" if payload["dry_run"] else (
            "written" if f["changed"] else "unchanged")
        print("%-9s %-40s %5d rows  %s" % (f["kind"], f["file"], f["rows"], state))
    for fn in payload["removed"]:
        print("removed   %s" % fn)
    if payload["unconverted"]:
        print("%d review row(s) stayed stream-side only (not placeable on a record's clock)"
              % payload["unconverted"])
    for fn in payload["torn"]:
        print("torn last line dropped (append in flight?): %s" % fn)
    return 0


if __name__ == "__main__":
    sys.exit(main())
