"""The last module of the suite: the real home directory is as it was when the suite started.

On 2026-09-18 a suite run created `~/Netradio/cache` on the live machine, the future move target,
because `cache_budget.py` derived a default root from `~` and the owning modules' `os.makedirs`
followed. The module no longer has a default, and every fixture points the variables at temp
directories, but the rule is only held if something checks it: this module is discovered and
imported with the others (so its snapshot predates every test) and, sorted last, runs after them.

Three places are watched. `$HOME` itself, for a new entry. `~/Netradio`, two levels down, for new
directories AND new files — a file under a directory that already exists is as much of a leak as
the directory was. And the first level of `~/media` and `~/.cache`, which are where the paths this
plan removed used to live (`~/media/netradio-queue/thumb_cache`, `~/media/netradio-candidates`,
`~/media/netradio-tracks`, `~/.cache/netradio-streamalign`): reintroduce one of those fallbacks and
the suite creates it again, which the old snapshot could not see.

It FAILS, never skips. Live state is allowed to move underneath a running player (the real
events.jsonl gains rows), so the check is what a test could leave behind: a new entry directly
under `$HOME`, a new `~/Netradio`, a new name at the first level of the two old roots, or a row in
the real event log naming a temporary directory.
"""

import os
import tempfile
import unittest

HOME = os.path.expanduser("~")
TARGET = os.path.join(HOME, "Netradio")
OLD_ROOTS = (os.path.join(HOME, "media"), os.path.join(HOME, ".cache"))
EVENTS = os.path.join(TARGET, "cache", "events.jsonl")
TEMP_MARKERS = (tempfile.gettempdir(), "/var/folders", "/tmp/", "/private/tmp")


def _listdir(path):
    try:
        return set(os.listdir(path))
    except OSError:
        return set()


def _snapshot():
    tree = set()
    if os.path.isdir(TARGET):
        for dirpath, dirs, files in os.walk(TARGET):
            if dirpath.count(os.sep) - TARGET.count(os.sep) >= 2:
                dirs[:] = []
            tree.update(os.path.join(dirpath, d) for d in dirs)
            tree.update(os.path.join(dirpath, f) for f in files)
    lines = 0
    if os.path.exists(EVENTS):
        with open(EVENTS, encoding="utf-8", errors="replace") as fh:
            lines = sum(1 for _ in fh)
    return {"top": _listdir(HOME), "tree": tree, "events_lines": lines,
            "old_roots": {d: _listdir(d) for d in OLD_ROOTS}, "target": os.path.exists(TARGET)}


BEFORE = _snapshot()          # taken at discovery, before any test runs


class TestHomeUntouched(unittest.TestCase):
    def test_nothing_new_under_home(self):
        after = _snapshot()
        self.assertEqual(sorted(after["top"] - BEFORE["top"]), [],
                         "the suite created entries directly under the real $HOME")
        self.assertEqual(after["target"], BEFORE["target"],
                         "~/Netradio appeared (or vanished) during the suite")
        self.assertEqual(sorted(after["tree"] - BEFORE["tree"]), [],
                         "new directories or files under ~/Netradio during the suite")
        for root in OLD_ROOTS:
            self.assertEqual(sorted(after["old_roots"][root] - BEFORE["old_roots"][root]), [],
                             "the suite created something under %s, one of the fallback roots "
                             "this plan removed" % root)
        if after["events_lines"] > BEFORE["events_lines"] and os.path.exists(EVENTS):
            with open(EVENTS, encoding="utf-8", errors="replace") as fh:
                new = fh.readlines()[BEFORE["events_lines"]:]
            leaked = [l for l in new if any(m in l for m in TEMP_MARKERS)]
            self.assertEqual(leaked, [],
                             "rows naming a temp directory reached the real ~/Netradio/cache/events.jsonl")
