"""Blind pairwise alignment + overlap-graph discovery (toward P4).

To place *unlabelled* files we can't supply a seed offset, so we align blind: take
a long window from one file and find where it occurs in the other. Confidence
cleanly separates a real overlap (~0.99) from none (~0.1), so the same primitive
both measures the offset and discovers which files overlap at all.

The discovered edges + Tim's hand `verified` edges form the graph the global solve
walks, anchored at d000-018 = master 0.
"""

import re

import numpy as np

from . import audio as _audio

_RANGE = re.compile(r"^d-?(\d+)-(\d+)")


def filename_range(stem):
    """Rough (start_min, end_min) hint from a `dNNN-MMM` stem, or None.

    These are only hints (the real master offset differs from the filename minutes
    by minutes), used to prune candidate pairs — never as timing truth.
    """
    m = _RANGE.match(stem)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def find_window_in(probe, signal):
    """Best position of `probe` within `signal` (FFT). Returns (lag, ncc).

    lag = index in `signal` where probe[0] aligns; ncc = normalized correlation of
    the matched region in [0,1].
    """
    p = np.asarray(probe, dtype=np.float64)
    p = p - p.mean()
    s = np.asarray(signal, dtype=np.float64)
    s = s - s.mean()
    n = len(s) + len(p) - 1
    nfft = 1 << (n - 1).bit_length()
    cc = np.fft.irfft(np.fft.rfft(s, nfft) * np.conj(np.fft.rfft(p, nfft)), nfft)
    k = int(np.argmax(cc))
    lag = k if k < nfft // 2 else k - nfft
    if lag < 0 or lag + len(p) > len(s):
        return lag, 0.0
    seg = s[lag:lag + len(p)]
    denom = np.linalg.norm(seg) * np.linalg.norm(p)
    ncc = float(np.dot(seg, p) / denom) if denom else 0.0
    return lag, ncc


def blind_offset(a_name, b_name, win_s=20.0, n_probes=8, sr=_audio.SR):
    """Seedless offset estimate: probe several windows of B, find them in A.

    Returns (offset_seconds, confidence). offset = master_start(b)-master_start(a)
    (a[i] ~ b[i-offset]); confidence is the best probe's normalized correlation.

    SCOPE / RELIABILITY (important): this reliably finds **large, mostly-clean**
    overlaps (confidence ~0.99) and reliably rejects non-overlaps (~0.1-0.3). It is
    NOT a definitive overlap test: a **small overlap** (a short region of two long
    files) or a **skip-heavy** overlap can score low (a 20 s probe rarely lands in a
    clean sub-segment of a short overlap, and an unbounded full-file search does not
    make the true — possibly quiet — peak dominate). Such pairs return low
    confidence even though they DO overlap (e.g. d084-103b/d065-087 ≈ 0.6,
    d086-105/d065-087 ≈ 0.1). So a low score means "no large clean overlap found",
    not "no overlap". Robust detection of small/skip-heavy overlaps is unsolved —
    see README. Use this for seeding, and prefer Tim's `verified` edges where they
    exist.
    """
    a = _audio.load_audio(a_name)
    b = _audio.load_audio(b_name)
    win = int(win_s * sr)
    if len(b) < win or len(a) < win:
        return 0.0, 0.0
    best_off, best_ncc = 0.0, -1.0
    for i in range(n_probes):
        # spread probes across B, away from the very edges
        frac = (i + 0.5) / n_probes
        p0 = max(0, min(int(frac * len(b)), len(b) - win))
        lag, ncc = find_window_in(b[p0:p0 + win], a)
        if ncc > best_ncc:
            best_ncc = ncc
            best_off = (lag - p0) / float(sr)
    return best_off, best_ncc


def candidate_pairs(stems, max_gap_min=30):
    """Pairs whose filename ranges are close enough to plausibly overlap.

    Prunes the O(n^2) blind sweep: only test pairs whose `dNNN-` start minutes are
    within `max_gap_min`. Files without a parseable range are paired with all
    (we don't know where they sit).
    """
    info = {s: filename_range(s) for s in stems}
    pairs = []
    for i, a in enumerate(stems):
        for b in stems[i + 1:]:
            ra, rb = info[a], info[b]
            if ra and rb and abs(ra[0] - rb[0]) > max_gap_min:
                continue
            pairs.append((a, b))
    return pairs


def discover_overlaps(stems, conf_min=0.8, max_gap_min=30, win_s=20.0):
    """Blind-align candidate pairs; keep the ones that clearly overlap.

    Returns (edges, skipped) where edges = [{a,b,offset_s,conf}] with conf>=conf_min.

    BEST-EFFORT, NOT COMPLETE: this finds large, mostly-clean overlaps and will
    **miss small / skip-heavy ones** (see `blind_offset` scope). So a missing edge
    does NOT prove two files don't overlap, and `connected_components()` over these
    edges can show spurious islands from detector false-negatives — do not treat
    component structure as ground truth. For the labelled region prefer Tim's
    `alignment_edges()` (known-good overlap pairs).
    """
    edges = []
    skipped = 0
    for a, b in candidate_pairs(stems, max_gap_min=max_gap_min):
        try:
            off, conf = blind_offset(a, b, win_s=win_s)
        except FileNotFoundError:
            continue
        if conf >= conf_min:
            edges.append({"a": a, "b": b, "offset_s": off, "conf": conf})
        else:
            skipped += 1
    return edges, skipped


def connected_components(stems, edges):
    """Union-find over the overlap edges → list of components (sets of stems)."""
    parent = {s: s for s in stems}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for e in edges:
        ra, rb = find(e["a"]), find(e["b"])
        if ra != rb:
            parent[ra] = rb
    comps = {}
    for s in stems:
        comps.setdefault(find(s), set()).add(s)
    return sorted(comps.values(), key=len, reverse=True)
