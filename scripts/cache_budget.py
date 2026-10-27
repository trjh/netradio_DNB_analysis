"""Size-bounded local caches: one registry, one reserve before every write, one eviction run,
one disk floor.

A cache is one directory (subdirectories included) that the owning module registers once, at
import, with the settings below. Every file under the directory is an entry. The owning module
calls `reserve(name, nbytes)` before it writes an entry and `commit(name, path)` after the entry
has landed; every deletion of an entry goes through `remove(name, path, reason)` or through the
eviction run. Nothing here scans the disk for caches: a directory nobody registered is never
touched.

Settings per cache (`register`), each overridable from the environment, where `<NAME>` is the
cache's name in upper case:

    cap        bytes, or None for no cap           NETRADIO_<NAME>_CACHE_GB (decimal, or `none`)
    headroom   bytes of the cap that a write of    NETRADIO_<NAME>_CACHE_HEADROOM_MB
               known size may not use
    max_age    days, or None                       NETRADIO_<NAME>_CACHE_MAX_AGE_DAYS (or `none`)
    dir        the cache's directory               NETRADIO_<NAME>_CACHE_DIR
               (default `$NETRADIO_CACHE_ROOT/<name>`)
    order      which entry goes first: `oldest-added` (file mtime, oldest first), `newest-added`
               (the reverse) or `by-score` (the lowest `score(path)` first)
    pinned     predicate(path) -> True for an entry that is never evicted or removed
    refill     how an evicted entry comes back (reported, never acted on here)
    rank       integer, or None for never: the order in which caches give up entries when the
               volume is past the floor and no cache is over its cap (1 goes first)
    companions function(path) -> [paths] of the files that belong to an entry and go with it,
               or None. A companion is part of its entry, not an entry of its own: its bytes
               count with the entry's, it is never a candidate alone, and an eviction or a
               removal deletes it first, then the entry itself. The relation is one-way: a
               companion's own answer must be empty, and a file that is already a companion
               claims none of its own (a two-way answer would otherwise make both files
               companions and the pair would vanish from the cache). Two entries' companion
               sets must not overlap: a file both name is counted with one of them, but
               deleted with either. The age limit and the cache's order read the entry's own
               mtime, never a companion's: refreshing a companion does not make an entry
               younger. `remove()` takes an entry's path; given a companion's path it deletes
               that file alone

Machine-wide settings:

    NETRADIO_CACHE_ROOT         required. Unset, the module is dark: `register` records nothing,
                                `reserve` admits, `commit` and `run_eviction` do nothing,
                                `status()` says so.
    NETRADIO_DISK_MAX_PCT       the floor, default 82: no volume a cache sits on is allowed past
                                this full (used / (used + free)), whatever any cache's cap says.
                                Each volume is measured on its own: a cache on a second disk is
                                refused and relieved by that disk's reading, never the root's.
    NETRADIO_CACHE_EVENTS_DAYS  how long a record stays in the event log, default 30.
    NETRADIO_CACHE_IN_PROGRESS_MIN
                                how long a write in progress is held, default 60 (below).

The event log is one JSON record per line in `events.jsonl`, under `NETRADIO_DOWNLOAD_ROOT` when
that is set and under `NETRADIO_CACHE_ROOT` otherwise. Every admit, eviction, removal and refusal
writes one record. The lock file `.cache_budget.lock` under the root serialises every change,
across processes, so two eviction runs never work on one directory at once.

A file whose name ends in `.tmp` or `.part` and was modified within the last
`NETRADIO_CACHE_IN_PROGRESS_MIN` minutes is a write in progress: it counts toward the cache's size
and is never evicted or removed. Older than that, it is a write that died part-way and is evicted
like any other entry.

A name nobody registered is an error while the module is on: `reserve`, `commit` and `remove`
raise `KeyError`, so a misspelt name never writes uncapped. A caller whose cache is registered
only on some machines asks `registered(name)` first. A directory `register` refuses raises
`CacheRefused`.

The registry file. Every process that registers a cache writes its rows to
`<NETRADIO_CACHE_ROOT>/registry/<pid>.json`, and holds an exclusive lock on `<pid>.lock` beside
it for as long as it runs. A row whose lock is free belongs to a process that has ended, and is
ignored (and deleted). `register` refuses a directory that overlaps a cache another live process
has registered, unless it is the same cache: the same name and the same directory.
`other_processes()` is the read for a page that shows every cache on the machine. Under
`NETRADIO_READ_ONLY` the rows are read but never written or deleted.

The read API, for modules that show or plan around the caches: `names()`, `settings(name)` (the
registry row, no scan), `usage(name)` (one scan), `entries(name)` (one scan, every entry with its
held flag and companions), `status()` (every cache), `volume()` (the floor and the root's
reading, no scan), `held_here()` (this thread's depth inside the machine lock),
`record_event(...)`, `read_settings(name, ...)` (a cache's settings as the environment would set
them, without registering it) and `parse_number(text, default, what)`.
"""

import atexit
import collections
import contextlib
import fcntl
import json
import math
import os
import shutil
import sys
import threading
from datetime import datetime, timedelta, timezone

GB = 1000 ** 3
MB = 1000 ** 2
DEFAULT_CAP = 4 * GB
DEFAULT_DISK_MAX_PCT = 82
DEFAULT_EVENTS_DAYS = 30
ORDERS = ("oldest-added", "newest-added", "by-score")
IN_PROGRESS = (".tmp", ".part")
DEFAULT_IN_PROGRESS_MIN = 60    # a write in progress older than this died part-way
LOCK_NAME = ".cache_budget.lock"
EVENTS_NAME = "events.jsonl"
REGISTRY_DIR = "registry"

_REGISTRY = {}          # name -> the cache's record, in registration order
_STATS = {}             # name -> {"last_run", "evicted", "removed", "over_cap"}
_mutex = threading.Lock()
_held_here = threading.local()   # this thread's depth inside `_machine_lock`
_own_row = {"dir": None, "fd": None, "atexit": False}   # this process's registry file and lock


class CacheRefused(ValueError):
    """`register` will not make this directory a cache's (the reason is the message)."""


# --- the environment ------------------------------------------------------------------------

def enabled():
    return bool(os.environ.get("NETRADIO_CACHE_ROOT", "").strip())


def _read_only():
    return os.environ.get("NETRADIO_READ_ONLY", "").strip().lower() in ("1", "true", "yes", "on")


def root():
    """The directory every cache's default `dir` sits under, or None when dark."""
    value = os.environ.get("NETRADIO_CACHE_ROOT", "").strip()
    return os.path.expanduser(value) if value else None


def _env(name, suffix):
    return os.environ.get("NETRADIO_%s_CACHE_%s" % (name.upper(), suffix), "").strip()


def _number(text, default, what):
    try:
        value = float(text)
    except ValueError:
        value = None
    if value is None or not math.isfinite(value) or value < 0:
        print("cache_budget: ignoring %s=%r (not a number of zero or more)" % (what, text))
        return default
    return value


def parse_number(text, default, what):
    """`text` as a number of zero or more, else `default` with a warning naming `what`."""
    return _number(text, default, what)


def disk_max_pct():
    text = os.environ.get("NETRADIO_DISK_MAX_PCT", "").strip()
    return _number(text, DEFAULT_DISK_MAX_PCT, "NETRADIO_DISK_MAX_PCT") if text else DEFAULT_DISK_MAX_PCT


def events_days():
    text = os.environ.get("NETRADIO_CACHE_EVENTS_DAYS", "").strip()
    return _number(text, DEFAULT_EVENTS_DAYS, "NETRADIO_CACHE_EVENTS_DAYS") if text else DEFAULT_EVENTS_DAYS


def in_progress_s():
    """Seconds a `.tmp`/`.part` file is held as a write in progress."""
    text = os.environ.get("NETRADIO_CACHE_IN_PROGRESS_MIN", "").strip()
    minutes = (_number(text, DEFAULT_IN_PROGRESS_MIN, "NETRADIO_CACHE_IN_PROGRESS_MIN")
               if text else DEFAULT_IN_PROGRESS_MIN)
    return minutes * 60


def _read_cap(name, default):
    text = _env(name, "GB")
    if not text:
        return default
    if text.lower() == "none":
        return None
    gb = _number(text, None, "NETRADIO_%s_CACHE_GB" % name.upper())
    return default if gb is None else int(gb * GB)


def _read_headroom(name, default):
    text = _env(name, "HEADROOM_MB")
    if not text:
        return default
    mb = _number(text, None, "NETRADIO_%s_CACHE_HEADROOM_MB" % name.upper())
    return default if mb is None else int(mb * MB)


def _read_max_age(name, default):
    text = _env(name, "MAX_AGE_DAYS")
    if not text:
        return default
    if text.lower() == "none":
        return None
    return _number(text, default, "NETRADIO_%s_CACHE_MAX_AGE_DAYS" % name.upper())


def _read_dir(name, default):
    text = _env(name, "DIR")
    if text:
        return os.path.expanduser(text)
    return default or os.path.join(root(), name)


def read_settings(name, dir=None, cap=DEFAULT_CAP, max_age=None):
    """{dir, cap, max_age_days} for a cache as `register` would read them from this process's
    environment, each argument the default where the environment sets nothing. Registers
    nothing, so a page can show a cache another process owns. None while the module is dark and
    no `dir` is given."""
    if dir is None and not enabled():
        return None
    return {"dir": _read_dir(name, dir), "cap": _read_cap(name, cap),
            "max_age_days": _read_max_age(name, max_age)}


# --- the registry ---------------------------------------------------------------------------

def register(name, dir=None, cap=DEFAULT_CAP, headroom=0, max_age=None, order="oldest-added",
             pinned=None, refill="none", rank=None, score=None, companions=None):
    """Record one cache. The environment overrides `dir`, `cap`, `headroom` and `max_age`.

    Returns the record, or None when the module is dark. Raises `CacheRefused` for a directory
    it refuses: one that holds the root, sits in the registry directory, equals, contains or sits
    inside another cache's directory (this process's, or one another live process registered
    under a different name or directory), has a `.git` entry at its top level, or is or holds
    the home directory or the download root. A refused cache would be an uncapped one, so the
    caller stops rather than carries on. Registering a name again replaces it, and rewrites
    this process's registry file.
    """
    if not enabled():
        return None
    if order not in ORDERS:
        raise ValueError("unknown order %r" % order)
    if order == "by-score" and score is None:
        raise ValueError("order 'by-score' needs a score function")
    cache_dir = _read_dir(name, dir)
    with _registry_locked():            # no other process checks or publishes in between
        return _register(name, cache_dir, cap, headroom, max_age, order, pinned, refill, rank,
                         score, companions)


def _register(name, cache_dir, cap, headroom, max_age, order, pinned, refill, rank, score,
              companions):
    clash = _overlap(name, cache_dir)
    if clash:
        raise CacheRefused("cache_budget: not registering %r: its directory %s %s"
                           % (name, cache_dir, clash))
    rec = {
        "name": name,
        "dir": cache_dir,
        "cap": _read_cap(name, cap),
        "headroom": _read_headroom(name, headroom),
        "max_age": _read_max_age(name, max_age),
        "order": order,
        "pinned": pinned,
        "refill": refill,
        "rank": rank,
        "score": score,
        "companions": companions,
    }
    with _mutex:
        _REGISTRY[name] = rec
        _STATS.setdefault(name, {"last_run": None, "evicted": 0, "removed": 0,
                                 "over_cap": False})
        _publish()
    return rec


def _settings_of(rec):
    return {"name": rec["name"], "dir": rec["dir"], "cap": rec["cap"],
            "headroom": rec["headroom"], "max_age_days": rec["max_age"], "order": rec["order"],
            "refill": rec["refill"], "rank": rec["rank"]}


# --- the registry file ----------------------------------------------------------------------

def registry_dir():
    """`<root>/registry`, where each registering process keeps its rows; None when dark."""
    base = root()
    return os.path.join(base, REGISTRY_DIR) if base else None


@contextlib.contextmanager
def _registry_locked():
    """An exclusive lock on the registry directory itself, across processes, so one process's
    overlap check and the publication of its row are never split by another's. Under
    NETRADIO_READ_ONLY nothing is created or locked (nothing is published either). A lock that
    cannot be taken (an unwritable root) is printed and the registration goes on unlocked, as it
    did before the registry file existed."""
    if _read_only():
        yield
        return
    where, fd = registry_dir(), None
    try:
        for _attempt in range(5):
            os.makedirs(where, exist_ok=True)
            try:
                fd = os.open(where, os.O_RDONLY)
            except FileNotFoundError:
                continue                        # removed by an exiting process: make it again
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                same = os.fstat(fd).st_ino == os.stat(where).st_ino
            except OSError:
                same = False
            if same:
                break
            os.close(fd)                        # locked a directory removed under us
            fd = None
    except OSError as exc:
        print("cache_budget: registry lock not taken: %s" % exc)
        if fd is not None:
            os.close(fd)
            fd = None
    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)


def _publish():
    """Write this process's rows to `<pid>.json`, holding `<pid>.lock` for the process's life.
    Called with `_mutex` held. Never under NETRADIO_READ_ONLY. A failure is printed: the
    registration itself stands, only the other processes' view of it is missing."""
    if _read_only():
        return
    where = registry_dir()
    pid = os.getpid()
    try:
        os.makedirs(where, exist_ok=True)
        if _own_row["dir"] != where:
            _drop_own_row()
            fd = os.open(os.path.join(where, "%d.lock" % pid), os.O_RDWR | os.O_CREAT, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                raise
            _own_row.update(dir=where, fd=fd)
            if not _own_row["atexit"]:
                atexit.register(_drop_own_row)
                _own_row["atexit"] = True
        row = {"pid": pid, "process": os.path.basename(sys.argv[0] if sys.argv else "") or "python",
               "at": _now().isoformat(),
               "caches": [_settings_of(r) for r in _REGISTRY.values()]}
        path = os.path.join(where, "%d.json" % pid)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(row, fh, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as exc:
        print("cache_budget: registry file write failed: %s" % exc)


def _drop_own_row():
    """Delete this process's registry file and release its lock (at exit, or on a new root)."""
    where, fd = _own_row["dir"], _own_row["fd"]
    _own_row.update(dir=None, fd=None)
    if where is None:
        return
    pid = os.getpid()
    try:
        dfd = os.open(where, os.O_RDONLY)
        fcntl.flock(dfd, fcntl.LOCK_EX)         # never remove the directory mid-registration
    except OSError:
        dfd = None
    try:
        for fname in ("%d.json" % pid, "%d.lock" % pid):
            try:
                os.unlink(os.path.join(where, fname))
            except OSError:
                pass
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.rmdir(where)     # only when no other process has a row there
        except OSError:
            pass
    finally:
        if dfd is not None:
            os.close(dfd)


def other_processes():
    """[{pid, process, at, caches: [settings rows]}] for every other live process that has
    registered a cache under this root. A row whose lock nobody holds is a process that has
    ended: it is skipped, and deleted unless NETRADIO_READ_ONLY. An unreadable row is skipped.
    [] while dark."""
    where = registry_dir()
    if not where or not os.path.isdir(where):
        return []
    out = []
    try:
        names = sorted(os.listdir(where))
    except OSError:
        return []
    for fname in names:
        stem, ext = os.path.splitext(fname)
        if ext != ".json" or not stem.isdigit() or int(stem) == os.getpid():
            continue
        path = os.path.join(where, fname)
        if not _alive(os.path.join(where, stem + ".lock")):
            if not _read_only():
                for gone in (path, os.path.join(where, stem + ".lock")):
                    try:
                        os.unlink(gone)
                    except OSError:
                        pass
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                row = json.load(fh)
            if isinstance(row, dict) and isinstance(row.get("caches"), list):
                out.append(row)
        except (OSError, ValueError):
            continue
    return out


def _alive(lock_path):
    """True while some process holds `lock_path` exclusively (its owner is running)."""
    try:
        fd = os.open(lock_path, os.O_RDWR)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:
        return False
    finally:
        os.close(fd)          # closing releases a lock this call took
    return False


def _nested(a, b):
    """True when directory `a` is `b` or lies inside it. Compared by path, and then by
    identity: on a case-insensitive volume `~/MUSIC` names the same directory as `~/Music`,
    and only the files say so -- even for a directory not created yet (`_identity`)."""
    a, b = os.path.realpath(a), os.path.realpath(b)
    if os.path.commonpath([a, b]) == b:
        return True
    target = _identity(b)
    path = a
    while True:
        if _identity(path) == target:
            return True
        parent = os.path.dirname(path)
        if parent == path:
            return False
        path = parent


def _identity(path):
    """(device, inode) of the nearest existing ancestor of `path` (itself included), and the
    names below it -- case-folded when that ancestor's volume ignores case, so two spellings
    of one directory get one identity whether or not it exists yet."""
    rest = []
    while not os.path.exists(path):
        parent, name = os.path.split(path)
        if parent == path:
            break
        rest.append(name)
        path = parent
    try:
        st = os.stat(path)
    except OSError:
        return (None, None, path, tuple(reversed(rest)))
    swapped = path.swapcase()
    folds = swapped != path and os.path.exists(swapped) and os.path.samefile(path, swapped)
    names = tuple(n.casefold() if folds else n for n in reversed(rest))
    return (st.st_dev, st.st_ino, None, names)


def _overlap(name, cache_dir):
    """Why `cache_dir` may not be a cache's directory, or None. Two caches never share or nest
    directories, and no cache holds the root, where the lock file lives. A directory that looks
    like real data is refused too, because the cap would delete its oldest files: one with a
    `.git` entry at its top level, the home directory or one that holds it, and the download
    root (`NETRADIO_DOWNLOAD_ROOT`) or one that holds it. A cache inside the download root is
    allowed; the download root's own caches sit there."""
    if _nested(root(), cache_dir):
        return "holds the cache root"
    if _nested(cache_dir, registry_dir()):
        return "sits in the registry directory"
    if os.path.lexists(os.path.join(cache_dir, ".git")):
        return "holds a .git entry (a repository checkout)"
    if _nested(os.path.expanduser("~"), cache_dir):
        return "is or holds the home directory"
    download = os.environ.get("NETRADIO_DOWNLOAD_ROOT", "").strip()
    if download and _nested(os.path.expanduser(download), cache_dir):
        return "is or holds the download root"
    for other in _REGISTRY.values():
        if other["name"] != name and (_nested(cache_dir, other["dir"])
                                      or _nested(other["dir"], cache_dir)):
            return "overlaps the %r cache's directory" % other["name"]
    for proc in other_processes():
        for other in proc["caches"]:
            try:
                theirs = other["dir"]
                if (other["name"] == name
                        and os.path.realpath(theirs) == os.path.realpath(cache_dir)):
                    continue                    # the same cache, registered there too
                if _nested(cache_dir, theirs) or _nested(theirs, cache_dir):
                    return ("overlaps the %r cache's directory, registered by process %s (%s)"
                            % (other["name"], proc.get("pid"), proc.get("process")))
            except (KeyError, TypeError, ValueError):
                continue                        # a malformed row names no directory
    return None


def registered(name):
    return enabled() and name in _REGISTRY


def names():
    """The registered caches' names, in registration order ([] while dark)."""
    return list(_REGISTRY) if enabled() else []


def settings(name):
    """The registry row of one cache, read without a scan: {name, dir, cap, headroom,
    max_age_days, order, refill, rank}. None when it is not registered or the module is dark."""
    rec = _REGISTRY.get(name) if enabled() else None
    return _settings_of(rec) if rec else None


def _rec_of(name):
    """The record a writer's call acts on: None while dark, else the registered record. A name
    nobody registered raises KeyError: admitting it would write uncapped for ever."""
    if not enabled():
        return None
    rec = _REGISTRY.get(name)
    if rec is None:
        raise KeyError("cache_budget: no cache named %r is registered" % (name,))
    return rec


def dir_of(name):
    """The registered cache's directory, or None when it is not registered (or the module is dark)."""
    rec = _REGISTRY.get(name) if enabled() else None
    return rec["dir"] if rec else None


# --- the disk, the lock and the event log ---------------------------------------------------

def _disk_usage(path):
    """(total, used, free) of the volume holding `path`. A seam for the tests."""
    return shutil.disk_usage(path)


def _existing(path):
    path = os.path.abspath(path)
    while not os.path.exists(path):
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return path


def _device(path):
    """The volume holding `path`: a cache on another volume frees nothing on the root's."""
    return os.stat(_existing(path)).st_dev


def _disk_pct(path, extra=0):
    _total, used, free = _disk_usage(_existing(path))
    if used + free <= 0:
        return 0.0
    return 100.0 * (used + extra) / (used + free)


@contextlib.contextmanager
def _machine_lock():
    """One holder at a time across every process that shares the root. The kernel releases the
    lock when the descriptor closes, so a killed holder leaves nothing to clean up. Re-entrant
    within a thread: a flock taken on a second descriptor would wait on the first forever, so a
    nested call (a `remove()` inside `locked()`) only counts its depth."""
    depth = getattr(_held_here, "depth", 0)
    if depth:
        try:
            _held_here.depth = depth + 1
            yield
        finally:
            _held_here.depth = depth
        return
    os.makedirs(root(), exist_ok=True)
    fd = os.open(os.path.join(root(), LOCK_NAME), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            _held_here.depth = 1
            yield
        finally:
            _held_here.depth = 0
    finally:
        os.close(fd)


def locked():
    """The machine-wide lock, for a caller whose own change must not interleave with an eviction
    or a removal: an entry and its companions moved together, say. Re-entrant within a thread, so
    a `remove()` or `commit()` inside the block runs under the same hold. Nothing to hold while
    the module is dark. Never fork inside the block: the child would copy the descriptor and the
    depth and believe it holds the lock. A process started by exec never carries it (the
    descriptor is not inherited)."""
    return _machine_lock() if enabled() else contextlib.nullcontext()


def held_here():
    """How deep this thread is inside the machine lock: 0 when it does not hold it. For a guard
    that must never open something under the lock (a write transaction, say)."""
    return getattr(_held_here, "depth", 0)


def events_path():
    base = os.environ.get("NETRADIO_DOWNLOAD_ROOT", "").strip()
    base = os.path.expanduser(base) if base else root()
    return os.path.join(base, EVENTS_NAME) if base else None


def _now():
    return datetime.now(timezone.utc)


def _record(event, cache, entry=None, nbytes=None, reason=None, **extra):
    path = events_path()
    if not path:
        return
    row = {"at": _now().isoformat(), "event": event, "cache": cache, "entry": entry,
           "bytes": nbytes, "reason": reason}
    row.update(extra)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as exc:
        print("cache_budget: event log write failed: %s" % exc)


def record_event(event, cache, entry=None, nbytes=None, reason=None, **extra):
    """Append one record to the event log, as the policy writes its own: `at`, `event`, `cache`,
    `entry`, `bytes`, `reason` and the extra fields. A caller writing several at once holds
    `locked()` around them. Nothing while no log is configured; a failed write is printed."""
    _record(event, cache, entry, nbytes, reason, **extra)


def _prune_events():
    """Drop records older than NETRADIO_CACHE_EVENTS_DAYS. Called with the lock held."""
    path = events_path()
    if not path or not os.path.isfile(path):
        return
    cutoff = _now() - timedelta(days=events_days())
    keep = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    at = datetime.fromisoformat(json.loads(line)["at"])
                    if at.tzinfo is None:
                        at = at.replace(tzinfo=timezone.utc)
                except (ValueError, KeyError, TypeError):
                    continue
                if at >= cutoff:
                    keep.append(line if line.endswith("\n") else line + "\n")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.writelines(keep)
        os.replace(tmp, path)
    except OSError as exc:
        print("cache_budget: event log prune failed: %s" % exc)


# --- entries --------------------------------------------------------------------------------

def _inside(rec, path, base=None, real=None):
    base = base or os.path.realpath(rec["dir"])
    real = real or os.path.realpath(path)
    return real != base and os.path.commonpath([base, real]) == base


class _Entries(list):
    """The scan's entries, with each entry's companions as the scan found them (`comps`, keyed
    by the entry's path), so the candidates and the holds need not ask the function again."""
    comps = None


def _rel(rec, path):
    return os.path.relpath(path, rec["dir"])


def _scan(rec):
    """Every entry under the cache's directory: [(path, size, mtime)], a companion's size counted
    in its entry's and the companion itself left out (`_companions`). The relation is one-way: a
    file already claimed as a companion claims nothing, and a file that has claimed companions is
    never claimed itself, so a two-way or chained answer still leaves every byte counted once."""
    files = _files(rec)
    if not rec.get("companions"):
        return files
    base = os.path.realpath(rec["dir"])
    reals = {p: os.path.realpath(p) for p, _s, _m in files}
    sizes = {reals[p]: size for p, size, _m in files}
    owned = set()
    extra = collections.defaultdict(int)        # entry -> its companions' bytes, in one pass
    comps = {}
    for path, _size, _mtime in files:
        owner = reals[path]
        if owner in owned:
            continue                            # a companion claims nothing of its own
        mine = []
        for comp in _companions(rec, path, base, owner):
            real = os.path.realpath(comp)
            if real in sizes and real not in owned and real not in extra:
                owned.add(real)
                extra[owner] += sizes[real]
                mine.append(comp)
        comps[path] = mine
    out = _Entries()
    out.comps = comps
    for path, size, mtime in files:
        real = reals[path]
        if real in owned:
            continue
        out.append((path, size + extra[real], mtime))
    return out


def _companions(rec, path, base=None, real=None):
    """The companion files of one entry that exist inside the cache, never the entry itself."""
    fn = rec.get("companions")
    if not fn:
        return []
    base = base or os.path.realpath(rec["dir"])
    real = real or os.path.realpath(path)
    out = []
    for c in fn(path) or []:
        creal = os.path.realpath(c)
        if (creal != real and _inside(rec, c, base, creal) and os.path.isfile(c)
                and not os.path.islink(c)):
            out.append(c)
    return out


def _files(rec):
    """Every regular file under the cache's directory: [(path, size, mtime)], in name order, so
    which file claims a companion never depends on the filesystem's enumeration order."""
    out = []
    for dirpath, dirs, files in os.walk(rec["dir"]):
        dirs.sort()
        for fname in sorted(files):
            if fname in (LOCK_NAME, EVENTS_NAME, EVENTS_NAME + ".tmp"):
                continue
            path = os.path.join(dirpath, fname)
            try:
                st = os.lstat(path)
            except OSError:
                continue
            if os.path.isfile(path) and not os.path.islink(path):
                out.append((path, st.st_size, st.st_mtime))
    return out


def _held(rec, path, comps=None):
    """Pinned, or a write in progress: never evicted. An entry whose companion is held is held
    too, so the two never part. `comps` is the scan's list, when the caller has one."""
    if comps is None:
        comps = _companions(rec, path)
    if any(_held_one(rec, c) for c in comps):
        return True
    return _held_one(rec, path)


def _held_one(rec, path):
    if path.endswith(IN_PROGRESS):
        try:
            if _now().timestamp() - os.path.getmtime(path) < in_progress_s():
                return True
        except OSError:
            return False                        # gone: nothing left to hold
    pinned = rec["pinned"]
    return bool(pinned and pinned(path))


def _candidates(rec, entries, protect=()):
    """The entries the cache may give up, in the cache's own order. A protected path protects
    its entry, and so does a protected companion of it."""
    protect = {os.path.realpath(p) for p in protect}
    known = getattr(entries, "comps", None)

    def comps_of(path):
        if known is not None and path in known:
            return known[path]
        return _companions(rec, path)

    def guarded(path, comps):
        if os.path.realpath(path) in protect:
            return True
        return bool(protect) and any(os.path.realpath(c) in protect for c in comps)
    out = []
    for e in entries:
        comps = comps_of(e[0])
        if not guarded(e[0], comps) and not _held(rec, e[0], comps):
            out.append(e)
    if rec["order"] == "oldest-added":
        out.sort(key=lambda e: e[2])
    elif rec["order"] == "newest-added":
        out.sort(key=lambda e: e[2], reverse=True)
    else:
        out.sort(key=lambda e: rec["score"](e[0]))
    return out


def _unlink_entry(rec, path):
    """Delete one entry with its companions, the companions first. Asked again at the moment of
    deletion: an entry no longer at its path, or held now, keeps every file. Returns None when the
    entry is deleted, else why it stays: "pinned", or "companion: <error>" when a companion could
    not be deleted. Raises the entry's own OSError, and FileNotFoundError when it is no longer at
    its path (moved, with its companions: nothing was deleted or recorded).

    The companions go first so that a crash part-way never leaves a companion that names an entry
    already gone. The cost: when the entry's own delete fails after its companions are gone, the
    entry stays without them. That case is printed, and the next scan sees the entry alone."""
    if not os.path.lexists(path):
        raise FileNotFoundError(path)
    comps = _companions(rec, path)
    if _held(rec, path, comps):
        return "pinned"
    gone = []
    for comp in comps:
        try:
            os.unlink(comp)
        except FileNotFoundError:
            pass
        except OSError as exc:
            print("cache_budget: could not remove %s: %s" % (comp, exc))
            if gone:
                print("cache_budget: %s keeps %s; these companions were removed: %s"
                      % (path, comp, ", ".join(gone)))
            return "companion: %s" % exc
        else:
            gone.append(comp)
    try:
        os.unlink(path)
    except FileNotFoundError:
        raise
    except OSError as exc:
        if comps:
            print("cache_budget: %s stays without its companions, which were removed: %s"
                  % (path, exc))
        raise
    return None


def _evict(rec, entry, reason):
    path, size, _mtime = entry
    if not _inside(rec, path):
        return False
    try:
        why = _unlink_entry(rec, path)
        if why is not None:
            if why != "pinned":
                _record("refuse", rec["name"], _rel(rec, path), size, reason, op="evict",
                        why=why)
            return False
    except FileNotFoundError:
        return True
    except OSError as exc:
        print("cache_budget: could not evict %s: %s" % (path, exc))
        return False
    _STATS[rec["name"]]["evicted"] += 1
    _record("evict", rec["name"], _rel(rec, path), size, reason)
    return True


def _evict_to(rec, entries, target, reason, protect=()):
    """Evict in the cache's order until its size is at most `target`. Returns the new size."""
    size = sum(e[1] for e in entries)
    for entry in _candidates(rec, entries, protect):
        if size <= target:
            break
        if _evict(rec, entry, reason):
            size -= entry[1]
    return size


def _size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


# --- the calls a writer makes ---------------------------------------------------------------

def reserve(name, nbytes=None):
    """Make room for one new entry, then say whether it may be written.

    `nbytes` is the entry's size when it is known in advance: the cache fills only to
    cap - headroom. `None` is a write of unplanned length: admitted while the cache is under its
    cap, and free to run into the headroom and past the cap (it is never stopped part-way; the
    `commit` after it corrects the overflow). Evicts other entries in the cache's order to fit;
    refuses when it cannot — evicting nothing when everything left is held or when the volume
    would pass the floor, and refusing after the attempt when the evictions did not free enough,
    because an entry could not be deleted. A size that is not a finite number of zero or more
    is refused. Admitted while the module is dark; a name that is not registered raises
    KeyError.
    """
    rec = _rec_of(name)
    if rec is None:
        return True
    with _machine_lock():
        if nbytes is not None and (isinstance(nbytes, bool) or not isinstance(nbytes, (int, float))
                                   or not math.isfinite(nbytes) or nbytes < 0):
            _record("refuse", name, reason="size", op="reserve", asked=repr(nbytes))
            return False
        if _disk_pct(rec["dir"], nbytes or 0) > disk_max_pct():
            _record("refuse", name, nbytes=nbytes, reason="floor", op="reserve")
            return False
        cap = rec["cap"]
        if cap is None:
            return True
        target = cap - rec["headroom"] - nbytes if nbytes is not None else cap - 1
        entries = _scan(rec)
        size = sum(e[1] for e in entries)
        if size <= target:
            return True
        freeable = sum(e[1] for e in _candidates(rec, entries))
        if target < 0 or size - freeable > target:
            _record("refuse", name, nbytes=nbytes, reason="cap", op="reserve")
            return False
        if _evict_to(rec, entries, target, "cap") > target:
            _record("refuse", name, nbytes=nbytes, reason="cap", op="reserve")
            return False
        return True


def commit(name, path):
    """Record an entry that has landed, its companions' bytes counted with it. If the cache is now
    over its cap, or the volume past the floor, run the eviction at once: other entries go, in the
    cache's order, never this one. A companion's path protects the entry it belongs to, so a
    writer that lands the companion last may commit either. Nothing while dark; a name that is
    not registered raises KeyError."""
    rec = _rec_of(name)
    if rec is None:
        return
    if not _inside(rec, path):
        raise ValueError("%s is not inside the %s cache" % (path, name))
    with _machine_lock():
        try:
            nbytes = os.path.getsize(path) + sum(_size(c) for c in _companions(rec, path))
        except OSError:
            nbytes = None
        _record("admit", name, _rel(rec, path), nbytes)
        size = sum(e[1] for e in _scan(rec))
        over = rec["cap"] is not None and size > rec["cap"]
        if over or _disk_pct(rec["dir"]) > disk_max_pct():
            _run([rec], protect={path})


def remove(name, path, reason):
    """Delete one entry for a reason the caller names, its companions first. Refuses (and records
    the refusal) a path outside the cache's directory and a pinned entry. An entry no longer at
    its path keeps its companions. Returns True when the entry is gone. `path` is the entry's:
    given a companion's path, it deletes that file alone and leaves its entry. False while dark;
    a name that is not registered raises KeyError."""
    rec = _rec_of(name)
    if rec is None:
        return False
    with _machine_lock():
        if not _inside(rec, path):
            _record("refuse", name, path, reason=reason, op="remove", why="outside the cache")
            return False
        if _held(rec, path):
            _record("refuse", name, _rel(rec, path), reason=reason, op="remove", why="pinned")
            return False
        try:
            nbytes = os.path.getsize(path) + sum(_size(c) for c in _companions(rec, path))
            why = _unlink_entry(rec, path)
            if why is not None:
                _record("refuse", name, _rel(rec, path), reason=reason, op="remove", why=why)
                return False
        except FileNotFoundError:
            return True
        except OSError as exc:
            print("cache_budget: could not remove %s: %s" % (path, exc))
            return False
        _STATS[name]["removed"] += 1
        _record("remove", name, _rel(rec, path), nbytes, reason)
        return True


# --- the eviction run -----------------------------------------------------------------------

def run_eviction(name=None):
    """Apply `max_age` and the cap to one cache (or every cache), then the floor to all of them.

    Then, for each volume a cache sits on (the root's included) that is past the floor, the
    caches on that volume give up entries by `rank`, 1 first, each in its own order, until that
    volume is back under the floor. Returns a summary.
    """
    if not enabled():
        return {"ran": False, "reason": "NETRADIO_CACHE_ROOT is unset"}
    if name is not None and name not in _REGISTRY:
        return {"ran": False, "reason": "no cache named %r" % name}
    recs = [_REGISTRY[name]] if name is not None else list(_REGISTRY.values())
    with _machine_lock():
        summary = _run(recs)
        if name is None:
            _prune_events()
    return summary


def _volumes():
    """{device: a path on it} for the root's volume and every registered cache's, the root
    first: the floor is kept on each volume a cache sits on."""
    out = {}
    for path in [root()] + [r["dir"] for r in _REGISTRY.values()]:
        try:
            out.setdefault(_device(path), path)
        except OSError:
            continue
    return out


def _run(recs, protect=()):
    """The run itself. Called with the lock held."""
    before = {r["name"]: _STATS[r["name"]]["evicted"] for r in _REGISTRY.values()}
    now = _now()
    for rec in recs:
        entries = _scan(rec)
        if rec["max_age"] is not None:
            cutoff = now.timestamp() - rec["max_age"] * 86400
            for entry in _candidates(rec, entries, protect):
                if entry[2] < cutoff:
                    _evict(rec, entry, "age")
            entries = _scan(rec)
        size = sum(e[1] for e in entries)
        if rec["cap"] is not None and size > rec["cap"]:
            size = _evict_to(rec, entries, rec["cap"], "cap", protect)
        stats = _STATS[rec["name"]]
        stats["over_cap"] = rec["cap"] is not None and size > rec["cap"]
        stats["last_run"] = now.isoformat()
    floor = disk_max_pct()
    for volume, where in _volumes().items():
        if _disk_pct(where) <= floor:
            continue
        ranked = sorted((r for r in _REGISTRY.values()
                         if r["rank"] is not None and _device(r["dir"]) == volume),
                        key=lambda r: r["rank"])
        for rec in ranked:
            for entry in _candidates(rec, _scan(rec), protect):
                if _disk_pct(where) <= floor:
                    break
                _evict(rec, entry, "floor")
            if _disk_pct(where) <= floor:
                break
    return {"ran": True,
            "evicted": {n: _STATS[n]["evicted"] - before[n] for n in before},
            "disk_pct": round(_disk_pct(root()), 1), "floor_pct": floor}


# --- the reads ------------------------------------------------------------------------------

Entry = collections.namedtuple("Entry", "path size mtime held companions")


def _listing(rec):
    entries = _scan(rec)
    comps = getattr(entries, "comps", None) or {}
    out = []
    for path, size, mtime in entries:
        mine = comps.get(path)
        out.append(Entry(path, size, mtime, _held(rec, path, mine),
                         list(mine) if mine is not None else _companions(rec, path)))
    return out


def entries(name):
    """Every entry of one cache, from one scan: [Entry(path, size, mtime, held, companions)],
    in name order. `size` counts the companions' bytes; `held` is pinned or a write in progress.
    [] when the cache is not registered or the module is dark."""
    rec = _REGISTRY.get(name) if enabled() else None
    return _listing(rec) if rec else []


def _usage_of(listing):
    return {"size": sum(e.size for e in listing), "entries": len(listing),
            "pinned": sum(1 for e in listing if e.held)}


def usage(name):
    """{size, entries, pinned} of one cache, from one scan. None when it is not registered or
    the module is dark."""
    rec = _REGISTRY.get(name) if enabled() else None
    return _usage_of(_listing(rec)) if rec else None


def status_row(name, listing=None):
    """One cache's status row: its settings, its usage and its counts since start. `listing` is
    the cache's `entries(name)` when the caller already has it, so nothing is scanned twice.
    None when it is not registered or the module is dark."""
    rec = _REGISTRY.get(name) if enabled() else None
    if rec is None:
        return None
    row = _settings_of(rec)
    row.update(_usage_of(_listing(rec) if listing is None else listing))
    stats = _STATS[name]
    row.update({"last_run": stats["last_run"], "evicted_since_start": stats["evicted"],
                "removed_since_start": stats["removed"], "over_cap": stats["over_cap"]})
    return row


def volume():
    """The floor and the root volume's reading, no scan: {enabled, root, floor_pct, disk_pct},
    or {enabled: False, reason} while dark."""
    if not enabled():
        return {"enabled": False, "reason": "NETRADIO_CACHE_ROOT is unset"}
    return {"enabled": True, "root": root(), "floor_pct": disk_max_pct(),
            "disk_pct": round(_disk_pct(root()), 1)}


def status():
    """One row per registered cache (`status_row`), plus the floor and the volume's reading.
    Scans every cache: a reader of one cache asks `settings` or `usage` instead."""
    st = volume()
    if not st["enabled"]:
        st["caches"] = []
        return st
    st["caches"] = [r for r in (status_row(n) for n in list(_REGISTRY)) if r is not None]
    return st
