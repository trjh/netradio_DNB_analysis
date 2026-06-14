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
    lag_d = k if k < nfft // 2 else k - nfft
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
    a = _as_float(a)
    b = _as_float(b)
    # Overlap region in a's coordinates given offset `around` (a[t] ~ b[t-around]).
    lo = max(0, around)
    hi = min(len(a), len(b) + around)
    if hi - lo < 1000:
        return float(around), 0.0
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
    k = int(np.argmax(ccp))
    lag = k if k < nfft // 2 else k - nfft
    frac = _parabolic(ccp, k)
    # Convention: ccp[k] = sum_i aseg[i]*bseg[i-k], so the peak lag means
    # aseg[i] ~ bseg[i-lag]. With aseg[i]=a[a0+i] and bseg[j]=b[b0+j], a match
    # a[a0+i] ~ b[(a0+i)-offset] gives  b0 + i - lag = a0 + i - offset, i.e.
    #   offset = (a0 - b0) + lag.
    offset = (a0 - b0) + (lag + frac)
    conf = _ncc_at(aseg, bseg, lag)
    return float(offset), float(conf)


def _ncc_at(aseg, bseg, lag):
    """Normalized cross-correlation of the samples aligned at integer `lag`.

    Same convention as the peak: aseg[i] pairs with bseg[i-lag].
    """
    shift = -lag  # bseg index = i + shift
    if shift >= 0:
        a_ = aseg[:max(0, len(aseg) - shift)]
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
