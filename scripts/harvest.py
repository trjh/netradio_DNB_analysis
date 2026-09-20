#!/usr/bin/env python3
"""Sign the audio that lands in the harvest directories, and score it against the Mysteries.

    . .venv/bin/activate && python scripts/harvest.py --run          # the loop, for weeks
    . .venv/bin/activate && python scripts/harvest.py --status

The harvester knows directories, and nothing else. `NETRADIO_HARVEST_DIRS` (in `.env`) names
one or more absolute directories, `:`-separated; the loop reads the TOP LEVEL of each and
writes nothing into any of them. An audio file is `<key>.<ext>`, and beside it the feeder
leaves `<key>.json`, the sidecar — written after the audio's final rename, so a file whose
sidecar has not landed yet is simply not finished, and is neither read nor signed. The sidecar
is the only notice the harvester takes: it never reads any queue, index or library to decide
what to work on, and it is not responsible for moving audio into or out of the directories.
The full contract, written for whatever fills the directories, is
[docs/HARVEST_FEED.md](docs/HARVEST_FEED.md).

Two files another process writes tell the harvester what not to do:

* `.harvest/rulings.json` — the retired set, `{key: reason}`: every key the search must never
  propose again, for any mystery, present or future. The process that owns the rulings writes
  the file whole, atomically, at its start and after every ruling; this side only ever READS
  it, and the supervisor will not start a harvester while it is absent — a search that has
  forgotten every ruling hands back records already rejected.
* `.harvest/PAUSED` — the pause flag, noticed within one pass.

The idea
--------
The matcher can only find what is in the pool, and the pool we want is far bigger than this
disk. So **keep the signature, not the audio**: a chroma signature is a 12xN float16 matrix,
~55KB against ~8MB for the track. A 100,000-track pool is ~5GB of signatures, and the audio
never has to be kept at all -- it is decoded, hashed to chroma, and dropped.

Except a brief excerpt of a near-miss. If a candidate scores near a Mystery Track we keep the
matched ~30-second window -- and only that window -- so a human can listen and confirm or reject
it. Cut from the audio already in memory, so nothing is decoded twice. It is not a copy of the
record; it is a magnifying glass over the moment the matcher flagged, swept after 30 days.

The ledger
----------
`.harvest/ledger.json` is the harvester's own record: one row per key, `signed` or `delayed`
with a reason, seeded from the signature bucket's listing at the first start and reconciled
against that listing on every start. It is the mark -- nothing is renamed, moved, or written
beside the audio -- and it is the one thing a feeder reads back (see HARVEST_FEED.md).

The loop
--------
One pass: re-read the rulings, drop the excerpts of ruled-on leads, stamp the bucket's count,
score a bounded chunk of the not-yet-scored signatures, sign one file (decode in a child
process, upload, write the row, score it against every unsolved mystery, keep an excerpt for a
near match), then sleep and repeat. A pass with nothing to sign sleeps ~20s; the directories
are cheap to list, and a ruling or a pause must take effect within one pass.
"""

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np                                   # noqa: E402

from streamalign import audio as _audio              # noqa: E402
from streamalign import chroma_match as _cm          # noqa: E402
from streamalign import groundtruth as _gt           # noqa: E402
from streamalign import mystery as _mystery          # noqa: E402

import cache_budget                                  # noqa: E402  (the machine's one cache policy)
import chroma_recipe                                 # noqa: E402  (THE recipe, single source)
import memwatch                                      # noqa: E402  (footprint + allocator canary)
import selftest                                      # noqa: E402  (the canary; see run())
import sigstore                                      # noqa: E402  (bucket = the pool's only home)

HOME = _gt.REPO_ROOT
STATE_DIR = os.path.join(HOME, ".harvest")
STATE = os.path.join(STATE_DIR, "state.json")
LEDGER = os.path.join(STATE_DIR, "ledger.json")
PAUSE = os.path.join(STATE_DIR, "PAUSED")

# The retired set, from a rulings file another process writes: {key: reason}, the keys this
# search must never propose again, for any mystery, present or future -- one key per entry
# that entry's owner has ruled on, or that is the owner's own upload. The writer of the
# rulings owns the file (whole, atomically, at its start and after every ruling); this side
# only ever READS it, and the supervisor will not start a harvester while it is absent -- a
# search that has forgotten every ruling hands back records already rejected.
RULINGS = os.path.join(STATE_DIR, "rulings.json")

# The directories of audio to sign: one or more absolute paths, `:`-separated, read at import
# (the wrapper sources `.env` before any import). The loop reads the TOP LEVEL of each and
# writes nothing into any of them -- a subdirectory is never read, so a directory's own
# scratch can sit one level down and stay invisible. A listed directory that does not exist
# is skipped with a row in the issues list, so a typo in `.env` is visible rather than silent.
HARVEST_DIRS = os.environ.get("NETRADIO_HARVEST_DIRS", "")

# --- the harvester's two caches, on the machine's one cache policy (cache_budget.py) ----------
#
# `chroma` is the signature working cache: the chroma signature of every candidate analysed so
# far, a small derived matrix that the signature bucket takes over as each one is verified there
# (sigstore). `candidates` is the excerpt board: the best few ~30-second excerpts per mystery,
# cut where the matcher flagged so a person can rule on them by ear, and swept after
# KEEP_TTL_DAYS. Both hold only audio this project fetched or derived itself.
#
# Neither is a fixed path any more. Each registers on the cache policy at import, so its
# directory comes from the machine's settings: NETRADIO_CHROMA_CACHE_DIR /
# NETRADIO_CANDIDATES_CACHE_DIR, defaulting to $NETRADIO_CACHE_ROOT/<name>. The policy bounds
# them: `chroma` by a 14-day age (each entry is used the moment it is computed, and the bucket
# is its long-term home), `candidates` by a 250 MB cap evicting the worst excerpt of a mystery
# first -- the same rule KEEP_TOP applies to the board -- and by the same 30-day age. While
# NETRADIO_CACHE_ROOT is unset there is no cache directory at all: the harvester refuses to
# run rather than sign audio whose signatures it then cannot keep.
CHROMA_CACHE = "chroma"
CANDIDATES_CACHE = "candidates"
CHROMA_CACHE_MAX_AGE_DAYS = 14     # the bucket is the signature's long-term home
CANDIDATES_CACHE_CAP = 250 * cache_budget.MB   # a dozen ~30s excerpts per mystery are small

# The resolved directories, read at import and re-read by register_caches(). They stay module
# attributes because the code -- and the tests -- steer the harvester by setting them.
CACHE = None
KEEP = None
_CACHE_AT_IMPORT = None
_KEEP_AT_IMPORT = None


def _excerpt_score(path):
    """The excerpt board's `by-score` order: `MT<n>-<cost>-<hash>.wav` names its cost, and the
    cost is how good the excerpt is (lower = better). The policy evicts the LOWEST score first,
    so the score is the cost negated -- the worst excerpt of a mystery, the one a full board
    would drop for a better candidate, is the first to go."""
    try:
        return -float(os.path.basename(path).split("-")[1])
    except (IndexError, ValueError):
        return 0.0


def _excerpt_pinned(path):
    """The board's PROVENANCE.txt is pinned. Its name parses to no cost, so the by-score order
    counts it as an ordinary entry, and a cap-, floor- or age-driven eviction run could take
    it -- while `_write_provenance` would not restore it until the next kept excerpt. The note
    is the directory's one line of "this is not a music library"; it never leaves."""
    return os.path.basename(path) == "PROVENANCE.txt"


def register_caches():
    """(Re-)register the two caches on the cache policy, reading the environment now -- call it
    again to re-read it. While the policy is dark (NETRADIO_CACHE_ROOT unset) both stay
    unregistered and their directories are None; the same is true of either cache whose
    registration the policy refuses (a directory that overlaps another cache's)."""
    global CACHE, KEEP, _CACHE_AT_IMPORT, _KEEP_AT_IMPORT
    # rank 4 / rank 10: of the caches sharing the policy's floor, the signatures give up
    # entries fourth (each refills from the bucket by key) and the excerpt board tenth (each
    # excerpt is re-cut if its candidate is ever fetched again). The literal names, not the
    # constants above, so env_check.py's code scan sees the registrations and counts their
    # variable families as read.
    cache_budget.register("chroma", max_age=CHROMA_CACHE_MAX_AGE_DAYS,
                         refill="bucket:chroma/", rank=4)
    cache_budget.register("candidates", cap=CANDIDATES_CACHE_CAP, order="by-score",
                         score=_excerpt_score, max_age=KEEP_TTL_DAYS,
                         pinned=_excerpt_pinned,
                         refill="re-cut", rank=10)
    CACHE = _CACHE_AT_IMPORT = cache_budget.dir_of(CHROMA_CACHE)
    KEEP = _KEEP_AT_IMPORT = cache_budget.dir_of(CANDIDATES_CACHE)


def _chroma_dir():
    """The signature cache's directory, resolved through the registry at each call -- a root
    configured after import is honoured. A CACHE set by hand, or patched by a test, still
    wins. None while the cache is dark."""
    return CACHE if CACHE is not _CACHE_AT_IMPORT else cache_budget.dir_of(CHROMA_CACHE)


def _keep_dir():
    """The excerpt board's directory, resolved the same way as `_chroma_dir`. None while the
    cache is dark."""
    return KEEP if KEEP is not _KEEP_AT_IMPORT else cache_budget.dir_of(CANDIDATES_CACHE)

# THE ledger/state writer lock. run() and the on-demand --sign-one -- the two paths that take
# it -- each hold this flock for their lifetime, so a second of them refuses loudly instead of
# interleaving their writes of ledger.json and state.json. The rare hand tool --forget also
# rewrites state.json, with no lock: use it on a stopped run, since beside a live one it can
# interleave. The path keeps its historic name (collector.lock) -- the split runtime's collector
# shared it -- so a writer still running under an old binary and this one still exclude each
# other.
WRITER_LOCK = os.path.join(STATE_DIR, "collector.lock")


def acquire_writer_lock():
    """Take the ONE-writer flock (non-blocking). Returns the open file on success -- KEEP A
    REFERENCE, the lock lives and dies with it -- or None when another writer holds it."""
    os.makedirs(STATE_DIR, exist_ok=True)
    fh = open(WRITER_LOCK, "a")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh

HOP = chroma_recipe.HOP          # single source: chroma_recipe.py
QUERY_S = 120.0
# The shortest clip we will search WITH. MT7's is 23 seconds, and it produced five confident false
# positives all within 0.0007 of each other: a short query drives every cost down until the matcher
# can no longer tell records apart, and a degenerate ranking looks exactly like a real one. Better
# to search for nothing than to search for everything. A mystery below this floor leaves the query
# set and comes back by itself once a longer clip is cut.
MIN_QUERY_S = 60.0

# RETAINING EXCERPTS FOR AURAL CHECK: a bounded leaderboard of SHORT EXCERPTS, not a library.
#
# We never keep a full track. What is retained is a ~30-second excerpt around the matched instant
# (see write_excerpt) -- enough to recognise a record by ear, far too short to be a copy of it --
# and only for the best few candidates per mystery, and only for KEEP_TTL_DAYS.
#
# A bounded leaderboard, not a threshold, because the calibration (docs/CALIBRATION.md) is
# unambiguous: the true-match and non-match populations OVERLAP (true up to 0.0971, non-match down
# to 0.0376, median 0.0949). No cost gate works -- low excludes real matches, high keeps
# everything. So: the best KEEP_TOP excerpts PER MYSTERY, evicting the worst when a better one
# lands. Bounded, predictable, and it cannot be wrong about a threshold because it does not use
# one.
KEEP_TOP = 12
# How many cached signatures to re-score per pass of the loop. Small on purpose: rescanning is CPU,
# so a modest chunk each pass rides along instead of stalling the signing.
RESCAN_PER_PASS = 25
KEEP_CEILING = 0.130      # never retain an excerpt worse than the worst plausible true match
KEEP_TTL_DAYS = 30        # a lead not listened to in a month is not a lead -- swept
# A reported MATCH still needs cost AND margin. The populations OVERLAP (true match up to 0.0971,
# non-match down to 0.0376), so no cost alone can separate them: RANK is the reliable signal, and
# the margin test is what actually carries the gate. 40 of 41 tracks rank #1 against their own
# original, so the margin is real.
MATCH_COST = 0.050

# THE BACKSTOP, not the rule. A file whose declared length -- or whose decoded length, when the
# sidecar declares none -- is over four hours is `delayed` with `too_long`, refused and never
# truncated: analysing the first four hours of a six-hour set and filing the result under the
# key would be a partial answer wearing a complete one's clothes. Anything that long is a
# master the feeder is expected to hand over as parts, each part a key of its own.
MAX_DURATION_S = 4 * 3600

# How long a pass sleeps when it finds nothing to sign, and how long the pause flag is polled:
# the pause notice says "within ~20s" and a ruling must reach the search within one pass.
PASS_GAP_S = 20.0

register_caches()                 # at import: the wrapper sources .env before any import

# --- the key ------------------------------------------------------------------------------------
#
# An audio file is `<key>.<ext>`, its key `u` + the first 20 hex characters of the SHA-1 of
# its source URL (docs/HARVEST_FEED.md). The harvester takes the STEM as the key and computes
# nothing: a `url` in the sidecar is data it carries, never something it reads for meaning.
# The shape is still checked, because the pool's own listing admits only objects named that
# way (sigstore filters on it) -- a stem of any other shape would file a signature where the
# pool cannot see it, and a signature nobody can list is a signature nobody can score.
_KEY = re.compile(r"u[0-9a-f]{20}", re.ASCII)


def file_key(path):
    """The key a file's own name carries: the stem of its basename."""
    return os.path.basename(path).rsplit(".", 1)[0]


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load(path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _save(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


def blank_state():
    return {"started": _now(), "updated": _now(), "analyzed": 0, "kept": 0, "errors": 0,
            "skipped_cached": 0, "matches": [], "issues": []}


# Past this, a number is not a length, it is a typo or an attack. A declared duration is
# unbounded JSON -- a 5,000-digit int raises OverflowError on its way to a float, and the one
# thing a refusal message must never do is become the crash it was written to report. The
# comparison below is integer-only, so it is safe at any magnitude.
_PRINTABLE_MAX_S = 10 ** 12


def _hours(seconds):
    """`seconds` as hours, for a message. Total: it prints something for any number at all."""
    if seconds > _PRINTABLE_MAX_S:
        return "beyond any real length"
    return "%.1f h" % (seconds / 3600.0)


def _expect_s(sidecar):
    """The sidecar's `duration_s` as a real length, or None when it carries none.

    The length check runs only on a claim the sidecar actually made. A duration that is
    missing, non-numeric, a bool (which `isinstance(x, int)` accepts in Python), or zero is
    no evidence of length at all: `duration_s: 0` is how a source with no length to declare
    reads, and taken literally it would refuse every file over ten seconds. None means "no
    claim", and no claim is never a mismatch.
    """
    d = (sidecar or {}).get("duration_s")
    if isinstance(d, bool) or not isinstance(d, (int, float)) or d <= 0:
        return None
    return float(d)


# --- stopping ----------------------------------------------------------------------------------
#
# There was no signal handler at all, once. A SIGINT or SIGTERM to this pid alone raised
# KeyboardInterrupt inside whatever was running and the process left -- while ffmpeg carried
# on decoding with nobody watching. (The supervisor was never affected: it signals the whole
# process group.)
#
# The handler sets a FLAG and does not raise. Raising lands on whatever bytecode happened to be
# executing, which includes the middle of a state save; a flag is checked at points we choose.

_STOP = {"signum": 0, "child": None, "procs": [], "part": None}


def _stop_requested():
    return bool(_STOP["signum"])


# The error a decode reports when a signal interrupted it. A SENTINEL, not a message: it is the one
# value here that must never be read as a per-file failure, because a failure is a VERDICT and a
# verdict retires the file, which is never re-signed while its row stands.
STOPPED = "stopped"


def was_stopped(err):
    """True when a decode came back interrupted rather than failed.

    A guard that reads a different signal from the one the danger travels on is not a guard. A
    stop reaches a caller by either of two routes -- THIS process was signalled, which raises the
    flag, or only the child was, which arrives as its exit code turned into this string -- so
    every guard checks both. `selftest.was_stopped` is the same predicate, duplicated rather than
    imported because selftest must never import the harvester.
    """
    return (err or "").strip().lower() == STOPPED


def _stop_name():
    try:
        return signal.Signals(_STOP["signum"]).name
    except ValueError:
        return "a signal"


def _end(proc, grace=2.0):
    """Ask a child to stop, give it `grace` seconds, then insist."""
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass
    except OSError:
        pass


def _parent_stop(signum, _frame):
    """Parent handler: raise the flag, then stop whatever decode is actually in flight.

    Normally the decode is a child process, so terminate it. Under NETRADIO_HARVEST_CHILD=0
    it is ffmpeg spawned by THIS process, sitting in `_STOP["procs"]` -- leaving that running
    against a parent that has gone is exactly the behaviour this handler exists to end.
    """
    _STOP["signum"] = signum
    child = _STOP["child"]
    if child is not None and child.poll() is None:
        try:
            child.terminate()
        except OSError:
            pass
    for proc in _STOP["procs"]:                 # the in-process path only
        _end(proc)
    _STOP["procs"] = []


def _child_stop(signum, _frame):
    """Decode-child handler: stop ffmpeg, then leave with 128 + signum."""
    _STOP["signum"] = signum
    for proc in _STOP["procs"]:
        _end(proc)
    _STOP["procs"] = []
    if _STOP["part"]:
        _unlink(_STOP["part"])
    os._exit(128 + signum)


def install_signal_handlers(child=False):
    """Handle SIGINT and SIGTERM. A no-op off the main thread, which is where tests run."""
    handler = _child_stop if child else _parent_stop
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            pass


def _nap(seconds):
    """Sleep, but in one-second slices, and come back the moment a stop is asked for.

    A pass that finds nothing sleeps this long, and a pause waits it out; sleeping through a
    SIGTERM makes a stopped harvester look exactly like a hung one. Returns True when the nap
    was cut short.
    """
    end = time.time() + seconds
    while True:
        if _stop_requested():
            return True
        left = end - time.time()
        if left <= 0:
            return False
        time.sleep(min(1.0, left))


def _unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass


# --- the decode: one child process per file -----------------------------------------------------
#
# The recipe's working set is large and short-lived, and macOS does not give it back (see
# memwatch). Running each file's decode in its own process means the long-lived parent -- which
# holds the state, the ledger, the mysteries and the matching board -- never touches a file's
# audio at all, and whatever a child allocates leaves with the child. The parent's footprint
# stays flat across files instead of climbing to a high-water mark and staying there.

JOBS = os.path.join(STATE_DIR, "tmp")     # one directory per in-flight decode
STDERR_KEEP = 4096                        # bytes of ffmpeg's stderr we hold on to
JOB_STALE_S = 3600                        # a job dir older than this belongs to a crashed child


def _drain(stream, sink):
    """Read a pipe to EOF into a bounded tail, on a thread. `sink` is a ONE-element list.

    ffmpeg can fill its 64 KB stderr pipe long before the decode finishes, and nobody reads
    it until then, so the two can deadlock: ffmpeg blocked writing stderr, the parent blocked
    waiting on ffmpeg. Only the tail matters -- the error we report is the last line -- so the
    buffer is bounded.

    The thread REPLACES `sink[0]` rather than editing a shared list in place. A list item
    assignment is a single bytecode, so a reader that gave up waiting for this thread to finish
    sees some whole earlier tail, never a half-rewritten one.
    """
    buf = b""
    try:
        for chunk in iter(lambda: stream.read(65536), b""):
            buf = (buf + chunk)[-STDERR_KEEP:]
            sink[0] = buf
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _last_line(chunks, prefix=""):
    """The last non-empty line of a drained stderr, as the error string callers already expect.

    Only the last STDERR_KEEP bytes survive the drain, so a single final line longer than that
    comes back as its tail. That is a fair trade for a bounded buffer: the result is truncated
    to 160 characters either way, and a message that long is a stack trace, not a reason.
    """
    text = b"".join(chunks).decode("utf-8", "replace")
    lines = [ln.strip() for ln in text.strip().split("\n") if ln.strip()]
    return (prefix + lines[-1])[:160] if lines else ""


def _wait(proc, poll_s=1.0, spool=None, cap_bytes=None):
    """Wait for a subprocess a second at a time, so a stop is acted on during a long decode.

    A decode can run for hours; the poll is what lets a stop reach it, and the spool is
    unlinked by the stop path either way. `spool` with `cap_bytes` bounds the decode itself:
    once the spool holds more than `cap_bytes`, the file's length has passed the caller's
    backstop and the refusal is already decided, so the process is stopped here instead of
    spooling hours more of a file that will be refused anyway -- the caller's own measured
    check below the wait speaks the verdict. Returns the exit status, or None when a stop
    was asked for.
    """
    while True:
        try:
            return proc.wait(timeout=poll_s)
        except subprocess.TimeoutExpired:
            if _stop_requested():
                return None
            if spool is not None and cap_bytes is not None:
                try:
                    past = os.path.getsize(spool) > cap_bytes
                except OSError:
                    past = False
                if past:
                    _end(proc)


def job_dir(key):
    return os.path.join(JOBS, key)


def sweep_job_dirs(max_age_s=JOB_STALE_S):
    """Remove what a crashed decode child left behind. Nothing younger than an hour: a decode
    child may still be mid-decode, and its spool file is not ours to delete."""
    if not os.path.isdir(JOBS):
        return 0
    now = time.time()
    n = 0
    for name in sorted(os.listdir(JOBS)):
        path = os.path.join(JOBS, name)
        try:
            if now - os.path.getmtime(path) > max_age_s:
                shutil.rmtree(path, ignore_errors=True)
                n += 1
        except OSError:
            pass
    return n


def _decode_and_sign(path, job, expect_s=None):
    """Decode one file and write its signature. THE CHILD'S WHOLE JOB. Returns `result.json`.

    ffmpeg reads the FILE, whole: no `-ss`, no `-t`, no fragment -- a part's file is already
    on the part's own clock, and the harvester parses no fragment (docs/HARVEST_FEED.md).
    `expect_s` is the sidecar's declared `duration_s` as the sign read it beside the file
    (`sign_file` re-reads the sidecar rather than trusting the scan's copy), carried across
    the fork in the job file, so the length check inside the child weighs the claim the sign
    itself weighed.

    The decoded PCM goes to a FILE in the job directory, written by ffmpeg itself: one
    allocation at the size the decode turned out to be, read back as a memory map so an
    excerpt can still be cut without a second read.

    A signature is written ONLY when ffmpeg exited 0, the spool is long enough, the file is
    not over-long, the length matches the sidecar's claim, and no stop was requested. A
    truncated or wrong-length decode would produce a perfectly plausible, permanently wrong
    recipe-1 signature for this key -- cached, uploaded, and never decoded again.
    """
    try:
        # THE FIRST DOOR, and the cheapest: the sidecar's own claim, refused before anything
        # is spawned. `expect_s` was validated on the way in (see `_expect_s`); a claim over
        # the cap is not audio to decode for hours and then refuse.
        if expect_s is not None and expect_s > MAX_DURATION_S:
            return {"ok": False, "reason": "too_long",
                    "error": "too long: %s -- refused, not truncated" % _hours(expect_s)}
        os.makedirs(job, exist_ok=True)
        part = os.path.join(job, "pcm.f32le.part")
        pcm = os.path.join(job, "pcm.f32le")
        _STOP["part"] = part
        started = time.time()

        ff_argv = ["ffmpeg", "-v", "error", "-i", path,
                   "-ac", "1", "-ar", str(_audio.SR), "-f", "f32le", "pipe:1"]
        with open(part, "wb") as spool:
            ff = subprocess.Popen(ff_argv, stdout=spool, stderr=subprocess.PIPE)
            _STOP["procs"] = [ff]
            ff_err = [b""]                      # one-element sink -- see _drain
            drains = [threading.Thread(target=_drain, args=(ff.stderr, ff_err), daemon=True)]
            for t in drains:
                t.start()
            # THE SPOOL'S BACKSTOP. A sidecar that declares no length closes the first door
            # above, so decoding in full is the only way the four-hour backstop can be
            # answered -- and before this bound, a 20-hour file spooled every hour of itself
            # (4.6 GB) before the refusal below. Once the spool holds more than the cap, the
            # decode's own measure has already answered, so ffmpeg is stopped and the
            # measured-mark refusal below speaks the verdict: nothing is truncated, and a
            # file with no claim costs the cap to refuse, not its whole length.
            _wait(ff, spool=part, cap_bytes=MAX_DURATION_S * _audio.SR * 4)
            for t in drains:
                t.join(timeout=5)
        _STOP["procs"] = []

        if _stop_requested():
            _unlink(part)
            return {"ok": False, "error": STOPPED}

        size = os.path.getsize(part) if os.path.exists(part) else 0
        n_samples = size // 4
        seconds = n_samples / _audio.SR

        # THE MEASURED MARK. The claim above is the sidecar's; this is the decode's. A file
        # whose sidecar declares no length is never refused on a claim, but the decode's own
        # length still answers the one question the backstop exists for, at the cost of one
        # `os.path.getsize` the code already paid for. Nothing is ever truncated: the
        # signature is the thing that must not exist.
        if seconds > MAX_DURATION_S:
            _unlink(part)
            return {"ok": False, "reason": "too_long",
                    "error": "too long: %s of audio -- refused, not truncated" % _hours(seconds)}

        if ff.returncode != 0 or size == 0:
            _unlink(part)
            return {"ok": False, "reason": "decode_failed",
                    "error": _last_line(ff_err, "ffmpeg: ") or
                             ("ffmpeg: exit %s" % ff.returncode)}

        if n_samples < chroma_recipe.MIN_SECONDS * _audio.SR:
            _unlink(part)
            return {"ok": False, "reason": "decode_failed",
                    "error": "too short (%.0fs)" % seconds}

        # THE PERMANENT LENGTH CHECK (the one piece of the old interim guard that survives):
        # a hand-over whose own label disagrees with its bytes is not signed. 2% or ten
        # seconds, whichever is larger -- container padding and rounded declarations are
        # noise, and a decode that lands outside it did not run to completion. A sidecar
        # that declares no length makes no claim, and no claim is never a mismatch.
        if expect_s is not None and abs(seconds - expect_s) > max(10.0, 0.02 * expect_s):
            _unlink(part)
            return {"ok": False, "reason": "length_mismatch",
                    "error": "length mismatch: decoded %.0f s, expected %.0f s"
                             % (seconds, expect_s)}

        os.replace(part, pcm)
        _STOP["part"] = None
        y = np.fromfile(pcm, dtype="float32")      # ONE allocation, at the final size
        if _stop_requested():
            return {"ok": False, "error": STOPPED}

        c = chroma_recipe.compute_chroma(y)        # THE recipe, in one place (chroma_recipe.py)
        del y
        if _stop_requested():                      # nothing half-decoded reaches the cache
            return {"ok": False, "error": STOPPED}

        # The signature is the harvester's product, so its write goes through the cache policy
        # like every other write into the `chroma` cache: `reserve` makes room (a planned size
        # -- the matrix is in hand) and refuses past the disk floor, `commit` records the entry
        # and runs the eviction at once if the cache went over its cap. A refusal means the
        # signature is NOT written and the file is `delayed` with `no_space` -- retried on a
        # later pass once an eviction run has made room, never retired.
        key = file_key(path)
        sig = os.path.join(_chroma_dir(), key + ".npy") if _chroma_dir() else None
        if sig is None:
            return {"ok": False, "reason": "no_space",
                    "error": "the signature cache is dark: NETRADIO_CACHE_ROOT is unset -- set "
                             "it in .env (see .env.example) and start again"}
        nbytes = int(c.size * np.dtype(chroma_recipe.STORE_DTYPE).itemsize)
        if not cache_budget.reserve(CHROMA_CACHE, nbytes):
            return {"ok": False, "reason": "no_space",
                    "error": "the cache policy refused room for the signature: the disk is "
                             "past its floor, or the chroma cache is over its cap with nothing "
                             "evictable"}
        os.makedirs(_chroma_dir(), exist_ok=True)
        # A .tmp name, written whole and renamed into place: the policy never evicts a fresh
        # .tmp, so a parent running an eviction while this child writes cannot delete the
        # half-written entry (np.save is handed a file handle so it cannot rename .tmp to .npy).
        tmp = "%s.%d.tmp" % (sig, os.getpid())
        with open(tmp, "wb") as fh:
            np.save(fh, c.astype(chroma_recipe.STORE_DTYPE))
        os.replace(tmp, sig)
        cache_budget.commit(CHROMA_CACHE, sig)
        if not os.path.isfile(sig):
            # THE LANDING CHECK. The rename and the commit are two calls, and a bounded cache
            # may lose any entry at any time -- an eviction run that starts between them can
            # take a signature that has not been recorded yet. What a writer must never do is
            # report a success whose entry is not there.
            return {"ok": False, "reason": "no_space",
                    "error": "the signature did not survive its own landing: an eviction run "
                             "took it before the policy recorded it"}
        # The bucket is the signature's long-term home (see sigstore). Upload now, verified; on
        # failure the local file stays and the row carries no etag -- a flaky upload costs disk
        # space, never data (sigstore's cold eviction refuses any key the bucket has not
        # verified), and the feeder's rule for a signed row with no etag is to feed the key
        # again.
        etag = None
        if sigstore.enabled():
            etag = sigstore.put(sig, key + ".npy")
        # The float32 chroma, for the parent's matcher. NOT the float16 round-trip: the matcher
        # scores float32 today, and a float16 cast and back moves values by an ULP, which is
        # enough to move a borderline verdict. Not a signature change either way -- the
        # signature is the file above.
        np.save(os.path.join(job, "chroma32.npy"), c)

        current, peak = memwatch.footprint_mb()
        return {"ok": True, "error": None, "reason": None, "uploaded_etag": etag,
                "n_samples": int(n_samples), "seconds": round(seconds, 1),
                "took_s": round(time.time() - started, 1),
                "footprint_mb": current, "peak_mb": peak, "footprint_kind": memwatch.kind()}
    finally:
        _STOP["part"] = None            # nothing left for the signal handler to clean up
        _STOP["procs"] = []


def _spawn_argv(job):
    """The decode child's command line. THE FILE PATH IS NOT ON IT.

    The supervisor finds a live harvester by looking for `--run` as a SUBSTRING of the whole
    `ps` command line. So anything on this argv that somebody else chose is a way for a
    process that lives for one file to be mistaken for the harvester. The audio path therefore
    travels in the job directory (`sign.json`) and the argv carries only the job path, whose
    last component is a key.
    """
    return [sys.executable, os.path.abspath(__file__), "--sign-job", job]


def _run_sign_child(path, job, expect_s=None):
    """Spawn `harvest.py --sign-job DIR` and read back its result."""
    env = dict(os.environ)
    # macOS libmalloc caches freed LARGE blocks inside the process instead of returning them to
    # the kernel, so the harvester's footprint never comes down between files and those dirty
    # pages end up compressed and swapped. Measured on this OS: 764 MB retained of 800 MB
    # allocated and freed; 0 MB with this set. (MallocSpaceEfficient=1 measured the same; this is
    # the one the memory eval standardised on.) libmalloc reads it at process START, so it has to
    # be on the child's environment -- setting it from inside a running process does nothing.
    env.setdefault("MallocLargeCache", "0")
    argv = _spawn_argv(job)
    # The invariant, checked rather than argued. `_spawn_argv` cannot put `--run` on the command
    # line, but the interpreter path and the repo path are not ours to promise, and a command line
    # carrying that substring anywhere would be adopted by the supervisor as the harvester
    # itself. So look, and run the decode here rather than spawn something that will be misread.
    if "--run" in " ".join(argv):
        return _decode_and_sign(path, job, expect_s)
    if _stop_requested():
        return {"ok": False, "error": STOPPED}   # do not start a decode we are about to abandon
    # THE JOB FILE DESCRIBES THE JOB -- all of it. The audio path moved here because the command
    # line is read by another process; the declared length follows it because they are two halves
    # of one fact ("decode this, and it is this long"), and a job half-described in one place and
    # half on an argv is how the two drift apart. Absent when the sidecar declared no length,
    # which is honest: an unmeasurable length is not a mismatch, it is simply unmeasured.
    _save(os.path.join(job, "sign.json"), {"path": path, "expect_s": expect_s})
    try:
        child = subprocess.Popen(argv, cwd=HOME, env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        return {"ok": False, "reason": "decode_failed",
                "error": "could not start the decode child: %s" % exc}
    # Same process group as us on purpose (no start_new_session), so the supervisor's killpg
    # reaches the parent, this child and ffmpeg together.
    _STOP["child"] = child
    try:
        if _stop_requested():
            # A signal that arrived between the spawn and the registration above found no child
            # to pass itself on to. Deliver it here, or a whole decode runs unwatched while this
            # process sits in communicate().
            _end(child)
        _out, err = child.communicate()
    finally:
        _STOP["child"] = None

    if child.returncode in (130, 143):          # 128 + SIGINT / SIGTERM
        # The child was signalled. Usually this process was too -- the supervisor signals the
        # whole group -- but our own handler may not have run yet, and a `kill` aimed at the
        # child alone would not raise the flag at all. Raise it here, so the naps, the loop
        # checks and the callers' guards all read one answer and none of them can file a
        # signalled decode as a failed one.
        _STOP["signum"] = _STOP["signum"] or (child.returncode - 128)
        return {"ok": False, "error": STOPPED}
    result = _load(os.path.join(job, "result.json"), None)
    if child.returncode != 0 or not isinstance(result, dict):
        tail = _last_line([err or b""])
        return {"ok": False, "reason": "decode_failed",
                "error": ("child failed (exit %s): %s" % (child.returncode, tail))[:160]}
    return result


# The last decode child's result.json. The memory rows (see `record_memory`) want the child's
# peak footprint, and sign_file's callers want to know how the decode went, so the result
# rides here rather than on the return value.
_LAST_CHILD = {}


# --- the ledger ---------------------------------------------------------------------------------

def load_ledger():
    """The ledger, `{key: row}`, or {} before the first seed. One row per key, written only
    by the harvester -- the shape and every field's meaning are the published contract
    (docs/HARVEST_FEED.md)."""
    ledger = _load(LEDGER, {})
    return ledger if isinstance(ledger, dict) else {}


def _sidecar_row_fields(sidecar):
    """The row fields the sidecar contributes. Carried, never read for meaning: a `url` on a
    row is for a third party's benefit and the join is always on the key."""
    return {"url": sidecar.get("url"), "title": sidecar.get("title"),
            "artist": sidecar.get("artist"), "duration_s": sidecar.get("duration_s")}


def _row(key, size, mtime, status, reason, signed_at, uploaded_etag, sidecar):
    return {"key": key, "size": size, "mtime": mtime, "status": status, "reason": reason,
            "signed_at": signed_at, "uploaded_etag": uploaded_etag,
            **_sidecar_row_fields(sidecar)}


def reconcile_ledger(state=None):
    """Seed the ledger at first start, and reconcile it against the bucket's listing on every
    writer start. This replaces the old lost-signature recovery: the `done` lists are gone,
    and the listing is the only record of what is signed.

      * the ledger is EMPTY -> seed one `signed` row per bucket key, `size` and `mtime`
        empty: the ledger is the complete record of the pool from its first day.
      * a `signed` row whose object is GONE -> loses its `uploaded_etag`, so the feeder feeds
        that key again.
      * a `signed` row missing its etag whose object is THERE -> gains it: a failed upload that
        a later run landed must stop the feeder re-feeding a key the bucket already holds.
      * the bucket cannot be LISTED -> nothing at all. "Unknown" is never "gone": with the
        listing dark, a bucket-held signature and a missing one are indistinguishable.
      * more than the cap of signed rows would lose their etags -> the STORE broke, not the
        rows. Report (a standing `state["sig_alert"]`) and touch nothing: a mass drop would
        put every key back on the feeder's list for days over a configuration fault, exactly
        the loss the old recovery's cap existed to prevent. The alert stands down on the
        first later reconcile that does not report: an alarm that outlives the healed store
        it was raised over is a page reporting a break that is gone.

    Mutates the caller's `state` when it keeps one (run does); loads its own otherwise.
    Returns {"seeded", "dropped", "restored", "reported", "cleared", "why"}.
    """
    res = {"seeded": 0, "dropped": 0, "restored": 0, "reported": False, "cleared": False}
    objects = _remote_objects()
    if objects is None:
        return dict(res, why="the bucket cannot be listed -- cannot tell a gone object from "
                             "an unlistable store, so the ledger was left untouched")
    if state is None:
        state = _load(STATE, blank_state())
    ledger = load_ledger()

    if not ledger:
        rows = {}
        for name, etag in objects.items():
            key = name[:-len(".npy")]
            rows[key] = _row(key, None, None, "signed", None, None, etag or None, {})
        _save(LEDGER, rows)
        return dict(res, seeded=len(rows),
                    why="seeded one signed row per bucket key, size and mtime empty -- the "
                        "ledger is the pool's complete record from its first day")

    signed = [k for k, r in ledger.items()
              if isinstance(r, dict) and r.get("status") == "signed"]
    gone = [k for k in signed if (k + ".npy") not in objects]
    cap = _reconcile_cap()
    if len(gone) > cap * max(1, len(signed)):
        why = ("%d of %d signed rows point at objects the listing does not hold (%.0f%% > the "
               "%.0f%% cap) -- NOT dropping their etags: a loss that size means the store "
               "broke, not the rows. Check the bucket endpoint/profile and the listing; if the "
               "loss is REAL, the deliberate override is: NETRADIO_RECONCILE_DROP_CAP=1 "
               ".venv/bin/python scripts/harvest.py --sign-one <key>"
               % (len(gone), len(signed), 100.0 * len(gone) / max(1, len(signed)), cap * 100))
        first = "sig_alert" not in state
        state["sig_alert"] = {"at": _now(), "kind": "store", "missing": len(gone),
                              "corpus": len(signed), "why": why}
        if first:
            state["issues"] = ((state.get("issues") or []) +
                               [{"at": _now(), "issue": "ledger: " + why}])[-50:]
        return dict(res, reported=True, why=why)

    for key, row in ledger.items():
        if not (isinstance(row, dict) and row.get("status") == "signed"):
            continue
        if (key + ".npy") in objects:
            if not row.get("uploaded_etag"):
                etag = objects[key + ".npy"]
                if etag:
                    row["uploaded_etag"] = etag
                    res["restored"] += 1
        elif row.get("uploaded_etag"):
            row["uploaded_etag"] = None
            res["dropped"] += 1
    if res["dropped"] or res["restored"]:
        _save(LEDGER, ledger)
    # ANY reconcile that did not report stands the STORE alert down: `gone` can be empty here
    # with the alert still standing (a mis-listed bucket, fixed between starts), and a clear
    # that waits for a later loss would report a healed store as broken forever. Only a
    # store-kind alert is cleared here; a canary-kind alert (a matcher failure) is a different
    # break and survives a healthy reconcile -- the canary's own pass is what clears it (see
    # score_canary). A legacy alert written before the `kind` field landed has no `kind` but
    # carries the store-loss shape (`missing`/`corpus`); it is treated as store-owned here so
    # an upgrade does not leave a pre-existing store alarm standing forever, while the
    # canary path (which never clears a store alert) leaves it alone either way.
    if not res["reported"]:
        alert = state.get("sig_alert")
        if isinstance(alert, dict) and (alert.get("kind") == "store"
                                        or ("kind" not in alert
                                            and "missing" in alert)):
            state.pop("sig_alert", None)
            res["cleared"] = True             # the store healed -- stand down
    why = ("dropped the etag of %d signed row(s) whose object is gone; restored %d missing "
           "etag(s) whose object is back; every other signed row still points at its object"
           % (res["dropped"], res["restored"])) if (res["dropped"] or res["restored"]) else \
          "every signed row still points at an object the bucket holds"
    return dict(res, why=why)


# Above this fraction of the signed corpus, reconcile_ledger refuses to act on its own.
# A few gone objects is routine wear and the drop is exactly what the feeder needs; a loss
# bigger than this means the STORE broke -- a wrong endpoint, a wrong profile -- and dropping
# thousands of etags would put the whole pool back on the feeder's list while destroying the
# evidence of what went wrong. So past the cap it REPORTS and stands still; a human raises
# NETRADIO_RECONCILE_DROP_CAP deliberately (e.g. =1) if the loss turns out to be real.
RECONCILE_DROP_CAP = 0.10


def _reconcile_cap():
    try:
        return float(os.environ.get("NETRADIO_RECONCILE_DROP_CAP", "") or RECONCILE_DROP_CAP)
    except ValueError:
        return RECONCILE_DROP_CAP


# --- the scan -----------------------------------------------------------------------------------

def _dirs(issues=None, said=None):
    """The configured directories, in the order they are named, empties dropped.

    ABSOLUTE paths only. A relative entry would resolve against whatever directory the
    process happens to start from -- a hand-off that signs a different directory than the
    one the configuration names -- so it is refused with an issue row, not silently
    resolved. (`issues` collects the refusals, deduped per run through `said`, exactly like
    the scan's own.)"""
    out = []
    for p in HARVEST_DIRS.split(":"):
        p = os.path.expanduser(p.strip())
        if not p:
            continue
        if not os.path.isabs(p):
            if issues is not None and (said is None or p not in said):
                if said is not None:
                    said.add(p)
                issues.append({"at": _now(), "dir": p,
                               "issue": "NETRADIO_HARVEST_DIRS names %r, which is not an "
                                        "absolute path -- skipped; the contract's paths "
                                        "are absolute" % p})
            continue
        out.append(p)
    return out


def scan_directories(ledger, issues=None, said=None):
    """The files that want a sign, oldest first. Returns `(todo, covered)`.

    `todo` is a list of `{"key", "path"}` records, oldest first, one per key (a key that
    sits in more than one configured directory is proposed once, for its oldest copy -- a
    row covers only one file's bytes); `covered` counts the keys whose row already matches
    their file -- the scan skips them, and the caller counts each once per run
    (`state["skipped_cached"]`).

    THE COMPLETENESS RULE: a file with no sidecar beside it is not finished, and is neither
    read nor logged. The sidecar is how the harvester knows the feeder is done with the file;
    a torn or unreadable one is a transient state, not a refusal, and is simply retried next
    pass. Two things ARE refused, loudly, because a feeder must hear about its own bugs: a
    sidecar whose `key` differs from the file's stem, and a stem that is not a key's shape
    (`u` + 20 hex). Both land in `issues` (deduped per run through `said`) -- a silent refusal
    of a file the feeder thinks it delivered is a wall the feeder cannot see.

    A name ending in `.part` is skipped without a row, whatever its stem: it is a download
    still in progress, feeder state rather than a feeder bug.

    The TOP LEVEL only: a subdirectory is never read, whatever it holds.
    """
    todo, covered = [], []
    said = said if said is not None else set()
    for d in _dirs(issues=issues, said=said):
        if not os.path.isdir(d):
            if d not in said and issues is not None:
                said.add(d)
                issues.append({"at": _now(), "dir": d,
                               "issue": "NETRADIO_HARVEST_DIRS names %s, which is not there -- "
                                        "skipped until it exists (a directory created after "
                                        "this row was written is picked up on the next pass)"})
            continue
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for name in names:
            path = os.path.join(d, name)
            if not os.path.isfile(path):
                continue                     # never a subdirectory, however tempting
            if name.endswith(".json"):
                continue                     # a sidecar is not audio
            if name.endswith(".part"):
                continue                     # a download in progress: feeder state, not a bug
            stem = name.rsplit(".", 1)[0]
            if not _KEY.fullmatch(stem):
                if path not in said and issues is not None:
                    said.add(path)
                    issues.append({"at": _now(), "key": stem,
                                   "issue": "refused %s: the file's stem is not a key "
                                            "(u + 20 hex) -- a signature filed under it would "
                                            "be invisible to the pool's own listing" % name})
                continue
            sidecar_path = os.path.join(d, stem + ".json")
            if not os.path.isfile(sidecar_path):
                continue                     # incomplete: not read, not signed, not logged
            sidecar = _load(sidecar_path, None)
            if not isinstance(sidecar, dict):
                continue                     # torn or mid-write: retried next pass
            if sidecar.get("key") != stem:
                if path not in said and issues is not None:
                    said.add(path)
                    issues.append({"at": _now(), "key": stem,
                                   "issue": "refused %s: its sidecar's key (%r) differs from "
                                            "the file's stem -- a feeder bug, not a verdict on "
                                            "the audio" % (name, sidecar.get("key"))})
                continue
            if not sidecar.get("fed_at"):
                # `fed_at` is required of the feeder (docs/HARVEST_FEED.md) -- the record of
                # when the hand-over was made, which the backfill that synthesises sidecars
                # for the old pool writes too. Refused loudly rather than silently skipped: a
                # feeder that forgot one line of the contract must hear about it, not watch
                # its file sit unsigned with no row and no reason.
                if path not in said and issues is not None:
                    said.add(path)
                    issues.append({"at": _now(), "key": stem,
                                   "issue": "refused %s: its sidecar carries no fed_at -- the "
                                            "one required field it is missing" % name})
                continue
            try:
                st = os.stat(path)
            except OSError:
                continue                     # vanished between listdir and now
            row = ledger.get(stem)
            if (row is not None and row.get("size") == st.st_size
                    and row.get("mtime") == st.st_mtime
                    and not (row.get("status") == "delayed"
                             and row.get("reason") == "no_space")):
                if stem not in covered:
                    covered.append(stem)      # the ledger already covers these exact bytes
                continue
            todo.append({"key": stem, "path": path})
    todo.sort(key=lambda rec: os.path.getmtime(rec["path"]) if os.path.exists(rec["path"])
              else 0)
    # ONE CANDIDATE PER KEY, the oldest copy of it: the same key can sit in more than one
    # configured directory, and a row covers only one file's bytes -- two candidates for one
    # key in a single pass is a ping-pong in the making (whichever copy is signed, the other
    # is wanted again the next pass, and back). The feeder that leaves two differing copies
    # under one key must take one of them away; the scan proposes one at a time, oldest first.
    seen, one_per_key = set(), []
    for rec in todo:
        if rec["key"] in seen:
            continue
        seen.add(rec["key"])
        one_per_key.append(rec)
    return one_per_key, covered


# --- signing ------------------------------------------------------------------------------------

def sign_file(path, issues=None):
    """Sign one audio file: decode it (in the child), upload the signature and the sidecar
    beside it in the bucket, and write the ledger row. Returns `(chroma, samples)` when the
    file is signed -- the chroma to score, the decoded samples as a memory map so the caller
    can cut an excerpt without a second decode -- and `(None, None)` otherwise.

    THE ROW IS THE VERDICT. A file that decodes badly, or whose length disagrees with its own
    sidecar, or that the cache policy has no room for, is `delayed` with its reason, and the
    row is written all the same: the feeder reads the reason and decides what to do with the
    file. Three things write NO row, on purpose:

      * a STOP, ours or the child's -- a stop is never a verdict, and the file is signed on a
        later pass;
      * a file that VANISHED while it was being signed -- the row's reason vocabulary is a
        verdict on the bytes as fed, and bytes that left mid-decode are not the feeder's
        fault; no row leaves the key on the feeder's list, which is exactly where an evicted
        file must go;
      * a file whose sidecar is unreadable at sign time -- it was complete when the scan saw
        it and is not now, which is the mid-write state the completeness rule already covers.

    THE LENGTH CLAIM IS THE SIDECAR AS IT READS NOW, never the scan's copy. A re-offer
    replaces a file under a key that already has a sidecar (docs/HARVEST_FEED.md), and the
    scan can list the directory inside that window -- new bytes beside the OLD sidecar,
    whose `key` matches and whose `fed_at` is present, so the file reads as finished.
    Weighed against the old claim, the new bytes take a `length_mismatch` on a verdict they
    never earned -- and the row, carrying the new bytes' own size and mtime, would cover
    the file for good. The claim this sign weighs is the one beside the file when the sign
    starts; the contract's half of the same rule is to take the old sidecar away before the
    new audio lands.
    """
    _LAST_CHILD.clear()
    key = file_key(path)
    sidecar_path = os.path.join(os.path.dirname(path), key + ".json")
    sidecar = _load(sidecar_path, None)
    if not isinstance(sidecar, dict) or sidecar.get("key") != key:
        return None, None                    # torn or mismatched: the scan's refusals cover it
    if not sidecar.get("fed_at"):
        # The one required field, missing at the last moment: the same refusal the scan
        # makes, visible from this path too -- the hand tool must not tell the operator to
        # consult an issues list it never wrote to.
        if issues is not None:
            issues.append({"at": _now(), "key": key,
                           "issue": "refused %s: its sidecar carries no fed_at -- the one "
                                    "required field it is missing"
                                    % os.path.basename(path)})
        return None, None
    # Not the scan's copy: the sidecar as it reads now, validated by `_expect_s` (a claim that
    # is not a length makes no claim, and no claim is never a mismatch) -- the docstring's
    # last paragraph is the why.
    expect_s = _expect_s(sidecar)
    try:
        st = os.stat(path)
    except OSError:
        return None, None                   # gone before the decode began: no row
    job = job_dir(key)
    shutil.rmtree(job, ignore_errors=True)          # a stale job dir for this key is not ours
    os.makedirs(job, exist_ok=True)
    try:
        if os.environ.get("NETRADIO_HARVEST_CHILD") == "0":
            result = _decode_and_sign(path, job, expect_s)
        else:
            result = _run_sign_child(path, job, expect_s)
        _LAST_CHILD.update(result)
        if _stop_requested() or was_stopped(result.get("error")):
            return None, None

        # A FAILURE ON A FILE THAT IS NO LONGER THERE IS NOT A VERDICT ON THE FILE. The cache
        # policy, not the harvester, owns the directories' space, and a file it took away
        # mid-sign must go back on the feeder's list -- a `decode_failed` row would be a final
        # verdict on bytes nobody can re-feed.
        if not result.get("ok") and not os.path.exists(path):
            if issues is not None and path not in _said_vanished:
                _said_vanished.add(path)
                issues.append({"at": _now(), "key": key,
                               "issue": "%s left while it was being signed -- no row, so it "
                                        "stays on the feeder's list and is signed again when "
                                        "it comes back" % os.path.basename(path)})
            return None, None

        ledger = load_ledger()
        if result.get("ok"):
            etag = result.get("uploaded_etag")
            # THE SIDECAR BESIDE THE SIGNATURE, the index the pool has never had
            # (docs/HARVEST_FEED.md). With the store on, BOTH objects must land before the
            # row is written: a `signed` row is the promise that `<key>.npy` and `<key>.json`
            # are in the bucket, and the scan treats the row as covering the file -- so a
            # half-landed sign recorded as signed would never be retried, and the pool would
            # hold a signature with no sidecar beside it for good. A failed upload of either
            # writes NO row: the file is still wanted, the next pass signs it again (one
            # re-decode per retry -- the cost of a bucket blip, never a hole in the index),
            # and the issue row names what failed.
            if sigstore.enabled() and (etag is None or
                                       not sigstore.put(sidecar_path, key + ".json")):
                what = "the signature" if etag is None else "the sidecar beside the signature"
                if issues is not None:
                    issues.append({"at": _now(), "key": key,
                                   "issue": "%s did not upload -- no row, so the file is "
                                            "signed again on a later pass" % what})
                return None, None
            ledger[key] = _row(key, st.st_size, st.st_mtime, "signed", None, _now(),
                               etag, sidecar)
            _save(LEDGER, ledger)
            c = np.load(os.path.join(job, "chroma32.npy"))
            samples = np.memmap(os.path.join(job, "pcm.f32le"), dtype="float32", mode="r")
            return c, samples
        reason = result.get("reason") or "decode_failed"
        ledger[key] = _row(key, st.st_size, st.st_mtime, "delayed", reason, None, None,
                           sidecar)
        _save(LEDGER, ledger)
        return None, None
    finally:
        # POSIX keeps an unlinked file alive for as long as something has it open or mapped, so
        # the memmap above stays readable and the space is reclaimed when `samples` is dropped.
        shutil.rmtree(job, ignore_errors=True)


# Per-run memory for sign_file's vanished-file rows: the issue is news once per file, not
# once per pass. (A module-level set rather than a caller's `said`, because --sign-one and run
# are the same writer and the dedupe belongs to the process.)
_said_vanished = set()


def find_file(key):
    """The file for `key` in the configured directories: `<key>.<ext>` with a complete
    sidecar beside it, or None. The hand tool's finder -- the scan answers "what wants a
    sign", while --sign-one answers "where is this key's file", which a row that already
    covers it must not hide."""
    for d in _dirs():
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for name in names:
            if name.endswith(".json") or name.rsplit(".", 1)[0] != key:
                continue
            path = os.path.join(d, name)
            sidecar = os.path.join(d, key + ".json")
            if not (os.path.isfile(path) and os.path.isfile(sidecar)):
                continue
            data = _load(sidecar, None)
            if (isinstance(data, dict) and data.get("key") == key
                    and data.get("fed_at")):
                return path
    return None


# A short excerpt AROUND the matched instant is all we retain -- long enough to recognise the
# record by ear, far too short to be a copy of it. This is not a library; it is a magnifying
# glass held over the exact moment the matcher flagged, so a human can listen and confirm or
# reject it.
EXCERPT_S = 30.0


def write_excerpt(samples, at_s, path):
    """Write ~EXCERPT_S seconds of `samples` centred on the matched instant. In memory in, file
    out -- no second decode. A brief excerpt for aural verification, swept after KEEP_TTL_DAYS.

    Returns True when the excerpt is on disk, False when it is not: nothing to write, or the
    cache policy refusing the write (the disk past its floor, the board's cap with nothing
    evictable). A refused excerpt is simply not kept -- the LEAD survives with its numbers, and
    the caller must not count a refusal as kept or name the path as the lead's audio."""
    import soundfile as sf
    lo = max(0, int((at_s - EXCERPT_S / 2) * _audio.SR))
    hi = min(len(samples), lo + int(EXCERPT_S * _audio.SR))
    clip = np.asarray(samples[lo:hi], dtype="float32")

    # THE HARD CAP. Everything about this project's copyright posture rests on one claim: what we
    # retain is an excerpt, "far too short to be a copy". That claim must be enforced by the code,
    # not merely intended by it -- because it has already failed once. 2.1 GB of FULL-LENGTH audio
    # was found in the candidates directory (one file was a 108-minute DJ mix, retained whole),
    # written by a harvester whose in-memory code did not match what is in git. The slice above is
    # correct today; this is here so that a slice that is ever wrong again cannot reach the disk.
    cap = int(EXCERPT_S * _audio.SR)
    if len(clip) > cap:
        clip = clip[:cap]
    if len(clip) == 0:
        return False                            # nothing to hear; do not leave an empty file

    if not cache_budget.reserve(CANDIDATES_CACHE, len(clip) * 2 + 64):
        # A planned size: 16-bit PCM, two bytes a sample. The policy makes room by score (the
        # worst excerpt of a mystery first) and refuses past the disk floor.
        return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # A .tmp name, written whole and renamed into place, so no eviction this process or another
    # runs can delete the half-written entry (the policy holds a fresh .tmp for an hour).
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "wb") as fh:
        sf.write(fh, clip, _audio.SR, format="WAV")
    os.replace(tmp, path)
    cache_budget.commit(CANDIDATES_CACHE, path)
    if not os.path.isfile(path):
        # THE LANDING CHECK (see the signature write for the reason): an eviction run that
        # starts between the rename and the commit can take an excerpt not yet recorded.
        # Not kept, not counted -- the lead survives its numbers.
        return False
    _write_provenance()
    return True


def purge_audio():
    """Throw away every retained excerpt. The LEADS survive.

    Retained audio turned out to be the weakest part of this design. It is the only thing here that
    is a copy of someone's record, it is the thing that went wrong (full-length mixes were retained
    instead of excerpts), and it is not actually needed: a lead is a URL, and a URL can be listened
    to at the source. So the audio goes, and the harvest page plays the candidate from an embed instead.

    What survives is everything that took work to compute -- the url, the cost, the mystery it
    matched, the key it matched in, and WHERE in the candidate it matched. Nothing is re-fetched and
    nothing is re-analysed; the chroma signatures (which are not audio) are untouched, so no
    candidate will ever be downloaded twice.
    """
    freed = n = 0
    keep = _keep_dir()
    if keep and os.path.isdir(keep):
        # Through the policy's one door, like every deletion of a cache entry: each removal is
        # checked against the cache's directory and recorded with its reason. While the policy
        # is dark there is no registered directory and nothing to delete -- the state half
        # below still runs.
        for name in os.listdir(keep):
            if not name.lower().endswith((".wav", ".mp3", ".flac", ".m4a")):
                continue                      # leave PROVENANCE.txt alone
            path = os.path.join(keep, name)
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            if cache_budget.remove(CANDIDATES_CACHE, path, "purge-audio"):
                freed += size
                n += 1

    state = _load(STATE, blank_state())
    for m in state.get("matches") or []:
        m.pop("audio", None)                  # the lead stays; the copy does not
    state["kept"] = 0
    _save(STATE, state)
    return "purged %d retained files (%.1f GB). %d leads kept -- review them by embed on the " \
           "harvest page." % (n, freed / 1e9, len(state.get("matches") or []))


def _sig_key(url):
    """The signature file name a URL used to be written under: `<key>.npy`.

    The migration of the old `matches` rows is this function's last user. Everything else
    reads the key a file or a row already carries and computes nothing (see file_key) --
    the URL-to-key rule itself belongs to whoever feeds the directories, and the one-time
    move of the old rows onto keys is the one place this side still has to apply it.
    """
    return "u" + hashlib.sha1(url.encode()).hexdigest()[:20] + ".npy"


def migrate_matches(state):
    """Move every old match row from its `url` to the `key` the row now joins on.

    Rows written before the ledger existed carry `url` and no `key`, and every reader joins
    on the key. The move runs at every writer start and is idempotent: a row that already
    carries a key is left alone, and a row with neither field (corrupt, or older than the
    field) is left alone too -- the readers skip it, as they always skipped a row with no
    url. Returns how many rows moved.
    """
    moved = 0
    for m in state.get("matches") or []:
        if not isinstance(m, dict) or "key" in m:
            continue
        url = m.pop("url", None)
        if isinstance(url, str) and url:
            m["key"] = _sig_key(url)[:-len(".npy")]
            moved += 1
    return moved


# THE BUCKET's object listing, {name: etag}, cached for a while -- None when the store is dark
# or the listing failed (callers must not treat that as 'empty').
_REMOTE_OBJECTS = {"at": 0.0, "objects": None}


def _remote_objects(max_age_s=900):
    if not sigstore.enabled():
        return None
    now = time.time()
    if _REMOTE_OBJECTS["objects"] is not None and now - _REMOTE_OBJECTS["at"] < max_age_s:
        return _REMOTE_OBJECTS["objects"]
    objects = sigstore.list_objects()
    if objects is not None:
        _REMOTE_OBJECTS.update(at=now, objects=objects)
    return _REMOTE_OBJECTS["objects"]


def stamp_pool(state):
    """Record the BUCKET's signature count on the state, for the page. Post-migration the
    bucket is the pool's only home, so counting local `.npy` undercounts by thousands (the
    working cache holds work-in-progress, ~1 file). Rides `_remote_objects()`'s ≤15-min
    session cache -- no extra bucket listing, and a reader without the bucket's credentials
    never needs one; when the store is dark or the listing failed, the previous stamp (with
    its honest `at`) is left standing. Returns True when the stamp changed, so a caller with
    no other reason to save knows this one is worth persisting.

    Also stamps the count's CANARY: the self-test's known record, uploaded like any other but
    never a candidate. The canary is named by its key (`NETRADIO_CANARY_KEY`), so membership
    is a dictionary lookup, not a fetch. The `active`/`retired` breakdown the stamp once
    carried came from a read this side no longer makes: every such figure is the rulings
    writer's own join of the ledger with its queue and the rulings file."""
    objects = _remote_objects()
    if objects is None:
        return False
    prev = state.get("pool") or {}
    canary_key = (os.environ.get("NETRADIO_CANARY_KEY") or "").strip()
    canary = 1 if canary_key and (canary_key + ".npy") in objects else 0
    pool = {"count": len(objects), "at": _now(), "canary": canary}
    state["pool"] = pool
    return any(pool.get(k) != prev.get(k) for k in ("count", "canary"))


def _canary_key():
    """The canary's key, set in .env (see .env.example). Empty when unconfigured -- the
    self-test then reports "not configured", as it does today when the searched hit is
    refused, and the run carries on."""
    return (os.environ.get("NETRADIO_CANARY_KEY") or "").strip()


def score_canary(state, qs):
    """Re-score the canary's STORED signature every pass and raise `sig_alert` on failure.

    The canary is one known track whose file arrived through the feeder like any entry, was
    signed once, and whose signature lives in the bucket keyed by `NETRADIO_CANARY_KEY`. A
    broken harvester and a pool without the answer look identical from here -- zero matches,
    for weeks -- so before the search reports another "no match" it proves it can still find
    a record it KNOWS it holds: pull the canary's signature back (the working cache first, the
    bucket if it is not local) and score it against the canary's mix and the current
    mysteries, demanding the known cost, rank and margin. A re-score, never a re-sign: the
    canary needs no file and no fetch.

    Returns True when the state changed (worth a save). `sig_alert` is the one alarm the page
    shows for "the store or the matcher is broken", and the two paths that raise it -- the
    ledger's reconcile (a mass bucket loss) and the canary (a matcher failure) -- are KEPT
    APART by a `kind` field on the alert. The canary stands up a `kind: "canary"` alert on a
    hard failure and stands THAT alert down on a pass; it never touches a store-loss alert
    (`kind` absent or `"store"`), so a canary already in the local cache cannot hide a mass
    bucket loss -- only the reconcile that raised a store alert clears it, the same way a
    healed store clears its own alert. A "not configured" or "not available" result (the key
    unset, the signature missing) is NOT a failure and touches no alert: the run carries on,
    the same way it does today when the searched hit is refused.
    """
    key = _canary_key()
    if not key:
        # The "not configured" state: the self-test reports it, no alert, no save needed.
        # The run carries on -- signing is worth doing whatever the canary looks like.
        return False
    c_canary = _load_sig(key)
    st = selftest.live(c_canary, mystery_queries=qs)
    changed = False
    if st.get("ok") is True:
        # A pass stands the CANARY alert down (the matcher is working), and ONLY that alert:
        # a store-loss alert the ledger's reconcile raised is a different break, and a
        # canary already in the local cache passing while the bucket is still reporting a
        # mass loss must not hide it. The reconcile that raised a store alert is the one
        # that clears it, the same way a healed store clears its own.
        alert = state.get("sig_alert")
        if isinstance(alert, dict) and alert.get("kind") == "canary":
            state.pop("sig_alert", None)
            changed = True
    elif st.get("ok") is False:
        why = "self-test failed: %s" % st.get("why")
        alert = state.get("sig_alert")
        # A STORE alert (a mass bucket loss) takes precedence: a broken store is the more
        # fundamental break -- the pool's contents cannot be trusted -- so the canary does
        # not clobber it with its own. A canary alert already standing with the same why is
        # not rewritten either (no fresh issues row every pass). Only when no alert is
        # standing, or a canary alert with a DIFFERENT why is, does the canary record its
        # failure.
        if isinstance(alert, dict) and alert.get("kind") == "store":
            pass                         # the store break is the news; leave it
        elif isinstance(alert, dict) and alert.get("kind") == "canary" \
                and alert.get("why") == why:
            pass                         # already recorded; no duplicate issues row
        else:
            state["sig_alert"] = {"at": _now(), "kind": "canary", "why": why}
            state["issues"] = ((state.get("issues") or []) +
                               [{"at": _now(), "issue": why}])[-50:]
            changed = True
    return changed


def _load_sig(key):
    """A signature by hook or by crook: the working cache first, then the bucket. None if it
    exists in neither (i.e. this key genuinely needs its audio decoded). While the signature
    cache is dark there is no local signature and nowhere to land a pulled one, so None."""
    d = _chroma_dir()
    if d is None:
        return None
    path = os.path.join(d, key + ".npy")
    if not os.path.exists(path) and sigstore.enabled():
        # A bucket pull is a write into the chroma cache: through the policy, so a refusal
        # (past the floor) leaves the pair for a later pass instead of filling the disk.
        if not cache_budget.reserve(CHROMA_CACHE, None):
            return None
        if not sigstore.fetch(key + ".npy", d):
            return None
        cache_budget.commit(CHROMA_CACHE, path)
    try:
        return np.load(path).astype("float32")
    except (OSError, ValueError):
        return None


def unscored_pairs(state, ledger, ruled, qs, limit=None):
    """Every (mystery, held-signature) pair we have NOT scored yet.

    The corpus is the LEDGER's signed rows -- the pool as the harvester's own record holds
    it, which since the first-start seed is every key the bucket has ever held, not only the
    ones this machine has decoded. A chroma signature is not tied to the question you asked
    of it: the same 12xN matrix answers MT4 today and MT8 next month, for free and with no
    network. So the pairing is what we track -- `state["scored"][mystery] = [signature keys]`
    -- and anything unpaired is work to do.

    Skips anything ruled on (a key from the rulings file): a `not_a_match` is not a match for
    ANYTHING we are waiting for. A row whose signature is held nowhere (an evicted cache and
    a bucket that no longer has it) is skipped too -- there is nothing to score until the
    feeder feeds that key again.
    """
    scored = state.setdefault("scored", {})
    out = []
    for num, qc, qkey in qs:
        seen = set(scored.get(qkey, []))
        for key, row in ledger.items():
            if not (isinstance(row, dict) and row.get("status") == "signed"):
                continue                     # a delayed row holds no signature to score
            if key in ruled:
                continue
            if key + ".npy" in seen:
                continue
            # A signature counts as HELD if it is in the working cache OR the bucket -- eviction
            # (sigstore) moves cold ones out of the cache, and _load_sig pulls them back to score.
            # A dark cache holds nothing at all, so only the bucket can answer.
            path = os.path.join(_chroma_dir(), key + ".npy") if _chroma_dir() else None
            if path is None or not os.path.exists(path):
                remote = _remote_objects()
                if remote is None or (key + ".npy") not in remote:
                    continue
            out.append((num, qc, qkey, key))
            if limit and len(out) >= limit:
                return out
    return out


def score_cached(state, num, qc, qkey, key):
    """Score one held signature against one mystery. No network, ~0.06s. Returns a hit or None.

    Updates an existing row rather than duplicating it -- which is also how the missing `at_s`
    gets filled in. Every match saved before the harvester recorded WHERE it hit has `at_s: None`,
    so the page could not cue the link and the position had to be hunted by hand. The position
    was never lost: it is recomputable from the signature we already hold.

    A match row carries `key`, never `url`: the row's join to whatever owns the candidate is
    the key alone, and the readers compute the same key from their own side.

    Advances the `current_query` block when it scores the mystery the block names -- the
    page's "what the scorer is working on right now" (§5.6): one more compared, one fewer
    remaining, an `updated` stamp. A score against a different mystery (the sign step scores
    a freshly signed file against every mystery inline, not through here) leaves the block
    alone -- the block tracks the rescan, not the sign step.
    """
    c = _load_sig(key)
    if c is None:
        return None
    cost, shift, at = _cm.match(qc, c)
    state.setdefault("scored", {}).setdefault(qkey, []).append(key + ".npy")

    # The `current_query` block: the scorer keeps it while it works one mystery. One more
    # compared, one fewer remaining, an `updated` stamp -- but only when this score is the
    # one the block names (a rescan works one mystery at a time; the sign step's inline
    # scores are a different mystery and do not move the block).
    cq = state.get("current_query")
    if (isinstance(cq, dict) and cq.get("mystery") == num
            and cq.get("query_key") == qkey):
        cq["compared"] = int(cq.get("compared") or 0) + 1
        cq["remaining"] = max(0, int(cq.get("remaining") or 0) - 1)
        cq["updated"] = _now()

    if cost is None or cost > KEEP_CEILING:
        return None
    verdict = "MATCH" if cost <= MATCH_COST else "near"
    for m in state["matches"]:
        if m.get("key") == key and m.get("mystery") == num:
            m.update(cost=round(float(cost), 4), semitones=shift,
                     at_s=round(float(at or 0), 1), verdict=verdict)
            return m
    hit = {"at": _now(), "mystery": num, "cost": round(float(cost), 4),
           "semitones": shift, "at_s": round(float(at or 0), 1), "key": key,
           "verdict": verdict}
    state["matches"].append(hit)
    evict_overfull(state, num)
    return hit


def forget(state, num):
    """Drop everything we have learned about one mystery: its leads, and its scored pairings.

    For when the QUESTION was bad, not the answers. MT7's clip is 23 seconds; every lead it
    produced is suspect, and leaving them on the board invites a human to rule on evidence gathered
    with a broken instrument. Clearing the scored pairings means the next clip starts from a clean
    slate against the whole corpus.

    (Re-cutting the clip alone would also force a full re-score -- `scored` is keyed on the clip's
    fingerprint -- but the stale LEADS would survive, and they are the part that misleads you.)
    """
    before = len(state.get("matches") or [])
    state["matches"] = [m for m in state.get("matches") or [] if m.get("mystery") != num]
    scored = state.setdefault("scored", {})
    dropped_keys = [k for k in scored if k.split(":", 1)[0] == str(num)]
    for k in dropped_keys:
        del scored[k]
    return before - len(state["matches"]), len(dropped_keys)


def rescan(state, ledger, ruled, qs, limit=None, verbose=True):
    """Work through the unscored pairs. Returns how many were scored.

    Advances the `current_query` block (§5.6) as it works one mystery: `mystery`, `query_key`,
    `started` (when this mystery's chunk began), `compared`, `remaining`, `updated`. A rescan
    works the mysteries in the order `qs` carries them, and a chunk per pass
    (`RESCAN_PER_PASS`) may finish a mystery and start the next within one call -- so the
    block is reset the moment the mystery changes, and `remaining` is the count left in this
    rescan batch for the mystery the block names.
    """
    pairs = unscored_pairs(state, ledger, ruled, qs, limit=limit)
    # Count the pairs per mystery in THIS batch, so `remaining` starts at the batch's size
    # for a mystery and counts down to zero as the block advances.
    remaining_in_batch = {}
    for num, _qc, qkey, _key in pairs:
        remaining_in_batch[(num, qkey)] = remaining_in_batch.get((num, qkey), 0) + 1
    for num, qc, qkey, key in pairs:
        cq = state.get("current_query")
        if not (isinstance(cq, dict) and cq.get("mystery") == num
                and cq.get("query_key") == qkey):
            # A new mystery, or the block is stale (a re-cut clip changed the key): start the
            # block fresh. `started` is when THIS mystery's chunk began, `remaining` is how
            # many pairs this batch holds for it, and `compared` begins at zero.
            state["current_query"] = {"mystery": num, "query_key": qkey,
                                      "started": _now(), "compared": 0,
                                      "remaining": remaining_in_batch.get((num, qkey), 0),
                                      "updated": _now()}
        hit = score_cached(state, num, qc, qkey, key)
        if hit and verbose:
            a = int(hit.get("at_s") or 0)
            print("  %s  MT%d  cost %.4f  at %d:%02d  %s  (from a held signature -- no decode)"
                  % (hit["verdict"], num, hit["cost"], a // 60, a % 60, key))
    return len(pairs)


def evict_overfull(state, num):
    """Trim mystery `num`'s board back to KEEP_TOP, keeping the best (lowest cost).

    A row's "audio" key is OPTIONAL, and its absence is the normal case: `purge_audio()` above pops
    it from every match, and a lead is a URL -- not a copy of a record. So an evicted row with no
    file is evicted quietly, and only a row that still points at one decrements `kept`.

    This must never raise. It runs on the hot path of every hit, and it used to do
    `os.unlink(dead["audio"])` catching only OSError -- so once the audio was purged, the first
    match in any full board raised KeyError and killed the harvester. Every board was already at
    KEEP_TOP, so that was every start, forever, and the watchdog restarted it into the same wall.
    """
    board = sorted([m for m in state["matches"] if m["mystery"] == num],
                   key=lambda m: m["cost"])
    for dead in board[KEEP_TOP:]:
        state["matches"].remove(dead)
        path = dead.get("audio")
        if not path:
            continue
        # Through the policy's one door: the removal is recorded with its reason, refuses a
        # path outside the cache, and -- while the policy is dark -- deletes nothing (a refused
        # `kept` count is honest either way: the row is gone, and the file stays where it is).
        if cache_budget.remove(CANDIDATES_CACHE, path, "board-overfull"):
            state["kept"] -= 1


def _write_provenance():
    """State, in plain words, what the kept files are -- so nobody, including a future me, ever
    mistakes this directory for a music library."""
    keep = _keep_dir()
    if not keep:
        return                                 # the cache is dark: no directory, no note
    note = os.path.join(keep, "PROVENANCE.txt")
    if os.path.exists(note):
        return
    os.makedirs(keep, exist_ok=True)
    with open(note, "w", encoding="utf-8") as fh:
        fh.write(
            "These are SHORT EXCERPTS (~%ds), retained TEMPORARILY so a human can listen and "
            "confirm or reject a track-identification hypothesis.\n\n"
            "This is not a music library. Full tracks are never kept -- the harvester decodes "
            "audio, reduces it to a chroma signature, and drops it. Only the matched ~%d-second "
            "window of a near-miss is written here, and it is swept after %d days.\n\n"
            "If an excerpt confirms a record, ACQUIRE THE RECORD (buy it, or rip your own copy). "
            "Do not promote an excerpt into a source file.\n"
            % (int(EXCERPT_S), int(EXCERPT_S), KEEP_TTL_DAYS))


def sweep_excerpts():
    """Delete kept excerpts older than KEEP_TTL_DAYS -- the cache's own age limit, applied by
    the harvester's own backstop sweep (the board's age is registered on the policy too, so
    any eviction run over `candidates` reaches the same files). A lead you haven't listened to
    in a month is not a lead, and holding it any longer serves no purpose. Each deletion goes
    through the policy's one door, so it is recorded with its reason; while the policy is dark
    there is no registered directory and nothing is swept."""
    keep = _keep_dir()
    if not keep or not os.path.isdir(keep):
        return
    cutoff = time.time() - KEEP_TTL_DAYS * 86400
    for name in os.listdir(keep):
        if not name.endswith(".wav"):
            continue
        path = os.path.join(keep, name)
        try:
            if os.path.getmtime(path) < cutoff:
                cache_budget.remove(CANDIDATES_CACHE, path, "expired")
        except OSError:
            pass


def drop_ruled_excerpts(state, ruled):
    """A ruled-on lead loses its audio; the numbers stay. Returns how many were dropped.

    The excerpt exists for exactly one purpose: to let a human confirm or reject the lead by ear.
    Once the ruling is made -- match, not-a-match, heard, any key in the rulings file -- that
    purpose is spent, and holding the audio a day longer serves nothing. The lead itself survives
    whole (key, cost, mystery, at_s, verdict): the SCORE is the record; the audio was only
    ever the evidence.

    Runs on every pass, right after the rulings file is re-read, so a ruling takes effect
    within one loop iteration. The TTL sweep above remains the backstop for anything ruled
    while the harvester was off.
    """
    dropped = 0
    for m in state.get("matches") or []:
        key = m.get("key")
        path = m.pop("audio", None) if (isinstance(key, str) and key in ruled) else None
        if not path:
            continue
        # The row stops carrying its audio whatever the policy answers (already gone, dark,
        # pinned): the ruling is the reason the audio existed, and that reason is spent. The
        # removal itself is the policy's to make and to record.
        cache_budget.remove(CANDIDATES_CACHE, path, "ruled-on")
        state["kept"] = max(0, state.get("kept", 0) - 1)
        dropped += 1
    return dropped


# The retirement read. The retired set is a file the rulings' writer owns: one key per entry
# that carries a ruling or is the writer's own upload, `not_a_match` deliberately global --
# "this record is not any Mystery Track", including the mysteries whose clips do not exist
# yet, which is what makes `rescan()` safe. (A `not_a_match` does NOT imply `listened`: you
# can rule a record out as a match and still want to hear it -- the two verdicts are kept
# apart on the writing side.)


def load_rulings():
    """The retired set, from the rulings file: a set of the bare keys (`u<sha1(url)[:20]>`,
    the pool's own naming -- the same stem every file and row already carries), or None when
    the file is absent, torn or the wrong shape -- "cannot read" is never "nothing ruled",
    so a caller must refuse or stand still rather than score against an empty set.

    The reasons are for the human reading the file; the search reads the keys alone. A caller
    that needs a key's signature-file name appends `.npy` where it compares against one.
    """
    data = _load(RULINGS, None)
    if not isinstance(data, dict):
        return None
    return set(data)


def note_no_queries(state, qs):
    """Keep the "nothing to search for" state truthful for the caller that just refreshed
    the query set (run(), at its start).

    An empty query set is a first-class state, not a print-and-vanish: the harvester KEEPS
    RUNNING -- signing files is worth doing whatever the mysteries look like -- but the page
    must not claim the scoring is working while nothing is searchable. Stamped once (the `at`
    is when it AROSE, like sig_alert), and it stands down by itself the moment a refresh
    finds something searchable. Returns True when the state changed (worth persisting)."""
    if not qs:
        if "no_queries" in state:
            return False                     # standing -- an idle pass is not worth a write
        state["no_queries"] = {"at": _now(),
                               "why": "no unsolved mysteries with a usable clip -- nothing to "
                                      "search for. See the searching table: every mystery is "
                                      "either solved, clipless, or its clip was refused."}
        return True
    return state.pop("no_queries", None) is not None


# --- the run ------------------------------------------------------------------------------------

def clip_fingerprint(path):
    """A short hash of the clip's CONTENTS. Changes the moment the clip is re-cut."""
    h = hashlib.sha1()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return "missing"
    return h.hexdigest()[:12]


def queries(state=None):
    """The unsolved mysteries, as chroma. From track-metadata.json -- never from filenames.

    Returns (mystery_number, chroma, query_key) triples. The QUERY KEY is the mystery number plus a
    fingerprint of the clip's contents, and it is what `state["scored"]` is keyed on -- so a clip
    that is RE-CUT invalidates every pairing made against the old one, and every held signature is
    scored again automatically. Keying on the number alone would have meant a better MT7 clip
    inherited the old clip's verdicts and was never actually asked: exactly the false negatives Tim
    was worried about.

    A clip shorter than MIN_QUERY_S is REFUSED, and this is why: MT7's clip is 23 seconds, and it
    produced five confident false positives all within 0.0007 of each other. A short query drives
    every cost down until the matcher cannot tell anything apart -- the ranking goes degenerate, and
    a degenerate match looks exactly like a real one. Better to search for nothing than to search
    for everything. The mystery comes back into the query set by itself once a longer clip is cut.
    """
    import librosa
    out, skipped = [], []
    for e in _mystery.searchable():
        secs = _audio.duration(e["clip"]) if hasattr(_audio, "duration") else None
        if secs is None:
            y_full = _audio.load_audio(e["clip"])
            secs = len(y_full) / float(_audio.SR)
        if secs < MIN_QUERY_S:
            skipped.append({"mystery": e["number"], "clip_s": round(secs),
                            "why": "the clip is %ds, below the %ds floor -- a query that short "
                                   "drives every cost down and the matcher stops being able to "
                                   "tell records apart (MT7's 23s clip produced five 'confident' "
                                   "false positives within 0.0007 of each other). Cut a longer one "
                                   "and it re-enters the search by itself."
                                   % (round(secs), MIN_QUERY_S)})
            continue
        y = _audio.load_audio(e["clip"])[:int(QUERY_S * _audio.SR)]
        c = chroma_recipe.compute_chroma(y)          # same recipe as the candidates it scores
        qkey = "%d:%s" % (e["number"], clip_fingerprint(e["clip"]))
        out.append((e["number"], c, qkey))

    if state is not None:                     # so the page can say what it is NOT asking, and why
        state["searching"] = [n for n, _, _ in out]
        state["skipped_queries"] = skipped
        # The CURRENT clip's query key per mystery. `state["scored"]` is keyed on these, and
        # a re-cut clip CHANGES the key -- so only the harvester can say which key is "now".
        # The page joins this map to state["scored"] for its live "compared: N of pool"
        # column, instead of guessing among stale fingerprints.
        state["query_keys"] = {str(n): qkey for n, _, qkey in out}
    for s in skipped:
        print("# NOT searching MT%d -- %s" % (s["mystery"], s["why"]))
    return out


def _stopped(state):
    """Record a clean stop and save.

    The file that was being signed gets NO row (see sign_file), so the only honest thing to
    say is that it is signed on a later pass -- which is exactly what a stop is.
    """
    name = _stop_name()
    state["current"] = None
    _save(STATE, state)
    print("# stopped on %s -- state saved; the file it was signing gets no row and is signed "
          "again on a later pass" % name)


# --- how much memory this is costing -------------------------------------------------------------
#
# One row per file, so "the harvester is at 40 GB again" is a number somebody can read rather
# than a thing somebody eventually notices. The parent should stay flat -- it never holds a
# file's audio now. The child's peak is the interesting number, and it comes back in result.json.

MEM_LOG_KEEP = 50


def mem_ceiling_mb():
    """The parent's restart ceiling in MB, from NETRADIO_HARVEST_MEM_CEILING_MB. 0 = off.

    Off by default on purpose. The right number depends on what a long file actually costs
    after this change, and that measurement has not been taken yet; a guessed ceiling would
    restart a healthy harvester.
    """
    try:
        return float(os.environ.get("NETRADIO_HARVEST_MEM_CEILING_MB") or 0)
    except ValueError:
        return 0.0


def _mb(value):
    return None if value is None else round(float(value), 1)


def record_memory(state, key, child=None):
    """Sample this process's footprint and write the row. `child` is the decode child's
    result.json, or None for a candidate answered from a held signature."""
    current, peak = memwatch.footprint_mb()
    child = child or {}
    row = {"at": _now(), "key": key, "kind": memwatch.kind(),
           "seconds": child.get("seconds"),
           "parent_mb": _mb(current), "parent_peak_mb": _mb(peak),
           "child_peak_mb": _mb(child.get("peak_mb")),
           "child_after_mb": _mb(child.get("footprint_mb"))}
    state["mem"] = row
    state["mem_log"] = ((state.get("mem_log") or []) + [row])[-MEM_LOG_KEEP:]
    return row


def check_memory(state, key, child=None):
    """Record the row, and say whether the PARENT should stand down to be restarted.

    A child over the ceiling only earns an issues row: its memory left with it, so there is
    nothing to restart. A parent over the ceiling returns from `run()` with exit 0 -- the
    supervisor's watchdog sees a run that is not standing still and spawns a fresh one.
    """
    row = record_memory(state, key, child)
    ceiling = mem_ceiling_mb()
    if not ceiling:
        return False
    if (row["child_peak_mb"] or 0) > ceiling:
        state["issues"] = ((state.get("issues") or []) + [{
            "at": _now(), "key": key,
            "issue": "decode child peaked at %.0f MB, over the %.0f MB ceiling -- reported only, "
                     "the child's memory went with it" % (row["child_peak_mb"], ceiling)}])[-50:]
    if (row["parent_mb"] or 0) > ceiling:
        state["issues"] = ((state.get("issues") or []) + [{
            "at": _now(),
            "issue": "parent at %.0f MB, over the %.0f MB ceiling -- standing down so the "
                     "supervisor can restart it" % (row["parent_mb"], ceiling)}])[-50:]
        print("# parent footprint %.0f MB is over the %.0f MB ceiling -- stopping so the "
              "watchdog restarts us" % (row["parent_mb"], ceiling))
        return True
    return False


def run(args):
    install_signal_handlers()
    lock = acquire_writer_lock()
    if lock is None:
        print("another ledger/state writer is running (another harvest.py --run) -- "
              "ONE writer, always. Not starting.")
        return
    state = _load(STATE, blank_state())
    # THE RETIRED SET IS A FILE ANOTHER PROCESS WRITES: every key this search must never
    # propose again lives in the rulings file, so without it the run has no way to know what
    # it has already been told to stop looking at -- and a search that has forgotten every
    # ruling hands back records already rejected. Refuse here, naming the file (the same
    # refusal the process that starts this one makes), rather than searching on amnesia.
    ruled = load_rulings()
    if ruled is None:
        print("the rulings file (%s) is absent or unreadable: the search has no way to know which "
              "keys it must never propose again. Its writer writes the file at its start and "
              "after every ruling -- with the file back in place, start again." % RULINGS)
        return
    # THE OLD ROWS MOVE ONTO KEYS before anything reads them: every reader joins on the key,
    # and a row still carrying its url is invisible to that join.
    moved = migrate_matches(state)
    if moved:
        _save(STATE, state)
        print("# moved %d old match row(s) from their urls onto keys" % moved)
    qs = queries(state)
    if note_no_queries(state, qs):
        _save(STATE, state)                   # queries(state) stamped searching/skipped too
    if not qs:
        print("no unsolved mysteries with a usable clip -- nothing to search for; the signing "
              "keeps going")
    # THE CACHES MUST EXIST before the first decode: a signature the harvester cannot keep is
    # decode cost paid for nothing, and every file would be `delayed` failing the same way.
    # Refuse here, naming the setting, rather than grinding the directories on a dark policy.
    dark = [n for n, d in ((CHROMA_CACHE, _chroma_dir()), (CANDIDATES_CACHE, _keep_dir()))
            if d is None]
    if dark:
        print("the %s cache %s dark (NETRADIO_CACHE_ROOT unset, or the registration was "
              "refused): the harvester has nowhere to keep a signature or an excerpt.\n"
              "Set NETRADIO_CACHE_ROOT in .env (see .env.example) and start again."
              % (" and the ".join(dark), "is" if len(dark) == 1 else "are"))
        return
    # NO DIRECTORIES, NOTHING TO SIGN, EVER. Refuse here, naming the setting, rather than
    # idling forever over an unset variable -- the same refusal a dark cache earns. A
    # relative entry is refused by name in the issues row the gate collects, so an operator
    # sees WHICH entry was wrong, not only that the setting came up empty.
    said = set()                        # per-run dedupe: a refusal or a gap is news once
    _said_vanished.clear()
    dirs = _dirs(issues=state.setdefault("issues", []), said=said)
    if not dirs:
        print("NETRADIO_HARVEST_DIRS is unset, empty, or names no absolute directory: the "
              "harvester has no directories of audio to sign. Name one or more (absolute, "
              "`:`-separated) in .env -- see .env.example and docs/HARVEST_FEED.md -- and "
              "start again.")
        _save(STATE, state)
        return
    print("# searching for Mystery Tracks %s"
          % (", ".join(str(n) for n, _, _ in qs) or "none"))
    print("# signing the top level of %d director%s, one file a pass. Ctrl-C or SIGTERM stops "
          "cleanly: state and ledger are saved, and the file in flight is signed on a later "
          "pass." % (len(dirs), "y" if len(dirs) == 1 else "ies"))

    sweep_excerpts()                    # drop anything past its TTL before we start
    swept = sweep_job_dirs()            # and whatever a crashed decode child left in .harvest/tmp
    if swept:
        print("# swept %d stale decode job director%s" % (swept, "y" if swept == 1 else "ies"))

    # THE ALLOCATOR CANARY. `MallocLargeCache=0` is what stops macOS holding on to every large
    # block the recipe frees, and it is an UNDOCUMENTED variable -- so an OS release that stops
    # honouring it would quietly restore the old behaviour, with nothing to see but a swap file
    # slowly growing. Measure it on every start instead of trusting a number from a past release.
    _before, _after, retained = memwatch.allocator_canary()
    if retained is not None:
        print(memwatch.canary_line(retained))
        issue = memwatch.canary_issue(retained)
        if issue:
            state["issues"] = (state.get("issues") or [])[-49:] + [{"at": _now(), "issue": issue}]
            _save(STATE, state)

    # THE CANARY, at start: re-run one solved calibration case from local files. A broken
    # harvester and a pool without the answer look identical from here: zero matches, for
    # weeks. So before searching for something we have never found, prove we can still find
    # something we HAVE -- the offline check runs the matching maths against local files and
    # catches a break in chroma_match before the first pass. The per-pass re-score of the
    # canary's STORED signature (score_canary, inside the loop) is the other half: it proves
    # the pool still holds the record the canary names, every pass.
    st = selftest.offline()
    if st.get("ok"):
        print("# self-test PASS -- %s: cost %.4f, rank %d, beat the field by %.4f"
              % (st["name"], st["cost"], st["rank"], st["margin"]))
    elif st.get("ok") is False:
        print("!! SELF-TEST FAILED -- %s" % st.get("why"))
        print("!! The matcher cannot find a record we KNOW it holds. Every 'no match' it reports")
        print("!! from here is meaningless. Fix this before trusting another day of searching.")
        state.setdefault("issues", []).append(
            {"at": _now(), "issue": "self-test failed: %s" % st.get("why")})
        _save(STATE, state)
    else:
        print("# self-test skipped -- %s" % st.get("why"))

    # THE LEDGER, seeded and reconciled. The bucket's listing is the only record of what is
    # signed today, so at the first start it becomes one `signed` row per key; on every later
    # start the rows are checked against it. Past the cap the reconciliation reports and
    # stands still -- the sig_alert it leaves is what the notices path shows.
    rec = reconcile_ledger(state)
    print("# ledger: %s" % rec["why"])
    if rec["reported"]:
        print("!! " + rec["why"])
    if rec["seeded"] or rec["dropped"] or rec["restored"] or rec["reported"] or rec["cleared"]:
        _save(STATE, state)

    while True:
        if _stop_requested():
            return _stopped(state)
        if os.path.exists(PAUSE):
            state["current"] = None
            _save(STATE, state)
            if _nap(PASS_GAP_S):
                return _stopped(state)
            continue

        ledger = load_ledger()
        # Re-read the rulings file every pass, before anything consumes it: a ruling reaches the
        # file within seconds, and must reach this search within one loop iteration. Unreadable
        # is NOT "nothing ruled" -- a run that carried on would score against an empty retired
        # set and hand back records already rejected, so stand down and let the supervisor
        # restart the run once the file is back (its writer rewrites it at its start).
        ruled = load_rulings()
        if ruled is None:
            state["current"] = None
            state["issues"] = ((state.get("issues") or []) + [{
                "at": _now(),
                "issue": "stopped: the rulings file (%s) could not be read -- a search that "
                         "has forgotten every ruling hands back records already rejected"
                         % RULINGS}])[-50:]
            _save(STATE, state)
            print("!! the rulings file (%s) could not be read -- stopping this run; start it "
                  "again once the file is back." % RULINGS)
            return

        # A ruling spends the excerpt: the audio existed to let the human make the call, and the
        # call has been made. Drop it now, not at the 30-day sweep.
        n_dropped = drop_ruled_excerpts(state, ruled)
        if n_dropped:
            _save(STATE, state)
            print("dropped %d ruled-on excerpt(s) -- the leads keep their numbers" % n_dropped)
        if stamp_pool(state):
            _save(STATE, state)          # the bucket's count for the page; ≤15-min cached

        # THE CANARY, re-scored every pass. A broken matcher and a pool without the answer look
        # identical from here, so before the search reports another "no match" it proves it can
        # still find a record it KNOWS it holds -- the canary's stored signature, pulled back
        # from the bucket by its key, scored against the canary's mix and the current mysteries.
        # A failure raises `sig_alert` (the same alarm the ledger's reconcile raises); a pass
        # stands it down. Unconfigured, it reports "not configured" and carries on, as it does
        # today when the searched hit is refused.
        if score_canary(state, qs):
            _save(STATE, state)

        # Score held signatures against any mystery they have not met yet -- a bounded chunk per
        # pass, so it rides along with the signing instead of blocking it. This is CPU only
        # (~0.06s each), and it is what makes a NEW mystery see the WHOLE corpus: the day MT8's
        # clip lands, every signature the pool holds gets scored against it, without a single
        # new decode. Positions (`at_s`) on old rows get filled in on the way.
        todo = len(unscored_pairs(state, ledger, ruled, qs))
        if todo:
            state["rescan_pending"] = todo
            n = rescan(state, ledger, ruled, qs, limit=RESCAN_PER_PASS)
            state["rescan_pending"] = max(0, todo - n)
            _save(STATE, state)
        elif sigstore.enabled():
            # Rescan backlog empty = every held signature is scored vs every current mystery,
            # which is exactly when cold ones may leave the disk (verified-remote only).
            n_ev, freed = sigstore.evict_cold(_chroma_dir(), state.get("scored") or {},
                                              [qk for _, _, qk in qs])
            if n_ev:
                print("evicted %d cold signature(s) to the bucket (%.1f MB freed)"
                      % (n_ev, freed / 1e6))

        # --- the sign step: one file a pass ------------------------------------------------
        # Room first, decode second. The probe is the policy's own answer to "is there room
        # for a signature", asked once per pass so a full disk does not put every file through
        # a multi-hour decode that ends in `no_space`; the child asks again with the real
        # size, and its refusal is the row's reason.
        if not cache_budget.reserve(CHROMA_CACHE, None):
            if "no-room" not in said:
                said.add("no-room")
                print("# the cache policy refuses room for a signature (the disk is past its "
                      "floor, or the chroma cache is over its cap with nothing evictable) -- "
                      "signing waits for an eviction run to make room")
            if _nap(PASS_GAP_S):
                return _stopped(state)
            continue
        todo_files, covered = scan_directories(ledger, issues=state.setdefault("issues", []),
                                               said=said)
        # A scan can refuse a whole directory's worth of badly-named files in one pass; trim
        # here (before any continue path) so the list stays the last fifty rows it is.
        state["issues"] = (state.get("issues") or [])[-50:]
        fresh = [k for k in covered if k not in said]
        if fresh:
            said.update(fresh)
            state["skipped_cached"] += len(fresh)
            _save(STATE, state)
        if not todo_files:
            state["current"] = None
            if _nap(PASS_GAP_S):
                return _stopped(state)
            continue

        rec_file = todo_files[0]
        state["current"] = rec_file["key"]
        _save(STATE, state)
        c, samples = sign_file(rec_file["path"], issues=state["issues"])
        # A stop is never a verdict, and it arrives by either route: this process was signalled
        # (the flag), or only the decode child was (the sentinel error). Checking one and not
        # the other is not a guard -- the row's writer is right below.
        if _stop_requested() or was_stopped(_LAST_CHILD.get("error")):
            samples = None
            return _stopped(state)
        if check_memory(state, rec_file["key"], dict(_LAST_CHILD)):
            samples = None
            _save(STATE, state)
            return

        row = load_ledger().get(rec_file["key"]) or {}
        if row.get("status") == "signed":
            state["analyzed"] += 1
            print("  signed %s (%s)" % (rec_file["key"], _LAST_CHILD.get("seconds")))
        elif row.get("status") == "delayed":
            print("  delayed %s: %s -- %s"
                  % (rec_file["key"], row.get("reason"), _LAST_CHILD.get("error")))
        elif row:
            # A row that is neither: not one of this run's writes. Left alone -- the next pass
            # rescans and decides.
            pass
        else:
            # No row at all: the file left while it was being signed (sign_file already wrote
            # the issue row), or its sidecar went between the scan and the sign. Nothing to
            # count as work, and nothing to retire -- the next pass sees the truth.
            state["errors"] += 1

        if c is not None:
            for num, qc, _qkey in qs:
                cost, shift, at = _cm.match(qc, c)
                if cost is None or cost > KEEP_CEILING:
                    continue
                board = [m for m in state["matches"] if m["mystery"] == num]
                board.sort(key=lambda m: m["cost"])
                if len(board) >= KEEP_TOP and cost >= board[-1]["cost"]:
                    continue                       # not good enough to displace anyone

                excerpt = os.path.join(_keep_dir(), "MT%d-%.4f-%s.wav"
                                       % (num, cost,
                                          hashlib.sha1(rec_file["key"].encode()).hexdigest()[:8]))
                kept = os.path.exists(excerpt)
                if not kept:
                    if samples is None:            # held signature, no audio in hand -> no excerpt
                        continue
                    # A refused excerpt (past the disk floor, the board's cap with nothing
                    # evictable) is not on disk, so it is neither counted as kept nor named as
                    # the lead's audio: the lead itself survives and the page plays it from the
                    # source embed.
                    kept = write_excerpt(samples, at or 0, excerpt)   # from memory; NO second decode
                    if kept:
                        state["kept"] += 1
                hit = {"at": _now(), "mystery": num, "cost": round(cost, 4),
                       "semitones": shift, "at_s": round(at or 0, 1), "key": rec_file["key"],
                       "audio": excerpt if kept else None,
                       "verdict": "MATCH" if cost <= MATCH_COST else "near"}
                state["matches"].append(hit)

                evict_overfull(state, num)
                print("  %s  MT%d  cost %.4f  %s  at %s  %s"
                      % (hit["verdict"], num, cost, _cm.describe_shift(shift),
                         _cm.describe_at(at), rec_file["key"]))
        samples = None                          # drop the decoded audio; it never persists
        state["updated"] = _now()
        _save(STATE, state)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", action="store_true", help="run the loop (signs and scores; runs "
                                                       "for weeks)")
    ap.add_argument("--sign-one", metavar="KEY",
                    help="sign ONE file now: the file in the configured directories whose stem "
                         "is this key, through the same path the loop signs it with. It takes "
                         "the writer lock, reconciles the ledger against the bucket first, and "
                         "writes the row; it does not score.")
    ap.add_argument("--sign-job", metavar="DIR",
                    help="the form --run spawns: decode the file named in DIR/sign.json and "
                         "write result.json. The audio path stays OFF the command line, "
                         "because the supervisor reads that command line to find the "
                         "harvester -- see _spawn_argv.")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--pause", action="store_true")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--purge-audio", action="store_true",
                    help="delete every retained excerpt. The LEADS survive (key, cost, mystery, "
                         "at) -- only the audio goes. Review them by embed.")
    ap.add_argument("--forget", type=int, metavar="N",
                    help="drop every lead for Mystery Track N, and every scored pairing against "
                         "it. For when the QUESTION was bad -- a clip too short to distinguish "
                         "records with. The next clip then starts clean against the whole pool.")
    ap.add_argument("--migrate-sigs", action="store_true",
                    help="move the signature archive fully into the bucket: upload+verify every "
                         "local signature, then evict the cold ones (verified remote AND scored "
                         "against every current mystery). Run with the harvester STOPPED or "
                         "paused. After this, local disk holds only work in progress.")
    ap.add_argument("--rescan", action="store_true",
                    help="score every held signature against every mystery it has not met yet, "
                         "in one go. No network. The running harvester does this by itself, a "
                         "chunk at a time -- this is for when you want it finished NOW (e.g. you "
                         "have just added a Mystery Track clip).")
    args = ap.parse_args()

    # THE DECODE CHILD, dispatched before any lock, ledger or state access -- it must never take
    # the writer lock or touch ledger.json / state.json. One file, one process, and the recipe's
    # working set goes away when it exits.
    if args.sign_job:
        install_signal_handlers(child=True)
        job = args.sign_job
        spec = _load(os.path.join(job, "sign.json"), {}) or {}
        path = spec.get("path")
        if not path:
            print("no audio path: --sign-job needs DIR/sign.json naming the file",
                  file=sys.stderr)
            return 2
        try:
            result = _decode_and_sign(path, job, spec.get("expect_s"))
        except Exception:                    # a crash is the parent's "child failed" path
            traceback.print_exc()
            return 1
        _save(os.path.join(job, "result.json"), result)
        return 0

    os.makedirs(STATE_DIR, exist_ok=True)
    if args.purge_audio:
        print(purge_audio())
        return
    if args.forget:
        state = _load(STATE, blank_state())
        leads, pairs = forget(state, args.forget)
        _save(STATE, state)
        print("# forgot MT%d: dropped %d lead(s) and %d scored pairing(s)."
              % (args.forget, leads, pairs))
        print("# Cut a better clip and it re-enters the search by itself, against every held "
              "signature.")
        return
    if args.migrate_sigs:
        if not sigstore.enabled():
            print("# sigstore is dark -- set NETRADIO_SIG_BUCKET (and profile/endpoint) in "
                  ".env first.")
            return
        cache = _chroma_dir()
        if cache is None:
            print("# the signature cache is dark -- set NETRADIO_CACHE_ROOT in .env first.")
            return
        state = _load(STATE, blank_state())
        qs = queries()
        names = sorted(n for n in os.listdir(cache)
                       if n.startswith("u") and n.endswith(".npy")) if os.path.isdir(cache) else []
        up = failed = 0
        for name in names:
            path = os.path.join(cache, name)
            try:
                local = os.path.getsize(path)
            except OSError:
                continue
            if sigstore.remote_size(name) == local:
                continue                        # already there, verified
            if sigstore.put(path, name):
                up += 1
            else:
                failed += 1
        n_ev, freed = sigstore.evict_cold(cache, state.get("scored") or {},
                                          [qk for _, _, qk in qs])
        left = len(names) - n_ev
        print("# migrate: %d uploaded, %d upload failure(s); %d evicted (%.1f MB freed); "
              "%d signature(s) still local (unscored vs a current mystery, or unverified)."
              % (up, failed, n_ev, freed / 1e6, left))
        if failed:
            print("# NOTHING that failed to upload was deleted. Fix the store config and re-run.")
        return
    if args.sign_one:
        # The hand tool, and a writer: it takes the same lock, reconciles the ledger the same
        # way a run does, and signs through the same sign_file. It does not score -- scoring
        # is the loop's, and the next run's rescan finds the fresh row.
        lock = acquire_writer_lock()
        if lock is None:
            print("# a ledger/state writer is RUNNING (a harvest.py --run) -- not signing "
                  "under it. Stop the writer first if you need this now.")
            return
        state = _load(STATE, blank_state())
        if not _dirs(issues=state.setdefault("issues", [])):
            print("# NETRADIO_HARVEST_DIRS is unset, empty, or names no absolute directory: "
                  "--sign-one looks for the file in the configured directories. Name them in "
                  ".env (see .env.example) and try again.")
            _save(STATE, state)
            return
        rec = reconcile_ledger(state)
        print("# ledger: %s" % rec["why"])
        # The same save a run makes after its reconcile, and for the same reason: an alert it
        # raised -- or stood down -- must reach the state file even when this hand sign then
        # finds no file and returns early below.
        if rec["seeded"] or rec["dropped"] or rec["restored"] or rec["reported"] or rec["cleared"]:
            _save(STATE, state)
        moved = migrate_matches(state)
        if moved:
            _save(STATE, state)
        path = find_file(args.sign_one)
        if path is None:
            todo, _covered = scan_directories(load_ledger(),
                                              issues=state.setdefault("issues", []))
            print("# no file for key %s in the configured directories -- the scan wants %d "
                  "file(s): %s" % (args.sign_one, len(todo),
                                   ", ".join(r["key"] for r in todo[:5]) or "none"))
            return
        sign_file(path, issues=state.setdefault("issues", []))
        row = load_ledger().get(args.sign_one) or {}
        if row.get("status") == "signed":
            print("# signed %s (%s)" % (args.sign_one, _LAST_CHILD.get("seconds")))
        elif row.get("status") == "delayed":
            print("# delayed %s: %s -- %s"
                  % (args.sign_one, row.get("reason"), _LAST_CHILD.get("error")))
        else:
            print("# %s was not signed and has no row -- see the issues list" % args.sign_one)
        if state.get("issues"):
            _save(STATE, state)              # the scan's and sign_file's issue rows
        return
    if args.rescan:
        # The same refusal every cache-reading mode makes: unscored_pairs would count the
        # pairs (a bucket-held signature reads as held), _load_sig would answer None for every
        # one, and the run would stamp `rescan_pending` to 0 over "Every held signature has
        # now met every mystery" -- a completion claim about work that never ran.
        if _chroma_dir() is None:
            print("# the signature cache is dark -- set NETRADIO_CACHE_ROOT in .env first.")
            return
        # The same refusal run() makes: the rescan scores the corpus, and without the rulings
        # file it cannot know which keys it must never propose -- it would score records
        # already rejected and stamp `rescan_pending` over them.
        ruled = load_rulings()
        if ruled is None:
            print("# the rulings file (%s) is absent or unreadable -- not rescanning: without it the "
                  "rescan cannot tell a ruled-out candidate from an active one. The file's writer "
                  "writes it at its start and after every ruling." % RULINGS)
            return
        # The corpus the rescan walks is the LEDGER, so a ledger that does not exist yet means
        # the harvester has never run under this contract -- and a rescan over nothing would
        # stamp the same completion over work that never ran.
        if not os.path.exists(LEDGER):
            print("# %s does not exist yet -- start the harvester once (it seeds the ledger "
                  "from the bucket) and re-run." % LEDGER)
            return
        state = _load(STATE, blank_state())
        ledger = load_ledger()
        qs = queries()
        todo = len(unscored_pairs(state, ledger, ruled, qs))
        print("# rescanning %d (signature, mystery) pair(s) against MT%s -- no network, ~%.0f min"
              % (todo, "/MT".join(str(n) for n, _, _ in qs), todo * 0.06 / 60))
        n = rescan(state, ledger, ruled, qs)
        state["rescan_pending"] = 0
        _save(STATE, state)
        print("# scored %d. Every held signature has now met every mystery." % n)
        return
    if args.pause:
        open(PAUSE, "w").close()
        print("paused (the loop will notice within ~%ds)" % PASS_GAP_S)
        return
    if args.resume:
        if os.path.exists(PAUSE):
            os.unlink(PAUSE)
        print("resumed")
        return
    if args.status:
        s = _load(STATE, blank_state())
        ledger = load_ledger()
        signed = sum(1 for r in ledger.values()
                      if isinstance(r, dict) and r.get("status") == "signed")
        delayed = sum(1 for r in ledger.values()
                      if isinstance(r, dict) and r.get("status") == "delayed")
        print(json.dumps({"analyzed": s["analyzed"], "kept": s["kept"], "errors": s["errors"],
                          "signed": signed, "delayed": delayed,
                          "to_sign": len(scan_directories(ledger)[0]) if _dirs() else None,
                          "matches": len(s["matches"]),
                          "current": s.get("current"),
                          "paused": os.path.exists(PAUSE)}, indent=2))
        return
    if args.run:
        run(args)


if __name__ == "__main__":
    sys.exit(main())        # --sign-job's exit code is how the parent tells a crash from a refusal
