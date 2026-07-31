"""Slice provider for the player's /align sample inspector (align-tool Pass 2).

The player's server is stdlib-only by design, so every piece of DSP the inspector
needs -- decode, rate-correction, alignment slicing, RMS matching, polarity, and the
snap-to-best refine -- happens HERE, in the analysis venv, invoked as a subprocess
(`python -m streamalign inspect-slice ... --json`). The browser only draws, nudges
(a nudge is an array shift, done client-side on the margin this module includes),
and plays.

One call returns, for a single sync point at a given window:

- the stream slice, stereo, centred on the stream instant;
- the original slice, stereo, RATE-CORRECTED (so one sample spans the same
  wall-clock as one stream sample), centred on the corresponding original instant,
  polarity-applied and RMS-matched to the stream over the comparison window;
- both with MARGIN_S of extra audio each side, so the client can nudge +-margin
  sample-accurately without another round trip;
- the applied gain and the exact sample geometry, so every number the client
  displays is reproducible.

All comparison DSP is at the stream's native 16 kHz (the original is resampled
down): the original carries content above 8 kHz that the ISDN stream physically
lacks, and comparing full-band would guarantee a residual floor no alignment could
ever remove.

With --refine it instead runs GCC-PHAT (`align.refine_offset`) on the window at the
current seat and reports the residual nudge + confidence -- the inspector's
snap-to-best button.
"""

import base64

import numpy as np

from . import align as _align
from . import audio as _audio
from . import matchconv as _mc

SR = _audio.SR
MARGIN_S = 0.5            # nudge headroom shipped with every slice (client-side shifts)
MAX_WIN_S = 30.0          # hard cap; the player's endpoint enforces it too
MAX_REFINE_RADIUS_S = 2.0


def _check_params(stream_t, orig_t, rate, win_s, radius_s=0.05):
    """Finite-and-in-range guard: this runs as a subprocess whose stdout must stay
    JSON, so a nonsense parameter raises ValueError (caught at the CLI boundary into
    an {"error": ...}) rather than surfacing as a numpy traceback. The player
    validates first; this is the provider's own belt."""
    # upper bounds on win/radius CLAMP (documented behaviour, tested); everything
    # non-finite, below its floor, or outside a sane absolute range is rejected
    for name, v, lo, hi in (("stream_t", stream_t, 0.0, 24 * 3600.0),
                            ("orig_t", orig_t, 0.0, 24 * 3600.0),
                            ("rate", rate, 0.5, 2.0),
                            ("win", win_s, 0.1, None),
                            ("radius", radius_s, 1e-4, None)):
        v = float(v)
        if not np.isfinite(v) or v < lo or (hi is not None and v > hi):
            raise ValueError("%s out of range" % name)


def _b64_i16(x):
    """Little-endian int16 base64 of a float array in [-1, 1] (clipped)."""
    x = np.clip(np.asarray(x, dtype=np.float32), -1.0, 1.0)
    return base64.b64encode((x * 32767.0).astype("<i2").tobytes()).decode("ascii")


def _slice_stereo(sig, center_s, half_s):
    """(n, 2) window of `sig` centred at center_s, zero-padded at the edges."""
    n0 = int(round((center_s - half_s) * SR))
    n1 = n0 + int(round(2.0 * half_s * SR))
    out = np.zeros((n1 - n0, sig.shape[1]), dtype=np.float32)
    lo, hi = max(0, n0), min(len(sig), n1)
    if hi > lo:
        out[lo - n0:hi - n0] = sig[lo:hi]
    return out


def _rate_correct_stereo(orig, rate):
    """Per-channel resample so orig time advances `rate` native-seconds per stream-second."""
    chans = [_mc.resample_by_rate(orig[:, c], rate) for c in range(orig.shape[1])]
    n = min(len(c) for c in chans)
    return np.stack([c[:n] for c in chans], axis=1)


def build_slices(stream_src, orig_src, stream_t, orig_t, rate, invert, win_s):
    """The inspector's data for one sync point. Returns a JSON-ready dict.

    `stream_src` / `orig_src` are whatever `audio.load_audio` accepts (stem or
    path); `stream_t` is capture-local seconds; `orig_t` is ORIGINAL-NATIVE
    seconds (the number on an `origNNN sync:` row); `rate` is original-seconds per
    stream-second (M6); `invert` applies the polarity flip to the original (M2).
    """
    _check_params(stream_t, orig_t, rate, win_s)
    win_s = min(float(win_s), MAX_WIN_S)
    stream = _audio.load_audio(stream_src, mono=False)
    orig = _audio.load_audio(orig_src, mono=False, use_cache=False)
    orig2 = _rate_correct_stereo(orig, float(rate))

    half = win_s / 2.0 + MARGIN_S
    s = _slice_stereo(stream, float(stream_t), half)
    o = _slice_stereo(orig2, float(orig_t) / float(rate), half)
    if invert:
        o = -o

    core = slice(int(round(MARGIN_S * SR)), int(round((MARGIN_S + win_s) * SR)))
    rms_s = float(np.sqrt(np.mean(s[core] ** 2)))
    rms_o = float(np.sqrt(np.mean(o[core] ** 2)))
    gain = (rms_s / rms_o) if rms_o > 1e-9 else 1.0
    o = o * gain

    return {
        "sr": SR,
        "win_s": win_s,
        "margin_s": MARGIN_S,
        "stream_t": float(stream_t),
        "orig_t": float(orig_t),
        "rate": float(rate),
        "invert": bool(invert),
        "rms_gain": float(gain),
        "n": int(len(s)),
        "stream": {"L": _b64_i16(s[:, 0]), "R": _b64_i16(s[:, 1])},
        "orig": {"L": _b64_i16(o[:, 0]), "R": _b64_i16(o[:, 1])},
    }


def refine_seat(stream_src, orig_src, stream_t, orig_t, rate, invert, win_s,
                radius_s=0.05):
    """Snap-to-best: GCC-PHAT residual at the current seat. JSON-ready dict.

    Returns the residual in samples/ms (positive = the original's content sits
    EARLY and should move later), the confidence at the refined seat, and the
    corrected original-native instant to adopt.
    """
    _check_params(stream_t, orig_t, rate, win_s, radius_s)
    win_s = min(float(win_s), MAX_WIN_S)
    radius_s = min(float(radius_s), MAX_REFINE_RADIUS_S)
    stream = _audio.load_audio(stream_src, mono=False)
    orig = _audio.load_audio(orig_src, mono=False, use_cache=False)
    orig2 = _rate_correct_stereo(orig, float(rate))

    half = win_s / 2.0 + radius_s + 0.5
    s = _slice_stereo(stream, float(stream_t), half).mean(axis=1)
    o = _slice_stereo(orig2, float(orig_t) / float(rate), half).mean(axis=1)
    if invert:
        o = -o
    # both slices are centred on the claimed seat, so the residual is the offset at 0
    off, conf = _align.refine_offset(s, o, around=0,
                                     radius=int(radius_s * SR), win=int(win_s * SR))
    new_orig_t = float(orig_t) - (off / SR) * float(rate)
    return {
        "offset_samples": float(off),
        "offset_ms": float(off / SR * 1000.0),
        "confidence": float(conf),
        "new_orig_t": new_orig_t,
    }
