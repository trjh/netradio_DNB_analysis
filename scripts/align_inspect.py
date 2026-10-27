"""The /align sample inspector's server side (align-tool Pass 2).

The align server stays stdlib-only, so every piece of DSP the inspector needs runs in this
checkout's venv as a subprocess (`python -m streamalign inspect-slice ...`). This module owns
everything about that boundary:

- **capability**: dark unless the checkout's venv, the streamalign package and the
  originals directory all exist (`enabled()`); the page then shows a friendly "not
  configured" instead of a broken tool;
- **identifiers, never paths** (the §3.3/Phase-2 security contract): the client names
  a stream STEM and an original NUMBER; this module maps them to paths itself from
  its own listings, realpath-containment-checked under the configured roots. Numeric
  parameters are validated with hard caps so a request can't buy unbounded
  ffmpeg/FFT work. The subprocess is a fixed executable + fixed argument list,
  `shell=False`, every value its own argv entry;
- **one refine at a time**: a module lock; a concurrent request gets a 429-style
  busy answer instead of a second ffmpeg;
- **the keep-warm worker (DP-07)**: the biggest latency lever — a long-lived
  `streamalign inspect-worker` child (fixed argv, shell=False, same env as the
  one-shot path) that holds the decoded pair between requests, spoken to over
  pipes as JSON lines with a per-request timeout. Restarted if dead; any
  transport failure FALLS BACK to the one-shot subprocess for that request, so
  the worker is purely an accelerator, never a new failure mode. Single-flight
  semantics are unchanged — the same module lock wraps both paths;
- **hint ingestion**: parses the Pass-1 converter's `*.match.hints.tsv` pairs from
  the labels dir into ready-to-open sync points (rate, polarity, per-point
  confidence), so the inspector opens on real data with zero typing;
- **the placed-pairs provider (DP-22 §6.5 source b)**: `list_placed()` runs the
  read-only `streamalign placed` op through the SAME worker/one-shot
  seam (fixed argv, nothing client-supplied — not even an identifier — single-flight
  lock, dark without the venv) and returns the hand-placed pairs as source-tagged
  sets shaped like the hints sets, filtered to the server's own stream namespace and
  capped like every listing. The sources route serves them as the `placed` sibling
  of `hints` so the review page can interleave both into one selectable list.

The same identifier/caps contract now also covers the align server's whole-file
review-audio GETs (DP-22 §6.8) — resolved DSP-free in `review_audio.py` against this
module's namespaces (`list_streams()` / the sources-dir listing); the venv is never
involved in serving audio.

The routes themselves (in `align_server.py`) bind loopback unless told otherwise, and
require Basic Auth when it is configured: compute-on-demand is never exposed by accident.

Beside the boundary work, this module owns the server's registration of the
venv's decoded-audio cache on the one cache policy: `register_cache()` puts the `streamalign`
cache on `cache_budget` from the same variable family the venv child reads
(NETRADIO_STREAMALIGN_CACHE_*), so the child — which inherits this module's whole
environment — and the server agree on cap, age and directory. It is a call from
`align_server.main`, not at this module's import: the align server loads `.env`
after its imports, and the registration reads the environment once, at call time.

The child's `NETRADIO_AUDIO_DIR` is the MP3 directory: the inspector's DSP decodes
have been the transcode tiles since the server landed, and that is kept deliberately.
Switching the DSP to the lossless captures waits on a decision about timing (the
transcode's encoder delay against the labels' clock).
"""

import json
import math
import os
import queue
import re
import subprocess
import threading

import cache_budget

MAX_WIN_S = 30.0
MAX_RADIUS_S = 2.0
MAX_CONTEXT_S = 60.0
DEFAULT_CONTEXT_S = 45.0
MAX_T_S = 24 * 3600.0
RATE_RANGE = (0.5, 2.0)
ENGINES = ("phat", "match")
OPS = ("slice", "refine", "context", "overview")
MAX_OVERVIEW_POINTS = 64   # DP-10: cap on the sync points an overview request names
SUBPROCESS_TIMEOUT_S = 120
WORKER_PING_TIMEOUT_S = 30   # first ping pays the numpy import
STREAMALIGN_CACHE_MAX_AGE_DAYS = 14   # an align session is days, not months (decision made 2026-09-16)

_STEM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ORIG_FILE_RE = re.compile(r"^(\d{3})-(.+)\.(mp3|flac|m4a|opus|wav|wv|aif|aiff|mp4|ogg)$",
                           re.IGNORECASE)
_HINTS_NAME_RE = re.compile(r"^([A-Za-z0-9._-]+)\.orig(\d{3})\.match\.hints\.tsv$")
_SUMMARY_RE = re.compile(r"rate ([0-9.]+) .*polarity (INVERTED|not inverted)")
# the optional ` verified` token is DP-01's machine-measured mark -- Pass-1
# files carry it (matchconv emits `track sync: 1 verified confidence ...`), and so
# do the inspector's exports on a seat the machine measured (a hand-moved seat
# exports without it); a parser without it silently drops every such point.
# Neither regex anchors the line END on purpose: anchor rows are accepted BOTH with
# and without the trailing ` HINT` token (the converter dropped it from emitted
# sync/start/end rows in DP-26, and files written before that still carry it). The optional per-anchor `MATCH %+.3fs` token after
# the confidence (matchconv DP-03: MATCH-implied minus PHAT anchor position,
# original-native seconds) is CAPTURED, not just tolerated -- DP-33's page-side
# MATCH−PHAT jitter window is rebuilt from exactly these numbers.
_TRACK_SYNC_RE = re.compile(
    r"^track sync:\s+(\S+)(?:\s+verified)?\s+confidence ([0-9.]+)/10"
    r"(?:\s+MATCH ([+-]?[0-9.]+)s)?")
_ORIG_SYNC_RE = re.compile(r"^orig(\d{3}) sync:\s+(\S+)(?:\s+verified)?\s+confidence")

_run_lock = threading.Lock()


def _env(name):
    v = os.environ.get(name, "").strip()
    return os.path.expanduser(v) if v else None


# This module sits in scripts/; the checkout it belongs to, whose venv, scripts/streamalign
# and sources_local/ the DSP uses, is one level up. Tests point it elsewhere.
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def repo_root():
    """The checkout the DSP runs in, and whose labels/ the review notes and summaries use."""
    return ROOT_DIR


def _venv_python():
    repo = repo_root()
    return os.path.join(repo, ".venv", "bin", "python") if repo else None


def sources_dir():
    override = _env("NETRADIO_SOURCES_DIR")
    if override:
        return override
    repo = repo_root()
    return os.path.join(repo, "sources_local") if repo else None


def enabled():
    """The inspector's DSP capability: the venv, streamalign and the originals present."""
    repo = repo_root()
    py = _venv_python()
    return bool(
        repo and py and os.path.isfile(py)
        and os.path.isdir(os.path.join(repo, "scripts", "streamalign"))
        and os.path.isdir(sources_dir() or ""))


def register_cache():
    """Put the venv's decoded-audio cache on the one cache policy: the `.npy` arrays
    the venv decodes
    once and re-reads, refilled by decoding again in seconds, third of the
    policy's caches to give up entries when the floor is breached.

    The settings come from the same variable family the venv child reads —
    NETRADIO_STREAMALIGN_CACHE_{GB,HEADROOM_MB,MAX_AGE_DAYS,DIR}, defaulting to
    the policy's cap, a 14-day age (STREAMALIGN_CACHE_MAX_AGE_DAYS) and
    $NETRADIO_CACHE_ROOT/streamalign — so a value the
    server's `.env` sets bounds the child's cache too (the child inherits this
    module's whole environment). Called by `align_server.main` after `load_env`;
    call it again to re-read the environment. Returns the cache's record, or
    None when the policy is dark (NETRADIO_CACHE_ROOT unset) or its directory
    is refused."""
    return cache_budget.register("streamalign", max_age=STREAMALIGN_CACHE_MAX_AGE_DAYS,
                                 refill="re-decode", rank=3)


def _contained(path, root):
    """True if `path` resolves inside `root` (the realpath-containment check)."""
    rp = os.path.realpath(path)
    rr = os.path.realpath(root)
    return rp == rr or rp.startswith(rr + os.sep)


def list_streams(mp3_dir):
    """Capture stems the server can name (from its own MP3 listing)."""
    if not mp3_dir or not os.path.isdir(mp3_dir):
        return []
    out = []
    for fn in sorted(os.listdir(mp3_dir)):
        if fn.lower().endswith(".mp3") and _STEM_RE.match(fn[:-4]) \
                and _contained(os.path.join(mp3_dir, fn), mp3_dir):
            out.append(fn[:-4])
    return out


def list_originals():
    """[{num, name}] from the sources dir listing (the only original namespace)."""
    src = sources_dir()
    if not src or not os.path.isdir(src):
        return []
    out = []
    for fn in sorted(os.listdir(src)):
        m = _ORIG_FILE_RE.match(fn)
        if m and _contained(os.path.join(src, fn), src):
            out.append({"num": int(m.group(1)), "name": m.group(2)})
    return out


def parse_match_hints(labels_dir):
    """Pass-1 converter output → ready-to-open sync-point sets.

    Pairs `<stem>.origNNN.match.hints.tsv` (track sync rows, capture-local) with
    `origNNN.<stem>.match.hints.tsv` (orig sync rows, original-native seconds) by
    marker, and reads rate/polarity off the summary note. Anything malformed is
    skipped, never fatal — hints are suggestions, and this is a read of them.
    """
    if not labels_dir or not os.path.isdir(labels_dir):
        return []
    # DP-24: the converter emits into labels/automated/ now; a pre-migration
    # checkout still has the pairs loose at the labels/ root. Scan automated/
    # first, the root as fallback — a stem+orig pair seen in automated/ wins.
    scan_dirs = [d for d in (os.path.join(labels_dir, "automated"), labels_dir)
                 if os.path.isdir(d)]
    candidates = [(d, fn) for d in scan_dirs for fn in sorted(os.listdir(d))]
    sets = []
    seen_pairs = set()
    for scan_dir, fn in candidates:
        m = _HINTS_NAME_RE.match(fn)
        if not m:
            continue
        stem, orig = m.group(1), int(m.group(2))
        if (stem, orig) in seen_pairs:
            continue
        opath = os.path.join(scan_dir, "orig%03d.%s.match.hints.tsv" % (orig, stem))
        spath = os.path.join(scan_dir, fn)
        if not (os.path.isfile(opath)
                and _contained(spath, labels_dir) and _contained(opath, labels_dir)):
            continue
        # the pair is CLAIMED for this dir once both files exist — a fresh
        # automated/ pair that parses to zero points must yield nothing, not
        # let a stale root copy win (an independent review's finding)
        seen_pairs.add((stem, orig))
        try:
            srows = _read_tsv(spath)
            orows = _read_tsv(opath)
        except OSError:
            continue
        rate, inverted = 1.0, False
        for _, _, text in srows:
            sm = _SUMMARY_RE.search(text)
            if sm:
                rate = float(sm.group(1))
                inverted = sm.group(2) == "INVERTED"
        streams = {}
        for t, _, text in srows:
            tm = _TRACK_SYNC_RE.match(text)
            if tm:
                delta = float(tm.group(3)) if tm.group(3) is not None else None
                streams[tm.group(1)] = (t, float(tm.group(2)), delta)
        points = []
        for t, _, text in orows:
            om = _ORIG_SYNC_RE.match(text)
            if om and int(om.group(1)) == orig and om.group(2) in streams:
                s_t, conf, delta = streams[om.group(2)]
                points.append({"k": om.group(2), "stream_s": s_t, "orig_s": t,
                               "confidence": conf, "match_delta_s": delta})
        if points:
            sets.append({"stem": stem, "orig": orig, "rate": rate,
                         "inverted": inverted, "points": points})
    return sets


def _opt_num(value, lo=None, hi=None):
    """Coerce an optional number from the venv's answer: None / non-numeric /
    non-finite / out-of-range all become None (the listing shows the point without
    it). Non-finite matters: a bare NaN would serialize as invalid JSON on the
    sources route and can make a browser reject the whole response."""
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):   # Overflow: a JSON int too big for float
        return None
    if not math.isfinite(value):
        return None
    if lo is not None and value < lo:
        return None
    if hi is not None and value > hi:
        return None
    return value


def _validated_placed_sets(payload, streams):
    """The `placed` op's answer → the sets the sources route may serve. Nothing the
    venv says is trusted onto the wire: stems must be in the server's OWN stream
    namespace, numbers are coerced + range-checked, point lists are capped like
    MAX_OVERVIEW_POINTS, and anything malformed is dropped, never fatal."""
    if not isinstance(payload, dict) or not isinstance(payload.get("pairs"), list):
        return []
    sets = []
    for pair in payload["pairs"]:
        if not isinstance(pair, dict):
            continue
        stem = str(pair.get("stem") or "")
        if not _STEM_RE.match(stem) or stem not in streams:
            continue
        try:
            orig = int(pair.get("orig"))
        except (TypeError, ValueError):
            continue
        if not 0 <= orig <= 999:
            continue
        rate = _opt_num(pair.get("rate"), *RATE_RANGE)
        points = []
        for p in pair.get("points") or []:
            if not isinstance(p, dict):
                continue
            stream_s = _opt_num(p.get("stream_s"), 0.0, MAX_T_S)
            if stream_s is None:
                continue
            orig_s = _opt_num(p.get("orig_s"), 0.0, MAX_T_S)
            conf = _opt_num(p.get("seat_conf"))
            points.append({"k": str(p.get("k") or ""),
                           "stream_s": stream_s,
                           "orig_s": orig_s,
                           "derivable": bool(p.get("derivable")) and orig_s is not None,
                           "why": (str(p.get("why"))[:120]
                                   if p.get("why") is not None else None),
                           "verified": bool(p.get("verified")),
                           "grade": (str(p.get("grade"))[:32]
                                     if p.get("grade") is not None else None),
                           "seat_conf": conf})
            if len(points) >= MAX_OVERVIEW_POINTS:
                break
        if points:
            entry = {"source": "placed", "stem": stem, "orig": orig,
                     "rate": rate, "points": points}
            # the rate rules of #130 ride along for the operator:
            # AB-first multiplicity + duplicate-letter warnings (rate_note) —
            # free text from the venv, so control chars go and length is capped
            if pair.get("rate_note") is not None:
                note = re.sub(r"[\s\x00-\x1f\x7f]+", " ",
                              str(pair["rate_note"]))[:200].strip()
                if note:
                    entry["rate_note"] = note
            sets.append(entry)
    return sets


def list_placed(mp3_dir, runner=None, worker=None):
    """DP-22 §6.5 source (b): the hand-placed pairs, via the `streamalign placed` op.

    Same capability gate and single-flight discipline as every DSP call; the argv
    carries NOTHING client-supplied (the op lists every capture; filtering to the
    server's namespace happens here). Dark ([]) without the venv, on lock
    contention, on timeout, or when the op is absent/older than this server — the
    picker then shows hints pairs only, exactly as §6.5 specifies.
    """
    if not enabled():
        return []
    streams = set(list_streams(mp3_dir))
    if not streams:
        return []
    if not _run_lock.acquire(blocking=False):
        return []
    try:
        payload = None
        w = worker if worker is not None else (_worker if worker_enabled() else None)
        if w is not None:
            try:
                payload = w.request({"op": "placed"}, mp3_dir,
                                    timeout=SUBPROCESS_TIMEOUT_S)
            except WorkerError:
                payload = None
        if not isinstance(payload, dict) or payload.get("error"):
            argv = [_venv_python(), "-m", "streamalign", "placed"]
            env = dict(os.environ)
            # PYTHONPATH is redundant once the venv carries netradio-scripts.pth (`make venv`/
            # `make dep`, 2026-09-17); kept so a venv built by hand still works.
            env["PYTHONPATH"] = os.path.join(repo_root(), "scripts")
            env["NETRADIO_AUDIO_DIR"] = mp3_dir or ""   # env parity with run_request
            run = runner or subprocess.run
            proc = run(argv, cwd=repo_root(), env=env, shell=False,
                       capture_output=True, timeout=SUBPROCESS_TIMEOUT_S)
            try:
                payload = json.loads((proc.stdout or b"").decode("utf-8", "replace"))
            except ValueError:
                payload = None
        return _validated_placed_sets(payload, streams)
    except (subprocess.TimeoutExpired, OSError):
        return []
    finally:
        _run_lock.release()


def _read_tsv(path):
    rows = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 3:
                try:
                    rows.append((float(parts[0]), float(parts[1]), parts[2]))
                except ValueError:
                    continue
    return rows


def _validated_points(raw):
    """DP-10: the overview's sync points -- a bounded list of finite [a, b] pairs.
    Returns (points, None) or (None, error_string); nothing else about the value
    is trusted (the DSP side re-guards with its own _check_points)."""
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_OVERVIEW_POINTS:
        return None, "bad points"
    points = []
    for p in raw:
        if not isinstance(p, (list, tuple)) or len(p) != 2:
            return None, "bad points"
        try:
            a, b = float(p[0]), float(p[1])
        except (TypeError, ValueError, OverflowError):
            return None, "bad points"
        if not (0.0 <= a <= MAX_T_S and 0.0 <= b <= MAX_T_S):
            return None, "points out of range"
        points.append([a, b])
    return points, None


def _validated(params, mp3_dir, op="slice"):
    """(clean_dict, None) or (None, error_string). Identifier-based, hard caps."""
    if not isinstance(params, dict):
        return None, "request body must be a JSON object"
    stem = str(params.get("stem") or "")
    if not _STEM_RE.match(stem):
        return None, "bad stem"
    if stem not in list_streams(mp3_dir):
        return None, "unknown stem"
    try:
        orig = int(params.get("orig"))
    except (TypeError, ValueError):
        return None, "bad orig"
    if not any(o["num"] == orig for o in list_originals()):
        return None, "unknown orig"

    def _num(key, lo, hi, default=None):
        v = params.get(key, default)
        try:
            v = float(v)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("bad %s" % key)
        if not (lo <= v <= hi):
            raise ValueError("%s out of range" % key)
        return v

    engine = str(params.get("engine", "phat") or "phat")
    if engine not in ENGINES:
        return None, "bad engine"
    try:
        clean = {
            "stem": stem,
            "orig": orig,
            "stream_t": _num("stream_t", 0.0, MAX_T_S),
            "orig_t": _num("orig_t", 0.0, MAX_T_S),
            "rate": _num("rate", RATE_RANGE[0], RATE_RANGE[1], 1.0),
            "win": _num("win", 0.5, MAX_WIN_S, 6.0),
            "radius": _num("radius", 0.001, MAX_RADIUS_S, 0.05),
            "context": _num("context", 1.0, MAX_CONTEXT_S, DEFAULT_CONTEXT_S),
            "invert": bool(params.get("invert")),
            "engine": engine,
        }
    except ValueError as exc:
        return None, str(exc)
    if op == "overview":
        points, perr = _validated_points(params.get("points"))
        if perr:
            return None, perr
        clean["points"] = points
    return clean, None


def build_argv(clean, op="slice"):
    """The fixed subprocess argument list. Nothing client-supplied is interpolated —
    every value is its own argv entry, and the executable/module are constants."""
    argv = [_venv_python(), "-m", "streamalign", "inspect-slice",
            clean["stem"], str(clean["orig"]),
            "--stream-t", "%.6f" % clean["stream_t"],
            "--orig-t", "%.6f" % clean["orig_t"],
            "--rate", "%.6f" % clean["rate"],
            "--win", "%.3f" % clean["win"],
            "--sources", sources_dir()]
    if clean["invert"]:
        argv.append("--invert")
    if op == "refine":
        argv += ["--refine", "--radius", "%.3f" % clean["radius"],
                 "--engine", clean["engine"]]
    elif op == "context":
        argv += ["--context", "%.3f" % clean["context"]]
    elif op == "overview":
        argv.append("--overview")
        for a, b in clean["points"]:
            argv += ["--point", "%.6f:%.6f" % (a, b)]
    return argv


def worker_request(clean, op):
    """The JSON-lines request the keep-warm worker gets — same validated values,
    same identifiers-only shape, as the argv the one-shot subprocess would get."""
    req = {"op": op, "stem": clean["stem"], "orig": clean["orig"],
           "stream_t": clean["stream_t"], "orig_t": clean["orig_t"],
           "rate": clean["rate"], "invert": clean["invert"], "win": clean["win"]}
    if op == "refine":
        req["radius"] = clean["radius"]
        req["engine"] = clean["engine"]
    elif op == "context":
        req["context"] = clean["context"]
    elif op == "overview":
        req["points"] = clean["points"]
    return req


class WorkerError(Exception):
    """A transport-level worker failure — the caller falls back to one-shot."""


class WorkerClient:
    """The keep-warm `streamalign inspect-worker` child (DP-07).

    Spawned once (fixed argv, shell=False, the same env the one-shot path uses),
    spoken to over pipes as JSON lines with a per-request timeout, killed and
    respawned on any failure. `popen` is the test seam (defaults to
    subprocess.Popen). NOT thread-safe on its own — callers hold the module's
    single-flight lock, which is exactly the concurrency contract everywhere else
    in this file.
    """

    def __init__(self, popen=None):
        self._popen = popen or subprocess.Popen
        self._proc = None
        self._lines = None       # queue of stdout lines; None sentinel = EOF
        self._spawn_key = None   # the mp3_dir baked into the child's env

    def _alive(self):
        return self._proc is not None and self._proc.poll() is None

    def stop(self):
        if self._proc is not None:
            try:
                self._proc.kill()
            except OSError:
                pass
        self._proc = None
        self._lines = None

    def _spawn(self, mp3_dir):
        argv = [_venv_python(), "-m", "streamalign", "inspect-worker",
                "--sources", sources_dir()]
        env = dict(os.environ)
        env["PYTHONPATH"] = os.path.join(repo_root(), "scripts")   # redundant with the .pth; see run_request
        env["NETRADIO_AUDIO_DIR"] = mp3_dir or ""
        self._proc = self._popen(argv, cwd=repo_root(), env=env, shell=False,
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, text=True, bufsize=1)
        lines = queue.Queue()

        def _pump(proc, q):
            try:
                for line in proc.stdout:
                    q.put(line)
            except (OSError, ValueError):
                pass
            q.put(None)

        threading.Thread(target=_pump, args=(self._proc, lines), daemon=True).start()
        self._lines = lines
        self._spawn_key = mp3_dir
        self._send({"op": "ping"})
        if not self._recv(WORKER_PING_TIMEOUT_S).get("ok"):
            raise WorkerError("worker ping failed")

    def _send(self, payload):
        try:
            self._proc.stdin.write(json.dumps(payload) + "\n")
            self._proc.stdin.flush()
        except (OSError, ValueError) as exc:
            raise WorkerError("worker stdin closed: %s" % exc)

    def _recv(self, timeout):
        try:
            line = self._lines.get(timeout=timeout)
        except queue.Empty:
            raise WorkerError("worker timed out")
        if line is None:
            raise WorkerError("worker died")
        try:
            payload = json.loads(line)
        except ValueError:
            raise WorkerError("worker produced a non-JSON line")
        if not isinstance(payload, dict):
            raise WorkerError("worker produced a non-object response")
        return payload

    def request(self, req, mp3_dir, timeout=SUBPROCESS_TIMEOUT_S):
        """One request → one response dict. Raises WorkerError on any transport
        failure (spawn, write, timeout, death, garbage), after killing the child so
        the NEXT request starts fresh; the caller falls back to one-shot for this
        one. A changed mp3_dir also respawns — the child's env bakes it in."""
        mp3_dir = mp3_dir or ""
        try:
            if not self._alive() or self._spawn_key != mp3_dir:
                self.stop()
                self._spawn(mp3_dir)
            self._send(req)
            return self._recv(timeout)
        except WorkerError:
            self.stop()
            raise
        except (OSError, ValueError) as exc:   # spawn/launch failures
            self.stop()
            raise WorkerError("worker could not be started: %s" % str(exc)[:200])


_worker = WorkerClient()


def worker_enabled():
    """The keep-warm worker is on by default; NETRADIO_ALIGN_WORKER=0 disables it
    (leaving only the always-there one-shot subprocess path)."""
    return os.environ.get("NETRADIO_ALIGN_WORKER", "").strip().lower() not in (
        "0", "false", "no", "off")


def run_request(params, mp3_dir, op="slice", runner=None, worker=None):
    """Validate → keep-warm worker (or one-shot subprocess) → parsed JSON.
    Returns (payload, http_status).

    `op` is one of OPS ("slice", "refine", "context"). `runner` is the one-shot
    test seam (defaults to subprocess.run); `worker` the keep-warm one (defaults to
    the module client when `worker_enabled()`). The module lock makes this
    compute-on-demand single-file across BOTH paths: a second concurrent request
    gets 429. A worker failure of any kind falls back to the one-shot path for
    that request — the worker is an accelerator, never a new way to fail.
    """
    if op not in OPS:
        return {"error": "unknown op"}, 400
    if not enabled():
        return {"error": "align inspector not configured "
                         "(needs .venv and the originals directory)"}, 404
    clean, err = _validated(params, mp3_dir, op=op)
    if err:
        return {"error": err}, 400
    if not _run_lock.acquire(blocking=False):
        return {"error": "another align computation is running"}, 429
    try:
        w = worker if worker is not None else (_worker if worker_enabled() else None)
        if w is not None:
            try:
                payload = w.request(worker_request(clean, op), mp3_dir,
                                    timeout=SUBPROCESS_TIMEOUT_S)
                return payload, (400 if payload.get("error") else 200)
            except WorkerError:
                pass                       # one-shot fallback below
        argv = build_argv(clean, op=op)
        env = dict(os.environ)
        # PYTHONPATH is redundant once the venv carries netradio-scripts.pth (`make venv`/
        # `make dep`, 2026-09-17); kept so a venv built by hand still works.
        env["PYTHONPATH"] = os.path.join(repo_root(), "scripts")
        env["NETRADIO_AUDIO_DIR"] = mp3_dir or ""
        run = runner or subprocess.run
        proc = run(argv, cwd=repo_root(), env=env, shell=False,
                   capture_output=True, timeout=SUBPROCESS_TIMEOUT_S)
        out = (proc.stdout or b"").decode("utf-8", "replace")
        try:
            payload = json.loads(out)
        except ValueError:
            payload = None
        if not isinstance(payload, dict):    # no JSON, or a non-object (schema mismatch)
            tail = (proc.stderr or b"").decode("utf-8", "replace")[-300:]
            return {"error": "inspect-slice produced no JSON object", "detail": tail}, 502
        return payload, (400 if payload.get("error") else 200)
    except subprocess.TimeoutExpired:
        return {"error": "inspect-slice timed out"}, 504
    except OSError as exc:
        # a configured-but-unexecutable venv python, a vanished cwd, etc. -- a launch
        # failure is the same controlled 502 as bad output, never a dropped request
        return {"error": "inspect-slice could not be launched", "detail": str(exc)[:300]}, 502
    finally:
        _run_lock.release()
