"""The one resolver for the originals: which file a reader gets, and a missing one pulled.

Every tool that wants an original asks here, so one rule picks every file:

    path = sources_resolve.resolve(72)              # the original for track 072, or None
    name = sources_resolve.choose(names, "what")    # the pick among one stem's files

**The rule** (2026-09-16; the order, 2026-10-28). For one stem, the uncompressed file
(`.wav`/`.au`); else the lossless master (`.wv`/`.aif`/`.aiff`); else the `.flac`; else a lossy
file (`.mp3`/`.m4a`/`.opus`/`.ogg`/`.aac`). Two candidates at the rung that wins stop with
`Ambiguous`, which names both: the resolver never picks one silently. A stem is a track number
(`NNN-<anything>.<ext>`), a mystery's number (`Mystery Track N...`), or else the file name less
its extension. Any other extension is not an original (a cover image, a note, a promo video).

**The directory** is `sources_dir()`: NETRADIO_SOURCES_FLAC_CACHE_DIR, else `sources_local/` in
this checkout. It is flat, so a person reads it the way the resolver does.

**The pull.** With no local candidate for a stem, the original is pulled from the audio bucket's
`sources/` prefix (`audio_bucket.SOURCES_PREFIX`), where every original sits flat under its own
file name. The bucket's candidates for the stem are chosen by the same rule. The cache keeps
flac, so a lossless master in the bucket is pulled as its `.flac` sibling; a lossy one is pulled
as it is. A pull is the whole file, one lock per file name, checked by the object's SHA-256
metadata before it is renamed into place (`bucketio.fetch_verified`): a request that arrives
mid-pull waits, and no reader ever sees a partial file. Nothing is pulled with no audio bucket
set, under NETRADIO_READ_ONLY, or into a directory that is not this process's registered
`sources_flac` cache: then a missing original is reported missing, as before there was a bucket.

**The cache.** The directory is the `sources_flac` cache on the one cache policy:
NETRADIO_SOURCES_FLAC_CACHE_GB (default 4 GB; `none` where the directory is the master copy),
oldest-added first. A file is pinned, and so never evicted or removed, unless the bucket holds
it verified: a `sources/<file name>` object, named in one listing of the prefix with the file's
size, whose SHA-256 metadata (one `head` at eviction time) equals the file's own hash; a
definite verdict is held while neither the file nor the object changes. A file being
served or decoded is pinned too (`pinned()`, around each reader's decode of an original).

The pull's lock and the pins cover one process only. Two processes (the align server and its
DSP child, say) may pull the same object at once: each lands it through its own `.part` file and
an atomic rename, so that costs a second download, never a partial file. A pin in one process
does not stop an eviction another process runs. So nothing the bucket does not hold is ever evicted, and
until the originals are pushed, every file here is pinned.

This module imports no audio library: a caller that wants only a path can import it anywhere.
"""

import contextlib
import os
import re
import sys
import threading

import audio_bucket
import bucketio
import cache_budget
import envfile

CACHE = "sources_flac"

# The rungs, best first. A file whose extension is in none of them is not an original.
UNCOMPRESSED = (".wav", ".au")
LOSSLESS_MASTER = (".wv", ".aif", ".aiff")
FLAC = (".flac",)
LOSSY = (".mp3", ".m4a", ".opus", ".ogg", ".aac")
RUNGS = (UNCOMPRESSED, LOSSLESS_MASTER, FLAC, LOSSY)
_RANK = {ext: i for i, exts in enumerate(RUNGS) for ext in exts}
_FLAC_RUNG = _RANK[".flac"]

_NUMBERED_RE = re.compile(r"^(\d{3})-.")
_MYSTERY_RE = re.compile(r"^mystery\s+track\s+(\d+)", re.IGNORECASE)

# This module sits in scripts/; the checkout whose sources_local/ is the default is one level
# up. Tests point it elsewhere.
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Ambiguous(ValueError):
    """Two files at the rung that wins for one stem. `names` lists them, sorted. A ValueError,
    so a caller that reports a bad request reports this one too."""

    def __init__(self, what, names):
        self.names = sorted(names)
        super().__init__("%s: two files where one is expected, keep one: %s"
                         % (what, ", ".join(self.names)))


# --- the rule ------------------------------------------------------------------------------

def sources_dir():
    """The originals directory: NETRADIO_SOURCES_FLAC_CACHE_DIR, else this checkout's
    `sources_local/`."""
    value = os.environ.get("NETRADIO_SOURCES_FLAC_CACHE_DIR", "").strip()
    if value:
        return os.path.expanduser(value)
    return os.path.join(ROOT_DIR, "sources_local")


def rank(name):
    """The rung of a file name (0 is the best), or None when it is not an original."""
    return _RANK.get(os.path.splitext(name)[1].lower())


def number(name):
    """The track number an original's name carries (`072-...` -> 72), or None."""
    m = _NUMBERED_RE.match(name)
    return int(m.group(1)) if m and rank(name) is not None else None


def mystery_number(name):
    """The mystery's number a clip's name carries (`Mystery Track 4.wav` -> 4), or None."""
    m = _MYSTERY_RE.match(os.path.splitext(name)[0])
    return int(m.group(1)) if m and rank(name) is not None else None


def stem_key(name):
    """The stem a file belongs to: ("track", N), ("mystery", N) or ("name", <name less ext>).
    None when it is not an original."""
    if rank(name) is None:
        return None
    num = number(name)
    if num is not None:
        return ("track", num)
    num = mystery_number(name)
    if num is not None:
        return ("mystery", num)
    return ("name", os.path.splitext(name)[0])


def choose(names, what="this stem"):
    """The one name the rule picks among one stem's file names, or None when none is an
    original. Raises `Ambiguous` when two share the rung that wins."""
    ranked = [(rank(n), n) for n in names if rank(n) is not None]
    if not ranked:
        return None
    best = min(r for r, _ in ranked)
    top = sorted(n for r, n in ranked if r == best)
    if len(top) > 1:
        raise Ambiguous(what, top)
    return top[0]


def _what(key):
    if key[0] == "track":
        return "original %03d" % key[1]
    if key[0] == "mystery":
        return "Mystery Track %d" % key[1]
    return "original %s" % key[1]


def contained(path, root):
    """True if `path` resolves inside `root` (the realpath-containment check)."""
    rp = os.path.realpath(path)
    rr = os.path.realpath(root)
    return rp == rr or rp.startswith(rr + os.sep)


def local_names(src=None):
    """The originals in the directory: regular files (a symlink to one counts) that resolve
    inside it and carry an original's extension, sorted. [] when it does not exist."""
    src = src or sources_dir()
    if not src or not os.path.isdir(src):
        return []
    out = []
    for name in sorted(os.listdir(src)):
        path = os.path.join(src, name)
        if rank(name) is not None and os.path.isfile(path) and contained(path, src):
            out.append(name)
    return out


def group(names):
    """{stem key: [names]} for the originals among `names`."""
    out = {}
    for name in names:
        key = stem_key(name)
        if key is not None:
            out.setdefault(key, []).append(name)
    return out


def resolve(num, src=None, pull=True):
    """The path of track `num`'s original, or None. A local candidate is chosen by the rule;
    with none, the bucket's candidates are, and the pick is pulled (`pull=False`: never).
    Raises `Ambiguous` when two files share the winning rung, here or in the bucket. A pull
    that fails is printed and answers None."""
    src = src or sources_dir()
    key = ("track", int(num))
    names = group(local_names(src)).get(key, [])
    chosen = choose(names, _what(key))
    if chosen:
        return os.path.join(src, chosen)
    if not pull:
        return None
    path, failed = pull_stem(key, src)
    if failed is not None and failed[0] != 404:
        # stderr: in the inspector's DSP child stdout is the JSON channel
        print("sources: %s" % failed[1], file=sys.stderr, flush=True)
    return path


def nameable(num, src=None):
    """True when track `num`'s original can be had: a local candidate, or a pullable one in a
    held listing of the bucket. Pulls nothing; an ambiguous stem is nameable (the read that
    follows reports it)."""
    src = src or sources_dir()
    key = ("track", int(num))
    if group(local_names(src)).get(key):
        return True
    try:
        return _bucket_pick(key, src)[0] is not None
    except Ambiguous:
        return True


def originals(src=None, listed=True):
    """(chosen, refused) over every stem the directory holds, and, with `listed`, every stem
    the bucket holds that this directory could pull: chosen is {stem key: name}, refused is
    {stem key: [names]} for a stem whose winning rung holds two files. A stem with a local
    candidate is chosen among the local files only; one only the bucket holds is chosen among
    the bucket's, as the name its pull would land. Pulls nothing."""
    src = src or sources_dir()
    chosen, refused = {}, {}
    local = group(local_names(src))
    for key, names in local.items():
        try:
            chosen[key] = choose(names, _what(key))
        except Ambiguous as exc:
            refused[key] = exc.names
    if listed:
        listing = _pull_listing(src)
        if listing is not None:
            for key, names in group(listing).items():
                if key in local:
                    continue
                try:
                    pick = _pull_name(names, key)
                except Ambiguous as exc:
                    refused[key] = exc.names
                    continue
                if pick:
                    chosen[key] = pick
    return chosen, refused


def path_of(name, src=None, pull=True):
    """(path, None) for one original by its file name, pulled when it is not here (`pull=False`:
    never), else (None, (http status, why))."""
    src = src or sources_dir()
    path = os.path.join(src, name)
    if os.path.basename(name) != name or not contained(path, src):
        return None, (404, "%s: not an original's name" % name)
    if os.path.isfile(path):
        return path, None
    if not pull:
        return None, (404, "%s: not on this disk" % name)
    return fetch(name, src)


def list_originals(src=None):
    """[{num, name}] for every numbered original the align tool can name: one row per track
    number, from this directory and the bucket's listing. `name` is the chosen file's name less
    its `NNN-` prefix and extension; a refused number is listed under its first file's name, so
    opening it reports the two files."""
    chosen, refused = originals(src)
    rows = {}
    for key, name in chosen.items():
        if key[0] == "track":
            rows[key[1]] = name
    for key, names in refused.items():
        if key[0] == "track":
            rows[key[1]] = names[0]
    return [{"num": n, "name": os.path.splitext(rows[n])[0][4:]} for n in sorted(rows)]


# --- the pins ------------------------------------------------------------------------------

_pin_lock = threading.Lock()
_pins = {}                  # realpath -> how many holders pin it


@contextlib.contextmanager
def pinned(path):
    """Pin one file for the flight of a serve or a decode: no eviction this process runs
    removes it meanwhile. Counted, so pins nest."""
    real = os.path.realpath(path)
    with _pin_lock:
        _pins[real] = _pins.get(real, 0) + 1
    try:
        yield path
    finally:
        with _pin_lock:
            left = _pins.get(real, 1) - 1
            if left > 0:
                _pins[real] = left
            else:
                _pins.pop(real, None)


def _in_use(path):
    if os.path.realpath(path) in _pins:
        return True
    # a decode pins its file in streamalign.audio; asked only when that module is already
    # loaded, so this module never imports an audio library
    audio = sys.modules.get("streamalign.audio")
    probe = getattr(audio, "_pinned_entry", None)
    return bool(probe and probe(path))


_verified = {}              # (realpath, size, mtime_ns, etag) -> bool: a verdict held


def backed(path, bucket=None):
    """True when the audio bucket holds this file verified: a `sources/<file name>` object in
    one listing of the prefix with the file's size, whose SHA-256 metadata (one `head`) equals
    the file's own hash. False on any doubt: no bucket, no answer, no object, no metadata.

    A definite verdict (a match, a hash or size mismatch, or no SHA-256 metadata) is held for
    the same file (size and mtime) and the same object (the listing's ETag), so a later
    eviction run asks the bucket once more only when either has changed. A `head` that gets
    no answer is never held."""
    bucket = bucket or audio_bucket.bucket()
    if bucket is None:
        return False
    key = audio_bucket.SOURCES_PREFIX + os.path.basename(path)
    listed, err = bucketio.listing(bucket, audio_bucket.SOURCES_PREFIX)
    if err is not None:
        return False
    try:
        st = os.stat(path)
    except OSError:
        return False
    row = listed.get(key)
    if row is None or row.size != st.st_size:
        return False
    held = (os.path.realpath(path), st.st_size, st.st_mtime_ns, row.etag)
    if held in _verified:
        return _verified[held]
    got, err = bucketio.head(bucket, key)
    if got is None:
        return False                # no answer: asked again next time, never held
    if not got.sha256 or got.size != st.st_size:
        same = False
    else:
        try:
            same = bucketio.digests(path).sha256 == got.sha256
        except OSError:
            return False
    # a definite answer is held, either way, until the file or the object changes
    if len(_verified) > 4096:
        _verified.clear()
    _verified[held] = same
    return same


def _pinned(path):
    """The policy's pin for `sources_flac`: in use here, or not held verified by the bucket.
    A dead write (`.part`/`.tmp`) is not an original and is never pinned: the policy holds a
    live one by its age, and lets a dead one go."""
    if path.endswith(cache_budget.IN_PROGRESS):
        return False
    if _in_use(path):
        return True
    return not backed(path)


def register_cache():
    """Put the originals directory on the one cache policy as `sources_flac`. Reads the
    environment at call time; call it again to re-read it. Returns the cache's record, or None
    when the policy is dark or the directory is refused (printed): then nothing is pulled."""
    # rank None: the floor never takes an original. The cap alone evicts here, and only what
    # the bucket holds verified, so a master copy is never removed to relieve the disk. The
    # literal name, so env_check.py's code scan sees the registration and counts its variable
    # family as read.
    try:
        return cache_budget.register("sources_flac", dir=sources_dir(), pinned=_pinned,
                                     refill="audio bucket: sources/<name>", rank=None)
    except cache_budget.CacheRefused as exc:     # a clash with another process's cache too
        print("%s; the originals are read, and nothing is pulled" % exc, file=sys.stderr)
        return None


register_cache()


# --- the pull ------------------------------------------------------------------------------

_locks = {}
_locks_guard = threading.Lock()


def _lock(name):
    with _locks_guard:
        return _locks.setdefault(name, threading.Lock())


def _held_dir(src):
    """True when `src` is this process's registered `sources_flac` directory."""
    held = cache_budget.dir_of(CACHE) if cache_budget.registered(CACHE) else None
    return bool(held and src and os.path.realpath(held) == os.path.realpath(src))


def _bucket():
    return None if envfile.flag("NETRADIO_READ_ONLY") else audio_bucket.bucket()


def _pull_listing(src):
    """[file names under `sources/`] when `src` may be pulled into, else None: no bucket,
    read-only, not the registered cache, or no answer. The listing is held about a minute
    (`bucketio.listing`), so a burst of requests costs one LIST."""
    if not _held_dir(src):
        return None
    bucket = _bucket()
    if bucket is None:
        return None
    listed, err = bucketio.listing(bucket, audio_bucket.SOURCES_PREFIX)
    if err is not None:
        return None
    prefix = audio_bucket.SOURCES_PREFIX
    names = [k[len(prefix):] for k in listed if k.startswith(prefix)]
    return [n for n in names if n and "/" not in n]


def _pull_name(names, key):
    """The name a pull of this stem lands, among the bucket's `names` for it: the rule's pick,
    or its `.flac` sibling when the pick is a master (the cache keeps flac). None when a
    master has no flac sibling there. Raises `Ambiguous`."""
    pick = choose(names, _what(key))
    if pick is None or rank(pick) >= _FLAC_RUNG:
        return pick
    sibling = os.path.splitext(pick)[0] + ".flac"
    return sibling if sibling in names else None


def _bucket_pick(key, src):
    """(name, None) for the file a pull of this stem would land, else (None, (status, why)).
    Raises `Ambiguous`."""
    listing = _pull_listing(src)
    if listing is None:
        return None, (404, "%s: not on this disk, and nothing to pull it from" % _what(key))
    names = group(listing).get(key, [])
    pick = _pull_name(names, key)
    if pick is None:
        master = choose(names, _what(key))
        if master is not None:
            # the cache keeps flac: a master alone in the bucket is not pulled
            return None, (503, "%s: not on this disk; the audio bucket holds %s but no FLAC "
                               "copy of it" % (_what(key), master))
        return None, (404, "%s: not on this disk or in the audio bucket" % _what(key))
    return pick, None


def pull_stem(key, src=None):
    """(path, None) once this stem's pick is pulled, else (None, (status, why)). Raises
    `Ambiguous` when the bucket holds two files at the winning rung."""
    src = src or sources_dir()
    name, failed = _bucket_pick(key, src)
    if name is None:
        return None, failed
    return fetch(name, src)


def fetch(name, src=None):
    """(path, None) once `<name>` is in the originals directory, pulled from `sources/<name>`
    when it is not, else (None, (http status, why)): 404 nothing to pull it from or no such
    object, 502 no answer or a refused object, 507 no room."""
    src = src or sources_dir()
    path = os.path.join(src, name)
    if os.path.basename(name) != name or not contained(path, src):
        return None, (404, "%s: not an original's name" % name)
    if os.path.isfile(path):
        return path, None
    if not _held_dir(src):
        return None, (404, "%s: not on this disk, and the originals directory is not the "
                           "registered sources_flac cache (set NETRADIO_CACHE_ROOT), so "
                           "nothing is pulled" % name)
    bucket = _bucket()
    if bucket is None:
        return None, (404, "%s: not on this disk, and no audio bucket set" % name)
    with _lock(name):
        if os.path.isfile(path):
            return path, None               # a request ahead of this one pulled it
        key = audio_bucket.SOURCES_PREFIX + name
        got, err = bucketio.head(bucket, key)
        if got is None:
            if err is None:
                return None, (404, "%s: not on this disk or in the audio bucket" % name)
            return None, (502, "%s: %s" % (key, err))
        if not got.sha256:
            return None, (502, "%s: no sha256 metadata on the object; refused" % key)
        with pinned(path):
            if not cache_budget.reserve(CACHE, got.size):
                return None, (507, "%s: the sources_flac cache refused the room (the disk is "
                                   "past its floor, or the cache is full of files the bucket "
                                   "does not hold verified)" % key)
            os.makedirs(src, exist_ok=True)
            why = bucketio.fetch_verified(bucket, key, path, size=got.size, sha256=got.sha256)
            if why is not None:
                return None, (502, "%s: %s" % (key, why))
            cache_budget.commit(CACHE, path)
    return path, None
