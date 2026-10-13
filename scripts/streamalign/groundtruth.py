"""Ground truth for the stream alignment engine.

About 55 capture files were hand-aligned by overlaying them in Audacity and recording
the result as ``file start sync:`` rows in ``labels/*.labels.tsv``. Those resolved
master-start values are the ground truth the engine is graded against, and the
``verified [by] X`` annotations record which neighbour each file was aligned
*against* — i.e. the edges of the alignment graph built by hand.

The resolution is ``stream_files.resolve_timeline`` (the one label-timeline parser in
this repo), keyed by file stem here. A test checks it against known hand values
(``tests/test_streamalign.py`` ``SMOKE``).
"""

import os
import re

import stream_files

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LABELS_DIR = os.environ.get("NETRADIO_LABELS_DIR", os.path.join(REPO_ROOT, "labels"))
AUTOMATED_DIRNAME = "automated"


def automated_dir(labels_dir=None):
    """DP-24: where script-emitted label files land — labels/automated/.

    The labels/ root keeps only the hand-authoritative *.labels.tsv (plus the
    two human tools); machine emissions live one level down where they may be
    freely overwritten and are COMMITTED (no longer gitignored scratch).
    """
    return os.path.join(labels_dir or LABELS_DIR, AUTOMATED_DIRNAME)

# The label grammar and its resolution live in stream_files.py (scripts/, on sys.path wherever
# this package imports): seeds at local 0.0, `file start` fallbacks, and cross-file syncs whose
# trailing number is a verification anchor, never an offset.
_SYNC_SEED = stream_files.SYNC_SEED_RE
_SYNC_REF = stream_files.SYNC_REF_RE
_VERIFIED = re.compile(r"\bverified(?:\s+by)?\s+(.+)$", re.I)
_STEMISH = re.compile(r"[A-Za-z0-9][\w.\-]*")


def _stem(name):
    return os.path.splitext(os.path.basename((name or "").strip()))[0]


is_pipeline_label_file = stream_files.is_pipeline_label_file


def _read_label_rows(labels_dir):
    return stream_files.read_label_rows(labels_dir)


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

    stream_files.resolve_timeline's starts, keyed by file stem (no extension) so it maps
    directly onto audio files.
    """
    labels_dir = labels_dir or LABELS_DIR
    starts, _ends, _owners = stream_files.resolve_timeline(_read_label_rows(labels_dir), key=_stem)
    return starts


def alignment_edges(labels_dir=None):
    """Return [(stem, verified_against_stem), ...] from the `verified` annotations.

    These are the pairs actually overlaid by hand — useful both as P1
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
