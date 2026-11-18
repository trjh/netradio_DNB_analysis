"""Audio loading for the stream alignment engine.

All netradio captures are 16 kHz / stereo / 16-bit PCM (``.wav`` little-endian,
``.au`` big-endian). Alignment decodes only those lossless capture files, never the
``.mp3`` transcodes: the labels were placed on the capture files, and a lossy transcode
is not the audio they measure. We decode through ffmpeg
to float32 mono at a fixed sample rate so the rest of the engine never has to think about
container/format/endianness, and cache the decoded arrays on disk (these files get read
many times across a global solve).

Two questions, two functions:

- ``find_audio_file(name)`` is a local probe: the capture file on this disk, or None. It
  never reaches the bucket, so a loop over every capture that asks it costs nothing.
- ``capture_nameable(name)`` asks whether a decode can be had at all: the disk, the
  ``stream_master`` cache, or a key in a held listing of the audio bucket's
  ``stream/master/`` (one LIST a minute at most). A check in front of a decode asks this.

``load_audio`` decodes a capture the disk does not hold by pulling it from the audio bucket
into the ``stream_master`` cache first (``fetch_audio_file``), checked against the object's
SHA-256 metadata. With no audio bucket set, a missing capture fails exactly as before.

No third-party audio deps: ffmpeg for decode, numpy for everything else.
"""

import hashlib
import io
import json
import os
import subprocess
import sys
import threading

import numpy as np

# The bucket modules sit one level up, in scripts/, beside cache_budget.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import audio_bucket  # noqa: E402
import bucketio  # noqa: E402
import cache_budget  # noqa: E402

# The broadcast was 16 kHz RealAudio; every capture decodes at 16 kHz, so 1
# sample = 62.5 us and Audacity's 0.001 s label granularity is 16 samples.
SR = 16000

# Where the original captures live. Override with NETRADIO_AUDIO_DIR; defaults to the
# repo's jaz_links/ symlink (repo root = two levels up from scripts/streamalign/).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AUDIO_DIR = os.environ.get(
    "NETRADIO_AUDIO_DIR", os.path.join(_REPO_ROOT, "jaz_links"))

# The capture file extensions, in the order a stem resolves them when a label names it
# without (or with a different) extension. Never `.mp3`: a transcode is not a capture
# file, and the engine measures the capture files the labels were placed on. A
# stem with no capture file on this disk is pulled from the audio bucket by
# `load_audio` instead (`fetch_audio_file`).
_AUDIO_EXTS = (".wav", ".au")


# --- the decoded-array cache, on the one cache policy ---------------------------------
#
# The arrays are pure regenerable performance state: every entry re-decodes in
# seconds with ffmpeg, so the cache registers on the machine's cache policy
# (cache_budget.py) and is evicted oldest-added first under the policy's settings
# for `streamalign` — cap NETRADIO_STREAMALIGN_CACHE_GB (default 4 GB), age
# NETRADIO_STREAMALIGN_CACHE_MAX_AGE_DAYS (default 14 days, `none` for no age
# limit), directory NETRADIO_STREAMALIGN_CACHE_DIR
# (default $NETRADIO_CACHE_ROOT/streamalign). The policy is dark until
# NETRADIO_CACHE_ROOT is set: then there is no cache directory at all and every
# load decodes again — never an unbounded cache with no eviction.

CACHE = "streamalign"
CACHE_MAX_AGE_DAYS = 14     # an alignment session lasts days, not months

_pin_lock = threading.Lock()
_pinned_entries = {}        # realpath -> how many holders pin it


def _pinned_entry(path):
    """The policy's pin: an entry whose decode (or pull) is in flight in this
    process, so no eviction this process runs deletes what a decode is using."""
    return os.path.realpath(path) in _pinned_entries


class _Pinned:
    """Pin one cache entry for the flight of its decode: added on enter, dropped
    on exit whatever happened to the write. Counted, so a pin held inside another
    on the same path (a pull inside a decode) leaves the outer one in place."""

    def __init__(self, path):
        self.path = os.path.realpath(path)

    def __enter__(self):
        with _pin_lock:
            _pinned_entries[self.path] = _pinned_entries.get(self.path, 0) + 1

    def __exit__(self, *_exc):
        with _pin_lock:
            left = _pinned_entries.get(self.path, 1) - 1
            if left > 0:
                _pinned_entries[self.path] = left
            else:
                _pinned_entries.pop(self.path, None)


def _npy_bytes(signal):
    """The size of the `.npy` file `np.save` writes for `signal`: its header, then its data."""
    header = io.BytesIO()
    np.lib.format.write_array_header_1_0(
        header, np.lib.format.header_data_from_array_1_0(np.asarray(signal)))
    return len(header.getvalue()) + signal.nbytes


def register_cache():
    """Put the decoded-array cache on the one cache policy. Reads the environment
    at call time; call it again to re-read it. Returns the cache's record, or
    None when the policy is dark (NETRADIO_CACHE_ROOT unset) or the directory is
    refused."""
    # rank 3: of the caches sharing the policy's floor, this one gives up
    # entries third — a re-decode is seconds, the refill costs nothing.
    # The literal name, not the CACHE constant, so env_check.py's code scan sees
    # the registration and counts its variable family as read.
    try:
        return cache_budget.register("streamalign", max_age=CACHE_MAX_AGE_DAYS,
                                     pinned=_pinned_entry,
                                     refill="re-decode", rank=3)
    except cache_budget.CacheRefused as exc:
        print(exc)              # refused: decodes then run without the cache
        return None


register_cache()


# --- the pulled capture files, on the one cache policy --------------------------------
#
# A capture file this disk does not hold is pulled from the audio bucket for the seconds
# of its decode into its own cache, `stream_master`: cap NETRADIO_STREAM_MASTER_CACHE_GB
# (default 4 GB, about sixteen captures), directory NETRADIO_STREAM_MASTER_CACHE_DIR
# (default $NETRADIO_CACHE_ROOT/stream_master), evicted oldest-added first. A pulled file
# is pinned while it is pulled and decoded, then it is an ordinary eviction candidate: the
# decoded array in `streamalign` is what later runs read. Its directory is never the
# `streamalign` one. Each pulled file has a companion, `<file>.object.json`, recording the
# object it came from (key, size, SHA-256), which keys its decoded array without a bucket
# call; the policy evicts the two together.

STREAM_MASTER = "stream_master"
_OBJECT_META = ".object.json"
NAMEABLE_LISTING_S = 60     # how long the `stream/master/` listing is held for `capture_nameable`


def _master_companions(path):
    return [] if path.endswith(_OBJECT_META) else [path + _OBJECT_META]


def register_master_cache():
    """Put the pulled-capture cache on the one cache policy. Reads the environment at call
    time; call it again to re-read it. Returns the cache's record, or None when the policy
    is dark or the directory is refused (then nothing is pulled: a decode of a capture this
    disk does not hold fails as it did before there was a bucket)."""
    # rank 5: of the caches sharing the policy's floor, after the decoded arrays and the
    # signatures; each file comes back from the bucket. The literal name, so env_check.py's
    # code scan sees the registration and counts its variable family as read.
    try:
        return cache_budget.register("stream_master", pinned=_pinned_entry,
                                     refill="audio bucket: stream/master/<name>", rank=5,
                                     companions=_master_companions)
    except cache_budget.CacheRefused as exc:
        print(exc)              # refused: nothing is pulled
        return None


register_master_cache()


class CapturePullError(FileNotFoundError):
    """A capture this disk does not hold, which the audio bucket could not supply: not
    there, no answer, no SHA-256 metadata, a size or hash mismatch, or no room. A subclass
    of FileNotFoundError, so every caller that handles a missing capture handles this."""


_stem_locks = {}
_stem_locks_guard = threading.Lock()


def _stem_lock(stem):
    """One lock per stem: a second decode of a capture being pulled waits, then finds it."""
    with _stem_locks_guard:
        return _stem_locks.setdefault(stem, threading.Lock())


def stem_of(name):
    """`d019-040.wav` / `path/d019-040.mp3` / `d019-040` -> `d019-040`."""
    return os.path.splitext(os.path.basename((name or "").strip()))[0]


def find_audio_file(name, audio_dir=None):
    """The capture file for `name`'s stem on this disk (`.wav`, else `.au`), or None.

    A local probe: it never reaches the bucket and never pulls, so a report of which
    captures are present (and a loop over every capture) costs nothing. A `.mp3` of the
    same stem is never returned: it is a transcode, not a capture file. A check in front of
    a decode asks `capture_nameable` instead.
    """
    audio_dir = audio_dir or AUDIO_DIR
    stem = stem_of(name)
    for ext in _AUDIO_EXTS:
        path = os.path.join(audio_dir, stem + ext)
        if os.path.isfile(path):
            return path
    return None


def _held_master(stem):
    """(path, sha256 or None) for `stem`'s capture file in the `stream_master` cache, or None."""
    cache_dir = cache_budget.dir_of(STREAM_MASTER)
    if not cache_dir:
        return None
    for ext in _AUDIO_EXTS:
        path = os.path.join(cache_dir, stem + ext)
        if os.path.isfile(path):
            try:
                with open(path + _OBJECT_META, encoding="utf-8") as fh:
                    sha = (json.load(fh) or {}).get("sha256") or None
            except (OSError, ValueError, AttributeError):
                sha = None
            return path, sha
    return None


def _master_listing(bucket, max_age=bucketio.LISTING_MAX_AGE_S):
    """The audio bucket's `stream/master/` keys, or (None, detail) when it did not answer."""
    return bucketio.listing(bucket, audio_bucket.MASTER_PREFIX, max_age=max_age)


def capture_nameable(name, audio_dir=None):
    """True when a decode of `name`'s capture can be had: a capture file on this disk, one
    already in the `stream_master` cache, or the stem's key in a held listing of the audio
    bucket's `stream/master/` (one LIST a minute at most, so a loop over every capture costs
    one). With no audio bucket set, or a bucket that does not answer, the disk alone.

    The check in front of a decode asks this, so an evicted capture still reaches
    `load_audio`, which pulls it. A report of which captures are on this disk asks
    `find_audio_file`, which never reaches the bucket."""
    if name and os.path.isfile(name):
        return True
    stem = stem_of(name)
    if find_audio_file(stem, audio_dir) or _held_master(stem):
        return True
    bucket = audio_bucket.bucket()
    if bucket is None:
        return False
    listed, err = _master_listing(bucket, max_age=NAMEABLE_LISTING_S)
    if err is not None:
        return False
    return audio_bucket.master_key(stem, listed) is not None


def _locate(stem):
    """(bucket, key, Head) for `stem`'s capture object in the audio bucket. Raises
    CapturePullError when there is no bucket, no registered `stream_master` cache (checked
    before any bucket call), no such object, no answer, or an object with no SHA-256
    metadata (refused, never accepted on its size alone)."""
    bucket = audio_bucket.bucket()
    if bucket is None:
        raise CapturePullError("no capture file for %r on this disk, and no audio bucket set"
                               % stem)
    if not cache_budget.dir_of(STREAM_MASTER):
        # before any bucket call: with nowhere bounded to pull to, a LIST and a head buy
        # nothing
        raise CapturePullError("no capture file for %r on this disk, and the stream_master "
                               "cache is not registered (set NETRADIO_CACHE_ROOT), so there "
                               "is nowhere bounded to pull it to" % stem)
    listed, err = _master_listing(bucket)
    if err is not None:
        raise CapturePullError("no capture file for %r on this disk; the audio bucket did "
                               "not answer: %s" % (stem, err))
    key = audio_bucket.master_key(stem, listed)
    if key is None:
        raise CapturePullError("no capture file for %r on this disk or in the audio bucket"
                               % stem)
    got, err = bucketio.head(bucket, key)
    if got is None:
        raise CapturePullError("capture %s: %s" % (key, err or "no such object"))
    if not got.sha256:
        raise CapturePullError("capture %s: no sha256 metadata on the object; refused" % key)
    return bucket, key, got


def _pull(bucket, key, got):
    """Pull one capture object into the `stream_master` cache, checked by its SHA-256
    metadata: `reserve` its size, `fetch_verified` (which lands it whole, renaming its
    `.part` file only once the size and the hash match), record the object beside it, then
    `commit`. The destination is pinned throughout; the caller keeps it pinned for the
    decode. Returns the path, or raises CapturePullError."""
    cache_dir = cache_budget.dir_of(STREAM_MASTER)
    if not cache_dir:
        raise CapturePullError("capture %s is in the audio bucket, but the stream_master "
                               "cache is not registered (set NETRADIO_CACHE_ROOT), so there "
                               "is nowhere bounded to pull it to" % key)
    dest = os.path.join(cache_dir, os.path.basename(key))
    with _Pinned(dest):
        if not cache_budget.reserve(STREAM_MASTER, got.size):
            raise CapturePullError("capture %s: the stream_master cache refused the room "
                                   "(the disk is past its floor, or the cache is full of "
                                   "pinned entries)" % key)
        os.makedirs(cache_dir, exist_ok=True)
        why = bucketio.fetch_verified(bucket, key, dest, size=got.size, sha256=got.sha256)
        if why is not None:
            raise CapturePullError("capture %s: %s" % (key, why))
        meta = dest + _OBJECT_META
        tmp = "%s.%d.tmp" % (meta, os.getpid())
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"key": key, "size": got.size, "sha256": got.sha256}, fh)
            os.replace(tmp, meta)
        except OSError:
            pass            # the decode still runs; its array is keyed the same way
        cache_budget.commit(STREAM_MASTER, dest)
    return dest


def fetch_audio_file(name):
    """The path of `name`'s capture file, pulled from the audio bucket into the
    `stream_master` cache when it is not already there. A file already held is returned
    with no bucket call. Returns None when it cannot be had (printed): no bucket, no such
    object, no SHA-256 metadata, a size or hash mismatch, or no room.

    `load_audio` calls the same pull on its miss path; this is the entry point for a
    caller that hands the file to a tool other than `load_audio` (ffmpeg, say). The file
    is an ordinary eviction candidate once this returns: use it promptly."""
    stem = stem_of(name)
    with _stem_lock(stem):
        held = _held_master(stem)
        if held:
            return held[0]
        try:
            bucket, key, got = _locate(stem)
            return _pull(bucket, key, got)
        except CapturePullError as exc:
            print(exc, flush=True)
            return None


def _cache_key(path, sr, mono):
    st = os.stat(path)
    raw = "%s|%d|%d|%d|%s|%d" % (
        os.path.realpath(path), st.st_size, int(st.st_mtime), sr,
        "mono" if mono else "stereo", 1)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _object_cache_key(sha256, sr, mono):
    """A pulled capture's array is keyed by the object's SHA-256, never by the pulled
    file's path, size and time: the array outlives the file, so a later decode finds it
    with one `head` and no pull."""
    raw = "sha256:%s|%d|%s|%d" % (sha256, sr, "mono" if mono else "stereo", 1)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _ffmpeg_decode(path, sr, mono):
    """Decode `path` to a float32 numpy array via ffmpeg (mono if requested)."""
    channels = 1 if mono else 2
    cmd = [
        "ffmpeg", "-nostdin", "-v", "error", "-i", path,
        "-f", "f32le", "-ac", str(channels), "-ar", str(sr), "-",
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          check=False)
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg failed on %s: %s"
                           % (path, proc.stderr.decode("utf-8", "replace")[-400:]))
    data = np.frombuffer(proc.stdout, dtype="<f4")
    if not mono:
        data = data.reshape(-1, 2)
    return np.ascontiguousarray(data)


def _cache_path(key):
    cache_dir = cache_budget.dir_of(CACHE)   # None while the policy is dark
    return os.path.join(cache_dir, key + ".npy") if cache_dir else None


def _held_array(cache_path):
    if cache_path and os.path.isfile(cache_path):
        try:
            return np.load(cache_path, mmap_mode="r")
        except (OSError, ValueError):
            pass  # corrupt cache; re-decode
    return None


def _decode_into_cache(path, sr, mono, cache_path):
    """Decode `path`, and keep the array under `cache_path` when the policy admits it."""
    signal = _ffmpeg_decode(path, sr, mono)
    if cache_path:
        cache_dir = os.path.dirname(cache_path)
        try:
            with _Pinned(cache_path):
                if cache_budget.reserve(CACHE, _npy_bytes(signal)):
                    os.makedirs(cache_dir, exist_ok=True)
                    try:
                        # the .tmp suffix is the policy's write-in-progress mark:
                        # fresh, it is never evicted; older than
                        # NETRADIO_CACHE_IN_PROGRESS_MIN (a decode that died), it is
                        # evicted like any other entry.
                        tmp = "%s.%d.tmp" % (cache_path, os.getpid())
                        with open(tmp, "wb") as handle:  # file handle => np.save won't append .npy
                            np.save(handle, signal)
                        os.replace(tmp, cache_path)
                    except OSError:
                        pass            # ENOSPC, a race: never fail the decode
                    else:
                        cache_budget.commit(CACHE, cache_path)
                        # commit evicts only past the cap or the floor; this run
                        # applies the age limit too, so every write ages the cache
                        cache_budget.run_eviction(CACHE)
        except OSError:
            pass    # the lock file, an unwritable root: the cache is optional
    return signal


def _load_pulled(name, sr, mono, use_cache):
    """`load_audio`'s miss path: no capture file on this disk.

    In order: a file already in `stream_master` is decoded (its array keyed by the object
    it came from) with no bucket call; else one `head` of the stem's object gives its
    SHA-256, and an array already held under that hash is returned with no pull and no
    decode; else the object is pulled, checked by its SHA-256 metadata, decoded under that
    key, and unpinned, an ordinary eviction candidate from then on. With no audio bucket
    set this raises the FileNotFoundError a missing capture always raised."""
    stem = stem_of(name)
    with _stem_lock(stem):
        held = _held_master(stem)
        if held:
            path, sha = held
            if not use_cache:
                with _Pinned(path):
                    return _ffmpeg_decode(path, sr, mono)
            key = _object_cache_key(sha, sr, mono) if sha else _cache_key(path, sr, mono)
            cache_path = _cache_path(key)
            signal = _held_array(cache_path)
            if signal is not None:
                return signal
            with _Pinned(path):
                return _decode_into_cache(path, sr, mono, cache_path)
        bucket, key, got = _locate(stem)
        cache_path = _cache_path(_object_cache_key(got.sha256, sr, mono)) if use_cache else None
        signal = _held_array(cache_path)
        if signal is not None:
            return signal
        path = os.path.join(cache_budget.dir_of(STREAM_MASTER) or "", os.path.basename(key))
        with _Pinned(path):
            path = _pull(bucket, key, got)
            if not use_cache:
                return _ffmpeg_decode(path, sr, mono)
            return _decode_into_cache(path, sr, mono, cache_path)


def load_audio(name, sr=SR, mono=True, use_cache=True, audio_dir=None):
    """Load a capture as a float32 numpy array (mono, normalized to ~[-1, 1]).

    `name` may be a bare stem, a label filename, or a full path. Returns the
    decoded signal. A stem with no capture file on this disk (`find_audio_file`:
    `.wav`, else `.au`, never `.mp3`) is pulled from the audio bucket into the
    `stream_master` cache first (`_load_pulled`); with no bucket set, or when the
    bucket cannot supply it, this raises FileNotFoundError, as it always has.

    The decoded signal is cached on the policy when one is configured: `reserve`
    makes room for it (evicting oldest-first, never the entry this decode is
    writing, which is pinned), the write lands atomically, and `commit` records
    it. The cache stays optional throughout — a dark policy, a refused reserve
    (the cache over its cap with everything pinned, or the disk past its floor)
    or a failed write means the decode simply runs again next time; none of them
    ever fails the load.
    """
    path = name if os.path.isfile(name) else find_audio_file(name, audio_dir)
    if not path or not os.path.isfile(path):
        try:
            return _load_pulled(name, sr, mono, use_cache)
        except CapturePullError as exc:
            raise CapturePullError("no audio for %r under %s (%s)"
                                   % (name, audio_dir or AUDIO_DIR, exc)) from None
    if not use_cache:
        return _ffmpeg_decode(path, sr, mono)
    cache_path = _cache_path(_cache_key(path, sr, mono))
    signal = _held_array(cache_path)
    if signal is not None:
        return signal
    return _decode_into_cache(path, sr, mono, cache_path)


def duration_seconds(name, sr=SR, audio_dir=None):
    """Length of a capture in seconds (from the decoded mono length)."""
    return len(load_audio(name, sr=sr, audio_dir=audio_dir)) / float(sr)


# --- lengths without a decode ----------------------------------------------------------

STREAM_FILES_PATH = os.path.join(_REPO_ROOT, "stream-files.json")


def recorded_durations(path=None):
    """{stem: seconds} from the committed `stream-files.json`: each capture's length as
    measured once from its capture file. {} when the file is missing or unreadable."""
    try:
        with open(path or STREAM_FILES_PATH, encoding="utf-8") as fh:
            rows = (json.load(fh) or {}).get("files") or []
    except (OSError, ValueError, AttributeError):
        return {}
    out = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        seconds = row.get("duration_seconds")
        if not isinstance(seconds, (int, float)):
            continue
        for field in ("original_filename", "filename"):
            if row.get(field):
                out.setdefault(stem_of(row[field]), float(seconds))
    return out


def length_without_pull(name, audio_dir=None, recorded=None, sr=SR):
    """(seconds, source) for a capture, decoding nothing that is not on this disk and
    pulling nothing. `source` is "audio" (the capture file here, decoded as
    `duration_seconds` does), "cache" (the decoded array of a file in `stream_master`) or
    "stream-files.json" (the committed length: the capture is not on this disk). (None,
    None) when none of the three has it. `recorded` is `recorded_durations()`, passed in by
    a caller in a loop."""
    stem = stem_of(name)
    if find_audio_file(stem, audio_dir):
        return duration_seconds(stem, sr=sr, audio_dir=audio_dir), "audio"
    held = _held_master(stem)
    if held and held[1]:
        signal = _held_array(_cache_path(_object_cache_key(held[1], sr, True)))
        if signal is not None:
            return len(signal) / float(sr), "cache"
    recorded = recorded_durations() if recorded is None else recorded
    if stem in recorded:
        return recorded[stem], "stream-files.json"
    return None, None
