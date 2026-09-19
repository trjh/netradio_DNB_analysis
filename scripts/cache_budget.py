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

Machine-wide settings:

    NETRADIO_CACHE_ROOT         required. Unset, the module is dark: `register` records nothing,
                                `reserve` admits, `commit` and `run_eviction` do nothing,
                                `status()` says so.
    NETRADIO_DISK_MAX_PCT       the floor, default 82: the volume is never allowed past this full
                                (used / (used + free)), whatever any cache's cap says.
    NETRADIO_CACHE_EVENTS_DAYS  how long a record stays in the event log, default 30.

The event log is one JSON record per line in `events.jsonl`, under `NETRADIO_DOWNLOAD_ROOT` when
that is set and under `NETRADIO_CACHE_ROOT` otherwise. Every admit, eviction, removal and refusal
writes one record. The lock file `.cache_budget.lock` under the root serialises every change,
across processes, so two eviction runs never work on one directory at once.

A file whose name ends in `.tmp` or `.part` and was modified within the last hour is a write in
progress: it counts toward the cache's size and is never evicted or removed. Older than that, it is
a write that died part-way and is evicted like any other entry.
"""

import contextlib
import fcntl
import json
import math
import os
import shutil
import threading
from datetime import datetime, timedelta, timezone

GB = 1000 ** 3
MB = 1000 ** 2
DEFAULT_CAP = 4 * GB
DEFAULT_DISK_MAX_PCT = 82
DEFAULT_EVENTS_DAYS = 30
ORDERS = ("oldest-added", "newest-added", "by-score")
IN_PROGRESS = (".tmp", ".part")
IN_PROGRESS_S = 3600    # a write in progress older than this died part-way
LOCK_NAME = ".cache_budget.lock"
EVENTS_NAME = "events.jsonl"

_REGISTRY = {}          # name -> the cache's record, in registration order
_STATS = {}             # name -> {"last_run", "evicted", "removed", "over_cap"}
_mutex = threading.Lock()


# --- the environment ------------------------------------------------------------------------

def enabled():
    return bool(os.environ.get("NETRADIO_CACHE_ROOT", "").strip())


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


def disk_max_pct():
    text = os.environ.get("NETRADIO_DISK_MAX_PCT", "").strip()
    return _number(text, DEFAULT_DISK_MAX_PCT, "NETRADIO_DISK_MAX_PCT") if text else DEFAULT_DISK_MAX_PCT


def events_days():
    text = os.environ.get("NETRADIO_CACHE_EVENTS_DAYS", "").strip()
    return _number(text, DEFAULT_EVENTS_DAYS, "NETRADIO_CACHE_EVENTS_DAYS") if text else DEFAULT_EVENTS_DAYS


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


# --- the registry ---------------------------------------------------------------------------

def register(name, dir=None, cap=DEFAULT_CAP, headroom=0, max_age=None, order="oldest-added",
             pinned=None, refill="none", rank=None, score=None):
    """Record one cache. The environment overrides `dir`, `cap`, `headroom` and `max_age`.

    Returns the record, or None when the module is dark or the directory is refused: one that
    holds the root, or equals, contains or sits inside another cache's directory (a warning is
    printed). Registering a name again replaces it.
    """
    if not enabled():
        return None
    if order not in ORDERS:
        raise ValueError("unknown order %r" % order)
    if order == "by-score" and score is None:
        raise ValueError("order 'by-score' needs a score function")
    cache_dir = _read_dir(name, dir)
    clash = _overlap(name, cache_dir)
    if clash:
        print("cache_budget: not registering %r: its directory %s %s" % (name, cache_dir, clash))
        return None
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
    }
    with _mutex:
        _REGISTRY[name] = rec
        _STATS.setdefault(name, {"last_run": None, "evicted": 0, "removed": 0,
                                 "over_cap": False})
    return rec


def _nested(a, b):
    """True when directory `a` is `b` or lies inside it."""
    a, b = os.path.realpath(a), os.path.realpath(b)
    return os.path.commonpath([a, b]) == b


def _overlap(name, cache_dir):
    """Why `cache_dir` may not be a cache's directory, or None. Two caches never share or nest
    directories, and no cache holds the root, where the lock file lives."""
    if _nested(root(), cache_dir):
        return "holds the cache root"
    for other in _REGISTRY.values():
        if other["name"] != name and (_nested(cache_dir, other["dir"])
                                      or _nested(other["dir"], cache_dir)):
            return "overlaps the %r cache's directory" % other["name"]
    return None


def registered(name):
    return enabled() and name in _REGISTRY


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
    lock when the descriptor closes, so a killed holder leaves nothing to clean up."""
    os.makedirs(root(), exist_ok=True)
    fd = os.open(os.path.join(root(), LOCK_NAME), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


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

def _inside(rec, path):
    base = os.path.realpath(rec["dir"])
    real = os.path.realpath(path)
    return real != base and os.path.commonpath([base, real]) == base


def _rel(rec, path):
    return os.path.relpath(path, rec["dir"])


def _scan(rec):
    """Every regular file under the cache's directory: [(path, size, mtime)]."""
    out = []
    for dirpath, _dirs, files in os.walk(rec["dir"]):
        for fname in files:
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


def _held(rec, path):
    """Pinned, or a write in progress: never evicted."""
    if path.endswith(IN_PROGRESS):
        try:
            if _now().timestamp() - os.path.getmtime(path) < IN_PROGRESS_S:
                return True
        except OSError:
            return False                        # gone: nothing left to hold
    pinned = rec["pinned"]
    return bool(pinned and pinned(path))


def _candidates(rec, entries, protect=()):
    """The entries the cache may give up, in the cache's own order."""
    protect = {os.path.realpath(p) for p in protect}
    out = [e for e in entries
           if os.path.realpath(e[0]) not in protect and not _held(rec, e[0])]
    if rec["order"] == "oldest-added":
        out.sort(key=lambda e: e[2])
    elif rec["order"] == "newest-added":
        out.sort(key=lambda e: e[2], reverse=True)
    else:
        out.sort(key=lambda e: rec["score"](e[0]))
    return out


def _evict(rec, entry, reason):
    path, size, _mtime = entry
    if not _inside(rec, path):
        return False
    try:
        os.unlink(path)
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


# --- the calls a writer makes ---------------------------------------------------------------

def reserve(name, nbytes=None):
    """Make room for one new entry, then say whether it may be written.

    `nbytes` is the entry's size when it is known in advance: the cache fills only to
    cap - headroom. `None` is a write of unplanned length: admitted while the cache is under its
    cap, and free to run into the headroom and past the cap (it is never stopped part-way; the
    `commit` after it corrects the overflow). Evicts other entries in the cache's order to fit;
    refuses, evicting nothing, when that cannot fit it (everything left is pinned) or when the
    volume would pass the floor. A name that is not registered is always admitted.
    """
    rec = _REGISTRY.get(name) if enabled() else None
    if rec is None:
        return True
    with _machine_lock():
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
        _evict_to(rec, entries, target, "cap")
        return True


def commit(name, path):
    """Record an entry that has landed. If the cache is now over its cap, or the volume past the
    floor, run the eviction at once: other entries go, in the cache's order, never this one."""
    rec = _REGISTRY.get(name) if enabled() else None
    if rec is None:
        return
    if not _inside(rec, path):
        raise ValueError("%s is not inside the %s cache" % (path, name))
    with _machine_lock():
        try:
            nbytes = os.path.getsize(path)
        except OSError:
            nbytes = None
        _record("admit", name, _rel(rec, path), nbytes)
        size = sum(e[1] for e in _scan(rec))
        over = rec["cap"] is not None and size > rec["cap"]
        if over or _disk_pct(root()) > disk_max_pct():
            _run([rec], protect={path})


def remove(name, path, reason):
    """Delete one entry for a reason the caller names. Refuses (and records the refusal) a path
    outside the cache's directory and a pinned entry. Returns True when the entry is gone."""
    rec = _REGISTRY.get(name) if enabled() else None
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
            nbytes = os.path.getsize(path)
            os.unlink(path)
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

    Past the floor with no cache over its cap, the caches on the root's volume give up entries by
    `rank`, 1 first, each in its own order, until the volume is back under the floor. Returns a
    summary.
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
    if _disk_pct(root()) > floor:
        volume = _device(root())
        ranked = sorted((r for r in _REGISTRY.values()
                         if r["rank"] is not None and _device(r["dir"]) == volume),
                        key=lambda r: r["rank"])
        for rec in ranked:
            for entry in _candidates(rec, _scan(rec), protect):
                if _disk_pct(root()) <= floor:
                    break
                _evict(rec, entry, "floor")
            if _disk_pct(root()) <= floor:
                break
    return {"ran": True,
            "evicted": {n: _STATS[n]["evicted"] - before[n] for n in before},
            "disk_pct": round(_disk_pct(root()), 1), "floor_pct": floor}


# --- status ---------------------------------------------------------------------------------

def status():
    """One row per registered cache, plus the floor and the volume's reading."""
    if not enabled():
        return {"enabled": False, "reason": "NETRADIO_CACHE_ROOT is unset", "caches": []}
    rows = []
    for rec in list(_REGISTRY.values()):
        entries = _scan(rec)
        stats = _STATS[rec["name"]]
        rows.append({
            "name": rec["name"], "dir": rec["dir"], "cap": rec["cap"],
            "headroom": rec["headroom"], "max_age_days": rec["max_age"],
            "order": rec["order"], "refill": rec["refill"], "rank": rec["rank"],
            "size": sum(e[1] for e in entries), "entries": len(entries),
            "pinned": sum(1 for e in entries if _held(rec, e[0])),
            "last_run": stats["last_run"], "evicted_since_start": stats["evicted"],
            "removed_since_start": stats["removed"], "over_cap": stats["over_cap"],
        })
    return {"enabled": True, "root": root(), "floor_pct": disk_max_pct(),
            "disk_pct": round(_disk_pct(root()), 1), "caches": rows}
