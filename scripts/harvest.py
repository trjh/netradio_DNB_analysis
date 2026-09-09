#!/usr/bin/env python3
"""Harvest chroma signatures from the internet, slowly, and match them against the Mysteries.

    PYTHONPATH=scripts .venv/bin/python scripts/harvest.py --seed-channel https://www.youtube.com/@back2theoldskoolera999
    PYTHONPATH=scripts .venv/bin/python scripts/harvest.py --run          # work the queue
    PYTHONPATH=scripts .venv/bin/python scripts/harvest.py --status

This runs for WEEKS. It is built to keep its load on other people's servers as low as possible.

The idea
--------
The matcher can only find what is in the pool, and the pool we want is far bigger than this
disk. So **keep the signature, not the audio**: a chroma signature is a 12xN float16 matrix,
~55KB against ~8MB for the track. A 100,000-track pool is ~5GB of signatures, and the audio
never has to touch the disk at all -- it is streamed, hashed to chroma, and dropped.

Except a brief excerpt of a near-miss. If a candidate scores near a Mystery Track we keep the
matched ~30-second window -- and only that window -- so a human can listen and confirm or reject
it. Cut from the audio already in memory, so nothing is fetched twice. It is not a copy of the
record; it is a magnifying glass over the moment the matcher flagged, swept after 30 days.

Load discipline
---------------
The point of all of this is to put as little load as possible on the servers we read from. Each
control below is justified by load, not by hiding -- if a control only made sense as a way to
avoid being noticed, it would not be here.

* **Spread load across hosts.** Consecutive fetches go to different sites, so no single host
  carries a run of back-to-back requests.
* **Per-host rate limits**, so a slow response from one host never concentrates load on another.
* **Randomised gaps** between requests to a host, so we never send a synchronised train of them.
* **Bounded sessions**: work a few hours, then idle, so sustained load stays low over a day.
* **Exponential backoff** on 429/403, and a HARD STOP after repeated 403 -- that is the host
  telling us to stop, and we stop.
* **Each track is fetched once, ever.** The signature cache guarantees it -- the single biggest
  load reduction available, and free.

Pause/resume and progress live in `.harvest/` so the player's dashboard can drive them.
"""

import argparse
import fcntl
import hashlib
import json
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np                                   # noqa: E402

from streamalign import audio as _audio              # noqa: E402
from streamalign import chroma_match as _cm          # noqa: E402
from streamalign import groundtruth as _gt           # noqa: E402
from streamalign import mystery as _mystery          # noqa: E402

import chroma_recipe                                 # noqa: E402  (THE recipe, single source)
import memwatch                                      # noqa: E402  (footprint + allocator canary)
import selftest                                      # noqa: E402  (the canary; see run())
import sigstore                                      # noqa: E402  (bucket = the pool's only home)

HOME = _gt.REPO_ROOT
STATE_DIR = os.path.join(HOME, ".harvest")
STATE = os.path.join(STATE_DIR, "state.json")
QUEUE = os.path.join(STATE_DIR, "queue.json")
PAUSE = os.path.join(STATE_DIR, "PAUSED")
CACHE = os.path.join(HOME, ".chroma-cache")
KEEP = os.path.join(os.path.expanduser("~"), "media", "netradio-candidates")

# THE queue/state writer lock. queue.json and state.json have exactly ONE writer at a time:
# collector.run() in split mode, run() in Mode A, or the on-demand --requeue-missing-sigs.
# Every one of them takes this flock for its lifetime, so "never run Mode A alongside the
# collector" is enforced, not just documented. The path keeps its historic name
# (collector.lock) so a new binary and an already-running old collector still exclude
# each other.
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
# How many cached signatures to re-score per pass of the loop. Small on purpose: rescanning is CPU
# and fetching is network, so a modest chunk each pass rides along in the gaps (host backoff, the
# polite delay between fetches) instead of stalling the search. A full rescan of ~900 signatures
# against 3 mysteries is only ~3 minutes of CPU, so there is no hurry.
RESCAN_PER_PASS = 25
KEEP_CEILING = 0.130      # never retain an excerpt worse than the worst plausible true match
KEEP_TTL_DAYS = 30        # a lead not listened to in a month is not a lead -- swept
# A reported MATCH still needs cost AND margin. The populations OVERLAP (true match up to 0.0971,
# non-match down to 0.0376), so no cost alone can separate them: RANK is the reliable signal, and
# the margin test is what actually carries the gate. 40 of 41 tracks rank #1 against their own
# original, so the margin is real.
MATCH_COST = 0.050

# --- load discipline -------------------------------------------------------------------------
# Work for hours, then rest, so sustained load over a day stays low. No quiet hours (Tim's call);
# it is the spread across hosts and the randomised gaps that keep the load low, not the clock.
SESSION_S = (4 * 3600, 5 * 3600)
IDLE_S = (40 * 60, 120 * 60)
# Per-host: the MEAN gap between fetches to THAT host. Actual gaps are jittered 0.5x-2.0x, so a
# host never sees an even cadence.
HOST_GAP_S = {"youtube.com": 60.0, "youtu.be": 60.0, "soundcloud.com": 75.0,
              "bandcamp.com": 90.0, "_default": 90.0}
BACKOFF_START_S = 300.0
BACKOFF_MAX_S = 6 * 3600
BLOCK_AFTER = 5          # consecutive 403s from one host -> stop touching it


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


def host_of(url):
    h = url.split("//", 1)[-1].split("/", 1)[0].lower()
    for known in HOST_GAP_S:
        if known != "_default" and known in h:
            return known
    return h or "_default"


def blank_state():
    return {"started": _now(), "updated": _now(), "analyzed": 0, "kept": 0, "errors": 0,
            "skipped_cached": 0, "matches": [], "issues": [], "hosts": {},
            "session": {"phase": "idle", "until": 0}, "current": None}


# --- chunks: a URL that means "this slice of that audio" ----------------------------------------
#
# A queue entry for audio longer than two hours arrives split into CHUNKS: the same URL, once per
# slice, each carrying a W3C media fragment -- `#t=<start>,<end>`, in whole seconds. It is the
# fragment, and only the fragment, that makes one chunk different from another.
#
# yt-dlp ignores URL fragments. So a harvester that does not read them here would fetch, decode
# and sign the WHOLE multi-hour master once per chunk -- the exact cost splitting exists to
# prevent -- and would then store that one signature under every chunk's key, so each chunk would
# claim to be the master. Wrong answers, cached, never fetched again.

# The digit count is BOUNDED, and that bound is part of the definition of a fragment -- not a
# safety net bolted on after one. Ten digits of seconds is on the order of three centuries; no
# recording, and no slice of one, comes within many orders of magnitude of that, so nothing real
# is turned away. What an unbounded `\d+` let in was a field far too long to be a time at all:
# Python refuses to convert a string of more than 4,300 digits to an int, so `int()` raised
# straight through `listen_queue_split` -- which promises never to raise -- and out of
# `sync_listen_queue` into the main loop, where a single malformed queue entry would have ended a
# run meant to last for days. A 4,300-digit value cleared the parser instead and then overflowed
# while its own refusal message was being formatted.
#
# The bound belongs HERE, in the pattern, not in a `try` wrapped around each caller. A field that
# long is not a media fragment, so it simply fails to match and the URL is an ordinary one --
# leaving one definition of what a valid fragment is, rather than a permissive parser that
# everything downstream then has to survive.
_FRAGMENT = re.compile(r"t=(\d{1,10}),(\d{1,10})", re.ASCII)


def media_fragment(url):
    """`(start_s, end_s)` from a URL's `#t=<a>,<b>` fragment, or None if it has no valid one.

    Strict on purpose: two non-negative whole seconds of at most ten digits each, with a < b, and
    nothing else in the fragment. Anything that does not match is not a media fragment, so the URL
    is treated as an ordinary one and the whole audio is signed -- the same as before chunks
    existed. The one thing that must never happen is a HALF-applied fragment: audio cut somewhere
    we did not mean, signed under a key that says exactly where it should have been cut.

    NEVER RAISES, for any string at all. The queue is data this process does not control, and its
    callers -- `listen_queue_split`, and through it the main loop's `sync_listen_queue` -- have no
    business dying over one malformed entry.
    """
    _, sep, frag = url.partition("#")
    if not sep:
        return None
    m = _FRAGMENT.fullmatch(frag)
    if not m:
        return None
    start, end = int(m.group(1)), int(m.group(2))
    return (start, end) if start < end else None


# THE BACKSTOP, not the rule. The player splits audio longer than TWO hours into chunks; the
# harvester refuses anything that would decode more than FOUR hours in one go. The two numbers are
# deliberately different, because they answer different questions: two hours is where a set is
# long enough that splitting it pays, four is where one decode costs more time, more memory and
# more of a session than any single lead can be worth.
MAX_DURATION_S = 4 * 3600


def too_long(url, duration=None):
    """True when analysing this URL would decode more than MAX_DURATION_S of audio.

    WHICH LENGTH GOVERNS. A queue entry can carry both a declared `duration` and a fragment, and
    they can disagree -- a chunk's entry may well repeat the whole master's duration. The
    fragment's SPAN wins whenever there is one, because the span is what ffmpeg is actually told
    to decode and therefore what this run will cost. A declared duration decides only when there
    is no fragment, and a duration that is missing, non-numeric or a bool is no evidence of
    length at all, so it never refuses.

    A FRAGMENT IS A CLAIM, NOT A GUARANTEE. It would be easy to read "it has a fragment" as "it
    is a chunk, so it is short" and stop there. That is exactly the assumption a backstop exists
    to survive: chunks are short *by construction*, and the construction is upstream of here,
    where it can be wrong, stale or hand-written. So the span is measured, never assumed --
    `#t=0,21600` is six hours and is refused precisely like the unsplit master it was cut from.

    ONE PREDICATE, TWO DOORS -- AND THE SAME FACTS AT BOTH. This is called at the queue door
    (`listen_queue_split`, which never offers a refused entry as a candidate) and again at the
    decode door (`_decode_and_sign`, the last thing before ffmpeg is handed `-ss`/`-t`). Same
    function both times, so they cannot drift into disagreeing about what is acceptable.

    But a shared predicate is not enough on its own: it also has to be asked the same question.
    The decode runs in a child process, which knows only what its argv carries, and while the
    declared duration stayed behind in the parent the second call quietly answered a DIFFERENT
    question -- fragment-only -- and waved through six-hour masters the first call had refused.
    So the duration travels with the URL now (`--duration`, see `_run_fetch_child`).

    Where genuinely no duration is known -- someone typing `--fetch-one <url>` by hand -- the
    honest answer is that this URL is not length-checked, and the span still is. Inventing a
    length, or refusing everything unlabelled, would both be worse than saying so.
    """
    cut = media_fragment(url)
    if cut is not None:
        seconds = cut[1] - cut[0]
    elif isinstance(duration, bool) or not isinstance(duration, (int, float)):
        return False
    else:
        seconds = duration
    return seconds > MAX_DURATION_S


# Past this, a number is not a length, it is a typo or an attack. A fragment span is bounded by
# the parser, but a DECLARED duration is unbounded JSON -- a 5,000-digit int raises OverflowError
# on its way to a float, and the one thing a refusal message must never do is become the crash it
# was written to report. The comparison below is integer-only, so it is safe at any magnitude.
_PRINTABLE_MAX_S = 10 ** 12


def _hours(seconds):
    """`seconds` as hours, for a message. Total: it prints something for any number at all."""
    if seconds > _PRINTABLE_MAX_S:
        return "beyond any real length"
    return "%.1f h" % (seconds / 3600.0)


# --- the signature ----------------------------------------------------------------------------

def sig_path(url):
    # The fragment stays IN the url here, and that is the point: it is what gives each chunk of one
    # master its own key, its own cached signature and its own job directory.
    return os.path.join(CACHE, "u" + hashlib.sha1(url.encode()).hexdigest()[:20] + ".npy")


# --- YouTube wants to know you are a person -------------------------------------------------------
#
# YouTube now challenges anonymous downloads: "Sign in to confirm you're not a bot." Note what that
# error is NOT -- it has no 403, no 429, nothing the host-backoff logic looks for. So it sailed
# straight past the backoff and the harvester kept asking, over and over, filling the issue list and
# analysing nothing. A wall you cannot see is worse than one you can.
#
# Two ways to answer it, both OFF by default (the harvester must never touch a browser profile, or
# read a credential, unless explicitly told to):
#
#   NETRADIO_YTDLP_COOKIES=/path/to/cookies.txt    -- a cookies.txt export (Netscape format)
#   NETRADIO_YTDLP_COOKIES_FROM_BROWSER=chrome     -- or firefox / safari / brave / edge
#
# The cookie IS a credential: it is your logged-in YouTube session. Keep the file out of the repo
# (it is gitignored) and off the public remote.

def cookie_args():
    jar = os.environ.get("NETRADIO_YTDLP_COOKIES", "").strip()
    if jar and os.path.isfile(jar):
        return ["--cookies", jar]
    browser = os.environ.get("NETRADIO_YTDLP_COOKIES_FROM_BROWSER", "").strip()
    if browser:
        return ["--cookies-from-browser", browser]
    return []


# The errors that mean "you are not getting anything else out of me until you authenticate". These
# do not improve with waiting, so backing off is the wrong move: it just fails more slowly.
BOT_WALL = ("sign in to confirm", "confirm you're not a bot", "confirm you are not a bot",
            "use --cookies", "login required", "private video", "age-restricted")


def is_bot_wall(err):
    e = (err or "").lower()
    return any(p in e for p in BOT_WALL)


# --- stopping ----------------------------------------------------------------------------------
#
# There was no signal handler at all. A SIGINT or SIGTERM to this pid alone raised KeyboardInterrupt
# inside communicate() and the process left -- while yt-dlp and ffmpeg carried on pulling bandwidth
# from someone else's server with nobody watching. That is the one thing this program is built not
# to do. (The player's supervisor was never affected: it signals the whole process group.)
#
# The handler sets a FLAG and does not raise. Raising lands on whatever bytecode happened to be
# executing, which includes the middle of a state save; a flag is checked at points we choose.

_STOP = {"signum": 0, "child": None, "procs": [], "part": None}


def _stop_requested():
    return bool(_STOP["signum"])


def _stop_name():
    try:
        return signal.Signals(_STOP["signum"]).name
    except ValueError:
        return "signal %s" % _STOP["signum"]


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
    """Parent handler: raise the flag and pass the signal on to the fetch child, if one is up."""
    _STOP["signum"] = signum
    child = _STOP["child"]
    if child is not None and child.poll() is None:
        try:
            child.terminate()
        except OSError:
            pass


def _child_stop(signum, _frame):
    """Fetch-child handler: stop ffmpeg BEFORE yt-dlp, then leave with 128 + signum.

    The order is not a detail. Kill yt-dlp first and ffmpeg sees a clean EOF on its stdin, decodes
    what it already has, and exits 0 -- so a TRUNCATED stream looks like a complete one, and the
    only thing left between a partial decode and a signature cached forever under this URL is the
    length gate. Stop the decoder first and there is no such window.
    """
    _STOP["signum"] = signum
    for proc in _STOP["procs"]:                # [ffmpeg, yt-dlp], in that order
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

    The loop's rests are minutes long (a 40-120 minute idle, most of all). Sleeping through them
    means a SIGTERM is not acted on until the nap ends, which looks exactly like a hung process.
    Returns True when the nap was cut short.
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


# --- the fetch: one child process per candidate --------------------------------------------------
#
# The recipe's working set is large and short-lived, and macOS does not give it back (see
# memwatch). Running each candidate's fetch in its own process means the long-lived parent -- which
# holds the state, the queue, the mysteries and the matching board -- never touches a track's audio
# at all, and whatever a child allocates leaves with the child. The parent's footprint stays flat
# across candidates instead of climbing to a high-water mark and staying there.

JOBS = os.path.join(STATE_DIR, "tmp")     # one directory per in-flight fetch
STDERR_KEEP = 4096                        # bytes of each subprocess's stderr we hold on to
JOB_STALE_S = 3600                        # a job dir older than this belongs to a crashed child


def _drain(stream, sink):
    """Read a pipe to EOF into a bounded buffer, on a thread.

    yt-dlp can fill its 64 KB stderr pipe while ffmpeg is still decoding a two-hour track. Nobody
    was reading it until ffmpeg exited, so the two could deadlock: yt-dlp blocked writing stderr,
    ffmpeg blocked waiting for stdin. Only the tail matters -- the error we report is the last
    line -- so the buffer is bounded.
    """
    try:
        for chunk in iter(lambda: stream.read(65536), b""):
            sink.append(chunk)
            if len(sink) > 1:
                sink[:] = [b"".join(sink)[-STDERR_KEEP:]]
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _last_line(chunks, prefix=""):
    """The last non-empty line of a drained stderr, as the error string callers already expect."""
    text = b"".join(chunks).decode("utf-8", "replace")
    lines = [ln.strip() for ln in text.strip().split("\n") if ln.strip()]
    return (prefix + lines[-1])[:160] if lines else ""


def _wait(proc, poll_s=1.0):
    """Wait for a subprocess a second at a time, so a stop is acted on during a long decode."""
    while True:
        try:
            return proc.wait(timeout=poll_s)
        except subprocess.TimeoutExpired:
            if _stop_requested():
                return None


def job_dir(url):
    return os.path.join(JOBS, _sig_key(url)[:-4])


def sweep_job_dirs(max_age_s=JOB_STALE_S):
    """Remove what a crashed fetch child left behind. Nothing younger than an hour: the split
    harvester may be mid-fetch on its own schedule, and its spool file is not ours to delete."""
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


def _fetch_and_sign(url, job, duration=None):
    """Fetch one candidate, decode it, sign it. THE CHILD'S WHOLE JOB. Returns `result.json`.

    `duration` is the player's declared length for this URL when one is known, so the refusal in
    `_decode_and_sign` can weigh the same facts the queue door weighed. It is optional: a URL
    typed by hand has no trustworthy length, and inventing one would be worse than going without.

    The decoded PCM goes to a FILE in the job directory, written by ffmpeg itself. The old path
    piped it through `communicate()`, which accumulates a chunk list and then joins it -- two full
    copies of the audio in the parent's heap at the moment of the join, 1,034 MB for 451 MB of
    PCM. Writing to a file and reading it back with `np.fromfile` is exactly one allocation, at
    the size the decode turned out to be.

    A signature is written ONLY when yt-dlp exited 0, ffmpeg exited 0, the spool is long enough,
    and no stop was requested. A truncated decode would produce a perfectly plausible, permanently
    wrong recipe-1 signature for this URL -- cached, uploaded, and never fetched again.
    """
    try:
        return _decode_and_sign(url, job, duration)
    finally:
        _STOP["part"] = None            # nothing left for the signal handler to clean up
        _STOP["procs"] = []


def _decode_and_sign(url, job, duration=None):
    """`_fetch_and_sign`'s body -- see there. Split out only so the stop state is always reset."""
    # THE SECOND DOOR, and the last one before ffmpeg. `listen_queue_split` already refuses an
    # entry this long, but it is not the only way a URL arrives here -- a hand-run `--fetch-one`,
    # a working queue written before this rule existed, a caller yet to be written. Same predicate
    # as the queue door, given the same facts: `duration` is the queue's own, carried across the
    # fork (see `_run_fetch_child`). Refused before anything is spawned: nothing to stop, nothing
    # to clean up.
    if too_long(url, duration):
        span = media_fragment(url)
        why = ("%s in one slice" % _hours(span[1] - span[0])) if span else _hours(duration)
        return {"ok": False, "error": "too long: %s -- refused, not truncated" % why}
    os.makedirs(job, exist_ok=True)
    part = os.path.join(job, "pcm.f32le.part")
    pcm = os.path.join(job, "pcm.f32le")
    _STOP["part"] = part
    started = time.time()

    # A chunk URL asks for one slice of the audio (see `media_fragment`). `-ss` goes BEFORE `-i`
    # and `-t` after it: ffmpeg then reads and discards the head of the pipe and stops at the
    # slice's end, so the PCM this process ends up holding is the chunk's alone -- which is what
    # splitting is for. yt-dlp still fetches the whole master, because it ignores the fragment;
    # the network cost is the same until a cache can hand over the cut file, but MEMORY, the
    # constraint that made chunks necessary, is bounded either way.
    cut = media_fragment(url)
    ff_argv = ["ffmpeg", "-v", "error"]
    if cut:
        ff_argv += ["-ss", str(cut[0])]
    ff_argv += ["-i", "pipe:0"]
    if cut:
        ff_argv += ["-t", str(cut[1] - cut[0])]
    ff_argv += ["-ac", "1", "-ar", str(_audio.SR), "-f", "f32le", "pipe:1"]

    with open(part, "wb") as spool:
        yt = subprocess.Popen(["yt-dlp", "-q", "--no-warnings", "--no-playlist"] + cookie_args()
                              + ["-f", "bestaudio", "-o", "-", url],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        ff = subprocess.Popen(ff_argv,
                              stdin=yt.stdout, stdout=spool, stderr=subprocess.PIPE)
        yt.stdout.close()
        _STOP["procs"] = [ff, yt]              # the handler stops them in THIS order
        yt_err, ff_err = [], []
        drains = [threading.Thread(target=_drain, args=(yt.stderr, yt_err), daemon=True),
                  threading.Thread(target=_drain, args=(ff.stderr, ff_err), daemon=True)]
        for t in drains:
            t.start()
        _wait(ff)
        _wait(yt)
        for t in drains:
            t.join(timeout=5)
    _STOP["procs"] = []

    if _stop_requested():
        _unlink(part)
        return {"ok": False, "error": "stopped"}

    size = os.path.getsize(part) if os.path.exists(part) else 0
    n_samples = size // 4
    if yt.returncode != 0 or size == 0:
        _unlink(part)
        return {"ok": False, "error": _last_line(yt_err) or "no audio"}
    if ff.returncode != 0:
        _unlink(part)
        return {"ok": False,
                "error": _last_line(ff_err, "ffmpeg: ") or "ffmpeg: exit %s" % ff.returncode}
    if n_samples < chroma_recipe.MIN_SECONDS * _audio.SR:
        _unlink(part)
        return {"ok": False, "error": "too short (%.0fs)" % (n_samples / _audio.SR)}

    os.replace(part, pcm)
    _STOP["part"] = None
    y = np.fromfile(pcm, dtype="float32")      # ONE allocation, at the final size
    if _stop_requested():
        return {"ok": False, "error": "stopped"}

    c = chroma_recipe.compute_chroma(y)        # THE recipe, in one place (chroma_recipe.py)
    del y
    if _stop_requested():                      # nothing half-decoded reaches the cache
        return {"ok": False, "error": "stopped"}

    os.makedirs(CACHE, exist_ok=True)
    np.save(sig_path(url), c.astype(chroma_recipe.STORE_DTYPE))
    # The bucket is the signature's long-term home (see sigstore). Upload now, verified; on
    # failure the local file simply stays -- eviction never fires for an unverified key, so a
    # flaky upload costs disk space, never data.
    if sigstore.enabled():
        sigstore.put(sig_path(url), _sig_key(url))
    # The float32 chroma, for the parent's matcher. NOT the float16 round-trip: the matcher scores
    # float32 today, and a float16 cast and back moves values by an ULP, which is enough to move a
    # borderline verdict. Not a signature change either way -- the signature is the file above.
    np.save(os.path.join(job, "chroma32.npy"), c)

    current, peak = memwatch.footprint_mb()
    return {"ok": True, "error": None, "n_samples": int(n_samples),
            "seconds": round(n_samples / _audio.SR, 1), "took_s": round(time.time() - started, 1),
            "footprint_mb": current, "peak_mb": peak, "footprint_kind": memwatch.kind()}


def _run_fetch_child(url, job, duration=None):
    """Spawn `harvest.py --fetch-one URL --job DIR` and read back its result."""
    env = dict(os.environ)
    # macOS libmalloc caches freed LARGE blocks inside the process instead of returning them to
    # the kernel, so the harvester's footprint never came down between candidates and those dirty
    # pages ended up compressed and swapped. Measured on this OS: 764 MB retained of 800 MB
    # allocated and freed; 0 MB with this set. (MallocSpaceEfficient=1 measured the same; this is
    # the one the memory eval standardised on.) libmalloc reads it at process START, so it has to
    # be on the child's environment -- setting it from inside a running process does nothing.
    env.setdefault("MallocLargeCache", "0")
    # NEVER put `--run` in this argv. The player's supervisor finds a live harvester by looking
    # for a `harvest.py` command line that also contains `--run`, and it would adopt a fetch child
    # as the harvester itself -- then refuse to start the real one, and later signal the wrong
    # process group.
    argv = [sys.executable, os.path.abspath(__file__), "--fetch-one", url, "--job", job]
    # THE POINT OF CARRYING THIS ACROSS. A check is only as good as the information it is given,
    # and a boundary that drops an input silently converts a real check into a decorative one.
    # The queue door refuses on the entry's declared duration; the child is a different process
    # and knows only what this argv tells it, so without `--duration` it would re-run the SAME
    # predicate on strictly less information and wave through a six-hour master the parent had
    # already refused -- a check that looks like defence in depth and is theatre. Anything the
    # parent would refuse must still be refused after the fork, so the fact travels with the URL.
    #
    # Absent when there is no trustworthy length (a hand-run fetch). That is honest rather than
    # broken: the fragment span still applies, because it needs no outside information.
    if duration is not None:
        argv += ["--duration", repr(duration)]
    try:
        child = subprocess.Popen(argv, cwd=HOME, env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        return {"ok": False, "error": "could not start the fetch child: %s" % exc}
    # Same process group as us on purpose (no start_new_session), so the supervisor's killpg
    # reaches the parent, this child, yt-dlp and ffmpeg together.
    _STOP["child"] = child
    try:
        _out, err = child.communicate()
    finally:
        _STOP["child"] = None

    if child.returncode in (130, 143):          # 128 + SIGINT / SIGTERM
        return {"ok": False, "error": "stopped"}
    result = _load(os.path.join(job, "result.json"), None)
    if child.returncode != 0 or not isinstance(result, dict):
        tail = _last_line([err or b""])
        return {"ok": False,
                "error": ("child failed (exit %s): %s" % (child.returncode, tail))[:160]}
    return result


# The last fetch child's result.json. The memory rows (see `record_memory`) want the child's peak
# footprint, and stream_chroma's three callers want its three-tuple unchanged, so the extra fields
# ride here rather than on the return value.
_LAST_CHILD = {}


def stream_chroma(url, duration=None):
    """Stream the audio, reduce it to a chroma signature -> (chroma, samples, error).

    Unchanged as a contract: the three callers (`run()`, `harvester.work_once()`, and the live
    canary) see the same three-tuple and the same error strings as before.

    `duration` is the player's declared length for this URL, which the working queue does not
    carry (it holds bare URLs) -- callers get it from `queue_duration`. It exists so the refusal
    inside the child weighs what the queue door weighed; see `_run_fetch_child`. Optional, and
    None is a perfectly good answer: no length is no evidence of length.

    What changed is where the work happens. The fetch, the decode and the recipe now run in a
    CHILD process, so the memory they need dies with it; this process never holds a candidate's
    audio or the recipe's working set. `samples` is a memory map over the child's decoded PCM,
    which behaves like the array it used to be -- `write_excerpt` slices ~30 seconds out of it and
    `harvester.py` writes it to a FLAC job copy. The spool file is unlinked as soon as it is
    mapped, so the disk space comes back when the caller drops `samples`, and a crash anywhere
    after this point leaves nothing behind.

    Set NETRADIO_HARVEST_CHILD=0 to run the fetch in this process instead. That is for diagnosing
    an environment problem in the child, not for normal use: it brings the memory back with it.
    """
    _LAST_CHILD.clear()
    job = job_dir(url)
    shutil.rmtree(job, ignore_errors=True)          # a stale job dir for this URL is not ours
    os.makedirs(job, exist_ok=True)
    try:
        if os.environ.get("NETRADIO_HARVEST_CHILD") == "0":
            result = _fetch_and_sign(url, job, duration)
        else:
            result = _run_fetch_child(url, job, duration)
        _LAST_CHILD.update(result)
        if not result.get("ok"):
            return None, None, result.get("error") or "no signature"
        c = np.load(os.path.join(job, "chroma32.npy"))
        samples = np.memmap(os.path.join(job, "pcm.f32le"), dtype="float32", mode="r")
        return c, samples, None
    finally:
        # POSIX keeps an unlinked file alive for as long as something has it open or mapped, so
        # the memmap above stays readable and the space is reclaimed when `samples` is dropped.
        shutil.rmtree(job, ignore_errors=True)


# A short excerpt AROUND the matched instant is all we retain -- long enough to recognise the
# record by ear, far too short to be a copy of it. This is not a library; it is a magnifying
# glass held over the exact moment the matcher flagged, so a human can confirm or reject it.
EXCERPT_S = 30.0


def write_excerpt(samples, at_s, path):
    """Write ~EXCERPT_S seconds of `samples` centred on the matched instant. In memory in, file
    out -- no second fetch. A brief excerpt for aural verification, swept after KEEP_TTL_DAYS."""
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
        return                                   # nothing to hear; do not leave an empty file

    os.makedirs(os.path.dirname(path), exist_ok=True)
    sf.write(path, clip, _audio.SR)
    _write_provenance()


def purge_audio():
    """Throw away every retained excerpt. The LEADS survive.

    Retained audio turned out to be the weakest part of this design. It is the only thing here that
    is a copy of someone's record, it is the thing that went wrong (full-length mixes were retained
    instead of excerpts), and it is not actually needed: a lead is a URL, and a URL can be listened
    to at the source. So the audio goes, and `/harvest` plays the candidate from an embed instead.

    What survives is everything that took work to compute -- the url, the cost, the mystery it
    matched, the key it matched in, and WHERE in the candidate it matched. Nothing is re-fetched and
    nothing is re-analysed; the chroma signatures (which are not audio) are untouched, so no
    candidate will ever be downloaded twice.
    """
    freed = n = 0
    if os.path.isdir(KEEP):
        for name in os.listdir(KEEP):
            if not name.lower().endswith((".wav", ".mp3", ".flac", ".m4a")):
                continue                      # leave PROVENANCE.txt alone
            path = os.path.join(KEEP, name)
            try:
                freed += os.path.getsize(path)
                os.unlink(path)
                n += 1
            except OSError:
                pass

    state = _load(STATE, blank_state())
    for m in state.get("matches") or []:
        m.pop("audio", None)                  # the lead stays; the copy does not
    state["kept"] = 0
    _save(STATE, state)
    return "purged %d retained files (%.1f GB). %d leads kept -- review them by embed at /harvest." % (
        n, freed / 1e9, len(state.get("matches") or []))


def _sig_key(url):
    """The signature's filename — a stable id for "this candidate's chroma", 20 chars not a URL."""
    return os.path.basename(sig_path(url))


_REMOTE_KEYS = {"at": 0.0, "keys": None}     # session cache of the bucket's key listing


def _remote_keys(max_age_s=900):
    """The bucket's signature keys, cached for a while -- None when the store is dark or the
    listing failed (callers must not treat that as 'empty')."""
    if not sigstore.enabled():
        return None
    now = time.time()
    if _REMOTE_KEYS["keys"] is not None and now - _REMOTE_KEYS["at"] < max_age_s:
        return _REMOTE_KEYS["keys"]
    keys = sigstore.list_keys()
    if keys is not None:
        _REMOTE_KEYS.update(at=now, keys=keys)
    return _REMOTE_KEYS["keys"]


def stamp_pool(state):
    """Record the BUCKET's signature count on the state, for /harvest. Post-migration the
    bucket is the pool's only home, so counting local `.npy` undercounts by thousands (the
    working cache holds work-in-progress, ~1 file). Rides `_remote_keys()`'s ≤15-min session
    cache -- no extra bucket listing, and the player never needs AWS creds; when the store is
    dark or the listing failed, the previous stamp (with its honest `at`) is left standing.
    Returns True when the stamp changed, so a caller with no other reason to save knows this
    one is worth persisting.

    Also stamps the count's BREAKDOWN, because the bare number confused exactly the person it
    was for (a bucket bigger than the scored ledger read as loss; it was the opposite):
      * `retired`  -- signatures whose candidate the search is permanently done with (the
                      RULED_ON flags + own-clips, the same retirement `unscored_pairs` honours:
                      heard, discarded, ignored, duplicate, not_a_match);
      * `canary`   -- the live self-test's known record, uploaded like any other but never a
                      candidate;
      * `active`   -- the rest: signatures still in play for every future mystery.
    Sig keys are content-addressed from the URL, so membership is a hash, not a fetch.
    The three always partition `count` (the canary is excluded from `retired` even when its
    URL is also in the queue). If the QUEUE cannot be read, the breakdown is omitted rather
    than fabricated -- and when the count did not move either, the prior stamp stands whole."""
    remote = _remote_keys()
    if remote is None:
        return False
    prev = state.get("pool") or {}
    pool = {"count": len(remote), "at": _now()}
    _, retired_urls, queue_ok = listen_queue_split_checked()
    if queue_ok:
        canary_url = (os.environ.get("NETRADIO_CANARY_URL") or "").strip()
        canary_key = _sig_key(canary_url) if canary_url else None
        canary = 1 if canary_key and canary_key in remote else 0
        retired_keys = {_sig_key(u) for u in retired_urls}
        retired_keys.discard(canary_key)
        retired = len(retired_keys & remote)
        pool.update(retired=retired, canary=canary,
                    active=pool["count"] - retired - canary)
    elif prev.get("count") == pool["count"]:
        return False              # queue dark, count unmoved: the honest prior stamp stands
    state["pool"] = pool
    return any(pool.get(k) != prev.get(k)
               for k in ("count", "retired", "canary", "active"))


def _load_sig(url):
    """A signature by hook or by crook: the working cache first, then the bucket. None if it
    exists in neither (i.e. this URL genuinely needs its audio fetched)."""
    path = sig_path(url)
    if not os.path.exists(path) and sigstore.enabled():
        if not sigstore.fetch(_sig_key(url), CACHE):
            return None
    try:
        return np.load(path).astype("float32")
    except (OSError, ValueError):
        return None


def unscored_pairs(state, q, retired, qs, limit=None):
    """Every (mystery, cached-signature) pair we have NOT scored yet.

    The harvester only ever walked `pending`. Once a URL reached `done` it was never looked at
    again -- so a mystery whose clip arrives LATER was scored only against candidates fetched after
    it. Every signature gathered before that point, which is the entire corpus built over weeks,
    was silently never tested against it. Tim assumed the opposite, reasonably.

    A chroma signature is not tied to the question you asked of it: the same 12xN matrix answers
    MT4 today and MT8 next month, for free and with no network. So the pairing is what we track --
    `state["scored"][mystery] = [signature keys]` -- and anything unpaired is work to do.

    Skips anything ruled on: a `not_a_match` is not a match for ANYTHING we are waiting for.
    """
    scored = state.setdefault("scored", {})
    out = []
    for num, qc, qkey in qs:
        seen = set(scored.get(qkey, []))
        for url in q.get("done") or []:
            if url in retired:
                continue
            key = _sig_key(url)
            if key in seen:
                continue
            # A signature counts as HELD if it is in the working cache OR the bucket -- eviction
            # (sigstore) moves cold ones out of the cache, and _load_sig pulls them back to score.
            if not os.path.exists(sig_path(url)):
                remote = _remote_keys()
                if remote is None or key not in remote:
                    continue
            out.append((num, qc, qkey, url, key))
            if limit and len(out) >= limit:
                return out
    return out


# Above this fraction of the corpus missing, requeue_missing_sigs refuses to act on its own.
# A few lost signatures is routine wear (a crash mid-write, a bad eviction) and re-fetching is
# exactly what the queue is for. A loss bigger than this means the STORE broke -- a wiped cache
# AND absent bucket keys, a wrong endpoint, a wrong profile -- and mass re-fetching hundreds of
# tracks would hammer hosts for days while destroying the evidence of what went wrong. So past
# the cap it REPORTS and stands still; a human raises NETRADIO_REQUEUE_MISSING_CAP deliberately
# (e.g. =1) if the loss turns out to be real. (Policy: Tim, 2026-07-30.)
REQUEUE_MISSING_CAP = 0.10


def _requeue_cap():
    try:
        return float(os.environ.get("NETRADIO_REQUEUE_MISSING_CAP", "") or REQUEUE_MISSING_CAP)
    except ValueError:
        return REQUEUE_MISSING_CAP


def requeue_missing_sigs(state, q, retired):
    """Move `done` URLs whose signature is LOST -- in neither the working cache nor the bucket --
    back to `pending`, so the ordinary fetch path regenerates them.

    `done` is never re-fetched, so before this existed a lost signature left its candidate
    permanently dark to every FUTURE mystery -- in the worst case the whole pre-wipe corpus
    (the resurfaced §7.d item). Regeneration is automatic: at the start of every run, and on
    demand via --requeue-missing-sigs. `state["scored"]` is left alone on purpose -- the sig
    key is content-addressed from the URL and the recipe is deterministic, so old pairings
    stay valid and only unmet mysteries score the regenerated signature.

    Two refusals, both deliberate:
      * the bucket cannot be LISTED -> do nothing at all. An evicted-cold signature lives only
        in the bucket; with the listing dark, "lost" and "evicted" are indistinguishable, and
        requeuing evicted sigs would re-fetch the whole cold corpus for nothing.
      * more than the cap is missing -> report, do not requeue (see REQUEUE_MISSING_CAP above).
        The report is a standing `state["sig_alert"]` block (cleared here the moment the
        condition stops holding) plus one `issues` row when it first arises -- not one per
        supervisor respawn.

    Mutates `q` and `state` in place; the CALLER saves whichever the result says changed.
    Returns {"checked", "missing", "requeued", "reported", "cleared", "why"}.
    """
    res = {"checked": 0, "missing": 0, "requeued": 0, "reported": False, "cleared": False}
    done = [u for u in (q.get("done") or []) if u not in retired]
    res["checked"] = len(done)
    if not done:
        return dict(res, why="nothing in done to check")

    remote = _remote_keys()
    if sigstore.enabled() and remote is None:
        return dict(res, why="bucket listing unavailable -- cannot tell lost from evicted, "
                             "so nothing was requeued; fix the store and re-check")
    remote = remote or set()

    missing = [u for u in done
               if not os.path.exists(sig_path(u)) and _sig_key(u) not in remote]
    res["missing"] = len(missing)

    if not missing:
        if state.pop("sig_alert", None) is not None:
            res["cleared"] = True                 # the loss was dealt with -- stand down
        return dict(res, why="every done signature is held (cache or bucket)")

    cap = _requeue_cap()
    frac = len(missing) / len(done)
    if frac > cap:
        why = ("%d of %d done signatures are LOST (%.0f%% > the %.0f%% cap) -- NOT requeuing: "
               "a loss that size means the store broke, not the files. Check the bucket "
               "endpoint/profile and the cache dir; if the loss is REAL, the deliberate "
               "override is: NETRADIO_REQUEUE_MISSING_CAP=1 .venv/bin/python "
               "scripts/harvest.py --requeue-missing-sigs"
               % (len(missing), len(done), frac * 100, cap * 100))
        first = "sig_alert" not in state
        state["sig_alert"] = {"at": _now(), "missing": len(missing), "corpus": len(done),
                              "why": why}
        if first:
            state.setdefault("issues", [])
            state["issues"] = (state["issues"] + [{"at": _now(),
                                                   "issue": "missing-sigs: " + why}])[-50:]
        return dict(res, reported=True, why=why)

    missing_set = set(missing)
    q["done"] = [u for u in q["done"] if u not in missing_set]
    pend = set(q.get("pending") or [])
    q["pending"] = (q.get("pending") or []) + [u for u in missing if u not in pend]
    if state.pop("sig_alert", None) is not None:
        res["cleared"] = True
    # Leave a visible record on /harvest (its issues list): routine self-healing should
    # still be SEEN -- losing signatures at all is worth a raised eyebrow, even when the
    # recovery needs no human. Not a notice: only the past-the-cap alert reddens a button.
    state.setdefault("issues", [])
    state["issues"] = (state["issues"] + [{"at": _now(),
                                           "issue": "missing-sigs: requeued %d lost "
                                                    "signature(s) for re-fetch" % len(missing)}])[-50:]
    return dict(res, requeued=len(missing),
                why="requeued %d done URL(s) whose signature was lost -- the fetch path will "
                    "regenerate them" % len(missing))


def note_no_queries(state, qs):
    """Keep the "nothing to search for" state truthful for WHICHEVER runtime just refreshed
    the query set (Mode A's run(), or the split collector each pass).

    An empty query set is a first-class state, not a print-and-vanish: in Mode A the
    process EXITS and the supervisor respawns it in a loop, and before this stamp /harvest
    kept showing the LAST session's stale phase ("working") with no explanation while the
    queue page's button correctly went red. Stamped once (the `at` is when it AROSE, like
    sig_alert), and it stands down by itself the moment a refresh finds something
    searchable. Returns True when the state changed (worth persisting)."""
    if not qs:
        already = ("no_queries" in state
                   and (state.get("session") or {}).get("phase") == "nothing to search for")
        if already:
            return False                      # standing -- an idle pass is not worth a write
        state["no_queries"] = {"at": _now(),
                               "why": "no unsolved mysteries with a usable clip -- nothing to "
                                      "search for. See the searching table: every mystery is "
                                      "either solved, clipless, or its clip was refused."}
        state["session"] = {"phase": "nothing to search for", "until": 0}
        return True
    return state.pop("no_queries", None) is not None


def recover_missing_sigs_at_start(state=None):
    """Lost-signature recovery at WRITER startup -- the one entry point all three writers
    share: run() (Mode A), collector.run() (split mode), and --requeue-missing-sigs (on
    demand). The caller must already hold the writer lock.

    Pass the caller's live `state` when it keeps one across a session (run() does, and a
    later _save from it would clobber rows written by an independent load here); leave it
    None to load-and-save independently (the collector reloads state every pass, the CLI
    holds nothing).
    """
    if state is None:
        state = _load(STATE, blank_state())
    q = _load(QUEUE, {"pending": [], "done": []})
    _, retired = listen_queue_split()
    rq = requeue_missing_sigs(state, q, retired)
    if rq["requeued"]:
        _save(QUEUE, q)
        print("# %s" % rq["why"])
    if rq["requeued"] or rq["reported"] or rq["cleared"]:
        _save(STATE, state)               # the requeue also records an issues row
        if rq["reported"]:
            print("!! %s" % rq["why"])
    return rq


def score_cached(state, num, qc, qkey, url, key):
    """Score one cached signature against one mystery. No network, ~0.06s. Returns a hit or None.

    Updates an existing row rather than duplicating it -- which is also how the missing `at_s`
    gets filled in. Every match saved before the harvester recorded WHERE it hit has `at_s: None`,
    so `/harvest` could not cue the link and Tim had to hunt through a 108-minute mix by hand.
    The position was never lost: it is recomputable from the signature we already hold.
    """
    c = _load_sig(url)
    if c is None:
        return None
    cost, shift, at = _cm.match(qc, c)
    state.setdefault("scored", {}).setdefault(qkey, []).append(key)

    if cost is None or cost > KEEP_CEILING:
        return None
    verdict = "MATCH" if cost <= MATCH_COST else "near"
    for m in state["matches"]:
        if m.get("url") == url and m.get("mystery") == num:
            m.update(cost=round(float(cost), 4), semitones=shift,
                     at_s=round(float(at or 0), 1), verdict=verdict)
            return m
    hit = {"at": _now(), "mystery": num, "cost": round(float(cost), 4),
           "semitones": shift, "at_s": round(float(at or 0), 1), "url": url,
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
    state["matches"] = [m for m in (state.get("matches") or []) if m.get("mystery") != num]
    scored = state.setdefault("scored", {})
    dropped_keys = [k for k in scored if k.split(":", 1)[0] == str(num)]
    for k in dropped_keys:
        del scored[k]
    return before - len(state["matches"]), len(dropped_keys)


def rescan(state, q, retired, qs, limit=None, verbose=True):
    """Work through the unscored pairs. Returns how many were scored."""
    pairs = unscored_pairs(state, q, retired, qs, limit=limit)
    for num, qc, qkey, url, key in pairs:
        hit = score_cached(state, num, qc, qkey, url, key)
        if hit and verbose:
            a = int(hit.get("at_s") or 0)
            print("  %s  MT%d  cost %.4f  at %d:%02d  %s  (from cache -- no fetch)"
                  % (hit["verdict"], num, hit["cost"], a // 60, a % 60, url))
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
        try:
            os.unlink(path)
            state["kept"] -= 1
        except OSError:
            pass


def _write_provenance():
    """State, in plain words, what the kept files are -- so nobody, including a future me, ever
    mistakes this directory for a music library."""
    note = os.path.join(KEEP, "PROVENANCE.txt")
    if os.path.exists(note):
        return
    os.makedirs(KEEP, exist_ok=True)
    with open(note, "w", encoding="utf-8") as fh:
        fh.write(
            "These are SHORT EXCERPTS (~%ds), retained TEMPORARILY so a human can listen and "
            "confirm or reject a track-identification hypothesis.\n\n"
            "This is not a music library. Full tracks are never kept -- the harvester streams "
            "audio, reduces it to a chroma signature, and drops it. Only the matched ~%d-second "
            "window of a near-miss is written here, and it is swept after %d days.\n\n"
            "If an excerpt confirms a record, ACQUIRE THE RECORD (buy it, or rip your own copy). "
            "Do not promote an excerpt into a source file.\n"
            % (int(EXCERPT_S), int(EXCERPT_S), KEEP_TTL_DAYS))


def sweep_excerpts():
    """Delete kept excerpts older than KEEP_TTL_DAYS. A lead you haven't listened to in a month
    is not a lead, and holding it any longer serves no purpose."""
    if not os.path.isdir(KEEP):
        return
    cutoff = time.time() - KEEP_TTL_DAYS * 86400
    for name in os.listdir(KEEP):
        if not name.endswith(".wav"):
            continue
        path = os.path.join(KEEP, name)
        try:
            if os.path.getmtime(path) < cutoff:
                os.unlink(path)
        except OSError:
            pass


def drop_ruled_excerpts(state, retired):
    """A ruled-on lead loses its audio; the numbers stay. Returns how many were dropped.

    The excerpt exists for exactly one purpose: to let a human confirm or reject the lead by ear.
    Once the ruling is made -- match, not-a-match, heard, any of RULED_ON -- that purpose is spent,
    and holding the audio a day longer serves nothing. The lead itself survives whole (url, cost,
    mystery, key, at_s, verdict): the SCORE is the record; the audio was only ever the evidence.

    Runs on every pass, right after the listen queue is re-read, so a ruling made at /harvest
    takes effect within one loop iteration. The TTL sweep above remains the backstop for anything
    ruled while the harvester was off.
    """
    dropped = 0
    for m in state.get("matches") or []:
        path = m.pop("audio", None) if m.get("url") in retired else None
        if not path:
            continue
        try:
            os.unlink(path)
        except OSError:
            pass                       # already gone -- the row still stops carrying it
        state["kept"] = max(0, state.get("kept", 0) - 1)
        dropped += 1
    return dropped


# --- the queue ---------------------------------------------------------------------------------

def enumerate_channel(url, limit=None):
    """Track URLs on a channel/playlist -- metadata only, NO audio. One cheap request."""
    cmd = ["yt-dlp", "-q", "--no-warnings", "--flat-playlist", "--print", "%(url)s", url]
    if limit:
        cmd += ["--playlist-end", str(limit)]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    return [u.strip() for u in out.stdout.split("\n") if u.strip().startswith("http")]


# Tim's own channel publishes the Mystery Track clips themselves. A harvester that "finds" one
# there has found nothing -- it has rediscovered its own question, and would report a triumphant
# 0.00 match. Never queue it.
EXCLUDE_CHANNELS = ("UCuYTatE2k5dOV8J8Bi3rK0g",)   # Tim Hunter

# The player, which is the SINGLE WRITER of the listen queue.
PLAYER_URL = os.environ.get("NETRADIO_PLAYER_URL", "http://127.0.0.1:8765")


def add_to_queue(urls, source):
    """Queue candidates by handing them to the PLAYER, not by writing our own queue.

    This used to append straight into `.harvest/queue.json`, and that was the bug. It gave the
    harvester a second, private door that the listen queue knew nothing about: 400 candidates got
    in that way, invisible at /queue, untagged, and impossible to remove when a channel turned out
    to be feeding us noise. Tim found it by adding a video by hand and getting no duplicate warning
    for a record we had already analysed.

    So there is now ONE door. Everything enters through the listen queue, tagged with where it came
    from, and `sync_listen_queue()` folds it back into our working queue on the next pass. We only
    ever READ that file -- the player owns it, and two writers on one JSON file is how you lose the
    file -- so we ask the player over HTTP and let it do the write.

    A dead player is a hard failure, not a silent fallback to the private queue: falling back is
    precisely how the candidates went dark in the first place.
    """
    if any(c in (source or "") for c in EXCLUDE_CHANNELS):
        print("refusing to queue %s -- it publishes the mystery clips themselves" % source)
        return 0

    origin = "seed-channel:%s" % (source or "unknown")
    added = 0
    for url in dict.fromkeys(urls):
        body = json.dumps({"url": url, "origin": origin}).encode()
        req = urllib.request.Request(PLAYER_URL.rstrip("/") + "/api/queue/add", data=body,
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                for res in (json.loads(r.read()) or {}).get("results") or []:
                    if res.get("status") == "new":
                        added += 1
                    elif res.get("status") == "refused":
                        print("  refused: %s -- %s" % (url, res.get("why")))
        except urllib.error.URLError as e:
            raise SystemExit(
                "cannot reach the player at %s (%s).\n"
                "Candidates are queued THROUGH the player now -- it owns the listen queue and is\n"
                "its only writer. Start it (scripts/run_player.sh start), or set NETRADIO_PLAYER_URL."
                % (PLAYER_URL, e))
    return added


# --- the listen queue: one queue, two stores -----------------------------------------------------
#
# The player owns `listen_queue.json`; we only ever READ it. Two writers on one JSON file is how
# you lose the file, so the harvester keeps its own working queue in `.harvest/queue.json` and
# merely SYNCS from the player's -- new unheard entries flow in, and anything a human has since
# ruled on flows out of pending. Subscriptions therefore feed the search automatically, which was
# the whole point: before this, subscriptions fed one queue and the harvester worked from another.
#
# Nothing here writes to the player's file. If the env var is unset (a harvester run by hand,
# without the player) this whole path is simply inert.
#
# NETRADIO_LISTEN_QUEUE may name any of three layouts -- we read all of them, still read-only:
#   * the legacy single `listen_queue.json` (a {"items": [...]} object), or a rendered merged
#     single-file view of the same shape;
#   * the player's sharded DIRECTORY (the queue the player migrated to), or that directory's
#     `index.json` manifest named directly -- a manifest listing `shard-NNNN.json` files, each a
#     bare JSON array of items, which we read in manifest order and concatenate.
# We tell them apart by inspecting the path (a dir, or a basename of `index.json`), not a new env
# var -- the player owns the layout, and the harvester should follow it wherever it goes.

LISTEN_QUEUE = os.environ.get("NETRADIO_LISTEN_QUEUE", "")

# A human ruling retires an entry from the search. `duplicate` is the same audio as another entry;
# `ignored` was rejected outright.
#
# `not_a_match` is the important one, and it is DELIBERATELY GLOBAL: it means "this record is not
# any Mystery Track" -- including the mysteries whose clips do not exist yet. That is what makes
# `rescan()` below safe. Without it, the day MT8's clip lands, every record Tim has already
# listened to and rejected would be scored again and handed straight back to him.
#
# Note `not_a_match` does NOT imply `listened`: you can rule a record out as a match and still want
# to hear it. The player keeps those two verdicts apart (see listen_queue_store.mark_not_a_match).
RULED_ON = ("listened", "discarded", "ignored", "duplicate", "not_a_match")


# Tim's own channel, as it appears in a listen-queue entry's `origin`.
OWN_ORIGINS = ("tim hunter", "trjh", "UCuYTatE2k5dOV8J8Bi3rK0g")


def _is_own_clip(item):
    """Ours? Then never analyse it: we would rediscover our own question and report ~0.00.

    This used to match ONLY the title `Mystery Track N`, and justified that by claiming
    "listen-queue entries carry no channel or uploader field, only a title." That was false --
    they carry `origin`, which names the subscription that produced them -- and the cost of the
    mistake was real: NINE of his uploads sat in the pending queue, uncaught, because they are
    titled "ID #1", "ID #2" and "Wave Forms [in the mix, low quality]". Not one of them contains
    the word "mystery". The last is an excerpt of the mix itself.

    So check WHO uploaded it first, and keep the title check only as a second net -- narrowly,
    because real records are called things like "No Mystery" and "Mystery Blend".
    """
    origin = item.get("origin")
    origin = origin.strip().lower() if isinstance(origin, str) else ""
    if any(o.lower() in origin for o in OWN_ORIGINS):
        return True
    title = item.get("title")
    title = title.strip().lower() if isinstance(title, str) else ""
    return title.startswith("mystery track") or title.startswith("netradio mystery")


def _load_queue_items():
    """The listen queue's flat items list, whatever layout NETRADIO_LISTEN_QUEUE points at.

    Single file -> read its `items`. Sharded (a directory, or an `index.json` manifest named
    directly) -> read the manifest and concatenate the items of each `shard-NNNN.json` it names,
    in manifest order (each shard is a bare JSON array of items -- the player's committed
    layout). Raises OSError/ValueError on a missing/torn/mid-write/wrong-shaped file -- the
    caller turns that into "empty, try again next pass".
    """
    if os.path.isdir(LISTEN_QUEUE):
        manifest_path = os.path.join(LISTEN_QUEUE, "index.json")
    elif os.path.basename(LISTEN_QUEUE) == "index.json":
        manifest_path = LISTEN_QUEUE
    else:
        manifest_path = None             # a plain file -> the legacy single-file layout

    if manifest_path is None:
        with open(LISTEN_QUEUE, "r", encoding="utf-8") as fh:
            items = (json.load(fh) or {}).get("items") or []
    else:
        shard_dir = os.path.dirname(manifest_path)
        with open(manifest_path, "r", encoding="utf-8") as fh:
            manifest = json.load(fh) or {}
        # Shape errors are ValueError on purpose: syntactically-valid-but-wrong JSON must
        # land in the caller's "try again next pass" net, not crash the harvester.
        if not isinstance(manifest, dict):
            raise ValueError("manifest is not an object")
        items = []
        for entry in manifest.get("shards") or []:
            if not isinstance(entry, dict):
                raise ValueError("manifest shard entry is not an object")
            name = entry.get("name")
            if name is None:
                continue
            # Only the committed filename form may be opened -- a bare `shard-NNNN.json`. This is
            # containment, not just validation: `name` is joined to the queue dir, so a traversal
            # ("../x.json") or absolute path in a tampered manifest would read an UNRELATED file
            # as the queue. Anything else = corrupt manifest = retry next pass.
            if not isinstance(name, str) or not re.fullmatch(r"shard-\d+\.json", name):
                raise ValueError("manifest shard name is not shard-NNNN.json")
            path = os.path.join(shard_dir, name)
            # ...and the file itself must LIVE in the queue dir: a canonically-named symlink
            # pointing elsewhere would defeat the containment the name check promises.
            if os.path.realpath(path) != os.path.join(os.path.realpath(shard_dir), name):
                raise ValueError("shard %s resolves outside the queue directory" % name)
            with open(path, "r", encoding="utf-8") as fh:
                chunk = json.load(fh)
            if not isinstance(chunk, list):
                raise ValueError("shard %s is not a list" % name)
            items.extend(chunk)
    if not isinstance(items, list):
        raise ValueError("listen queue items is not a list")
    # One corrupt entry must not starve the harvester of the other thousands: drop non-object
    # items rather than failing the whole read (a torn file can't produce these -- that's a
    # JSON parse error -- so this is programmatic corruption, tolerated per-item).
    return [it for it in items if isinstance(it, dict)]


def _is_cooling(item):
    """True while the item's `retry_after` (ISO `YYYY-MM-DD`) is still in the FUTURE.

    Mirrors the player's rule: a URL a recent fetch failed on is held back from the network until
    its date passes, and NOTHING else changes. Only the CANONICAL form the player writes
    (zero-padded `YYYY-MM-DD`, then a real calendar date) may cool: the player stamps dates with
    isoformat(), so anything else is corruption, and the safe failure mode for corruption is
    not-cooling -- one wasted probe beats "2999-1-1" suppressing a URL until 2999. The compare is
    on parsed dates, never lexical ("tomorrow" > "2026-..." forever).
    """
    ra = item.get("retry_after")
    if not isinstance(ra, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", ra):
        return False
    try:
        ra_date = datetime.strptime(ra, "%Y-%m-%d").date()
    except ValueError:                   # right shape, impossible date ("2026-13-45")
        return False
    return ra_date > datetime.now(timezone.utc).date()


def listen_queue_split(issues=None):
    """(candidates, retired) from the player's listen queue. Read-only; never raises.

    Reads whichever layout NETRADIO_LISTEN_QUEUE names (single file, merged view, or sharded
    dir/manifest -- see _load_queue_items).
    """
    candidates, retired, _ = listen_queue_split_checked(issues)
    return candidates, retired


def listen_queue_split_checked(issues=None):
    """(candidates, retired, ok) -- listen_queue_split plus an honesty bit. ok=False means the
    queue is MISSING or UNREADABLE; an empty queue reads ok=True with empty results.

    Empty-on-failure is the right contract for the search loop (a weird queue is "try again
    next pass", not a dead harvester) -- but a publisher of DERIVED facts must not mistake
    "could not read" for "nothing there": stamp_pool uses the bit so a torn queue read can't
    republish every ruled-out signature as active.

    `issues`, when a list is passed, collects one `{"url": ..., "reason": ...}` row per entry
    refused here. A refusal is silent otherwise, and a silent refusal is indistinguishable from
    a queue that simply had nothing in it."""
    if not LISTEN_QUEUE or not os.path.exists(LISTEN_QUEUE):
        return [], set(), False
    try:
        items = _load_queue_items()
    except (OSError, ValueError, TypeError, AttributeError):
        # OSError/ValueError = missing/torn/mid-write/wrong-shaped (the explicit checks in
        # _load_queue_items raise ValueError). TypeError/AttributeError = the final net for any
        # shape this code did not think of -- the docstring says NEVER raises, so make it true;
        # a weird queue is "empty, try again next pass", not a dead harvester.
        return [], set(), False

    candidates, retired = [], set()
    for it in items:
        url = it.get("url")
        if not isinstance(url, str):     # a non-string url is a corrupt item, not a crash
            continue
        url = url.strip()
        if not url.startswith("http"):
            continue
        if any(it.get(f) for f in RULED_ON) or _is_own_clip(it):
            retired.add(url)             # a ruling wins over cooling: retirement is permanent-ish
        elif _is_cooling(it):
            continue                     # cooling gates the network only -- hold the URL back, but
                                         # do NOT retire it: it rejoins on its own once the date
                                         # passes, so nothing here drops it from pending.
        elif too_long(url, it.get("duration")):
            # Refused whole, never truncated: analysing the first four hours of a six-hour set
            # and filing the result under the URL is a partial answer wearing a complete one's
            # clothes. Not `retired` either -- no human ruled on it; the machine did.
            if issues is not None:
                issues.append({"url": url, "reason": "too_long"})
        else:
            candidates.append(url)
    return candidates, retired, True


# The player's declared durations, url -> seconds, cached for a few minutes.
_DURATIONS = {"at": 0.0, "by_url": None}


def queue_duration(url, max_age_s=300):
    """The player's declared length for `url`, or None when there is no trustworthy one.

    THE FETCH PATH HAS TO ASK FOR THIS. Our working queue holds bare URLs, so by the time a
    candidate is fetched the duration the queue door judged it on is simply gone -- and a
    predicate handed less information than it was designed for stops being the check it looks
    like. This is where the fetch path gets it back.

    Cached for a few minutes: the listen queue is re-read every pass regardless, one fetch takes
    minutes, and a recording's length does not change. Never raises -- same reason as
    `listen_queue_split`: the queue is data this process does not control.
    """
    now = time.time()
    if _DURATIONS["by_url"] is None or now - _DURATIONS["at"] > max_age_s:
        by_url = {}
        if LISTEN_QUEUE and os.path.exists(LISTEN_QUEUE):
            try:
                for it in _load_queue_items():
                    u, d = it.get("url"), it.get("duration")
                    if (isinstance(u, str) and isinstance(d, (int, float))
                            and not isinstance(d, bool)):
                        by_url[u.strip()] = d
            except (OSError, ValueError, TypeError, AttributeError):
                by_url = {}      # unreadable is not "zero seconds"; it is "no answer"
        _DURATIONS.update({"at": now, "by_url": by_url})
    return _DURATIONS["by_url"].get(url)


def sync_listen_queue(q, issues=None):
    """Fold the player's queue into ours. Returns (added, dropped); mutates `q` in place.

    Length is deliberately NOT a filter: a record can hide inside an hour-long DJ mix, and the
    match reports WHERE it hit (`at`), so a long mix is a feature, not a cost. The one exception
    is the MAX_DURATION_S backstop -- see `too_long`; those rows land in `issues` if a list is
    passed.
    """
    refused = []
    candidates, retired = listen_queue_split(refused)
    if issues is not None:
        issues.extend(refused)
    if not candidates and not retired and not refused:
        return 0, 0

    seen = set(q["pending"]) | set(q["done"])
    fresh = [u for u in candidates if u not in seen]
    q["pending"].extend(fresh)

    # Drop anything a human ruled on while it sat in our pending list. Not from `done` -- that is
    # our record of work completed, and re-adding a URL later must not re-analyse it.
    #
    # A refused entry leaves pending by the same door. A backstop that only stopped NEW arrivals
    # would still let a master that was queued before the split rule existed be fetched whole,
    # which is the one thing it is here to prevent. It cannot come back: it is not a candidate.
    gone = set(retired) | {r["url"] for r in refused}
    before = len(q["pending"])
    q["pending"] = [u for u in q["pending"] if u not in gone]
    return len(fresh), before - len(q["pending"])


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
    that is RE-CUT invalidates every pairing made against the old one, and every cached signature is
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

    if state is not None:                     # so /harvest can say what it is NOT asking, and why
        state["searching"] = [n for n, _, _ in out]
        state["skipped_queries"] = skipped
        # The CURRENT clip's query key per mystery. `state["scored"]` is keyed on these, and
        # a re-cut clip CHANGES the key -- so only the harvester can say which key is "now".
        # The dashboard joins this map to state["scored"] for its live "compared: N of pool"
        # column, instead of guessing among stale fingerprints.
        state["query_keys"] = {str(n): qkey for n, _, qkey in out}
    for s in skipped:
        print("# NOT searching MT%d -- %s" % (s["mystery"], s["why"]))
    return out


def pick_next(pending, state):
    """Next URL, ROTATING hosts so no single site ever sees a burst.

    This is the core load-spreading move: consecutive fetches go to DIFFERENT hosts, so no single
    host ever carries a run of back-to-back requests, even during a fast stretch.
    """
    now = time.time()
    last = state.get("hosts", {})
    best, best_key = None, None
    for i, url in enumerate(pending):
        h = host_of(url)
        info = last.get(h, {})
        if info.get("blocked"):
            continue
        ready_at = info.get("next_ok", 0)
        # prefer the host we have left alone longest, and never one that isn't ready
        key = (ready_at > now, ready_at)
        if best_key is None or key < best_key:
            best, best_key = i, key
    return best


def _stopped(state):
    """Record a clean stop and save.

    The interrupted URL stays `pending`. Its child wrote no signature (see `_fetch_and_sign`), so
    the only honest thing to do with it is fetch it again later.
    """
    name = _stop_name()
    state["session"] = {"phase": "stopped (%s)" % name, "until": 0}
    state["current"] = None
    _save(STATE, state)
    print("# stopped on %s -- state saved, the interrupted URL is still pending" % name)


# --- how much memory this is costing -------------------------------------------------------------
#
# One row per candidate, so "the harvester is at 40 GB again" is a number somebody can read rather
# than a thing somebody eventually notices. The parent should stay flat -- it never holds a track's
# audio now. The child's peak is the interesting number, and it comes back in result.json.

MEM_LOG_KEEP = 50


def mem_ceiling_mb():
    """The parent's restart ceiling in MB, from NETRADIO_HARVEST_MEM_CEILING_MB. 0 = off.

    Off by default on purpose. The right number depends on what a long candidate actually costs
    after this change, and that measurement has not been taken yet; a guessed ceiling would
    restart a healthy harvester.
    """
    try:
        return float(os.environ.get("NETRADIO_HARVEST_MEM_CEILING_MB") or 0)
    except ValueError:
        return 0.0


def _mb(value):
    return None if value is None else round(float(value), 1)


def record_memory(state, url, child=None):
    """Sample this process's footprint and write the row. `child` is the fetch child's
    result.json, or None for a candidate that came from the signature cache."""
    current, peak = memwatch.footprint_mb()
    child = child or {}
    row = {"at": _now(), "url": url, "kind": memwatch.kind(),
           "seconds": child.get("seconds"),
           "parent_mb": _mb(current), "parent_peak_mb": _mb(peak),
           "child_peak_mb": _mb(child.get("peak_mb")),
           "child_after_mb": _mb(child.get("footprint_mb"))}
    state["mem"] = row
    state["mem_log"] = ((state.get("mem_log") or []) + [row])[-MEM_LOG_KEEP:]
    return row


def check_memory(state, url, child=None):
    """Record the row, and say whether the PARENT should stand down to be restarted.

    A child over the ceiling only earns an issues row: its memory left with it, so there is
    nothing to restart. A parent over the ceiling returns from `run()` with exit 0 -- the player's
    watchdog sees a phase that is not "queue empty" and spawns a fresh one.
    """
    row = record_memory(state, url, child)
    ceiling = mem_ceiling_mb()
    if not ceiling:
        return False
    if (row["child_peak_mb"] or 0) > ceiling:
        state["issues"] = ((state.get("issues") or []) + [{
            "at": _now(), "url": url,
            "issue": "fetch child peaked at %.0f MB, over the %.0f MB ceiling -- reported only, "
                     "the child's memory went with it" % (row["child_peak_mb"], ceiling)}])[-50:]
    if (row["parent_mb"] or 0) > ceiling:
        state["session"] = {"phase": "restarting: memory ceiling", "until": 0}
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
        print("another queue/state writer is running (the collector, or another harvest.py "
              "--run) -- ONE writer, always. Not starting.")
        return
    state = _load(STATE, blank_state())
    qs = queries(state)
    if not qs:
        note_no_queries(state, qs)
        _save(STATE, state)                   # queries(state) stamped searching/skipped too
        print("no unsolved mysteries with a usable clip -- nothing to search for")
        return
    if note_no_queries(state, qs):
        _save(STATE, state)                   # searchable again -> the state stands down NOW
    print("# searching for Mystery Tracks %s" % ", ".join(str(n) for n, _, _ in qs))
    print("# work %s, idle %s, rotating hosts, jittered. Ctrl-C or SIGTERM stops cleanly: state "
          "is saved, yt-dlp and ffmpeg are stopped too." % ("4-5h", "40-120m"))

    # A halt is a message to the human, not a permanent state: starting again IS the human saying
    # "I dealt with it". Clear it, and say whether they actually did the thing that was asked.
    if state.pop("halted", None):
        print("# clearing a previous halt -- cookies are %s"
              % ("CONFIGURED" if cookie_args() else "STILL NOT SET (this will halt again)"))
        _save(STATE, state)

    sweep_excerpts()                    # drop anything past its TTL before we start
    swept = sweep_job_dirs()            # and whatever a crashed fetch child left in .harvest/tmp
    if swept:
        print("# swept %d stale fetch job director%s" % (swept, "y" if swept == 1 else "ies"))

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

    # THE CANARY. A broken harvester and a pool without the answer look identical from here: zero
    # matches, for weeks. So before searching for something we have never found, prove we can still
    # find something we HAVE -- re-run one solved calibration case from local files.
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

    # LOST SIGNATURES REGENERATE. `done` is never re-fetched, so a signature missing from both
    # the cache and the bucket left its candidate permanently dark to every future mystery.
    # Check once per run, here at the start (loss happens out-of-band -- a wipe, a bad
    # eviction -- not mid-session); past the cap this reports instead of requeuing. Passing
    # OUR state keeps the alert/issue rows from being clobbered by this session's later saves.
    recover_missing_sigs_at_start(state)

    session_end = time.time() + random.uniform(*SESSION_S)
    said_refused = set()                # queue entries this run has already refused out loud
    while True:
        if _stop_requested():
            return _stopped(state)
        # The live canary, about once a day: the streaming path (yt-dlp -> ffmpeg -> chroma) is
        # exactly what the offline test does NOT exercise, and it is the part with moving parts.
        if selftest.due_for_live():
            # Pass (number, chroma) PAIRS -- selftest's contract. `qs` carries a third field (the
            # query key, which fingerprints the clip) that is ours alone, and leaking it across the
            # boundary is what broke this: selftest unpacked two and got three.
            lv = selftest.live(stream_chroma, [(n, qc) for n, qc, _ in qs])
            # A stop is never a verdict here either: an interrupted canary fetch is not a canary
            # FAILURE, and recording it as one would leave a standing "the matcher is broken"
            # issue behind every Ctrl-C.
            if _stop_requested():
                return _stopped(state)
            if lv.get("ok"):
                print("# live canary PASS -- fetched %s fresh and matched it at %.4f"
                      % (lv["name"], lv["cost"]))
            elif lv.get("ok") is False:
                print("!! LIVE CANARY FAILED -- %s" % lv.get("why"))
                state.setdefault("issues", []).append(
                    {"at": _now(), "issue": "live canary failed: %s" % lv.get("why")})
                _save(STATE, state)

        if os.path.exists(PAUSE):
            state["session"] = {"phase": "paused", "until": 0}
            _save(STATE, state)
            _nap(20)
            continue

        q = _load(QUEUE, {"pending": [], "done": []})
        # Re-read the player's queue every pass: a subscription that fired an hour ago should feed
        # this search without a restart, and a candidate ruled on at /harvest should leave it.
        refused = []
        added, dropped = sync_listen_queue(q, refused)
        if added or dropped:
            _save(QUEUE, q)
            print("listen queue: +%d new, -%d ruled on" % (added, dropped))
        # Say so ONCE per URL per run. The queue is re-read every pass, so the same refusal comes
        # back every few minutes for as long as the entry sits there; a row per pass would bury
        # the issue list under the one thing about it that is not news.
        for row in refused:
            if row["url"] in said_refused:
                continue
            said_refused.add(row["url"])
            print("# skipped %s -- %s" % (row["url"], row["reason"]))
            state["issues"] = ((state.get("issues") or []) + [
                {"at": _now(), "url": row["url"],
                 "issue": "skipped: over %d h to decode in one go (unsplit, or a chunk whose "
                          "own span is that long) -- refused whole, never analysed in part"
                          % (MAX_DURATION_S // 3600)}])[-50:]
            _save(STATE, state)

        # Score cached signatures against any mystery they have not met yet -- a bounded chunk per
        # pass, so it rides along with the fetching instead of blocking it. This is CPU only
        # (~0.06s each, no network), and it is what makes a NEW mystery see the WHOLE corpus: the
        # day MT8's clip lands, all ~900 signatures already on disk get scored against it, without
        # re-downloading a single track. Positions (`at_s`) on old rows get filled in on the way.
        _, retired = listen_queue_split()
        # A ruling spends the excerpt: the audio existed to let the human make the call, and the
        # call has been made. Drop it now, not at the 30-day sweep.
        n_dropped = drop_ruled_excerpts(state, retired)
        if n_dropped:
            _save(STATE, state)
            print("dropped %d ruled-on excerpt(s) -- the leads keep their numbers" % n_dropped)
        stamp_pool(state)                     # bucket sig count for /harvest; ≤15-min cached
        todo = len(unscored_pairs(state, q, retired, qs))
        if todo:
            state["rescan_pending"] = todo
            n = rescan(state, q, retired, qs, limit=RESCAN_PER_PASS)
            state["rescan_pending"] = max(0, todo - n)
            _save(STATE, state)
        elif sigstore.enabled():
            # Rescan backlog empty = every cached signature is scored vs every current mystery,
            # which is exactly when cold ones may leave the disk (verified-remote only).
            n_ev, freed = sigstore.evict_cold(CACHE, state.get("scored") or {},
                                              [qk for _, _, qk in qs])
            if n_ev:
                print("evicted %d cold signature(s) to the bucket (%.1f MB freed)"
                      % (n_ev, freed / 1e6))

        if not q["pending"]:
            # Nothing to fetch -- but a rescan backlog is still real work, so do it flat out rather
            # than declaring the queue empty and going home.
            if todo:
                state["session"] = {"phase": "rescanning cached signatures", "until": 0}
                _save(STATE, state)
                continue
            state["session"] = {"phase": "queue empty", "until": 0}
            _save(STATE, state)
            print("queue empty -- add more at /queue, or subscribe to a channel")
            return

        if time.time() > session_end:                 # rest
            nap = random.uniform(*IDLE_S)
            state["session"] = {"phase": "idle", "until": time.time() + nap}
            _save(STATE, state)
            print("# session over -- idling %.0f min" % (nap / 60))
            _nap(nap)
            session_end = time.time() + random.uniform(*SESSION_S)
            continue

        idx = pick_next(q["pending"], state)
        if idx is None:
            _nap(60)
            continue
        url = q["pending"][idx]
        host = host_of(url)
        hinfo = state.setdefault("hosts", {}).setdefault(host, {})

        wait = hinfo.get("next_ok", 0) - time.time()
        if wait > 0:
            state["session"] = {"phase": "waiting on %s" % host, "until": hinfo["next_ok"]}
            _save(STATE, state)
            _nap(min(wait, 60))
            continue

        state["session"] = {"phase": "working", "until": session_end}
        state["current"] = url
        _save(STATE, state)

        # `samples` is the decoded audio for this iteration, so an excerpt can be cut without a
        # second fetch. It is a memory map over the fetch child's spool file, already unlinked, so
        # dropping it at the end of the loop releases the disk space too. From the cache there is
        # no audio (only the signature), so a cached candidate cannot yield an excerpt -- which is
        # fine: we only ever excerpt something we are already streaming.
        samples = None
        c = _load_sig(url)                 # working cache, else the bucket -- no audio either way
        cached = c is not None
        if cached:
            err = None
            state["skipped_cached"] += 1
        else:
            # The declared length travels with the URL, so the refusal inside the child weighs
            # the same facts `sync_listen_queue` weighed a few lines above.
            c, samples, err = stream_chroma(url, queue_duration(url))

        if _stop_requested():
            samples = None                  # before the queue moves: the URL is still pending
            return _stopped(state)

        # One memory row per candidate, fetched or cached, and the parent's own ceiling check.
        if check_memory(state, url, None if cached else dict(_LAST_CHILD)):
            samples = None
            _save(STATE, state)
            return

        # --- the bot wall: STOP, do not grind ---
        #
        # "Sign in to confirm you're not a bot" does not get better by waiting, and it is not the
        # candidate's fault -- every subsequent fetch from this host will fail the same way. Grinding
        # on produces a wall of identical errors, burns the queue, and analyses nothing. So halt,
        # say plainly what is wrong and how to fix it, and let the human decide.
        if is_bot_wall(err):
            state["halted"] = {
                "at": _now(), "host": host, "error": (err or "")[:200],
                "reason": "%s is refusing anonymous downloads -- it wants a signed-in session" % host,
                "fix": "Give the harvester your YouTube cookies, then start it again:\n"
                       "  NETRADIO_YTDLP_COOKIES_FROM_BROWSER=chrome   (or firefox/safari/brave/edge)\n"
                       "or export a cookies.txt and set:\n"
                       "  NETRADIO_YTDLP_COOKIES=/path/to/cookies.txt\n"
                       "Put it in the analysis repo's .env_vars. The cookie is your logged-in "
                       "session -- keep it out of git.",
                "using_cookies": bool(cookie_args()),
            }
            state["session"] = {"phase": "halted", "until": 0}
            state["issues"] = (state["issues"] + [{"at": _now(), "host": host,
                                                   "issue": "HALTED: %s wants a signed-in session "
                                                            "(see the banner)" % host}])[-50:]
            state["errors"] += 1
            _save(STATE, state)
            print("!! HALTED -- %s" % state["halted"]["reason"])
            print(state["halted"]["fix"])
            return

        # --- host pacing: jittered, so the cadence is never even ---
        gap = HOST_GAP_S.get(host, HOST_GAP_S["_default"]) * random.uniform(0.5, 2.0)
        if err and ("403" in err or "429" in err or "blocked" in err.lower()):
            hinfo["strikes"] = hinfo.get("strikes", 0) + 1
            back = min(BACKOFF_START_S * (2 ** (hinfo["strikes"] - 1)), BACKOFF_MAX_S)
            hinfo["next_ok"] = time.time() + back
            if hinfo["strikes"] >= BLOCK_AFTER:
                hinfo["blocked"] = True               # it told us to go away. we listen.
                state["issues"].append({"at": _now(), "host": host,
                                        "issue": "blocked after %d refusals -- backing off for "
                                                 "good" % hinfo["strikes"]})
            state["errors"] += 1
            _save(STATE, state)
            continue
        hinfo["strikes"] = 0
        hinfo["next_ok"] = time.time() + gap

        q["pending"].pop(idx)
        q["done"].append(url)
        _save(QUEUE, q)

        if c is None:
            state["errors"] += 1
            state["issues"] = (state["issues"] + [{"at": _now(), "url": url,
                                                   "issue": err or "no signature"}])[-50:]
            _save(STATE, state)
            continue

        state["analyzed"] += 1
        for num, qc, _qkey in qs:
            cost, shift, at = _cm.match(qc, c)
            if cost is None or cost > KEEP_CEILING:
                continue
            board = [m for m in state["matches"] if m["mystery"] == num]
            board.sort(key=lambda m: m["cost"])
            if len(board) >= KEEP_TOP and cost >= board[-1]["cost"]:
                continue                       # not good enough to displace anyone

            excerpt = os.path.join(KEEP, "MT%d-%.4f-%s.wav"
                                   % (num, cost, hashlib.sha1(url.encode()).hexdigest()[:8]))
            if not os.path.exists(excerpt):
                if samples is None:            # cached signature, no audio in hand -> can't excerpt
                    continue
                write_excerpt(samples, at or 0, excerpt)      # from memory; NO second fetch
                state["kept"] += 1
            hit = {"at": _now(), "mystery": num, "cost": round(cost, 4),
                   "semitones": shift, "at_s": round(at or 0, 1), "url": url,
                   "audio": excerpt,
                   "verdict": "MATCH" if cost <= MATCH_COST else "near"}
            state["matches"].append(hit)

            evict_overfull(state, num)
            print("  %s  MT%d  cost %.4f  %s  at %s  %s"
                  % (hit["verdict"], num, cost, _cm.describe_shift(shift),
                     _cm.describe_at(at), url))
        samples = None                          # drop the decoded audio; it never persists
        state["updated"] = _now()
        _save(STATE, state)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed-channel", action="append", default=[],
                    help="enumerate a YouTube/SoundCloud channel or playlist into the queue")
    ap.add_argument("--limit", type=int, default=None, help="cap how many to take from a channel")
    ap.add_argument("--run", action="store_true", help="work the queue (runs for weeks)")
    ap.add_argument("--fetch-one", metavar="URL",
                    help="fetch ONE candidate and write its signature, then exit. This is the "
                         "child process `--run` spawns per candidate, so the recipe's memory "
                         "leaves with it; it writes only the signature and its own job "
                         "directory, never the queue or the state. Useful by hand for "
                         "reproducing one fetch.")
    ap.add_argument("--job", metavar="DIR",
                    help="where --fetch-one leaves pcm.f32le, chroma32.npy and result.json "
                         "(default: a directory under .harvest/tmp/)")
    ap.add_argument("--duration", type=float, default=None, metavar="SECONDS",
                    help="the player's declared length for --fetch-one's URL. `--run` passes it "
                         "so the child refuses an over-long candidate on exactly the facts the "
                         "queue used; by hand it is optional, and without it only the URL's own "
                         "#t= span is length-checked.")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--pause", action="store_true")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--purge-audio", action="store_true",
                    help="delete every retained excerpt. The LEADS survive (url, cost, mystery, "
                         "key, at) -- only the audio goes. Review them by embed at /harvest.")
    ap.add_argument("--forget", type=int, metavar="N",
                    help="drop every lead for Mystery Track N, and every scored pairing against it. "
                         "For when the QUESTION was bad -- a clip too short to distinguish records "
                         "with. The next clip then starts clean against the whole corpus.")
    ap.add_argument("--migrate-sigs", action="store_true",
                    help="move the signature archive fully into the bucket: upload+verify every "
                         "local signature, then evict the cold ones (verified remote AND scored "
                         "against every current mystery). Run with the harvester STOPPED or "
                         "paused. After this, local disk holds only work in progress.")
    ap.add_argument("--rescan", action="store_true",
                    help="score every cached signature against every mystery it has not met yet, "
                         "in one go. No network. The running harvester does this by itself, a "
                         "chunk at a time -- this is for when you want it finished NOW (e.g. you "
                         "have just added a Mystery Track clip).")
    ap.add_argument("--requeue-missing-sigs", action="store_true",
                    help="put done URLs whose signature is LOST (in neither the cache nor the "
                         "bucket) back into pending so the fetch path regenerates them. Both "
                         "runtimes do this by themselves at writer startup -- this is the "
                         "on-demand form, and it refuses to run while another queue/state "
                         "writer (the collector, or harvest.py --run) holds the writer lock. "
                         "Refuses past the safety cap (NETRADIO_REQUEUE_MISSING_CAP, default "
                         "10%% of the corpus) and reports instead.")
    args = ap.parse_args()

    # THE FETCH CHILD, dispatched before any lock, queue or state access -- it must never take the
    # writer lock or touch state.json / queue.json. One candidate, one process, and the recipe's
    # working set goes away when it exits.
    if args.fetch_one:
        install_signal_handlers(child=True)
        job = args.job or job_dir(args.fetch_one)
        try:
            result = _fetch_and_sign(args.fetch_one, job, args.duration)
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
        print("# Cut a better clip and it re-enters the search by itself, against every cached "
              "signature.")
        return
    if args.migrate_sigs:
        if not sigstore.enabled():
            print("# sigstore is dark -- set NETRADIO_SIG_BUCKET (and profile/endpoint) in "
                  ".env_vars first.")
            return
        state = _load(STATE, blank_state())
        qs = queries()
        names = sorted(n for n in os.listdir(CACHE)
                       if n.startswith("u") and n.endswith(".npy")) if os.path.isdir(CACHE) else []
        up = failed = 0
        for name in names:
            path = os.path.join(CACHE, name)
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
        n_ev, freed = sigstore.evict_cold(CACHE, state.get("scored") or {},
                                          [qk for _, _, qk in qs])
        left = len(names) - n_ev
        print("# migrate: %d uploaded, %d upload failure(s); %d evicted (%.1f MB freed); "
              "%d signature(s) still local (unscored vs a current mystery, or unverified)."
              % (up, failed, n_ev, freed / 1e6, left))
        if failed:
            print("# NOTHING that failed to upload was deleted. Fix the store config and re-run.")
        return
    if args.requeue_missing_sigs:
        lock = acquire_writer_lock()
        if lock is None:
            print("# a queue/state writer is RUNNING (the collector, or harvest.py --run) -- "
                  "not touching queue.json under it. Both requeue lost sigs themselves at "
                  "startup; stop the writer first if you need this now.")
            return
        res = recover_missing_sigs_at_start()
        if not res["requeued"] and not res["reported"]:
            print("# %s" % res["why"])
        return
    if args.rescan:
        state = _load(STATE, blank_state())
        q = _load(QUEUE, {"pending": [], "done": []})
        qs = queries()
        _, retired = listen_queue_split()
        todo = len(unscored_pairs(state, q, retired, qs))
        print("# rescanning %d (signature, mystery) pair(s) against MT%s -- no network, ~%.0f min"
              % (todo, "/MT".join(str(n) for n, _, _ in qs), todo * 0.06 / 60))
        n = rescan(state, q, retired, qs)
        state["rescan_pending"] = 0
        _save(STATE, state)
        print("# scored %d. Every cached signature has now met every mystery." % n)
        return
    if args.pause:
        open(PAUSE, "w").close()
        print("paused (the runner will notice within ~20s)")
        return
    if args.resume:
        if os.path.exists(PAUSE):
            os.unlink(PAUSE)
        print("resumed")
        return
    if args.status:
        s = _load(STATE, blank_state())
        q = _load(QUEUE, {"pending": [], "done": []})
        print(json.dumps({"analyzed": s["analyzed"], "kept": s["kept"], "errors": s["errors"],
                          "pending": len(q["pending"]), "matches": len(s["matches"]),
                          "phase": s.get("session", {}).get("phase"),
                          "paused": os.path.exists(PAUSE)}, indent=2))
        return
    for ch in args.seed_channel:
        urls = enumerate_channel(ch, args.limit)
        n = add_to_queue(urls, ch)
        print("seeded %d new track(s) from %s (%d found)" % (n, ch, len(urls)))
    if args.run:
        run(args)


if __name__ == "__main__":
    sys.exit(main())        # --fetch-one's exit code is how the parent tells a crash from a refusal
