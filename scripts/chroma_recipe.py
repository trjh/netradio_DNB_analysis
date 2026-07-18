"""THE chroma recipe, in one place — so "the signature of this audio" has a single definition.

A chroma signature is only comparable to another if both were computed the exact same way. That
recipe used to live inline inside `harvest.py: stream_chroma`, copied by hand into the collector,
the edge worker, and the canary generator — four places that could silently drift, each drift
quietly poisoning the pool. This module is the single source: `harvest.py` calls it, the bucket
tooling (`make_recipe.py`, `make_canary.py`) calls it, and `chroma/_recipe.json` is emitted from
`recipe_dict()`. A test pins the numbers so a change here is a change everyone sees.

The recipe:  ffmpeg -> mono float32 PCM @ 16 kHz  ->  librosa.feature.chroma_cqt(sr, hop) + 1e-6
             ->  L2-normalise each frame  ->  (store as float16).
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
    c = librosa.feature.chroma_cqt(y=np.asarray(y, dtype="float32"),
                                   sr=sr or SR, hop_length=HOP) + EPSILON
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
