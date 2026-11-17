"""The align server's capture identifiers: which stems it can name, and making one present.

Two questions, two functions, so a membership check never costs a download:

    list_streams(mp3_dir) -> [stem]
        Every capture stem the server can name: the MP3s in `mp3_dir`, plus the stems of a
        listing of the audio bucket's `stream/mp3/`. The listing is held in memory for about a
        minute (`bucketio.listing`), so a burst of requests costs one LIST. Under
        NETRADIO_READ_ONLY, or with no audio bucket set, the disk alone. A listing carries
        sizes and no user metadata, so the hash is checked on the pull, never here.

    ensure_stream(stem, mp3_dir) -> None | (http_status, "stream <stem>: <detail>")
        Make `<stem>.mp3` present in `mp3_dir`: a file already there is a no-op; else it is
        pulled whole from `stream/mp3/<stem>.mp3`, checked by the object's SHA-256 metadata
        before it is renamed into place (`bucketio.fetch_verified`), so no request is ever
        served a partial or unchecked file. One lock per stem: a second request waits for
        the first pull, then finds the file. A failed pull is answered from memory for
        `FAILURE_HOLD_S` seconds rather than run again. Nothing is pulled under
        NETRADIO_READ_ONLY or with no audio bucket set.

Every name passes the stem pattern and the realpath containment check before a path is
built from it: the client names a stem, never a path.

The MP3 directory is a cache on the one cache policy, `stream_mp3`. Where another process on
this machine has already registered that directory, that process holds it, and a pull here
lands the file whole (the `.part` rule) without a `reserve` of its own; that process's next
eviction run counts it. Only where no other process holds it does the align server register
it itself (`register_mp3_cache`), and then a pull is admitted through `reserve` and `commit`.
"""

import os
import re
import threading
import time

import audio_bucket
import bucketio
import cache_budget
import envfile

STEM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MP3_CACHE = "stream_mp3"
MP3_CACHE_GB = 2            # holds the whole MP3 set, so nothing is evicted except under the floor
FAILURE_HOLD_S = 60         # a failed pull is answered from memory this long

_locks = {}
_locks_guard = threading.Lock()
_failures = {}              # stem -> (monotonic time, (status, detail))


def contained(path, root):
    """True if `path` resolves inside `root` (the realpath-containment check)."""
    rp = os.path.realpath(path)
    rr = os.path.realpath(root)
    return rp == rr or rp.startswith(rr + os.sep)


def _read_only():
    return envfile.flag("NETRADIO_READ_ONLY")


def _disk_streams(mp3_dir):
    """The stems of the MP3s in `mp3_dir`. A symlinked MP3 is not listed: the pipeline ignores
    symlinks (`stream_files.plain_files`)."""
    if not mp3_dir or not os.path.isdir(mp3_dir):
        return []
    out = []
    for fn in sorted(os.listdir(mp3_dir)):
        path = os.path.join(mp3_dir, fn)
        if fn.lower().endswith(".mp3") and STEM_RE.match(fn[:-4]) \
                and not os.path.islink(path) and contained(path, mp3_dir):
            out.append(fn[:-4])
    return out


def _bucket():
    return None if _read_only() else audio_bucket.bucket()


def _bucket_streams(mp3_dir, bucket):
    listed, err = bucketio.listing(bucket, audio_bucket.MP3_PREFIX)
    if err is not None:
        return []                   # the bucket did not answer: the disk alone, this time
    out = []
    for key in listed:
        name = key[len(audio_bucket.MP3_PREFIX):]
        if "/" in name or not name.endswith(".mp3"):
            continue
        stem = name[:-4]
        if STEM_RE.match(stem) and contained(os.path.join(mp3_dir, name), mp3_dir):
            out.append(stem)
    return out


def list_streams(mp3_dir):
    """Capture stems the server can name: this disk's MP3s, plus the audio bucket's
    `stream/mp3/` (held about a minute), sorted. The disk alone under NETRADIO_READ_ONLY or with
    no audio bucket set."""
    stems = set(_disk_streams(mp3_dir))
    if mp3_dir:
        bucket = _bucket()
        if bucket is not None:
            stems.update(_bucket_streams(mp3_dir, bucket))
    return sorted(stems)


def _lock(stem):
    with _locks_guard:
        return _locks.setdefault(stem, threading.Lock())


def holds_mp3_dir(mp3_dir):
    """True when this process registered the MP3 directory as `stream_mp3`."""
    held = cache_budget.dir_of(MP3_CACHE) if cache_budget.registered(MP3_CACHE) else None
    return bool(held and mp3_dir and os.path.realpath(held) == os.path.realpath(mp3_dir))


def _pull(bucket, stem, path, mp3_dir):
    key = audio_bucket.MP3_PREFIX + stem + ".mp3"
    got, err = bucketio.head(bucket, key)
    if got is None:
        if err is None:
            return 404, "stream %s: not on this disk or in the audio bucket" % stem
        return 502, "stream %s: %s" % (stem, err)
    if not got.sha256:
        return 502, "stream %s: no sha256 metadata on the object; refused" % stem
    policy = holds_mp3_dir(mp3_dir)
    if policy and not cache_budget.reserve(MP3_CACHE, got.size):
        return 507, "stream %s: the MP3 cache refused the room" % stem
    why = bucketio.fetch_verified(bucket, key, path, size=got.size, sha256=got.sha256)
    if why is not None:
        return 502, "stream %s: %s" % (stem, why)
    if policy:
        cache_budget.commit(MP3_CACHE, path)
    return None


def ensure_stream(stem, mp3_dir):
    """None once `<stem>.mp3` is present in `mp3_dir` (pulled now if it had to be), else
    (http_status, "stream <stem>: <detail>"). See the module docstring for the rules."""
    if not mp3_dir or not stem or not STEM_RE.match(stem):
        return 404, "stream %s: unknown stem" % stem
    path = os.path.join(mp3_dir, stem + ".mp3")
    if not contained(path, mp3_dir):
        return 404, "stream %s: unknown stem" % stem
    if os.path.isfile(path) and not os.path.islink(path):
        return None
    bucket = _bucket()
    if bucket is None:
        return 404, "stream %s: not on this disk" % stem
    with _lock(stem):
        if os.path.isfile(path) and not os.path.islink(path):
            return None                 # a request ahead of this one pulled it
        failed = _failures.get(stem)
        if failed and time.monotonic() - failed[0] < FAILURE_HOLD_S:
            return failed[1]
        result = _pull(bucket, stem, path, mp3_dir)
        if result is None:
            _failures.pop(stem, None)
        else:
            _failures[stem] = (time.monotonic(), result)
            print("align: %s" % result[1], flush=True)
        return result


def captures_dir(mp3_dir):
    """Where the capture files sit: NETRADIO_CAPTURES_DIR, else the parent of the MP3
    directory (the rule `stream_files.py` uses), else None."""
    value = os.environ.get("NETRADIO_CAPTURES_DIR", "").strip()
    if value:
        return os.path.expanduser(value)
    if mp3_dir:
        return os.path.dirname(os.path.abspath(mp3_dir).rstrip(os.sep)) or None
    return None


def register_mp3_cache(mp3_dir):
    """Register the MP3 directory as `stream_mp3`, unless another live process on this machine
    already holds a cache over it (then that process counts what a pull lands). Returns the
    record, or None: the policy dark, no MP3 directory, held elsewhere, or refused."""
    if not cache_budget.enabled() or not mp3_dir:
        return None
    mine = os.path.realpath(mp3_dir)
    for proc in cache_budget.other_processes():
        for row in proc.get("caches") or []:
            try:
                theirs = os.path.realpath(row["dir"])
            except (KeyError, TypeError, ValueError):
                continue
            if contained(theirs, mine) or contained(mine, theirs):
                print("align: the MP3 directory is held by process %s (%s, cache %r); a pull "
                      "lands whole and that process counts it"
                      % (proc.get("pid"), proc.get("process"), row.get("name")), flush=True)
                return None
    # rank 7: an evicted MP3 comes back from the bucket on its next request. The literal name,
    # so env_check.py's code scan sees the registration and counts its variable family as read.
    try:
        return cache_budget.register("stream_mp3", dir=mp3_dir,
                                     cap=int(MP3_CACHE_GB * cache_budget.GB),
                                     refill="audio bucket: stream/mp3/<name>.mp3", rank=7)
    except cache_budget.CacheRefused as exc:
        print(exc, flush=True)
        return None
