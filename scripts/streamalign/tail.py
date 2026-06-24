"""Tail solve (P5): place the unlabelled tail captures d376-395 .. d525-532.

The tail was the documented blocker: these windows "do not overlap the anchored region
by enough for blind alignment to bridge them" (README known-limits). This module solves
them by exploiting two facts the generic solver didn't use:

  1. **The tail is its own dense overlap component.** d416-435 .. d525-532 ("Session B")
     overlap each other cleanly (conf > 0.98) — an internally-rigid 14-file body. It just
     had no edge to the anchored timeline, so it floated.

  2. **The broadcast is a loop, and Session B's tail wraps onto the loop start.** Its last
     files (d512-005, d525-532, d505-*) overlap the *placed* loop-start anchor d000-018
     (and d001-026b) at conf > 0.99. That wrap edge is the missing anchor: it pins the
     whole rigid body to the master clock.

Anchoring uses ONLY the clean, single-offset anchor file d000-018 / d001-026b — NOT the
pre-roll files d-25-000b / d-25-005b, which contain both the loop end and beginning and so
match at TWO offsets (the documented multi-match hazard). The d-25-* edges *do* corroborate
the same anchor once their own negative file-start offset is applied, but they are not used
to set it.

Two tail files remain unplaced by audio and are reported, not emitted:
  * d376-395  — overlaps the last placed file d356-375 by only ~59 s (conf ~0.6, a partial/
    skip-degraded lock); a placement CANDIDATE worth a by-ear confirm, not a corroborated edge.
  * d396-415  — butt-jointed on both sides (no overlap with d376-395 or d416-435) AND too far
    from the loop start to wrap; a genuine orphan, placeable only by contiguity (gap) evidence.

`solve_tail()` returns everything; `emit()` writes AUTO GENERATED labels for the corroborated
Session-B placements only.
"""

from statistics import median

from . import align as _align
from . import audio as _audio
from . import emit_labels as _emit
from . import groundtruth as _gt
from . import solve as _solve

# Session B: the dense internal overlap chain (every edge conf > 0.98).
SESSION_B_EDGES = [
    ("d416-435", "d425-444"), ("d416-435", "d425-438b"), ("d425-444", "d425-438b"),
    ("d425-444", "d436-455"), ("d425-438b", "d436-455"), ("d436-455", "d445-464"),
    ("d445-464", "d456-470"), ("d456-470", "d465-484"), ("d465-484", "d472-491"),
    ("d472-491", "d485-504"), ("d485-504", "d492-511"), ("d492-511", "d505-524"),
    ("d492-511", "d505-531b"), ("d505-524", "d505-531b"), ("d505-524", "d512-005"),
    ("d505-531b", "d525-532"), ("d512-005", "d525-532"),
]
ANCHOR_REF = "d416-435"   # arbitrary internal reference for the relative solve

# Clean loop-wrap anchor edges: Session-B tail -> the single-offset placed file d000-018
# (master 0, the SELF-INIT anchor) and d001-026b. Deliberately excludes the two-offset
# pre-roll files d-25-000b / d-25-005b.
WRAP_ANCHOR_EDGES = [
    ("d512-005", "d000-018"), ("d512-005", "d001-026b"), ("d525-532", "d000-018"),
]
# Corroborating pre-roll edges (two-offset; reported, not used to set the anchor).
WRAP_CORROBORATION_EDGES = [
    ("d505-524", "d-25-000b"), ("d505-531b", "d-25-000b"),
    ("d512-005", "d-25-000b"), ("d492-511", "d-25-000b"),
]

BRIDGE_EDGE = ("d356-375", "d376-395")   # last placed -> first tail (partial overlap)
ORPHAN = "d396-415"                       # butt-jointed both sides; no audio anchor

MAX_OVERLAP_S = 1200.0                     # two 1200 s windows can't overlap beyond this


def _measure(pairs):
    """[{a,b,offset_s,conf}] for pairs whose audio is present."""
    out = []
    for a, b in pairs:
        if not (_audio.find_audio_file(a) and _audio.find_audio_file(b)):
            continue
        r = _align.align_pair(a, b)
        out.append({"a": a, "b": b, "offset_s": r["offset_seconds"], "conf": r["confidence"]})
    return out


def solve_tail(labels_dir=None, anchor_conf_min=0.5):
    """Solve the tail. Returns a dict with the internal solve, the wrap anchor, the
    absolute Session-B placements, per-file diagnostics, and the two unplaced files.

    Master placements are in the loop-wrap (negative) representation anchored on
    d000-018 = 0, consistent with the project's existing negative-master pre-roll files.
    """
    gt = _gt.resolve_starts(labels_dir)

    # 1. Internal rigid solve of Session B (arbitrary anchor d416-435 = 0).
    sb = _measure(SESSION_B_EDGES)
    rel = _solve.solve_positions(sb, anchor=ANCHOR_REF, anchor_master=0.0)
    diag = _solve.placement_diagnostics(rel, sb)

    # 2. Anchor via the clean loop-wrap edges. Each implies S* = master_start(ANCHOR_REF):
    #    offset = master(placed_b) - master(sb_a) = gt[b] - (rel[a] + S*)
    anchor_estimates = []
    for a, b in WRAP_ANCHOR_EDGES:
        r = _align.align_pair(a, b)
        if r["confidence"] < anchor_conf_min or abs(r["offset_seconds"]) >= MAX_OVERLAP_S:
            continue
        s_star = gt[b] - r["offset_seconds"] - rel[a]
        anchor_estimates.append({"a": a, "b": b, "offset_s": r["offset_seconds"],
                                 "conf": r["confidence"], "s_star": s_star})
    if not anchor_estimates:
        raise RuntimeError("no clean loop-wrap anchor edge locked — tail stays floating")
    s_star = median(e["s_star"] for e in anchor_estimates)
    spread = max(e["s_star"] for e in anchor_estimates) - min(e["s_star"] for e in anchor_estimates)

    absolute = {stem: rel[stem] + s_star for stem in rel}

    # 3. The d376-395 bridge (candidate) and the d396-415 orphan.
    rb = _align.align_pair(*BRIDGE_EDGE)
    bridge = {"a": BRIDGE_EDGE[0], "b": BRIDGE_EDGE[1], "offset_s": rb["offset_seconds"],
              "conf": rb["confidence"], "master_start": gt[BRIDGE_EDGE[0]] + rb["offset_seconds"]}

    return {
        "relative": rel,
        "diagnostics": diag,
        "anchor_estimates": anchor_estimates,
        "s_star": s_star,
        "anchor_spread_s": spread,
        "absolute": absolute,
        "bridge": bridge,
        "orphan": ORPHAN,
    }


def emit(result, out_dir=None, labels_dir=None):
    """Write AUTO GENERATED labels for the corroborated Session-B placements only.

    The d376-395 candidate and the d396-415 orphan are intentionally NOT emitted — they are
    not corroborated. Returns {stem: path}.
    """
    out_dir = out_dir or _gt.LABELS_DIR
    positions = dict(result["absolute"])
    durations = _emit.durations_for(positions.keys())
    return _emit.emit_labels(positions, out_dir, durations=durations, labels_dir=labels_dir)
