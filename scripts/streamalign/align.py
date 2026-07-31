"""Pairwise alignment of two capture files (P1).

Between two stream captures, over a skip-free overlap, the audio is the *same
broadcast* — identical samples up to amplitude/noise (no clock drift, no polarity
flip; those were original-track-vs-stream artifacts, to be verified not assumed).
So aligning two captures is a pure time-delay estimation problem.

Strategy:
  1. Coarse: plain FFT cross-correlation on decimated signal -> integer offset to
     within `decim` samples, fast even for 20-min files.
  2. Fine: GCC-PHAT in a narrow window at full rate -> sharp peak; parabolic
     interpolation for sub-sample precision; normalized correlation as confidence.

Offset convention: `offset_samples` is `master_start(b) - master_start(a)` in
samples, i.e. how much later b's content sits than a's. Positive => b starts after
a. (a[t] == b[t - offset] over the overlap.)

`align_pair` derives the offset from scratch. To *verify* an already-labeled pair
(the `validate` command), `slice_check` instead cuts equal-length slices over the
labeled overlap and correlates those — robust for very different-length pairs,
where the coarse full-signal decode (split at `nfft/2`) would misread a large lag.
"""

import numpy as np

from . import audio as _audio


def _next_pow2(n):
    return 1 << (int(n) - 1).bit_length()


def _xcorr_full(a, b):
    """Linear cross-correlation via FFT. cc[k] ~ sum_t a[t]*b[t-k] for small k>0.

    Returns (cc, nfft); index k in [0,nfft): positive lag k for k<nfft/2, else the
    negative lag k-nfft.
    """
    n = len(a) + len(b) - 1
    nfft = _next_pow2(n)
    fa = np.fft.rfft(a, nfft)
    fb = np.fft.rfft(b, nfft)
    cc = np.fft.irfft(fa * np.conj(fb), nfft)
    return cc, nfft


def _as_float(x):
    return np.asarray(x, dtype=np.float64)


def coarse_offset(a, b, decim=8):
    """Integer-sample offset (full-rate) via decimated cross-correlation."""
    ad = _as_float(a[::decim])
    bd = _as_float(b[::decim])
    ad = ad - ad.mean()
    bd = bd - bd.mean()
    cc, nfft = _xcorr_full(ad, bd)
    k = int(np.argmax(cc))
    # Decode the circular index. Positive lags can only occupy k in [0, len(ad)-1]
    # (cc[k] pairs ad[t] with bd[t-k], so k >= len(ad) has no valid t); everything
    # above that is a wrapped negative lag. Splitting at nfft//2 instead is only
    # valid when len(a) == len(b) — with a short `a` against a long `b` (the
    # from-scratch solve path), a large negative lag wraps to k < nfft//2 and
    # decodes as a bogus positive offset.
    lag_d = k if k < len(ad) else k - nfft
    return lag_d * decim


def _parabolic(y, k):
    """Sub-sample peak offset from samples y[k-1..k+1] (returns delta in [-1,1])."""
    if k <= 0 or k >= len(y) - 1:
        return 0.0
    a, b, c = y[k - 1], y[k], y[k + 1]
    denom = (a - 2 * b + c)
    if denom == 0:
        return 0.0
    return 0.5 * (a - c) / denom


def refine_offset(a, b, around, radius=2000, win=None, phat=True):
    """Refine an integer offset near `around` (full-rate samples) with GCC-PHAT.

    Extracts a window from each signal over the overlap at the coarse offset and
    estimates the residual delay. Returns (offset_samples_float, confidence) where
    confidence is the normalized correlation peak in [0,1] (1 == identical).
    """
    return refine_offset_multi(a, b, around, radius=radius, win=win, phat=phat)[0]


def refine_offset_multi(a, b, around, radius=2000, win=None, phat=True,
                        n_peaks=1, min_sep=8000):
    """Like refine_offset, but returns the `n_peaks` best SEPARATED peaks in the range.

    Returns [(offset_samples_float, confidence)] sorted by correlation peak height,
    always at least one entry. Loop-based material (all of drum & bass) genuinely
    correlates at several whole-bar-shifted offsets, and the true seat is not always
    the tallest peak in a wide search window -- callers that search wide need the
    runners-up too. Peaks closer than `min_sep` samples (default 0.5 s at 16 kHz)
    count as the same peak.
    """
    a = _as_float(a)
    b = _as_float(b)
    # Overlap region in a's coordinates given offset `around` (a[t] ~ b[t-around]).
    lo = max(0, around)
    hi = min(len(a), len(b) + around)
    if hi - lo < 1000:
        return [(float(around), 0.0)]
    if win is None:
        win = min(hi - lo, 1 << 20)  # up to ~65 s at 16 kHz
    mid = (lo + hi) // 2
    a0 = max(lo, mid - win // 2)
    a1 = min(hi, a0 + win)
    aseg = a[a0:a1]
    # Corresponding b segment, widened by +/- radius for the fine search.
    b0 = a0 - around - radius
    b1 = a1 - around + radius
    if b0 < 0 or b1 > len(b):
        b0 = max(0, b0)
        b1 = min(len(b), b1)
    bseg = b[b0:b1]
    aseg = aseg - aseg.mean()
    bseg = bseg - bseg.mean()
    n = len(aseg) + len(bseg) - 1
    nfft = _next_pow2(n)
    fa = np.fft.rfft(aseg, nfft)
    fb = np.fft.rfft(bseg, nfft)
    cross = fa * np.conj(fb)
    if phat:
        mag = np.abs(cross)
        mag[mag < 1e-12] = 1e-12
        ccp = np.fft.irfft(cross / mag, nfft)
    else:
        ccp = np.fft.irfft(cross, nfft)
    # Search only the lags the caller asked for: `around` +/- `radius`. The correlation
    # array also contains every other circular lag -- including ranges that no sample
    # pairing can produce (zero-padding artefacts beyond the segment lengths) -- and a
    # spurious argmax there both breaks the "refine NEAR around" contract and, being
    # unpairable, scores confidence 0. (Found when the matchconv rate sweep widened
    # radius beyond the window length; small-radius callers get identical results.)
    lag0 = around - (a0 - b0)
    lags = np.arange(lag0 - radius, lag0 + radius + 1)
    # ...and only the lags whose sample pairing actually covers most of the window.
    # When the caller's b segment got clamped at the signal's edge, part of the
    # requested range pairs only a sliver of the segments (or nothing): its "peaks"
    # are zero-padding noise and their tiny-overlap confidences are meaningless.
    la, lb = len(aseg), len(bseg)
    shifts = -lags
    overlap = np.minimum(np.minimum(la, lb - shifts), la + shifts)
    lags = lags[overlap >= max(1000, la * 3 // 4)]
    if len(lags) == 0:
        return [(float(around), 0.0)]
    vals = ccp[lags % nfft].copy()
    peaks = []
    for _ in range(max(1, n_peaks)):
        i = int(np.argmax(vals))
        if not np.isfinite(vals[i]) or (peaks and vals[i] <= 0):
            break
        lag = int(lags[i])
        k = lag % nfft
        frac = _parabolic(ccp, k)
        # Convention: ccp[k] = sum_i aseg[i]*bseg[i-k], so the peak lag means
        # aseg[i] ~ bseg[i-lag]. With aseg[i]=a[a0+i] and bseg[j]=b[b0+j], a match
        # a[a0+i] ~ b[(a0+i)-offset] gives  b0 + i - lag = a0 + i - offset, i.e.
        #   offset = (a0 - b0) + lag.
        offset = (a0 - b0) + (lag + frac)
        peaks.append((float(offset), float(_ncc_at(aseg, bseg, lag))))
        vals[max(0, i - min_sep):i + min_sep + 1] = -np.inf
    return peaks


def _ncc_at(aseg, bseg, lag):
    """Normalized cross-correlation of the samples aligned at integer `lag`.

    Same convention as the peak: aseg[i] pairs with bseg[i-lag].
    """
    shift = -lag  # bseg index = i + shift
    if shift >= 0:
        # pair aseg[i] with bseg[i+shift]; aseg only shrinks if bseg runs out
        # (bseg is longer than aseg whenever the caller widened it by a search
        # radius, and truncating aseg by `shift` there zeroed the confidence of
        # every peak found beyond one aseg-length -- the radius>win bug)
        a_ = aseg[:max(0, min(len(aseg), len(bseg) - shift))]
        b_ = bseg[shift:shift + len(a_)]
    else:
        a_ = aseg[-shift:]
        b_ = bseg[:len(a_)]
    m = min(len(a_), len(b_))
    if m < 100:
        return 0.0
    a_ = a_[:m]
    b_ = b_[:m]
    na = np.linalg.norm(a_)
    nb = np.linalg.norm(b_)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a_, b_) / (na * nb))


def align_pair(a_name, b_name, decim=8, radius=2000, sr=_audio.SR):
    """Estimate the master-start offset between two captures by audio.

    Returns dict: offset_seconds, offset_samples, confidence, a, b.
    Positive offset_seconds => b's content starts later on the master timeline.
    """
    a = _audio.load_audio(a_name, sr=sr)
    b = _audio.load_audio(b_name, sr=sr)
    coarse = coarse_offset(a, b, decim=decim)
    offset, conf = refine_offset(a, b, around=coarse, radius=max(radius, decim * 4))
    return {
        "a": _audio.stem_of(a_name),
        "b": _audio.stem_of(b_name),
        "coarse_samples": coarse,
        "offset_samples": offset,
        "offset_seconds": offset / float(sr),
        "confidence": conf,
    }


# A chunk whose own confidence is below this is treated as "did not match", so its
# (meaningless) residual is not reported as the pair's drift. Separate from the CLI's
# --conf-ok, which decides the confirmed/suspect verdict.
_RESID_CONF_FLOOR = 0.5


def _chunk_xcorr(sla, slb):
    """Equal-length slice cross-correlation -> (residual_samples, confidence).

    Equal length is the whole point: the coarse full-signal bug came from very
    different-length inputs (the lag decode split at nfft/2 is only valid when
    len(a)==len(b)); with equal slices the peak is always well inside nfft/2, so any
    realistic residual decodes correctly. residual == how far the audio's best match
    sits from where the labels put it; confidence == normalized correlation (0 => no
    match at the labeled offset).
    """
    n = min(len(sla), len(slb))
    if n < 100:
        return 0.0, 0.0
    sla = _as_float(sla[:n]) - _as_float(sla[:n]).mean()
    slb = _as_float(slb[:n]) - _as_float(slb[:n]).mean()
    nfft = _next_pow2(2 * n - 1)
    cc = np.fft.irfft(np.fft.rfft(sla, nfft) * np.conj(np.fft.rfft(slb, nfft)), nfft)
    k = int(np.argmax(cc))
    lag = k if k < nfft // 2 else k - nfft          # sla[i] ~ slb[i-lag]
    resid = lag + _parabolic(cc, k)
    denom = float(np.sqrt(float((sla * sla).sum()) * float((slb * slb).sum())))
    conf = float(cc[k] / denom) if denom > 0 else 0.0
    return resid, conf


def _chunk_starts(span_start, span, win, hop_frac=0.5, max_chunks=400):
    """Window starts that tile `[span_start, span_start+span]` with no gaps.

    Windows are `win` seconds long stepping by `win*hop_frac` (<= win => overlapping,
    so every point of the span is inside a window and any divergence >= ~win/2 drops
    some window's confidence). Returns `(starts, win)`. If the span is longer than
    `max_chunks` windows can tile, `win` is WIDENED so `max_chunks` still cover it
    with the same overlap fraction (this raises the detection floor — a coarser check
    — rather than leaving unchecked gaps).
    """
    if span <= win:
        return [span_start], span
    hop = win * hop_frac
    n = int(np.ceil((span - win) / hop)) + 1              # last window covers the end
    if n > max_chunks:
        n = max_chunks
        win = span / (1.0 + (n - 1) * hop_frac)           # span = win + (n-1)*win*hop_frac
    step = (span - win) / (n - 1)                         # <= hop <= win => gap-free
    return [span_start + i * step for i in range(n)], win


def _slice_grade(a, b, gt_a, gt_b, sr=_audio.SR, min_overlap_s=5.0, pad_s=0.0,
                 chunk_s=10.0, max_chunks=400):
    """Grade a hand-labeled pair from decoded audio arrays. See `slice_check`.

    `gt_a`/`gt_b` are the labels' master-start seconds. Returns the same dict as
    `slice_check` minus the a/b stem keys.
    """
    dur_a = len(a) / float(sr)
    dur_b = len(b) / float(sr)
    ov0 = max(gt_a, gt_b)
    ov1 = min(gt_a + dur_a, gt_b + dur_b)
    overlap = ov1 - ov0
    if overlap < min_overlap_s:
        # Adjacent / butt-jointed captures: the labels place them end-to-end, so
        # there is no shared audio to correlate. NOT a misalignment.
        return {"status": "adjacent", "overlap_seconds": overlap}
    # Tile the WHOLE labeled overlap with overlapping equal-length windows and
    # correlate each pair of slices. Inspecting only a prefix (or leaving gaps between
    # spread-out windows) would confirm a pair while missing a skip / bad edit / label
    # drift elsewhere in the overlap. Gap-free coverage means no such region is
    # invisible. `pad_s` (default 0) can drop the outer seconds of the overlap — the
    # ends are a capture's start/end — but the correlation is amplitude-normalized, so
    # a fade there does not need trimming; the whole overlap is graded by default.
    pad = min(pad_s, overlap * 0.4)
    span_start = ov0 + pad
    span = overlap - 2 * pad
    starts, win = _chunk_starts(span_start, span, min(chunk_s, span), max_chunks=max_chunks)
    ns = int(round(win * sr))
    chunks = []
    for s in starts:
        na = int(round((s - gt_a) * sr))
        nb = int(round((s - gt_b) * sr))
        chunks.append(_chunk_xcorr(a[na:na + ns], b[nb:nb + ns]))
    confs = [c for _, c in chunks]
    # The pair confirms only if EVERY chunk matches -> the weakest chunk sets the
    # confidence. Report the drift of the well-matched chunk that is furthest off (a
    # constant post-skip offset shows up here even while confidence stays high); if no
    # chunk matched, the confidence already tells the story so residual is 0.
    min_conf = min(confs)
    good = [(r, c) for r, c in chunks if c >= _RESID_CONF_FLOOR]
    resid = max((rc[0] for rc in good), key=abs, default=0.0)
    return {
        "status": "graded",
        "overlap_seconds": overlap,
        "chunks": len(chunks),
        "residual_samples": resid,
        "residual_ms": resid / float(sr) * 1000.0,
        "confidence": min_conf,
    }


def slice_check(a_name, b_name, gt_a, gt_b, sr=_audio.SR, min_overlap_s=5.0,
                pad_s=0.0, chunk_s=10.0, max_chunks=400):
    """Verify a hand-labeled pair by comparing ONLY their overlapping audio.

    Tiles the whole labeled overlap (`gt_b - gt_a`) with overlapping equal-length
    windows and cross-correlates each pair of slices, rather than re-deriving the
    offset from scratch. Equal-length slices make it robust for pairs that differ
    greatly in length (where a full-signal cross-correlation can misdecode a large
    lag), and the gap-free tiling means a divergence anywhere in the overlap — start,
    middle, or end, and not falling between windows — is caught, down to ~chunk_s/2
    seconds (the uniform detection floor). Returns:

      status == 'adjacent'  overlap below `min_overlap_s`; nothing to compare.
                            (keys: a, b, status, overlap_seconds)
      status == 'graded'    (keys: a, b, status, overlap_seconds, chunks,
                            residual_samples, residual_ms, confidence)

    `confidence` is the WEAKEST chunk's normalized correlation: a pair confirms only
    if every chunk of the overlap matches. `residual` is the largest drift among the
    chunks that did match; it is only meaningful when `confidence` is high.
    """
    a = _audio.load_audio(a_name, sr=sr)
    b = _audio.load_audio(b_name, sr=sr)
    r = _slice_grade(a, b, gt_a, gt_b, sr=sr, min_overlap_s=min_overlap_s,
                     pad_s=pad_s, chunk_s=chunk_s, max_chunks=max_chunks)
    r["a"] = _audio.stem_of(a_name)
    r["b"] = _audio.stem_of(b_name)
    return r
