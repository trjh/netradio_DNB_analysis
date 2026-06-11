"""Localized alignment + skip detection (P2).

A single global offset is meaningless when a capture contains skips. Instead we
walk the overlap in short windows: in each window find the *local* offset to the
other file. Over a skip-free segment the offset is constant (confidence ~1); at a
skip the confidence dips and the offset steps by the skip magnitude. The sequence
of confident offsets is piecewise-constant; its steps ARE the skips.

This is the algorithmic form of Tim overlaying two files and listening for the
moment they fall out of sync.

Offset convention matches align.py: a[i] ~ b[i - offset] over the overlap; offset
in samples. A positive step in offset(t) means b jumped *back* relative to a (b
re-covered content); a negative step means b skipped *ahead*.
"""

import numpy as np

from . import audio as _audio
from .align import _ncc_at, _next_pow2, _parabolic


def local_offset(a, b, a_lo, a_hi, expected_offset, radius, phat=True):
    """Best offset for the A-window [a_lo,a_hi) searching +/- radius of expected.

    Returns (offset_samples_float, confidence) or None if the search region falls
    outside b. Confidence is the normalized correlation of the matched samples.
    """
    a_lo, a_hi = int(a_lo), int(a_hi)
    eo, radius = int(round(expected_offset)), int(radius)
    aseg = np.asarray(a[a_lo:a_hi], dtype=np.float64)
    if len(aseg) < 64:
        return None
    aseg = aseg - aseg.mean()
    b_lo = max(0, (a_lo - eo) - radius)
    b_hi = min(len(b), (a_hi - eo) + radius)
    if b_hi - b_lo < len(aseg):
        return None
    bseg = np.asarray(b[b_lo:b_hi], dtype=np.float64)
    bseg = bseg - bseg.mean()
    n = len(aseg) + len(bseg) - 1
    nfft = _next_pow2(n)
    fa = np.fft.rfft(aseg, nfft)
    fb = np.fft.rfft(bseg, nfft)
    cross = fa * np.conj(fb)
    if phat:
        mag = np.abs(cross)
        mag[mag < 1e-12] = 1e-12
        cc = np.fft.irfft(cross / mag, nfft)
    else:
        cc = np.fft.irfft(cross, nfft)
    k = int(np.argmax(cc))
    lag = k if k < nfft // 2 else k - nfft
    frac = _parabolic(cc, k)
    offset = (a_lo - b_lo) + (lag + frac)
    conf = _ncc_at(aseg, bseg, lag)
    return offset, conf


def walk_overlap(a, b, a_start_s, a_end_s, seed_offset_s,
                 win_s=8.0, hop_s=1.0, radius_s=3.0, track_conf=0.6,
                 sr=_audio.SR):
    """Walk A's overlap window-by-window, tracking the local offset to B.

    `seed_offset_s` is the approximate offset (seconds) at a_start. Returns a list
    of (a_time_s, offset_s, confidence). The offset estimate is carried forward
    only through confident windows, so it survives the low-confidence window that
    straddles a skip and re-locks just past it (skips up to ~radius are recovered).

    Defaults are the values validated against the documented d065-087/d084-103b
    skips and are NOT free knobs: a window < ~8 s locks onto DnB's periodic beat
    (confidence collapses), and a radius >= ~12 s admits wrong-beat false locks
    (offset is tracked continuously so each skip step is small). Override only with
    care. Large skips (the rare ~10 s one) need a wider radius — handle adaptively.
    """
    win, hop, radius = int(win_s * sr), int(hop_s * sr), int(radius_s * sr)
    offset = seed_offset_s * sr
    pos = int(a_start_s * sr)
    end = int(a_end_s * sr)
    out = []
    while pos + win <= end:
        r = local_offset(a, b, pos, pos + win, offset, radius)
        if r is not None:
            off, conf = r
            if conf >= track_conf:
                offset = off
            out.append((pos / sr, off / sr, conf))
        pos += hop
    return out


def detect_skips(walk, min_jump_s=0.04, conf_min=0.80, sr=_audio.SR):
    """Find skips as steps in the confident offset(t) track.

    `walk`: output of walk_overlap. Returns list of dicts:
      {at_s: A-time of the step, delta_s: offset jump, before_s, after_s}.
    Adjacent confident samples whose offset differs by > min_jump_s bracket a skip;
    the step is reported at their midpoint with the offset delta. Consecutive steps
    in the same direction within one window are merged.
    """
    pts = [(t, o) for t, o, c in walk if c >= conf_min]
    skips = []
    for i in range(1, len(pts)):
        (t0, o0), (t1, o1) = pts[i - 1], pts[i]
        delta = o1 - o0
        if abs(delta) >= min_jump_s:
            skips.append({"at_s": 0.5 * (t0 + t1), "delta_s": delta,
                          "before_s": t0, "after_s": t1})
    # Merge steps that are really one skip split across adjacent gaps (same sign,
    # within ~2 hops): keep the cumulative delta at the earliest position.
    merged = []
    for s in skips:
        if (merged and (s["at_s"] - merged[-1]["after_s"]) < 1.0
                and (s["delta_s"] > 0) == (merged[-1]["delta_s"] > 0)):
            merged[-1]["delta_s"] += s["delta_s"]
            merged[-1]["after_s"] = s["after_s"]
        else:
            merged.append(dict(s))
    return merged


def characterise_overlap(a_name, b_name, a_start_s, a_end_s, seed_offset_s,
                         **kw):
    """Full piecewise map of an overlap: the offset track + detected skips."""
    a = _audio.load_audio(a_name)
    b = _audio.load_audio(b_name)
    walk = walk_overlap(a, b, a_start_s, a_end_s, seed_offset_s,
                        **{k: v for k, v in kw.items()
                           if k in ("win_s", "hop_s", "radius_s", "track_conf")})
    skips = detect_skips(walk)
    return {"a": _audio.stem_of(a_name), "b": _audio.stem_of(b_name),
            "walk": walk, "skips": skips}
