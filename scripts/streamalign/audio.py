"""Audio loading for the stream alignment engine.

All netradio captures are 16 kHz / stereo / 16-bit PCM (``.wav`` little-endian,
``.au`` big-endian); the transcoded ``.mp3`` tiles exist too but the lossless
originals are preferred for alignment. We decode everything through ffmpeg to
float32 mono at a fixed sample rate so the rest of the engine never has to think
about container/format/endianness, and cache the decoded arrays on disk (these
files get read many times across a global solve).

No third-party audio deps: ffmpeg for decode, numpy for everything else.
"""

import hashlib
import os
import subprocess
import threading

import numpy as np

import cache_budget

# The broadcast was 16 kHz RealAudio; every capture decodes at 16 kHz, so 1
# sample = 62.5 us and Audacity's 0.001 s label granularity is 16 samples.
SR = 16000

# Where the original captures live. Override with NETRADIO_AUDIO_DIR; defaults to the
# repo's jaz_links/ symlink (repo root = two levels up from scripts/streamalign/).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AUDIO_DIR = os.environ.get(
    "NETRADIO_AUDIO_DIR", os.path.join(_REPO_ROOT, "jaz_links"))

# Preference order when a label names a file without (or with a different)
# extension: lossless originals first, transcode last. The .mp3 stays in the
# order deliberately — a machine can hold only the transcodes, and a stem with
# no capture file on disk must still resolve there.
_AUDIO_EXTS = (".wav", ".au", ".mp3")


# --- the decoded-array cache, on the one cache policy ---------------------------------
#
# The arrays are pure regenerable performance state: every entry re-decodes in
# seconds with ffmpeg, so the cache registers on the machine's cache policy
# (cache_budget.py) and is evicted oldest-added first under the policy's settings
# for `streamalign` — cap NETRADIO_STREAMALIGN_CACHE_GB (default 4 GB), age
# NETRADIO_STREAMALIGN_CACHE_MAX_AGE_DAYS, directory NETRADIO_STREAMALIGN_CACHE_DIR
# (default $NETRADIO_CACHE_ROOT/streamalign). The policy is dark until
# NETRADIO_CACHE_ROOT is set: then there is no cache directory at all and every
# load decodes again — never an unbounded cache with no eviction.

CACHE = "streamalign"

_pin_lock = threading.Lock()
_pinned_entries = set()


def _pinned_entry(path):
    """The policy's pin: an entry whose decode is in flight in this process, so
    no eviction this process runs deletes what a decode is writing."""
    return os.path.realpath(path) in _pinned_entries


class _Pinned:
    """Pin one cache entry for the flight of its decode: added on enter, dropped
    on exit whatever happened to the write."""

    def __init__(self, path):
        self.path = os.path.realpath(path)

    def __enter__(self):
        with _pin_lock:
            _pinned_entries.add(self.path)

    def __exit__(self, *_exc):
        with _pin_lock:
            _pinned_entries.discard(self.path)


def register_cache():
    """Put the decoded-array cache on the one cache policy. Reads the environment
    at call time; call it again to re-read it. Returns the cache's record, or
    None when the policy is dark (NETRADIO_CACHE_ROOT unset) or the directory is
    refused."""
    # rank 3: of the caches sharing the policy's floor, this one gives up
    # entries third — a re-decode is seconds, the refill costs nothing.
    return cache_budget.register(CACHE, pinned=_pinned_entry,
                                 refill="re-decode", rank=3)


register_cache()


def stem_of(name):
    """`d019-040.wav` / `path/d019-040.mp3` / `d019-040` -> `d019-040`."""
    return os.path.splitext(os.path.basename((name or "").strip()))[0]


def find_audio_file(name, audio_dir=None):
    """Resolve a label/sync filename to an actual audio file on disk.

    Labels reference `.wav`/`.au`/`.mp3` interchangeably; return the best
    available original for `name`'s stem, or None if nothing is present.
    """
    audio_dir = audio_dir or AUDIO_DIR
    stem = stem_of(name)
    for ext in _AUDIO_EXTS:
        path = os.path.join(audio_dir, stem + ext)
        if os.path.isfile(path):
            return path
    return None


def _cache_key(path, sr, mono):
    st = os.stat(path)
    raw = "%s|%d|%d|%d|%s|%d" % (
        os.path.realpath(path), st.st_size, int(st.st_mtime), sr,
        "mono" if mono else "stereo", 1)
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


def load_audio(name, sr=SR, mono=True, use_cache=True, audio_dir=None):
    """Load a capture as a float32 numpy array (mono, normalized to ~[-1, 1]).

    `name` may be a bare stem, a label filename, or a full path. Returns the
    decoded signal; raises FileNotFoundError if the audio can't be located — and
    an MP3 is never located (see `find_audio_file`).

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
        raise FileNotFoundError("no audio for %r under %s"
                                % (name, audio_dir or AUDIO_DIR))
    if not use_cache:
        return _ffmpeg_decode(path, sr, mono)

    cache_dir = cache_budget.dir_of(CACHE)   # None while the policy is dark
    cache_path = (os.path.join(cache_dir, _cache_key(path, sr, mono) + ".npy")
                  if cache_dir else None)
    if cache_path and os.path.isfile(cache_path):
        try:
            return np.load(cache_path, mmap_mode="r")
        except (OSError, ValueError):
            pass  # corrupt cache; re-decode
    signal = _ffmpeg_decode(path, sr, mono)
    if cache_path:
        try:
            with _Pinned(cache_path):
                if cache_budget.reserve(CACHE, signal.nbytes):
                    os.makedirs(cache_dir, exist_ok=True)
                    try:
                        # the .tmp suffix is the policy's write-in-progress mark:
                        # fresh, it is never evicted; an hour old (a decode that
                        # died), it is evicted like any other entry.
                        tmp = "%s.%d.tmp" % (cache_path, os.getpid())
                        with open(tmp, "wb") as handle:  # file handle => np.save won't append .npy
                            np.save(handle, signal)
                        os.replace(tmp, cache_path)
                    except OSError:
                        pass            # ENOSPC, a race: never fail the decode
                    else:
                        cache_budget.commit(CACHE, cache_path)
        except OSError:
            pass    # the lock file, an unwritable root: the cache is optional
    return signal


def duration_seconds(name, sr=SR, audio_dir=None):
    """Length of a capture in seconds (from the decoded mono length)."""
    return len(load_audio(name, sr=sr, audio_dir=audio_dir)) / float(sr)
