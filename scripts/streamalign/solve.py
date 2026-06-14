"""Global solve: chain pairwise offsets into absolute master positions (P4).

Given a set of overlapping pairs with measured offsets, anchor one file at master 0
and propagate outward: master_start(b) = master_start(a) + offset(a->b). The
result is every reachable file's absolute master start.

This first cut uses Tim's hand `verified` edges (known-good overlap pairs) and the
precise pairwise aligner, so it can be validated against ground truth on the
labelled region with NO dependence on the unsolved blind-overlap-discovery problem.
Redundant edges let `score.consistency_report` cross-check the result.
"""

from collections import defaultdict, deque

from . import align as _align
from . import audio as _audio
from . import groundtruth as _gt


def measure_edges(pairs, conf_min=0.7, decim=8):
    """Measure each (a, b) pair's offset with the precise aligner.

    Returns [{a, b, offset_s, conf}] for pairs that have audio and align with
    confidence >= conf_min. Low-confidence pairs (partial-overlap / multi-match /
    skip-heavy, per align.py's known limits) are dropped so they don't poison the
    solve; a file only reachable through such an edge is simply left unplaced.
    """
    edges = []
    for a, b in pairs:
        if not _audio.find_audio_file(a) or not _audio.find_audio_file(b):
            continue
        try:
            r = _align.align_pair(a, b, decim=decim)
        except (FileNotFoundError, RuntimeError):
            continue
        if r["confidence"] >= conf_min:
            edges.append({"a": a, "b": b, "offset_s": r["offset_seconds"],
                          "conf": r["confidence"]})
    return edges


def solve_positions(edges, anchor="d000-018", anchor_master=0.0):
    """Propagate offsets from the anchor → {stem: master_start_seconds}.

    BFS over the (undirected) overlap graph, preferring higher-confidence edges so
    each file is placed via its best available path. Files not connected to the
    anchor are omitted (reported separately by the caller).
    """
    adj = defaultdict(list)
    for e in edges:
        adj[e["a"]].append((e["b"], e["offset_s"], e["conf"]))
        adj[e["b"]].append((e["a"], -e["offset_s"], e["conf"]))
    pos = {anchor: float(anchor_master)}
    # Best-first by edge confidence (a light Prim/Dijkstra-ish walk): always extend
    # from the most confident frontier edge, so a noisy edge never wins when a
    # cleaner path exists.
    frontier = []  # list of (conf, from_node, to_node, offset)
    seen_edges = set()

    def push(u):
        for v, off, conf in adj[u]:
            if v not in pos:
                frontier.append((conf, u, v, off))

    push(anchor)
    while frontier:
        frontier.sort(key=lambda x: x[0])  # small list; highest conf at the end
        conf, u, v, off = frontier.pop()
        if v in pos:
            continue
        pos[v] = pos[u] + off
        push(v)
    return pos


def solve_from_verified(labels_dir=None, conf_min=0.7, anchor="d000-018"):
    """End-to-end: take Tim's verified edges, measure them, solve absolute positions.

    Returns (positions, edges). `positions` = {stem: master_start_seconds}.
    """
    pairs = _dedupe(_gt.alignment_edges(labels_dir))
    edges = measure_edges(pairs, conf_min=conf_min)
    positions = solve_positions(edges, anchor=anchor)
    return positions, edges


def placement_diagnostics(positions, edges):
    """Per-file reliability of a solve, from how its edges agree with the result.

    For each placed file, look at every edge touching it and compare the measured
    offset to the offset the solution implies; report the worst disagreement and the
    edge count. A file placed via a single edge is **uncorroborated** (residual 0 by
    construction — nothing cross-checks it, so a confident-but-wrong edge like the
    loop/pre-roll d-25-005b case is invisible to this check); a file with redundant
    edges and a small max residual is **corroborated**. Returns
    {stem: {edges, max_residual_s, corroborated}}.
    """
    by_file = defaultdict(list)
    for e in edges:
        a, b = e["a"], e["b"]
        if a in positions and b in positions:
            resid = abs(e["offset_s"] - (positions[b] - positions[a]))
            by_file[a].append(resid)
            by_file[b].append(resid)
    diag = {}
    for stem in positions:
        rs = by_file.get(stem, [])
        max_resid = max(rs) if rs else None
        diag[stem] = {
            "edges": len(rs),
            "max_residual_s": max_resid,
            # corroborated == cross-checked by >1 edge AND consistent to <0.1 s
            "corroborated": len(rs) > 1 and max_resid is not None and max_resid < 0.1,
        }
    return diag


def _dedupe(pairs):
    seen, out = set(), []
    for a, b in pairs:
        key = tuple(sorted((a, b)))
        if key not in seen:
            seen.add(key)
            out.append((a, b))
    return out
