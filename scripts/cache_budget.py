"""One cache policy for every cache, in both repos (docs/PLAN_data_tiering.md §2).

TWIN FILE. This module is byte-identical in the player repo (root) and the analysis repo
(`scripts/`); `make sync` refuses to run when the two copies differ, the way it does for
`scripts/tracklist_sync.sh`. It imports nothing from either repo: the environment and the
registry are all it reads.

WHAT A CACHE IS HERE
--------------------
A directory whose every entry has a refill path (a bucket object, a re-decode, a re-fetch), so
the policy may delete an entry at any moment. Each cache registers ONCE, at import, in the
module that owns it, with a record the eviction run reads (§2.2): a name, a directory, a cap,
headroom, an age limit, an order, a pin predicate, a refill path and a rank. Nothing is
discovered by scanning the disk.

THE FOUR CALLS
--------------
  reserve(name, nbytes, path=None)   make room, then add (§2.3): evict by the cache's order until
                                     the entry fits under cap - headroom; refuse when it cannot
                                     (everything pinned), when the volume is past the floor, or
                                     when the cache is dark. nbytes=None is a write of unplanned
                                     length: admitted while the cache is under its cap; it may
                                     run into the headroom and past the cap and is never killed.
  commit(name, path)                 record the entry ("landed"); when the cache is over cap or
                                     the volume past the floor, run the eviction there and then
                                     on OTHER entries, never the file just written.
  remove(name, path, reason)         the ONLY way a lifecycle deletes a cache entry (Tim,
                                     2026-09-17): refuses a path outside the cache's directory
                                     and a pinned entry, records the event with the caller's
                                     reason, updates the accounting.
  run() / periodic() / startup()     the eviction run (§2.3): max_age, over-cap by each cache's
                                     order, then the disk floor with the cross-cache ranking.
                                     One run at a time per machine (a kernel flock, see THE
                                     LOCK); a run that finds the lock held is skipped and
                                     recorded, never queued.

THE LOCK
--------
One eviction at a time per directory, so a cache is never evicted from twice at once — and that
covers the eviction a WRITER triggers inside `reserve` and `commit`, not just the periodic run.
Where the lock file sits follows one rule (Tim, 2026-09-18):

  NETRADIO_CACHE_ROOT set     `$NETRADIO_CACHE_ROOT/.eviction.lock`, one lock for the machine.
  NETRADIO_CACHE_ROOT unset   `<cache dir>/.eviction.lock`, one lock per live cache.

The cross-cache floor pass runs only under the machine lock, so it is root-only wherever it is
reached: in the run, and in the `commit` of a write that finds the volume past the floor.

A writer that finds the lock held never waits: `reserve` admits an unplanned write (its length
is not the caller's to give up) and refuses a planned one with `why="locked"`, and the next
periodic run corrects any overflow.

DARK
----
NETRADIO_CACHE_ROOT has NO default in code. Unset, nothing is derived from `~` and nothing is
created: `cache_root()` is None, a cache with no directory of ITS OWN resolves to None and
`reserve` refuses it, and the event log, the machine lock and `.eviction-last.json` — all three
under the root — are never written. The value a machine runs with (`~/Netradio/cache` on the
mini after the move) is set in `.env`, never assumed here, so a worktree, CI or a test process
that forgets the variable cannot touch the real machine's caches.

A cache whose OWNER supplies a directory (thumbs under the download root, streamalign under
NETRADIO_ALIGN_CACHE, the harvester's `.harvest/`) is live whether or not the root is set, and
so is its eviction: with the root unset `run()` walks every registered cache that has a live
directory, takes that cache's own lock, and applies its age and its cap. The cross-cache floor
pass needs the ranking over every cache at once, so it stays root-only. A registered cache whose
directory does not exist is dark: status shows it as missing, the run never touches it, reserve
refuses. Registration itself creates nothing. Tests set the variables to temporary directories
they own.

ENFORCE
-------
`enforce=False` registers a cache for status and ranking only: reserve, commit and the run are
no-ops on it. The listening and harvest caches register that way in PR 1 and PR C of the
fetch/analyse plan flips them (their eviction needs the bucket as the refill path first).
"""

import contextlib
import fcntl
import json
import os
import re
import shutil
import threading
import time
from collections import namedtuple
from datetime import datetime, timezone

# --- the vocabulary (§2.2, §2.3, §2.5, §2.6) -------------------------------------------------

NAMES = ("streamalign", "listening", "harvest", "keep", "thumbs", "clips", "stream_mp3",
         "stream_master", "sources_flac", "stream_tracks", "candidates", "chroma")
ORDERS = ("oldest-added", "newest-added", "by-score")
EVENTS = ("landed", "published", "signed", "heard", "moved", "evicted", "expired")
# The cross-cache ranking, evicted first -> last, used only when the floor is breached and no
# cache is over its cap (§2.3). The listening cache holds two ranks: its `trash/` entries go
# first of everything and its `unplayed/` entries last before `keep`, which is never evicted.
RANK = {"harvest": 2, "streamalign": 3, "chroma": 4, "stream_master": 5, "sources_flac": 6,
        "stream_mp3": 7, "stream_tracks": 8, "thumbs": 9, "candidates": 10, "clips": 11,
        "keep": "never"}
LISTENING_TRASH_RANK, LISTENING_HEAD_RANK = 1, 12

GB = 1_000_000_000          # a decimal GB: `0.25` is 250 MB (§2.2)
MB = 1_000_000
DEFAULT_CAP_GB = "4"
DEFAULT_DISK_MAX_PCT = 82
DEFAULT_EVENTS_DAYS = 30
START_SKIP_S = 300          # a second server start within five minutes of a recorded run skips
EVENTS_FILE = "events.jsonl"
LOCK_FILE = ".eviction.lock"
LAST_RUN_FILE = ".eviction-last.json"
_CAP_RE = re.compile(r"^(none|[0-9]+(\.[0-9]+)?)$")
_INT_RE = re.compile(r"^[0-9]+$")
_SKIP_SUFFIXES = (".tmp", ".part")

# Seams (tests replace them): the disk reading the floor is measured on, and the clock.
disk_usage = shutil.disk_usage
now = time.time


class CacheConfigError(ValueError):
    """A variable of §2.6 holds a value the grammar refuses. The message names the variable."""


Entry = namedtuple("Entry", "path bytes mtime")


class Cache:
    """One registry record (§2.2). The directory, cap, headroom and age are resolved from the
    environment at CALL time, never at registration: the player loads `.env` after importing
    the modules that register."""

    def __init__(self, name, dir_default=None, cap_default=DEFAULT_CAP_GB, headroom_default=0,
                 max_age_default=None, order="oldest-added", pinned=None, pinned_paths=None,
                 refill="none", rank=None, enforce=True, is_entry=None, score=None):
        if name not in NAMES:
            raise CacheConfigError("unknown cache name %r (one of %s)" % (name, ", ".join(NAMES)))
        if not (callable(order) or order in ORDERS):
            raise CacheConfigError("cache %s: order %r is not one of %s" % (name, order, ORDERS))
        if order == "by-score" and score is None:
            raise CacheConfigError("cache %s: order by-score needs a score(entry) callable" % name)
        self.name = name
        self.dir_default = dir_default          # str, callable -> str, or None
        self.cap_default = cap_default          # a §2.6 cap string: "4", "0.25", "none"
        self.headroom_default = headroom_default        # bytes
        self.max_age_default = max_age_default          # days or None
        self.order = order                      # a name from ORDERS, or key(entry) -> sortable
        self.pinned = pinned                    # entry -> bool
        self.pinned_paths = pinned_paths        # () -> set of paths, evaluated once per pass
        self.refill = refill
        self.rank = RANK.get(name) if rank is None else rank    # int, "never" or entry -> int
        self.enforce = enforce
        self.is_entry = is_entry                # path -> bool; None = every regular file
        self.score = score                      # entry -> float; higher is evicted first


_registry = {}
_lock = threading.RLock()           # the registry and the in-process eviction state
_inflight = set()                   # paths reserved and not yet committed: pinned meanwhile
_evicted_since_start = {}           # name -> count
_last_run = {}                      # the last run's summary in this process
_last_skip = None                   # the last skipped run: {"at", "why"}
_status_cache = {"at": 0.0, "value": None}
STATUS_TTL_S = 60


def register(name, **fields):
    """Register a cache (idempotent by name: a second call replaces the record). Returns it."""
    rec = Cache(name, **fields)
    with _lock:
        _registry[name] = rec
    return rec


def unregister(name):
    with _lock:
        _registry.pop(name, None)


def registered():
    """The registered records, in the order of NAMES."""
    with _lock:
        return [_registry[n] for n in NAMES if n in _registry]


def record(name):
    with _lock:
        return _registry.get(name)


# --- the environment contract (§2.6) --------------------------------------------------------

def var(name, field):
    """`NETRADIO_<NAME>_CACHE_<FIELD>`, the one shape both repos read."""
    return "NETRADIO_%s_CACHE_%s" % (name.upper(), field)


def cache_root():
    """The root every cache defaults under, or None when NETRADIO_CACHE_ROOT is unset (dark)."""
    raw = os.environ.get("NETRADIO_CACHE_ROOT", "").strip()
    return os.path.expanduser(raw) if raw else None


def _parse_cap(raw, variable):
    text = str(raw).strip()
    if not _CAP_RE.match(text):
        raise CacheConfigError("%s=%r: expected GB (decimal allowed, e.g. 0.25) or none"
                               % (variable, raw))
    return None if text == "none" else int(float(text) * GB)


def _parse_int(raw, variable, none_ok=False):
    text = str(raw).strip()
    if none_ok and text == "none":
        return None
    if not _INT_RE.match(text):
        raise CacheConfigError("%s=%r: expected a whole number%s"
                               % (variable, raw, " or none" if none_ok else ""))
    return int(text)


def dir_of(name):
    """The cache's one directory: `NETRADIO_<NAME>_CACHE_DIR`, else the owner's default, else
    `$NETRADIO_CACHE_ROOT/<name>`. Works for a name that is not registered (a reader that only
    needs the path, such as the player's view of the harvester's signature cache)."""
    raw = os.environ.get(var(name, "DIR"), "").strip()
    if raw:
        return os.path.expanduser(raw)
    rec = record(name)
    default = rec.dir_default if rec else None
    if callable(default):
        default = default()
    if default:
        return os.path.expanduser(default)
    root = cache_root()
    return os.path.join(root, name) if root else None


def cap_of(name):
    """Bytes, or None for an uncapped cache. Raises CacheConfigError on a bad value."""
    variable = var(name, "GB")
    rec = record(name)
    raw = os.environ.get(variable, "").strip()
    if raw:
        return _parse_cap(raw, variable)
    return _parse_cap(rec.cap_default if rec else DEFAULT_CAP_GB, variable + " (default)")


def headroom_of(name):
    variable = var(name, "HEADROOM_MB")
    raw = os.environ.get(variable, "").strip()
    if raw:
        return _parse_int(raw, variable) * MB
    rec = record(name)
    return int(rec.headroom_default) if rec else 0


def max_age_of(name):
    """Days, or None."""
    variable = var(name, "MAX_AGE_DAYS")
    raw = os.environ.get(variable, "").strip()
    if raw:
        return _parse_int(raw, variable, none_ok=True)
    rec = record(name)
    return rec.max_age_default if rec else None


def disk_max_pct():
    """The floor, a real percentage: 1..100. Outside that range the backstop would never fire
    (101) or refuse everything (0), so the grammar refuses the value instead."""
    raw = os.environ.get("NETRADIO_DISK_MAX_PCT", "").strip()
    if not raw:
        return DEFAULT_DISK_MAX_PCT
    pct = _parse_int(raw, "NETRADIO_DISK_MAX_PCT")
    if not 1 <= pct <= 100:
        raise CacheConfigError("NETRADIO_DISK_MAX_PCT=%r: expected a percentage from 1 to 100" % raw)
    return pct


def events_days():
    raw = os.environ.get("NETRADIO_CACHE_EVENTS_DAYS", "").strip()
    return _parse_int(raw, "NETRADIO_CACHE_EVENTS_DAYS") if raw else DEFAULT_EVENTS_DAYS


def resolve(name):
    """Every setting of one cache, resolved now. Raises CacheConfigError on a bad value."""
    rec = record(name)
    if rec is None:
        raise CacheConfigError("cache %r is not registered" % name)
    return {"name": name, "dir": dir_of(name), "cap": cap_of(name), "headroom": headroom_of(name),
            "max_age": max_age_of(name),
            "order": rec.order if isinstance(rec.order, str) else "custom",
            "refill": rec.refill, "rank": rec.rank if not callable(rec.rank) else "per-entry",
            "enforce": rec.enforce}


def validate():
    """Resolve every registered cache and the two global variables; raise on the first bad one.
    The servers call this at start and refuse to start on an error (§2.6 grammar)."""
    disk_max_pct()
    events_days()
    return [resolve(rec.name) for rec in registered()]


def ensure_dir(name):
    """Create the cache's directory for a writer, WITHOUT defeating dark mode: a directory named
    by the owner or by `NETRADIO_<NAME>_CACHE_DIR` is created; a directory that only follows
    from `NETRADIO_CACHE_ROOT` is created only when that root already exists (a root that is
    named but absent stays dark, like an unset one). Returns the directory, or None when dark."""
    d = dir_of(name)
    if not d:
        return None
    root = cache_root()
    if root and os.path.realpath(d) == os.path.realpath(os.path.join(root, name)) \
            and not os.path.isdir(root):
        return None
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        return None
    return d


def is_dark(name):
    root = dir_of(name)
    return not root or not os.path.isdir(root)


# --- the disk floor (§2.1, §2.3; assumption 22) ---------------------------------------------

def disk_pct(path):
    """Percent of the volume holding `path` in use, as used / (used + free). 0.0 if unknown."""
    try:
        _total, used, free = disk_usage(path)
    except OSError:
        return 0.0
    denom = used + free
    return (used / denom * 100.0) if denom else 0.0


def over_floor(path):
    return disk_pct(path) >= disk_max_pct()


# --- entries -------------------------------------------------------------------------------

def _is_entry(rec, path):
    base = os.path.basename(path)
    if base.startswith(".") or base.endswith(_SKIP_SUFFIXES):
        return False
    return rec.is_entry(path) if rec.is_entry else True


def entries(name):
    """Every entry of the cache, subdirectories included, as Entry(path, bytes, mtime)."""
    rec = record(name)
    root = dir_of(name)
    out = []
    if rec is None or not root or not os.path.isdir(root):
        return out
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in files:
            path = os.path.join(dirpath, f)
            if not _is_entry(rec, path):
                continue
            try:
                st = os.stat(path)
            except OSError:
                continue
            out.append(Entry(path, st.st_size, st.st_mtime))
    return out


def size_of(name):
    return sum(e.bytes for e in entries(name))


def _pinned_set(rec):
    if rec.pinned_paths is None:
        return set()
    try:
        return set(os.path.realpath(p) for p in (rec.pinned_paths() or ()))
    except Exception:       # noqa: BLE001 — a predicate that fails pins nothing it cannot see
        return set()


def _is_pinned(rec, entry, pinned_set):
    real = os.path.realpath(entry.path)
    with _lock:
        if real in _inflight or entry.path in _inflight:
            return True
    if real in pinned_set:
        return True
    if rec.pinned is not None:
        try:
            return bool(rec.pinned(entry))
        except Exception:   # noqa: BLE001 — a predicate that fails pins the entry (never deletes)
            return True
    return False


def _order_key(rec):
    if callable(rec.order):
        return rec.order
    if rec.order == "oldest-added":
        return lambda e: e.mtime
    if rec.order == "newest-added":
        return lambda e: -e.mtime
    return lambda e: -float(rec.score(e))          # by-score: the worst (highest score) first


def ordered(name):
    """The cache's entries in ITS eviction order, first to go first."""
    rec = record(name)
    if rec is None:
        return []
    return sorted(entries(name), key=_order_key(rec))


def _rank_of(rec, entry):
    r = rec.rank
    if callable(r):
        return r(entry)
    return r


def _contained(path, root):
    if not root:
        return False
    rp = os.path.realpath(path)
    rr = os.path.realpath(root)
    return rp == rr or rp.startswith(rr.rstrip(os.sep) + os.sep)


# --- the event log (§2.5) -------------------------------------------------------------------

def events_path():
    root = cache_root()
    return os.path.join(root, EVENTS_FILE) if root else None


def log_event(cache, event, path, nbytes=None, reason=None):
    """Append one record: {"ts","cache","event","path","bytes","reason"}. Dark (dropped) when
    the cache root does not exist. `event` must be one of EVENTS."""
    if event not in EVENTS:
        raise ValueError("event %r is not one of %s" % (event, EVENTS))
    root = cache_root()
    if not root or not os.path.isdir(root):
        return False
    rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "cache": cache,
           "event": event, "path": path, "bytes": nbytes, "reason": reason}
    try:
        with _lock:
            with open(events_path(), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        return False
    return True


def read_events(hours=24, limit=None):
    """The records of the last `hours`, newest first (the caches page's read)."""
    path = events_path()
    cutoff = datetime.fromtimestamp(now() - hours * 3600, timezone.utc).isoformat(timespec="seconds")
    out = []
    if not path:
        return out
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and (rec.get("ts") or "") >= cutoff:
                    out.append(rec)
    except OSError:
        return out
    out.reverse()
    return out[:limit] if limit else out


def prune_events(days=None):
    """Rewrite the event log dropping records older than `days` (NETRADIO_CACHE_EVENTS_DAYS).
    Returns the number dropped."""
    days = events_days() if days is None else days
    path = events_path()
    if not path:
        return 0
    cutoff = datetime.fromtimestamp(now() - days * 86400, timezone.utc).isoformat(timespec="seconds")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return 0
    keep = []
    for line in lines:
        try:
            ts = json.loads(line).get("ts") or ""
        except (ValueError, AttributeError):
            continue
        if ts >= cutoff:
            keep.append(line)
    dropped = len(lines) - len(keep)
    if dropped:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.writelines(keep)
        os.replace(tmp, path)
    return dropped


# --- the eviction lock (§2.3) ---------------------------------------------------------------

def lock_path(name=None):
    """Where the eviction lock for `name` lives, or None when there is nothing to lock.

    With NETRADIO_CACHE_ROOT set it is the machine lock, `$ROOT/.eviction.lock`, shared by every
    cache. With the root unset the caches that are live anyway lock in their OWN directories, so
    their eviction still runs one at a time (Tim, 2026-09-18). The file is a dot-file inside a
    directory its owner already writes, and `_is_entry` never counts it."""
    root = cache_root()
    if root and os.path.isdir(root):
        return os.path.join(root, LOCK_FILE)
    d = dir_of(name) if name else None
    return os.path.join(d, LOCK_FILE) if d and os.path.isdir(d) else None


@contextlib.contextmanager
def _locked(path):
    """Hold the eviction lock at `path` for the block, never waiting for it (§2.3: a run that
    finds it held is skipped, never queued). Yields (held, why): (True, None) when it is ours,
    (False, "locked") when another process holds it, (False, "lock-unopenable") when the file
    cannot be opened, and (True, None) when `path` is None — there is no lock to take, and
    nothing that could be contending for it."""
    if not path:
        yield True, None
        return
    try:
        fh = open(path, "a")
    except OSError:
        yield False, "lock-unopenable"
        return
    try:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False, "locked"
            return
        yield True, None
    finally:
        fh.close()          # closing the descriptor releases the kernel lock


# --- reserve / commit / remove (§2.1, §2.3) ---------------------------------------------------

def _delete(rec, entry, event, reason):
    """Delete one entry and account for it. Returns the bytes freed (0 when already gone)."""
    try:
        os.remove(entry.path)
    except FileNotFoundError:
        pass
    except OSError:
        return 0
    with _lock:
        _evicted_since_start[rec.name] = _evicted_since_start.get(rec.name, 0) + 1
    log_event(rec.name, event, entry.path, entry.bytes, reason)
    return entry.bytes


def _evict_until(rec, need, reason, exclude=None):
    """Evict in the cache's order while `need(freed_so_far)` is True. Skips pinned entries and
    counts them. Returns (evicted, freed, pinned, satisfied)."""
    pinned_set = _pinned_set(rec)
    exclude = os.path.realpath(exclude) if exclude else None
    evicted = freed = pinned = 0
    for entry in ordered(rec.name):
        if not need(freed):
            return evicted, freed, pinned, True
        if exclude and os.path.realpath(entry.path) == exclude:
            continue
        if _is_pinned(rec, entry, pinned_set):
            pinned += 1
            continue
        got = _delete(rec, entry, "evicted", reason)
        if got or not os.path.exists(entry.path):
            evicted += 1
            freed += entry.bytes
    return evicted, freed, pinned, not need(freed)


def reserve(name, nbytes, path=None):
    """Make room for `nbytes` (None: unplanned length), then admit. Returns (ok, why).

    `path`, when given, must lie inside the cache's directory as resolved NOW (a path built from
    an import-time idea of the directory is refused) and is pinned from now until
    commit()/release() — the file being written is never evicted from under its writer (§5's
    "the file being decoded")."""
    rec = record(name)
    if rec is None:
        return False, "unregistered"
    if not rec.enforce:
        _hold(path)
        return True, None
    root = dir_of(name)
    if not root or not os.path.isdir(root):
        return False, "dark"
    if path and not _contained(path, root):
        return False, "outside"          # a writer's stale idea of the directory is refused
    if over_floor(root):
        return False, "floor"
    cap = cap_of(name)
    if cap is None:
        _hold(path)
        return True, None
    size = size_of(name)
    if nbytes is None:                        # unplanned length: admitted while under the cap
        if size < cap:
            _hold(path)
            return True, None
        need = lambda freed: size - freed >= cap   # noqa: E731
        with _locked(lock_path(name)) as (held, _why):
            if not held:            # someone else is evicting here: an unplanned write is still
                _hold(path)         # admitted, and their run (or the next one) corrects the cap
                return True, None
            _evicted, _freed, _pinned, ok = _evict_until(rec, need, "by-cap")
        if ok:
            _hold(path)
            return True, None
        return False, "all-pinned"
    limit = cap - headroom_of(name)
    if nbytes > limit:
        return False, "larger-than-cap"
    need = lambda freed: size - freed + nbytes > limit   # noqa: E731
    if not need(0):                           # it fits as things stand: nothing to evict, no lock
        _hold(path)
        return True, None
    with _locked(lock_path(name)) as (held, _why):
        if not held:                # a planned write needs room MADE, and making it is exactly
            return False, "locked"  # what the other process is doing: refuse, and let it finish
        _evicted, _freed, _pinned, ok = _evict_until(rec, need, "by-cap")
    if ok:
        _hold(path)
        return True, None
    return False, "all-pinned"


def _hold(path):
    if path:
        with _lock:
            _inflight.add(os.path.realpath(path))


def release(name, path):
    """Abandon a reservation (the write failed); the path is no longer pinned."""
    with _lock:
        _inflight.discard(os.path.realpath(path))


def commit(name, path, reason=None):
    """Record a write ("landed") and, when the cache is over cap or the volume past the floor,
    run the eviction at once on OTHER entries. Returns (ok, why).

    The floor pass here is root-only, like the run's: it evicts across caches by the ranking, and
    the lock this call holds is one cache's. Without the root there is no machine-wide lock to
    serialise the other caches' deletions against, so `reserve`'s refusal past the floor and the
    next run's age and cap passes are what bounds a rootless machine."""
    rec = record(name)
    release(name, path)
    if rec is None:
        return False, "unregistered"
    root = dir_of(name)
    if not _contained(path, root):
        return False, "outside"
    try:
        nbytes = os.path.getsize(path)
    except OSError:
        return False, "missing"
    log_event(name, "landed", path, nbytes, reason)
    if not rec.enforce or not root or not os.path.isdir(root):
        return True, None
    _invalidate_status()
    cap = cap_of(name)
    over_cap = cap is not None and size_of(name) > cap
    past_floor = bool(cache_root()) and over_floor(root)
    if over_cap or past_floor:
        with _locked(lock_path(name)) as (held, _why):
            if held:            # held elsewhere: that run is already evicting here, and the
                if over_cap:    # next periodic one corrects whatever overflow is left
                    _evict_cache(rec, "by-cap", exclude=path)
                if past_floor:
                    _floor_run(exclude=path)
    return True, None


def remove(name, path, reason, event="expired"):
    """A lifecycle deletion. Refuses a path outside the cache's directory and a pinned entry;
    records the event with the caller's reason. Returns (ok, why)."""
    rec = record(name)
    if rec is None:
        return False, "unregistered"
    root = dir_of(name)
    if not _contained(path, root) or os.path.realpath(path) == os.path.realpath(root):
        return False, "outside"          # a dark cache (no directory) refuses every path
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return True, "already-gone"
    except OSError as exc:
        return False, str(exc)[:80]
    entry = Entry(path, st.st_size, st.st_mtime)
    if _is_pinned(rec, entry, _pinned_set(rec)):
        return False, "pinned"
    try:
        os.remove(path)
    except OSError as exc:
        return False, str(exc)[:80]
    log_event(name, event, path, entry.bytes, reason)
    _invalidate_status()
    return True, None


# --- the eviction run (§2.3) --------------------------------------------------------------

def _evict_cache(rec, reason, exclude=None):
    """One cache: max_age first, then over-cap in its order. Returns a summary dict."""
    out = {"name": rec.name, "aged": 0, "evicted": 0, "freed": 0, "pinned": 0, "over_cap": False,
           "all_pinned": False}
    root = dir_of(rec.name)
    if not rec.enforce or not root or not os.path.isdir(root):
        return out
    age = max_age_of(rec.name)
    if age is not None and reason != "by-cap":
        cutoff = now() - age * 86400
        pinned_set = _pinned_set(rec)
        for entry in entries(rec.name):
            if entry.mtime >= cutoff or (exclude and os.path.realpath(entry.path) == os.path.realpath(exclude)):
                continue
            if _is_pinned(rec, entry, pinned_set):
                out["pinned"] += 1
                continue
            freed = _delete(rec, entry, "evicted", "by-age")
            if freed or not os.path.exists(entry.path):
                out["aged"] += 1
                out["freed"] += entry.bytes
    cap = cap_of(rec.name)
    if cap is not None:
        size = size_of(rec.name)
        if size > cap:
            out["over_cap"] = True
            need = lambda freed: size - freed > cap   # noqa: E731
            evicted, freed, pinned, ok = _evict_until(rec, need, "by-cap", exclude=exclude)
            out["evicted"] += evicted
            out["freed"] += freed
            out["pinned"] += pinned
            out["all_pinned"] = not ok
    return out


def _floor_run(exclude=None):
    """The volume past the floor with no cache over cap: evict across caches by rank, each cache
    in its own order, until every live cache's volume is under the floor."""
    out = {"floor_evicted": 0, "floor_freed": 0, "floor_pinned": 0, "floor_breached": False,
           "floor_cleared": True}
    live = [rec for rec in registered()
            if rec.enforce and dir_of(rec.name) and os.path.isdir(dir_of(rec.name))]
    breached = [rec for rec in live if over_floor(dir_of(rec.name))]
    if not breached:
        return out
    out["floor_breached"] = True
    exclude = os.path.realpath(exclude) if exclude else None
    candidates = []             # (rank, cache index, position in the cache's order, rec, entry)
    for idx, rec in enumerate(live):
        pinned_set = _pinned_set(rec)
        for pos, entry in enumerate(ordered(rec.name)):
            rank = _rank_of(rec, entry)
            if rank == "never" or rank is None:
                continue
            if exclude and os.path.realpath(entry.path) == exclude:
                continue
            if _is_pinned(rec, entry, pinned_set):
                out["floor_pinned"] += 1
                continue
            candidates.append((rank, idx, pos, rec, entry))
    candidates.sort(key=lambda c: (c[0], c[1], c[2]))
    for _rank, _idx, _pos, rec, entry in candidates:
        if not any(over_floor(dir_of(r.name)) for r in live):
            break
        freed = _delete(rec, entry, "evicted", "by-floor")
        if freed or not os.path.exists(entry.path):
            out["floor_evicted"] += 1
            out["floor_freed"] += entry.bytes
    out["floor_cleared"] = not any(over_floor(dir_of(r.name)) for r in live)
    return out


def _last_run_path():
    root = cache_root()
    return os.path.join(root, LAST_RUN_FILE) if root else None


def last_run_recorded():
    """The last run any process on this machine recorded, or None."""
    path = _last_run_path()
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _record_run(summary):
    if not _last_run_path():
        return
    tmp = _last_run_path() + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=1)
        os.replace(tmp, _last_run_path())
    except OSError:
        pass


def run(reason="periodic", names=None):
    """The eviction run. Returns its summary; {"skipped": why} when it did not run.

    With NETRADIO_CACHE_ROOT set this is the machine-wide pass: every registered cache (or the
    `names` asked for) under the one machine lock, then the cross-cache floor pass. With the root
    unset the caches that are live anyway are swept just the same, each under its own directory's
    lock — see THE LOCK and DARK in the module docstring."""
    global _last_skip
    root = cache_root()
    if not root:
        return _run_unrooted(reason, names)
    if not os.path.isdir(root):
        _last_skip = {"at": now(), "why": "root-missing"}
        return {"skipped": "root-missing", "root": root}
    with _locked(lock_path()) as (held, why):
        if not held:
            _last_skip = {"at": now(), "why": why}
            return {"skipped": why}
        started = now()
        summary = {"reason": reason, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "caches": [], "root": root}
        for rec in registered():
            if names and rec.name not in names:
                continue
            try:
                summary["caches"].append(_evict_cache(rec, reason))
            except CacheConfigError as exc:
                summary["caches"].append({"name": rec.name, "error": str(exc)})
        if not names:
            summary.update(_floor_run())
            summary["events_dropped"] = prune_events()
        summary["seconds"] = round(now() - started, 3)
        with _lock:
            _last_run.clear()
            _last_run.update(summary)
        if not names:               # a scoped run is not the machine's last full pass, and must
            _record_run(summary)    # not make the next server's start pass skip its own (§2.3)
        _invalidate_status()
        return summary


def _run_unrooted(reason, names):
    """The run with NETRADIO_CACHE_ROOT unset: every registered cache that HAS a live directory
    of its own, each under a lock in that directory, for its age and its cap. No floor pass (it
    ranks across caches, which the root is what defines), no event log and no recorded run: both
    files live under the root. {"skipped": "root-unset"} when no cache has a directory — the
    fully dark case, where this changes nothing."""
    global _last_skip
    live = [rec for rec in registered()
            if (not names or rec.name in names) and rec.enforce
            and dir_of(rec.name) and os.path.isdir(dir_of(rec.name))]
    if not live:
        _last_skip = {"at": now(), "why": "root-unset"}
        return {"skipped": "root-unset", "root": None}
    started = now()
    summary = {"reason": reason, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "caches": [], "root": None}
    for rec in live:
        with _locked(lock_path(rec.name)) as (held, why):
            if not held:
                summary["caches"].append({"name": rec.name, "skipped": why})
                continue
            try:
                summary["caches"].append(_evict_cache(rec, reason))
            except CacheConfigError as exc:
                summary["caches"].append({"name": rec.name, "error": str(exc)})
    summary["seconds"] = round(now() - started, 3)
    with _lock:
        _last_run.clear()
        _last_run.update(summary)
    _invalidate_status()
    return summary


def periodic():
    return run("periodic")


def startup():
    """The start-of-server pass: skipped when another process recorded a run within
    START_SKIP_S (whichever server started first ran it)."""
    last = last_run_recorded()
    if last and last.get("at"):
        try:
            at = datetime.fromisoformat(last["at"]).timestamp()
        except ValueError:
            at = 0
        if now() - at < START_SKIP_S:
            return {"skipped": "recent", "last": last.get("at")}
    return run("start")


# --- status (§2.4) ------------------------------------------------------------------------

def _invalidate_status():
    _status_cache["value"] = None


def _row(rec):
    root = dir_of(rec.name)
    row = {"name": rec.name, "dir": root, "exists": bool(root and os.path.isdir(root)),
           "enforce": rec.enforce,
           "refill": rec.refill, "rank": rec.rank if not callable(rec.rank) else "per-entry",
           "order": rec.order if isinstance(rec.order, str) else "custom",
           "last_run": _last_run.get("at"), "evicted_since_start": _evicted_since_start.get(rec.name, 0)}
    try:
        row["cap"] = cap_of(rec.name)
        row["headroom"] = headroom_of(rec.name)
        row["max_age_days"] = max_age_of(rec.name)
    except CacheConfigError as exc:
        row.update({"cap": None, "headroom": 0, "max_age_days": None, "error": str(exc)})
    row["floor_pct"] = disk_max_pct()
    if not row["exists"]:
        row.update({"size": 0, "entries": 0, "pinned": 0, "disk_pct": None, "over_cap": False,
                    "over_floor": False, "all_pinned": False})
        return row
    ents = entries(rec.name)
    pinned_set = _pinned_set(rec)
    pinned = sum(1 for e in ents if _is_pinned(rec, e, pinned_set))
    size = sum(e.bytes for e in ents)
    row.update({"size": size, "entries": len(ents), "pinned": pinned,
                "disk_pct": round(disk_pct(root), 1),
                "over_cap": row["cap"] is not None and size > row["cap"],
                "over_floor": over_floor(root)})
    row["all_pinned"] = bool(row["over_cap"] and pinned == len(ents) and rec.enforce)
    return row


def status(force=False):
    """{root, floor_pct, last_run, last_skip, caches: [row per registered cache]}, cached for
    STATUS_TTL_S (the notices poll reads it every half minute)."""
    cached = _status_cache["value"]
    if cached is not None and not force and now() - _status_cache["at"] < STATUS_TTL_S:
        return cached
    root = cache_root()
    out = {"root": root, "root_exists": bool(root and os.path.isdir(root)),
           "floor_pct": disk_max_pct(), "last_run": dict(_last_run) if _last_run else None,
           "last_skip": _last_skip, "caches": [_row(rec) for rec in registered()]}
    _status_cache["value"], _status_cache["at"] = out, now()
    return out


def reset_for_tests():
    """Forget every registration and counter (tests only)."""
    with _lock:
        _registry.clear()
        _inflight.clear()
        _evicted_since_start.clear()
        _last_run.clear()
    global _last_skip
    _last_skip = None
    _invalidate_status()
