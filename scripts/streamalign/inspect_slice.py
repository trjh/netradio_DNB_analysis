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
snap-to-best button. `--engine match` (AP-14) swaps the snap target: the (trimmed,
Phase-B) MATCH path is computed for the pair and its implied original instant at the
current stream time becomes the target, with the PHAT-vs-MATCH delta reported; a
globally mis-seated path (the Phase-B gross-median case) is an error, never a snap.

With --context (AP-05) it emits the zoomed-out CONTEXT strip instead: decimated
min/max column pairs (COLS_PER_S per second, mono) for the stream and the
rate-corrected original around the point -- a small payload for the player's context
lane, NOT full audio (playback slices are fetched separately on click).

`LoadedPair` is the keep-warm seam (AP-08): the decode + rate-correction that
dominates a one-shot run's latency is bundled into one cacheable object, and every
entry point accepts `pair=` so the `inspect-worker` process can reuse it across
requests. The one-shot CLI simply builds a fresh pair per call.
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
DEFAULT_CONTEXT_S = 45.0  # AP-05: context strip half-width (± seconds around the point)
MAX_CONTEXT_S = 60.0      # hard cap; the player's endpoint enforces it too
CONTEXT_COLS_PER_S = 50   # min/max column pairs per second in the context payload


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


class LoadedPair:
    """One (stream, original, rate)'s decoded audio -- the keep-warm unit (AP-08).

    Holds the stereo stream, the stereo native original, and the rate-corrected
    original (the expensive resample). The MATCH path (AP-14's snap engine) is
    computed lazily and cached here too, keyed by its trim anchor so a snap at a
    far-away point recomputes rather than reads a window that missed it.
    """

    def __init__(self, stream_src, orig_src, rate):
        self.stream_src = stream_src
        self.orig_src = orig_src
        self.rate = float(rate)
        self.stream = _audio.load_audio(stream_src, mono=False)
        self.orig = _audio.load_audio(orig_src, mono=False, use_cache=False)
        self.orig2 = _rate_correct_stereo(self.orig, self.rate)
        self._match_pairs = None
        self._match_around = None

    def match_pairs(self, around_s):
        """The (trimmed) MATCH a_b path for this pair, cached per trim anchor.

        `around_s` is the capture-local position of the original's local 0 implied
        by the current seat; both files are trimmed to that seat-implied common
        start before sonic-annotator runs. A cached path is reused while the anchor
        stays within the trim margin -- beyond that the cached window may not cover
        the request.
        """
        if (self._match_pairs is not None and self._match_around is not None
                and abs(float(around_s) - self._match_around) <= _mc.TRIM_MARGIN_S):
            return self._match_pairs
        self._match_pairs = compute_match_pairs(
            self.stream.mean(axis=1), self.orig2.mean(axis=1), around_s, self.rate)
        self._match_around = float(around_s)
        return self._match_pairs


def compute_match_pairs(stream_mono, orig2_mono, around_s, rate):
    """Run the trimmed MATCH path for the inspector (AP-14). [(a_s, b_native_s)].

    Two departures from Phase B's match-hints trim, both possible because the seat
    here is the inspector's CURRENT point -- approximately right by construction --
    rather than rough metadata:

    * **Both files are trimmed to the seat-implied common start.** Phase B trims
      only the stream, which cannot help when the ORIGINAL's head precedes the
      capture: MATCH still sees a many-seconds forced-start skew and lands globally
      off (measured -41 s on the d376-395/072 gate pair). `around_s` is the
      capture-local position of original local 0 (negative = the original's head
      precedes the capture); trimming the entering side to it makes MATCH's forced
      files-start-together assumption hold to within the seat error.
    * **MATCH sees the RATE-CORRECTED original** (`orig2_mono`, stream clock).
      Against the native original MATCH's DTW recovers the wrong slope on this
      material (Phase B's gate-0 finding; re-measured here as a -7 s median drift
      on the gate pair) -- rate-corrected, the true path slope is 1.0, exactly the
      diagonal its online DTW tracks best. `b` is mapped back to original-NATIVE
      seconds (× rate) before returning.

    The gross-median gate downstream still protects against all of this being
    wrong: a badly wrong seat trims badly, MATCH lands off, and the snap refuses
    rather than compounding the error. Needs sonic-annotator on PATH;
    FileNotFoundError otherwise (the CLI/worker turn that into an error string the
    player shows).
    """
    import tempfile
    import os as _os

    around_s = float(around_s)
    stream_len_s = len(stream_mono) / SR
    orig2_len_s = len(orig2_mono) / SR        # stream-clock seconds
    if around_s >= 0.0:
        s_lo, o_lo = around_s, 0.0
    else:
        s_lo, o_lo = 0.0, -around_s
    # cap the stream tail near the original's end (plus slack); MATCH time is O(area)
    s_hi = min(stream_len_s, s_lo + (orig2_len_s - o_lo) + 60.0)
    if s_hi - s_lo < 10.0 or orig2_len_s - o_lo < 10.0:
        raise ValueError("seat leaves too little stream/original overlap for MATCH")
    with tempfile.TemporaryDirectory(prefix="inspect-match-") as tmp:
        s_wav = _mc.write_wav16(_os.path.join(tmp, "stream.wav"),
                                stream_mono[int(s_lo * SR):int(s_hi * SR)])
        o_wav = _mc.write_wav16(_os.path.join(tmp, "orig.wav"),
                                orig2_mono[int(o_lo * SR):])
        pairs = _mc.parse_ab_csv(_mc.run_match(s_wav, o_wav, tmp))
    return [(a + s_lo, (b + o_lo) * float(rate)) for a, b in pairs]


def build_slices(stream_src, orig_src, stream_t, orig_t, rate, invert, win_s,
                 pair=None):
    """The inspector's data for one sync point. Returns a JSON-ready dict.

    `stream_src` / `orig_src` are whatever `audio.load_audio` accepts (stem or
    path); `stream_t` is capture-local seconds; `orig_t` is ORIGINAL-NATIVE
    seconds (the number on an `origNNN sync:` row); `rate` is original-seconds per
    stream-second (M6); `invert` applies the polarity flip to the original (M2).
    `pair` (AP-08) is an already-loaded `LoadedPair` for this exact trio.
    """
    _check_params(stream_t, orig_t, rate, win_s)
    win_s = min(float(win_s), MAX_WIN_S)
    pair = pair or LoadedPair(stream_src, orig_src, rate)
    stream, orig2 = pair.stream, pair.orig2

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
                radius_s=0.05, engine="phat", pair=None):
    """Snap-to-best: the residual at the current seat. JSON-ready dict.

    `engine="phat"` (the default, today's refine): GCC-PHAT residual -- returns the
    residual in samples/ms (positive = the original's content sits EARLY and should
    move later), the confidence at the refined seat, and the corrected
    original-native instant to adopt.

    `engine="match"` (AP-14): the snap target is instead the (trimmed, Phase-B)
    MATCH path's implied original instant at the current stream time. The PHAT
    refine still runs, and the PHAT-vs-MATCH delta is reported (`match_vs_phat_ms`;
    `confidence` stays PHAT's -- MATCH has no per-instant confidence). A path whose
    median disagreement with the current seat exceeds REFEREE_GROSS_S is globally
    mis-seated (MATCH's forced-start failure): that raises ValueError with the
    Phase-B wording rather than snapping onto a wrong path.
    """
    if engine not in ("phat", "match"):
        raise ValueError("engine out of range")
    _check_params(stream_t, orig_t, rate, win_s, radius_s)
    win_s = min(float(win_s), MAX_WIN_S)
    radius_s = min(float(radius_s), MAX_REFINE_RADIUS_S)
    pair = pair or LoadedPair(stream_src, orig_src, rate)
    stream, orig2 = pair.stream, pair.orig2

    half = win_s / 2.0 + radius_s + 0.5
    s = _slice_stereo(stream, float(stream_t), half).mean(axis=1)
    o = _slice_stereo(orig2, float(orig_t) / float(rate), half).mean(axis=1)
    if invert:
        o = -o
    # both slices are centred on the claimed seat, so the residual is the offset at 0
    off, conf = _align.refine_offset(s, o, around=0,
                                     radius=int(radius_s * SR), win=int(win_s * SR))
    phat_orig_t = float(orig_t) - (off / SR) * float(rate)
    out = {
        "engine": engine,
        "offset_samples": float(off),
        "offset_ms": float(off / SR * 1000.0),
        "confidence": float(conf),
        "new_orig_t": phat_orig_t,
    }
    if engine == "match":
        # trim anchor: the capture-local position of the original's local 0 implied
        # by the CURRENT seat (orig_t native seconds span orig_t/rate stream-seconds)
        around = float(stream_t) - float(orig_t) / float(rate)
        pairs = pair.match_pairs(around)
        _check_match_seated(pairs, stream_t, orig_t, rate,
                            orig_len_s=len(pair.orig) / SR)
        target = _mc.match_predict(pairs, float(stream_t))
        if target is None:
            raise ValueError("MATCH path does not cover this stream instant")
        out.update({
            "new_orig_t": float(target),
            # same convention as PHAT: positive offset = orig content sits early
            "offset_samples": float((float(orig_t) - target) / float(rate) * SR),
            "offset_ms": float((float(orig_t) - target) / float(rate) * 1000.0),
            "phat_orig_t": phat_orig_t,
            "match_vs_phat_ms": float((target - phat_orig_t) * 1000.0),
        })
    return out


def _check_match_seated(pairs, stream_t, orig_t, rate, orig_len_s=None):
    """Raise if the MATCH path is globally mis-seated (Phase B's gross-median case).

    Deltas between the path and the straight line through the current seat at the
    current rate, sampled across the path's in-original span (past the original's
    end the a_b output just tracks the diagonal -- same filter as `coarse_map`); a
    median beyond REFEREE_GROSS_S is the forced-files-start-together failure mode,
    not a snap target.
    """
    rows = [(a, b) for a, b in pairs if b > 1.0
            and (orig_len_s is None or b < orig_len_s - 1.0)]
    if len(rows) < 10:
        raise ValueError("MATCH path too short to trust (%d usable rows)" % len(rows))
    deltas = [b - (float(orig_t) + (a - float(stream_t)) * float(rate))
              for a, b in rows]
    med = float(np.median(deltas))
    if abs(med) > _mc.REFEREE_GROSS_S:
        raise ValueError(
            "MATCH path is globally mis-seated: median %+.1f s from the current seat "
            "across the overlap (MATCH's forced files-start-together failure mode) -- "
            "not snapping; use the PHAT engine or verify by ear" % med)


def _minmax_columns(x, cols):
    """Per-column (min, max) of `x` split into `cols` equal columns, 3-decimal floats."""
    step = len(x) // cols
    y = np.asarray(x[:cols * step], dtype=np.float32).reshape(cols, step)
    return ([round(float(v), 3) for v in y.min(axis=1)],
            [round(float(v), 3) for v in y.max(axis=1)])


def build_context(stream_src, orig_src, stream_t, orig_t, rate, invert,
                  context_s=DEFAULT_CONTEXT_S, win_s=6.0, pair=None):
    """The zoomed-out context strip around one sync point (AP-05). JSON-ready dict.

    Emits DECIMATED min/max column pairs (CONTEXT_COLS_PER_S per second, mono) for
    the stream and the rate-corrected, polarity-applied, RMS-matched original over
    ±`context_s` around the point -- a small payload for the player's context lane,
    NOT full audio (the player fetches playback slices on click via the slice
    endpoint). `start_s` (capture-local) and `stream_len_s` let the client bound
    clicks to real audio; `win_s` is echoed so the analysis window can be marked.
    """
    _check_params(stream_t, orig_t, rate, win_s)
    ctx = float(context_s)
    if not np.isfinite(ctx) or ctx < 1.0:
        raise ValueError("context out of range")
    ctx = min(ctx, MAX_CONTEXT_S)
    win_s = min(float(win_s), MAX_WIN_S)
    pair = pair or LoadedPair(stream_src, orig_src, rate)
    stream, orig2 = pair.stream, pair.orig2

    s = _slice_stereo(stream, float(stream_t), ctx).mean(axis=1)
    o = _slice_stereo(orig2, float(orig_t) / float(rate), ctx).mean(axis=1)
    if invert:
        o = -o
    rms_s = float(np.sqrt(np.mean(s ** 2)))
    rms_o = float(np.sqrt(np.mean(o ** 2)))
    gain = (rms_s / rms_o) if rms_o > 1e-9 else 1.0
    o = o * gain

    cols = int(round(2.0 * ctx * CONTEXT_COLS_PER_S))
    s_min, s_max = _minmax_columns(s, cols)
    o_min, o_max = _minmax_columns(o, cols)
    return {
        "sr": SR,
        "cols_per_s": CONTEXT_COLS_PER_S,
        "context_s": ctx,
        "win_s": win_s,
        "stream_t": float(stream_t),
        "orig_t": float(orig_t),
        "rate": float(rate),
        "invert": bool(invert),
        "rms_gain": float(gain),
        "start_s": float(stream_t) - ctx,
        "stream_len_s": float(len(stream) / SR),
        "n_cols": cols,
        "stream": {"min": s_min, "max": s_max},
        "orig": {"min": o_min, "max": o_max},
    }
