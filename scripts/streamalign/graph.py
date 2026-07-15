"""Blind pairwise alignment + overlap-graph discovery (toward P4).

To place *unlabelled* files we can't supply a seed offset, so we align blind: take
a long window from one file and find where it occurs in the other. Confidence
cleanly separates a real overlap (~0.99) from none (~0.1), so the same primitive
both measures the offset and discovers which files overlap at all.

The discovered edges + Tim's hand `verified` edges form the graph the global solve
walks, anchored at d000-018 = master 0.
"""

import os
import re

from . import audio as _audio

# numpy is used only by the blind-alignment maths (find_window_in / blind_offset); imported
# lazily so those are the sole numpy touch-point in this module. (The package's `audio` import
# still brings numpy in transitively -- sort_tsv guards its next_stem call for that reason.)
def _np():
    import numpy as np
    return np


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
    np = _np()
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


def _loose(stem):
    """Coerce the notes' `dnb356-375` to the on-disk `d356-375` without needing the audio.

    `tracklist2017.normalise_stem` does this properly by checking which capture exists, but
    next_stem must run headless (in a worktree, in tests), so it falls back to the one known
    inconsistency: a `dnb` prefix for a `d` capture.
    """
    return re.sub(r"^dnb", "d", _audio.stem_of(stem), flags=re.IGNORECASE)


def _forward_link(stem, labels_dir=None):
    """The neighbour a `<stem>.labels.tsv` links as beginning latest inside it, or None.

    A `file_<other>:` row is the labeller saying "<other> starts here inside me" -- the
    authoritative successor relation. If several neighbours are linked (a file can overlap
    more than one), the one that begins LATEST is the successor; the earlier ones are
    mid-file overlaps.
    """
    from . import emit_labels as _emit
    from . import groundtruth as _gt
    path = os.path.join(labels_dir or _gt.LABELS_DIR, _audio.stem_of(stem) + ".labels.tsv")
    rows = _emit._read_label_lines(path)
    links = _emit._links_in(rows) if rows else {}
    if not links:
        return None
    return max(links, key=links.get)


def _tracklist_successor(stem):
    """The capture the 1998/2017 notes place immediately after `stem`, or None."""
    from . import tracklist2017 as _tl
    blocks = _tl.parse()
    target = _loose(stem)
    # prefer an explicit `(direct transition from <stem>)`
    for other, info in blocks.items():
        tf = info.get("transition_from")
        if tf and _loose(tf) == target:
            return _loose(other)
    # else the next block in the notes' stream order
    keys = [_loose(k) for k in blocks]
    if target in keys and keys.index(target) < len(keys) - 1:
        return keys[keys.index(target) + 1]
    return None


def _filename_successor(stem, labels_dir=None):
    """The labelled capture whose filename range starts nearest AFTER `stem`, or None.

    Weakest signal -- filename minutes are only hints (see filename_range) -- and used only
    when nothing better resolves.
    """
    from . import groundtruth as _gt
    here = filename_range(_audio.stem_of(stem))
    if not here:
        return None
    best, best_start = None, None
    for name in os.listdir(labels_dir or _gt.LABELS_DIR):
        if not name.endswith(".labels.tsv"):
            continue
        other = _audio.stem_of(name[: -len(".labels.tsv")])
        rng = filename_range(other)
        if not rng or rng[0] <= here[0]:
            continue
        if best_start is None or rng[0] < best_start:
            best, best_start = other, rng[0]
    return best


def next_stem(stem, labels_dir=None):
    """Best guess at the capture that follows `stem`, with the reason it was chosen.

    Returns `(stem, why)` or `(None, why)`. Sources, most-authoritative first:
      1. **hand link** -- a `file_<other>:` row in `<stem>.labels.tsv` (Tim saying "next");
      2. **1998/2017 notes** -- `tracklist2017`'s transition/stream order;
      3. **filename range** -- nearest labelled capture starting after this one (a hint).

    Pure logic (regex + reading label files + the notes) -- so sort_tsv can name the next file
    directly, before the slow engine step, without loading any audio.
    """
    stem = _audio.stem_of(stem)
    link = _forward_link(stem, labels_dir)
    if link:
        return link, "hand link (file_%s: in %s.labels.tsv)" % (link, stem)
    nb = _tracklist_successor(stem)
    if nb:
        return nb, "1998/2017 notes (follows %s)" % stem
    fn = _filename_successor(stem, labels_dir)
    if fn:
        return fn, "filename range -- a hint, verify (nearest after %s)" % stem
    return None, ("no successor found for %s -- add a file_<next>: link, or pass --nextfile"
                  % stem)


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
