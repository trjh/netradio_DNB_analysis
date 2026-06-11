"""Ground truth for the stream alignment engine.

Tim hand-aligned ~55 capture files by overlaying them in Audacity and recording
the result as ``file start sync:`` rows in ``labels/*.labels.tsv``. Those resolved
master-start values are the ground truth the engine is graded against, and the
``verified [by] X`` annotations record which neighbour each file was aligned
*against* — i.e. the edges of the alignment graph Tim built by hand.

The resolution logic here is a faithful port of the player's trusted
``server.py::parse_file_timeline`` (passes 1-3, starts only), kept independent so
the engine stands alone in the analysis repo. A test cross-checks it against the
player's parser and the TIMELINE_GUIDE smoke values.
"""

import os
import re

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LABELS_DIR = os.environ.get("NETRADIO_LABELS_DIR", os.path.join(REPO_ROOT, "labels"))

# A file-start sync row: `file [start] sync: NAME MASTER_OR_OFFSET [verified ...]`.
# The trailing number after a row that sits at local 0.0 in its own file IS that
# file's master start; embedded in another file at a non-zero timestamp it is a
# reference anchor and the real start is owner_master + row_seconds (resolved in
# the iterative pass). MARK / NEEDMARKINOWNFILE stand in for "offset is 0 here".
_SYNC_SEED = re.compile(r"\bfile(?:\s+start)?\s+sync:\s*([^\s]+)\s+(-?\d+(?:\.\d+)?)", re.I)
_SYNC_REF = re.compile(
    r"\bfile start sync:\s*([^\s]+)\s+(?:-?\d+(?:\.\d+)?|MARK|NEEDMARKINOWNFILE)", re.I)
_FILE_START = re.compile(r"\bfile start\s+([^\s]+\.(?:wav|au|mp3))\b", re.I)
_VERIFIED = re.compile(r"\bverified(?:\s+by)?\s+(.+)$", re.I)
_STEMISH = re.compile(r"[A-Za-z0-9][\w.\-]*")


def _stem(name):
    return os.path.splitext(os.path.basename((name or "").strip()))[0]


def _read_label_rows(labels_dir):
    rows = []
    if not os.path.isdir(labels_dir):
        return rows
    for fn in sorted(n for n in os.listdir(labels_dir) if n.endswith(".labels.tsv")):
        path = os.path.join(labels_dir, fn)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    parts = line.rstrip("\n").split("\t", 2)
                    if len(parts) < 3:
                        continue
                    try:
                        seconds = float(parts[0])
                    except ValueError:
                        continue
                    rows.append({"path": path, "seconds": seconds, "text": parts[2].strip()})
        except OSError:
            pass
    return rows


def _verified_refs(text):
    """Stems this row says it was verified against (graph edges)."""
    m = _VERIFIED.search(text)
    if not m:
        return []
    refs = []
    for tok in _STEMISH.findall(m.group(1)):
        # keep capture-file-looking tokens (start with a 'd' tile or an au name)
        if re.match(r"(?i)d[-\d]", tok) or tok.lower().startswith("dnb"):
            refs.append(_stem(tok))
    return refs


def resolve_starts(labels_dir=None):
    """Return {stem: master_start_seconds} from the hand `file start sync` rows.

    Port of server.py parse_file_timeline passes 1-3 (starts only), keyed by file
    stem (no extension) so it maps directly onto audio files.
    """
    labels_dir = labels_dir or LABELS_DIR
    rows = _read_label_rows(labels_dir)
    starts = {}
    current_by_path = {}

    # Pass 1: authoritative seeds — `file [start] sync: NAME MASTER` at local 0.0.
    for row in rows:
        m = _SYNC_SEED.search(row["text"])
        if not m or abs(row["seconds"]) > 0.01:
            continue
        name = _stem(m.group(1))
        try:
            master = float(m.group(2))
        except ValueError:
            continue
        starts[name] = master
        current_by_path.setdefault(row["path"], name)

    # Pass 2: plain `file start NAME` as a fallback for files with no sync seed.
    for row in rows:
        m = _FILE_START.search(row["text"])
        if not m or "file start sync" in row["text"].lower():
            continue
        name = _stem(m.group(1))
        if name in starts:
            continue
        owner = current_by_path.get(row["path"])
        if owner and owner in starts:
            starts[name] = starts[owner] + row["seconds"]

    # Pass 3: resolve cross-file syncs expressed in another file's coordinates.
    for _ in range(6):
        changed = False
        for row in rows:
            current = current_by_path.get(row["path"])
            if not current or current not in starts:
                continue
            m = _SYNC_REF.search(row["text"])
            if not m:
                continue
            name = _stem(m.group(1))
            if name == current:
                continue
            master = starts[current] + row["seconds"]
            old = starts.get(name)
            if old is None or abs(old - master) > 0.001:
                starts[name] = master
                changed = True
        if not changed:
            break
    return starts


def alignment_edges(labels_dir=None):
    """Return [(stem, verified_against_stem), ...] from the `verified` annotations.

    These are the pairs Tim actually overlaid by hand — useful both as P1
    validation pairs and as a seed for the P4 global-solve graph.
    """
    labels_dir = labels_dir or LABELS_DIR
    edges = []
    for row in _read_label_rows(labels_dir):
        m = _SYNC_SEED.search(row["text"]) or _SYNC_REF.search(row["text"])
        if not m:
            continue
        name = _stem(m.group(1))
        for ref in _verified_refs(row["text"]):
            if ref and ref != name:
                edges.append((name, ref))
    return edges


def ground_truth(labels_dir=None):
    """Convenience: {stem: master_start_seconds}, the grading table for P0."""
    return resolve_starts(labels_dir)
