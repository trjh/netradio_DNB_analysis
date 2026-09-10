"""What this process actually costs, measured -- stdlib only, and it never raises.

The harvester grew to tens of gigabytes and stayed there, and the first attempts to explain that
used RSS. RSS was the wrong number: on macOS it does not count pages the kernel has compressed or
swapped, so a process holding 40 GB of dirty pages can report a couple of gigabytes resident and
look healthy. `phys_footprint` counts them, which is why the fix was measured against it and why
this module reports it.

Two things live here:

  * `footprint_mb()` -- what the process owns now, and the most it has ever owned.
  * `allocator_canary()` -- allocate and free a large block, and report how much the process did
    NOT hand back. macOS libmalloc caches freed large blocks in-process; `MallocLargeCache=0`
    turns that off, and that variable is UNDOCUMENTED, so the harvester re-measures it on every
    start rather than trusting a number from a past OS release.

A broken probe must cost the harvester its watchdog, never its run. Every entry point here
returns None rather than raising.
"""

import ctypes
import gc
import os
import platform
import struct
import sys

# proc_pid_rusage(pid, RUSAGE_INFO_V4, buf), from libSystem. Two details are load-bearing:
# ctypes.CDLL(None) finds the symbol (loading libproc.dylib by path crashed the interpreter), and
# the buffer must be generous -- an exactly-sized RUSAGE_INFO_V0 buffer segfaulted, because the
# call writes the V4 struct regardless.
_RUSAGE_INFO_V4 = 4
_RUSAGE_BUF = 4096
_OFF_PHYS_FOOTPRINT = 72            # ri_phys_footprint, bytes
_OFF_LIFETIME_MAX = 240             # ri_lifetime_max_phys_footprint, bytes

# The canary's allocation: 8 blocks of 25 M float32, so 800 MB held for a fraction of a second.
# Large enough that libmalloc treats the blocks as "large" (the ones it caches), small enough to
# be over in about 0.2 s.
CANARY_BLOCKS = 8
CANARY_BLOCK_FLOATS = 25_000_000
CANARY_RETAINED_MB = 50             # above this, the allocator is holding on to freed blocks again

MB = 1024.0 * 1024.0


def _darwin_footprint():
    fn = ctypes.CDLL(None).proc_pid_rusage
    fn.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
    fn.restype = ctypes.c_int
    buf = ctypes.create_string_buffer(_RUSAGE_BUF)
    if fn(os.getpid(), _RUSAGE_INFO_V4, buf) != 0:
        return None, None
    current = struct.unpack_from("<Q", buf.raw, _OFF_PHYS_FOOTPRINT)[0]
    peak = struct.unpack_from("<Q", buf.raw, _OFF_LIFETIME_MAX)[0]
    return current / MB, peak / MB


def _linux_rss():
    """VmRSS / VmHWM. NOT the same quantity as phys_footprint -- it does not count swapped or
    compressed pages -- so callers label these rows `rss`."""
    current = peak = None
    with open("/proc/self/status", "r", encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("VmRSS:"):
                current = float(line.split()[1]) / 1024.0
            elif line.startswith("VmHWM:"):
                peak = float(line.split()[1]) / 1024.0
    return current, peak


def kind():
    """What `footprint_mb` is actually reporting, for whoever reads the state rows."""
    if sys.platform == "darwin":
        return "phys_footprint"
    if sys.platform.startswith("linux"):
        return "rss"
    return "unavailable"


def footprint_mb():
    """(current_mb, peak_mb) for this process -- (None, None) where it cannot be measured."""
    try:
        if sys.platform == "darwin":
            return _darwin_footprint()
        if sys.platform.startswith("linux"):
            return _linux_rss()
    except Exception:           # a probe that breaks must not take the harvester with it
        return None, None
    return None, None


def allocator_canary(sampler=None):
    """Allocate 800 MB, free it, and report what the process kept: (before, after, retained) MB.

    Returns (None, None, None) when the footprint cannot be measured or numpy is absent -- the
    check is diagnostic, so being unable to run it is not an error.
    """
    sampler = sampler or footprint_mb
    try:
        import numpy as np
    except ImportError:
        return None, None, None
    before = sampler()[0]
    if before is None:
        return None, None, None
    blocks = [np.ones(CANARY_BLOCK_FLOATS, dtype="float32") for _ in range(CANARY_BLOCKS)]
    del blocks
    gc.collect()
    after = sampler()[0]
    if after is None:
        return None, None, None
    return before, after, max(0.0, after - before)


def canary_line(retained_mb):
    """One line for the harvester's log, naming the OS and the variable the result depends on."""
    version = platform.mac_ver()[0] or platform.platform()
    return ("# allocator canary: %s %s · MallocLargeCache=%s · retained %.0f MB"
            % (platform.system(), version, os.environ.get("MallocLargeCache", "unset"),
               retained_mb))


def canary_issue(retained_mb):
    """The `issues` row for a canary that came back dirty, or None when it came back clean.

    `notices.py` in the player surfaces these, so a regression in the allocator shows up as a
    light on the queue page rather than as a slowly growing swap file nobody is watching.
    """
    if retained_mb is None or retained_mb <= CANARY_RETAINED_MB:
        return None
    return ("allocator retention back: %.0f MB of the %.0f MB just allocated and freed was not "
            "returned to the kernel, with MallocLargeCache=%s. Expect under %.0f MB. The "
            "harvester's footprint will climb again until this is understood."
            % (retained_mb, CANARY_BLOCKS * CANARY_BLOCK_FLOATS * 4 / 1e6,
               os.environ.get("MallocLargeCache", "unset"), CANARY_RETAINED_MB))
