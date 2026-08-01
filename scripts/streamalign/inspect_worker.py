"""Keep-warm DSP worker for the player's /align inspector (AP-08).

Every one-shot `inspect-slice` subprocess pays the decode + rate-correction cost
(seconds) before doing milliseconds of actual work. This worker is the long-lived
alternative: `python -m streamalign inspect-worker --sources <dir>` reads one JSON
request per line on stdin and writes exactly one JSON response line per request on
stdout (nothing unsolicited -- diagnostics go to stderr), holding the decoded
stream + rate-corrected original for the CURRENT (stem, orig, rate) pair and
reloading only when that trio changes.

Protocol (JSON lines):

    {"op": "ping"}                                    -> {"ok": true}
    {"op": "slice"|"refine"|"context",
     "stem": ..., "orig": N, "stream_t": ..., "orig_t": ...,
     "rate": ..., "invert": ..., "win": ...,
     "radius": ...,               # refine only
     "engine": "phat"|"match",    # refine only (AP-14)
     "context": ...,              # context only (AP-05)
     "id": anything}              -> the same dict inspect_slice returns
                                     (or {"error": ...}), "id" echoed when present

All parameter guards are inspect_slice's own (`_check_params` + the per-mode
clamps) -- the DSP is never duplicated here, and a bad request answers
{"error": ...} on its own line; the worker itself never dies on a request.
EOF on stdin is the shutdown signal.
"""

import json
import sys

from . import audio as _audio
from . import inspect_slice as _isl

OPS = ("ping", "slice", "refine", "context")


class PairCache:
    """The current (stem, orig, rate) pair's decoded audio; one entry, by design.

    The inspector works one sync-point set at a time, so a single slot captures the
    whole win; anything larger just holds hundreds of MB of dead float32. `loader`
    is the test seam (defaults to `inspect_slice.LoadedPair`).
    """

    def __init__(self, loader=None):
        self._loader = loader or _isl.LoadedPair
        self._key = None
        self._pair = None

    def get(self, stream_src, orig_src, rate):
        # the EXACT validated rate is the identity: any rounding here would reuse an
        # orig2 resampled at a slightly different ratio than the request's timing
        # math assumes, silently diverging from the one-shot path (review iter 1 P2)
        key = (stream_src, orig_src, float(rate))
        if key != self._key:
            self._pair = self._loader(stream_src, orig_src, float(rate))
            self._key = key
        return self._pair


def _resolve(stem, orig_num, sources_dir):
    """(stream_src, orig_path) or ValueError -- same lookups as the one-shot CLI."""
    from . import track_mix as _tm

    stem = _audio.stem_of(str(stem))
    if not _audio.find_audio_file(stem):
        raise ValueError("no audio for capture %s" % stem)
    orig_path = _tm.find_original(int(orig_num), sources_dir)
    if not orig_path:
        raise ValueError("no original %03d-* under %s" % (int(orig_num), sources_dir))
    return stem, orig_path


def handle(req, cache, sources_dir):
    """One request dict -> one response dict. Raises nothing request-shaped."""
    if not isinstance(req, dict):
        return {"error": "request must be a JSON object"}
    op = req.get("op")
    if op == "ping":
        return {"ok": True}
    if op not in OPS:
        return {"error": "unknown op %r" % (op,)}
    try:
        # coerce + guard EVERY numeric input at the request boundary, with
        # inspect_slice's own guards, BEFORE any decode/resample happens: a
        # malformed rate must answer "rate out of range" promptly, not buy a full
        # pair load first (review iteration 2 P2)
        rate = float(req.get("rate", 1.0))
        stream_t = float(req.get("stream_t"))
        orig_t = float(req.get("orig_t"))
        win = float(req.get("win", 6.0))
        radius = float(req.get("radius", 0.05))
        engine = str(req.get("engine", "phat"))
        context = float(req.get("context", _isl.DEFAULT_CONTEXT_S))
        _isl._check_params(stream_t, orig_t, rate, win, radius)
        if op == "context":
            _isl._check_context(context)
        if op == "refine" and engine not in _isl.ENGINES:
            raise ValueError("engine out of range")
        stem, orig_path = _resolve(req.get("stem"), req.get("orig"), sources_dir)
        pair = cache.get(stem, orig_path, rate)
        common = dict(stream_src=stem, orig_src=orig_path,
                      stream_t=stream_t, orig_t=orig_t,
                      rate=rate, invert=bool(req.get("invert")),
                      win_s=win, pair=pair)
        if op == "slice":
            return _isl.build_slices(**common)
        if op == "refine":
            return _isl.refine_seat(radius_s=radius, engine=engine, **common)
        return _isl.build_context(context_s=context, **common)
    except Exception as exc:  # noqa: BLE001 -- one bad request must not kill the worker
        return {"error": "%s: %s" % (type(exc).__name__, str(exc)[:300])}


def serve(sources_dir, stdin=None, stdout=None, cache=None):
    """The JSON-lines loop: one response line per request line, flushed, until EOF."""
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    cache = cache or PairCache()
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            req = None
            out = {"error": "bad JSON line"}
        else:
            out = handle(req, cache, sources_dir)
        if isinstance(req, dict) and "id" in req and isinstance(out, dict):
            out = dict(out, id=req["id"])
        stdout.write(json.dumps(out) + "\n")
        stdout.flush()
