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
import shutil
import subprocess

import numpy as np

# The broadcast was 16 kHz RealAudio; every capture decodes at 16 kHz, so 1
# sample = 62.5 us and Audacity's 0.001 s label granularity is 16 samples.
SR = 16000

# Where the original captures live. Override with NETRADIO_AUDIO_DIR; defaults to the
# repo's jaz_links/ symlink (repo root = two levels up from scripts/streamalign/).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AUDIO_DIR = os.environ.get(
    "NETRADIO_AUDIO_DIR", os.path.join(_REPO_ROOT, "jaz_links"))

# Decoded-array cache (keyed by source path + size + mtime + params): the `streamalign` cache of
# the cache policy (cache_budget.py, the player plan PLAN_data_tiering.md §5.1). Bounded by the
# policy, not by this module: 4 GB by default (NETRADIO_STREAMALIGN_CACHE_GB), oldest-added
# first, 14 days (NETRADIO_STREAMALIGN_CACHE_MAX_AGE_DAYS), and the disk floor
# NETRADIO_DISK_MAX_PCT above every cap. The directory is NETRADIO_STREAMALIGN_CACHE_DIR, else
# the older NETRADIO_ALIGN_CACHE, else $NETRADIO_CACHE_ROOT/streamalign; with none of the three
# set the cache is dark and every load decodes (nothing is derived from `~`). The two fractions of the
# disk this module used to read (a cap fraction and a disk-full fraction) are gone: a cache's
# useful size is fixed by its corpus, not by the volume it sits on.
import cache_budget

cache_budget.register("streamalign",
                      dir_default=lambda: os.environ.get("NETRADIO_ALIGN_CACHE", "").strip() or None,
                      max_age_default=14, refill="re-decode",
                      is_entry=lambda path: path.endswith(".npy"))


def cache_dir():
    return cache_budget.dir_of("streamalign")


# Preference order when a label names a file without (or with a different)
# extension: lossless originals first, transcode last.
_AUDIO_EXTS = (".wav", ".au", ".mp3")


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
    decoded signal; raises FileNotFoundError if the audio can't be located.
    """
    path = name if os.path.isfile(name) else find_audio_file(name, audio_dir)
    if not path or not os.path.isfile(path):
        raise FileNotFoundError("no audio for %r under %s"
                                % (name, audio_dir or AUDIO_DIR))
    if not use_cache:
        return _ffmpeg_decode(path, sr, mono)

    cdir = cache_budget.ensure_dir("streamalign")  # None when dark: the root unset or absent
    if not cdir:
        return _ffmpeg_decode(path, sr, mono)     # no cache, nothing created
    cache_path = os.path.join(cdir, _cache_key(path, sr, mono) + ".npy")
    if os.path.isfile(cache_path):
        try:
            return np.load(cache_path, mmap_mode="r")
        except (OSError, ValueError):
            pass  # corrupt cache; re-decode
    signal = _ffmpeg_decode(path, sr, mono)
    # The cache is optional. A planned-size reserve makes room by the policy's order and refuses
    # when it cannot (past the disk floor, everything pinned); the decode is returned either way.
    # The path is pinned from the reserve to the commit, so no other run evicts it mid-write. A
    # failed write (ENOSPC, a race) must never fail the decode -- just return the signal.
    ok, _why = cache_budget.reserve("streamalign", signal.nbytes + 128, cache_path)
    if ok:
        tmp = cache_path + ".part"            # `.part` is never counted as an entry
        try:
            with open(tmp, "wb") as handle:  # file handle => np.save won't append .npy
                np.save(handle, signal)
            os.replace(tmp, cache_path)
            cache_budget.commit("streamalign", cache_path, reason="decode")
        except OSError:
            cache_budget.release("streamalign", cache_path)
            try:
                os.remove(tmp)
            except OSError:
                pass
    return signal


def duration_seconds(name, sr=SR, audio_dir=None):
    """Length of a capture in seconds (from the decoded mono length)."""
    return len(load_audio(name, sr=sr, audio_dir=audio_dir)) / float(sr)
