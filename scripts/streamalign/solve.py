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
from . import skips as _skips


def measure_edges(pairs, conf_min=0.7, decim=8, skip_aware=True):
    """Measure each (a, b) pair's offset (default: skip-aware).

    Returns [{a, b, offset_s, conf}] for pairs that have audio and align with
    confidence >= conf_min. `skip_aware` measures the offset over the earliest
    skip-free segment (see measure_edge_skipaware), fixing the ~1 s bias when a skip
    sits between the dominant overlap and the files' shared start; set False to use
    align_pair's single global offset. Low-confidence pairs are dropped.
    """
    edges = []
    for a, b in pairs:
        if not _audio.find_audio_file(a) or not _audio.find_audio_file(b):
            continue
        try:
            if skip_aware:
                m = measure_edge_skipaware(a, b)
                if m is None:
                    continue
                offset_s, conf = m["offset_s"], m["conf"]
            else:
                r = _align.align_pair(a, b, decim=decim)
                offset_s, conf = r["offset_seconds"], r["confidence"]
        except (FileNotFoundError, RuntimeError):
            continue
        if conf >= conf_min:
            edges.append({"a": a, "b": b, "offset_s": offset_s, "conf": conf})
    return edges


def measure_edge_skipaware(a_name, b_name, min_overlap_s=25.0, walk_span_s=60.0,
                           sr=_audio.SR):
    """Edge offset measured over the EARLIEST skip-free segment of the overlap.

    `align_pair` returns one global offset = the dominant overlap region; a skip
    between that region and the files' shared start biases it by the skip size (the
    ~1 s d026-045 / d041-064 errors). For placement we want the offset over the
    first shared segment, which anchors b's start on the master clock correctly.

    Walks the first `walk_span_s` of the overlap (cheap), takes the median offset of
    the leading run of confident, same-offset windows. Returns {offset_s, conf} or
    None if no confident lock. Falls back to the single-offset refine when the
    overlap is too short to walk.
    """
    a = _audio.load_audio(a_name)
    b = _audio.load_audio(b_name)
    off0 = _align.coarse_offset(a, b) / float(sr)
    la, lb = len(a) / sr, len(b) / sr
    lo, hi = max(0.0, off0), min(la, lb + off0)
    if hi - lo < min_overlap_s:
        offset, conf = _align.refine_offset(a, b, around=int(round(off0 * sr)))
        return {"offset_s": offset / sr, "conf": conf}
    walk = _skips.walk_overlap(a, b, lo + 1.0, min(hi - 1.0, lo + 1.0 + walk_span_s),
                               off0)
    conf_pts = [(t, o, c) for t, o, c in walk if c >= 0.9]
    if not conf_pts:
        return None
    first_off = conf_pts[0][1]
    seg = []
    for _t, o, _c in conf_pts:  # leading run at the first segment's offset
        if abs(o - first_off) < 0.05:
            seg.append(o)
        else:
            break
    seg.sort()
    edge_off = seg[len(seg) // 2]
    confs = sorted(c for _t, _o, c in conf_pts)
    # Sanity: the first skip-free segment should sit within a few accumulated skips
    # of the coarse (dominant) offset. A large gap means the walk wandered onto a
    # wrong-beat lock (e.g. d104-108b +21 s) — fall back to the single dominant
    # offset rather than trust the wander.
    if abs(edge_off - off0) > 8.0:
        offset, conf = _align.refine_offset(a, b, around=int(round(off0 * sr)))
        return {"offset_s": offset / sr, "conf": conf}
    return {"offset_s": edge_off, "conf": confs[len(confs) // 2]}


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


def solve_robust(edges, anchor="d000-018", anchor_master=0.0,
                 max_residual_s=0.5, min_support=3, max_drops=20):
    """Solve, then drop only edges that are outliers vs *independently corroborated*
    placements.

    Naively dropping the worst residual against a confidence-ordered spanning tree is
    unsafe: a high-confidence *wrong* edge can enter the tree and make the residual
    pass blame a correct edge. So an edge is dropped only when, at one of its nodes,
    it disagrees (> `max_residual_s`) with the **median** of that node's incident
    edges AND the node has at least `min_support` incident edges (so the median has
    independent backing). Low-redundancy conflicts — a bare triangle, or a leaf —
    are genuinely ambiguous (especially if the bad edge is the confident one); those
    are NEVER dropped here and instead stay visible via `placement_diagnostics`.
    Returns (positions, dropped_edges).
    """
    kept = list(edges)
    dropped = []
    for _ in range(max_drops):
        pos = solve_positions(kept, anchor, anchor_master)
        incident = defaultdict(list)  # node -> [(edge, position it implies)]
        for e in kept:
            if e["a"] in pos and e["b"] in pos:
                incident[e["b"]].append((e, pos[e["a"]] + e["offset_s"]))
                incident[e["a"]].append((e, pos[e["b"]] - e["offset_s"]))
        worst = None  # (deviation, edge)
        for _node, lst in incident.items():
            if len(lst) < min_support:
                continue
            implied = sorted(p for _e, p in lst)
            med = implied[len(implied) // 2]
            for e, p in lst:
                dev = abs(p - med)
                if dev > max_residual_s and (worst is None or dev > worst[0]):
                    worst = (dev, e)
        if worst is None:
            break
        kept.remove(worst[1])
        dropped.append({**worst[1], "deviation_s": worst[0]})
    return solve_positions(kept, anchor, anchor_master), dropped


def solve_from_verified(labels_dir=None, conf_min=0.7, anchor="d000-018"):
    """End-to-end: take Tim's verified edges, measure them, solve absolute positions.

    Returns (positions, edges, dropped). `positions` = {stem: master_start_seconds};
    `dropped` = edges removed by consistency rejection.
    """
    pairs = _dedupe(_gt.alignment_edges(labels_dir))
    edges = measure_edges(pairs, conf_min=conf_min)
    positions, dropped = solve_robust(edges, anchor=anchor)
    return positions, edges, dropped


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
