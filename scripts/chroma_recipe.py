"""THE chroma recipe, in one place — so "the signature of this audio" has a single definition.

A chroma signature is only comparable to another if both were computed the exact same way. That
recipe used to live inline inside `harvest.py: stream_chroma`, copied by hand into the collector,
the edge worker, and the canary generator — four places that could silently drift, each drift
quietly poisoning the pool. This module is the single source: `harvest.py` calls it, the bucket
tooling (`make_recipe.py`, `make_canary.py`) calls it, and `chroma/_recipe.json` is emitted from
`recipe_dict()`. A test pins the numbers so a change here is a change everyone sees.

The recipe:  ffmpeg -> mono float32 PCM @ 16 kHz  ->  librosa.feature.chroma_cqt(sr, hop) + 1e-6
             ->  L2-normalise each frame  ->  (store as float16).

The tuning estimate runs in BLOCKS, and that is not a recipe change. `chroma_cqt(tuning=None)`
asks librosa how far the recording sits from concert pitch before it computes anything, and that
estimate used to run a pitch tracker over the WHOLE file at once -- several float copies of a
1025-row spectrogram with one column per 512 samples, about 9.8 GB for two hours of audio, which
was 95% of the harvester's peak memory. The CQT itself costs about a tenth of that. Every step
between the STFT and the final aggregation acts on one column at a time, and the aggregation is a
median and a histogram over a multiset of voiced bins, so it does not care what order the columns
arrive in: estimating in 300-second blocks returns the SAME float, and chroma_cqt then runs on the
same samples with the same number. The signature is byte-identical, which is the point --
thousands of signatures already sit in the pool under RECIPE_VERSION 1, and a changed recipe would
mean re-fetching every one of them. RECIPE_VERSION stays 1; `recipe_dict()` is untouched.
"""

import numpy as np

from streamalign import audio as _audio

SR = _audio.SR                 # 16000 — the sample rate everything is resampled to
HOP = 2048                     # chroma_cqt hop length
EPSILON = 1e-6                 # added before normalising, so an all-zero frame is well-defined
MIN_SECONDS = 45               # shorter than this and we do not trust the signature
STORE_DTYPE = "float16"        # what lands in the cache / bucket

# Cross-architecture agreement tolerance. arm64 (the Mac) and x86_64 (edge) produce chroma that
# differs by <=1.2e-4 on the reference canary (2/586 frames, one float16 ULP; the matcher scores
# them identically to 6dp). Byte-identity is only expected WITHIN one architecture; across
# architectures a worker's canary passes if max|diff| on the float32 view is within this.
# Measured 2026-07-18 — see the player repo's docs/FINDINGS_edge_feasibility.md.
TOLERANCE = 0.001

RECIPE_VERSION = 1

# --- the tuning estimate, block by block -------------------------------------------------------
#
# These are librosa's own defaults on the path the recipe takes, written down rather than read
# back out of librosa at runtime: chroma_cqt -> cqt -> vqt -> estimate_tuning(y, sr,
# bins_per_octave=36) -> piptrack(y, sr, n_fft=2048). Naming them here means a librosa release
# that changes a default cannot silently change what this loop reproduces -- the identity tests
# in tests/test_chroma_tuning.py compare against librosa's whole-file path and would fail.
TUNING_N_FFT = 2048            # estimate_tuning's default, forwarded to piptrack
TUNING_HOP = 512               # piptrack's default hop = n_fft // 4  (NOT the recipe's HOP)
TUNING_BPO = 36                # chroma_cqt's default bins_per_octave, forwarded to estimate_tuning
TUNING_RESOLUTION = 0.01       # estimate_tuning's default
# 300 seconds at 16 kHz on the 512-sample hop, exactly. The block length is in FRAMES, not
# seconds, so any sr a caller passes still lands on a whole number of hops (300 s does not, at
# 22050 or 44100 Hz).
TUNING_BLOCK_FRAMES = 9375


def estimate_tuning_blockwise(y, sr):
    """librosa's `estimate_tuning(y, sr, bins_per_octave=36)`, computed a block at a time.

    Returns the same float, and holds one block's spectrogram instead of the whole file's.

    Column t of the whole-file STFT is the windowed FFT of samples [512*t - 1024, 512*t + 1024),
    with anything outside [0, N) taken as zero (`center=True`, `pad_mode="constant"`), and the
    whole-file call yields 1 + N // 512 columns. So a segment that really holds those 2048
    samples -- with the same zero padding where, and only where, the file itself has none --
    produces the same column. Each block therefore starts 1024 samples early and drops the two
    columns that begin before it (they are centred in the previous block, which computed them
    from real samples), keeps exactly the columns it owns, and reaches 1024 samples past its last
    column or the end of the file, whichever comes first.

    A file whose length is an exact multiple of the block stride ends with a block of one column,
    and librosa warns that n_fft is larger than that 1024-sample segment. The column is still the
    file's, computed from the samples the file really has; the warning is cosmetic.
    """
    from librosa.core.pitch import piptrack, pitch_tuning

    n = int(y.shape[-1])
    n_frames = 1 + n // TUNING_HOP           # what the whole-file call would produce
    ctx = TUNING_N_FFT // 2                  # 1024 samples of context on each side
    pitches, mags = [], []
    t0 = 0
    while t0 < n_frames:
        t1 = min(t0 + TUNING_BLOCK_FRAMES, n_frames)          # this block owns columns [t0, t1)
        if t0 == 0:
            s0, drop = 0, 0                  # the file's own left zero-pad is the segment's
        else:
            s0, drop = t0 * TUNING_HOP - ctx, 2
        e0 = min(n, t1 * TUNING_HOP + ctx)   # clipped at N, so the right zero-pad matches too
        p, m = piptrack(y=y[s0:e0], sr=sr, n_fft=TUNING_N_FFT)     # every other argument default
        keep = slice(drop, drop + (t1 - t0))
        p, m = p[:, keep], m[:, keep]
        voiced = p > 0                       # only voiced bins reach the aggregation, and from
        pitches.append(p[voiced])            # there on the frame order is irrelevant
        mags.append(m[voiced])
        t0 = t1

    p = np.concatenate(pitches) if pitches else np.zeros(0, dtype="float32")
    m = np.concatenate(mags) if mags else np.zeros(0, dtype="float32")
    # estimate_tuning's own aggregation: the median magnitude over the voiced bins, 0.0 when
    # there are none (silence), then the histogram peak.
    threshold = np.median(m) if p.size else 0.0
    return pitch_tuning(p[m >= threshold],
                        resolution=TUNING_RESOLUTION, bins_per_octave=TUNING_BPO)


def compute_chroma(y, sr=None):
    """Decoded mono float samples -> the normalised chroma matrix (12xN, float32).

    Callers store it as float16 (`.astype(STORE_DTYPE)`); the float32 return is what the matcher
    scores. This is the ONLY place a *comparable* chroma signature is expressed — the pool,
    canary, harvester, matcher-queries, calibration and identification all go through here so
    they cannot disagree.

    `sr` overrides the sample rate for experimental/non-canonical analysis of audio that is NOT
    already at SR; canonical signatures (everything that touches the bucket) always use the
    default. It does NOT change hop/epsilon/norm. (The alignment engine's DTW rate/offset
    chroma in `streamalign/track_mix.py` is a *different* computation — configurable, sometimes
    un-normalised — and is deliberately not routed here; see its note.)"""
    import librosa
    y32 = np.asarray(y, dtype="float32")
    # Pass the tuning in explicitly. Left as None, chroma_cqt computes exactly this number from
    # the whole file at once and spends 95% of the peak memory doing it; the blocks are VIEWS of
    # y32, so nothing is copied to get there. See the module docstring.
    tuning = estimate_tuning_blockwise(y32, sr or SR)
    c = librosa.feature.chroma_cqt(y=y32, sr=sr or SR, hop_length=HOP, tuning=tuning) + EPSILON
    return librosa.util.normalize(c, norm=2, axis=0)


def toolchain():
    """The versions that shape chroma_cqt's output — informational, recorded in the recipe so a
    mismatch is diagnosable. Detection is best-effort: a missing library is noted, not fatal."""
    import sys
    out = {"python": sys.version.split()[0]}
    for mod in ("numpy", "librosa", "scipy", "numba"):
        try:
            out[mod] = __import__(mod).__version__
        except Exception:
            out[mod] = "absent"
    return out


def recipe_dict(with_toolchain=True):
    """The contract published as `chroma/_recipe.json`; workers assert-match against it."""
    d = {
        "version": RECIPE_VERSION,
        "pipeline": "ffmpeg -ac 1 -ar %d -f f32le | chroma_cqt(sr=%d, hop=%d) + %g | L2-per-frame"
                    % (SR, SR, HOP, EPSILON),
        "sr": SR,
        "hop": HOP,
        "feature": "chroma_cqt",
        "epsilon": EPSILON,
        "norm": "l2-per-frame",
        "dtype": STORE_DTYPE,
        "min_seconds": MIN_SECONDS,
        "tolerance": TOLERANCE,
        "comparison": "same-arch: byte-identical float16; cross-arch: max|diff| <= tolerance on "
                      "the float32 view (see the player repo's FINDINGS_edge_feasibility.md)",
        "key": "u + sha1(url).hexdigest()[:20] + .npy",
    }
    if with_toolchain:
        d["toolchain"] = toolchain()
    return d
