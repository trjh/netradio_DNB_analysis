"""The last module of the suite: the real home directory is as it was when the suite started.

On 2026-09-18 a suite run created `~/Netradio/cache` on the live machine, the future move target,
because `cache_budget.py` derived a default root from `~` and the owning modules' `os.makedirs`
followed. The module no longer has a default, and every fixture points the variables at temp
directories, but the rule is only held if something checks it: this module is discovered and
imported with the others (so its snapshot predates every test) and, sorted last, runs after them.

It FAILS, never skips. Live state is allowed to move underneath a running player (the real
events.jsonl gains rows), so the check is what a test could leave behind: a new entry directly
under `$HOME`, a new `~/Netradio`, or a row in the real event log naming a temporary directory.
"""

import os
import tempfile
import unittest

HOME = os.path.expanduser("~")
TARGET = os.path.join(HOME, "Netradio")
EVENTS = os.path.join(TARGET, "cache", "events.jsonl")
TEMP_MARKERS = (tempfile.gettempdir(), "/var/folders", "/tmp/", "/private/tmp")


def _snapshot():
    try:
        top = set(os.listdir(HOME))
    except OSError:
        top = set()
    tree = set()
    if os.path.isdir(TARGET):
        for dirpath, dirs, files in os.walk(TARGET):
            if dirpath.count(os.sep) - TARGET.count(os.sep) >= 2:
                dirs[:] = []
            tree.update(os.path.join(dirpath, d) for d in dirs)
    lines = 0
    if os.path.exists(EVENTS):
        with open(EVENTS, encoding="utf-8", errors="replace") as fh:
            lines = sum(1 for _ in fh)
    return {"top": top, "tree": tree, "events_lines": lines, "target": os.path.exists(TARGET)}


BEFORE = _snapshot()          # taken at discovery, before any test runs


class TestHomeUntouched(unittest.TestCase):
    def test_nothing_new_under_home(self):
        after = _snapshot()
        self.assertEqual(sorted(after["top"] - BEFORE["top"]), [],
                         "the suite created entries directly under the real $HOME")
        self.assertEqual(after["target"], BEFORE["target"],
                         "~/Netradio appeared (or vanished) during the suite")
        self.assertEqual(sorted(after["tree"] - BEFORE["tree"]), [],
                         "new directories under ~/Netradio during the suite")
        if after["events_lines"] > BEFORE["events_lines"] and os.path.exists(EVENTS):
            with open(EVENTS, encoding="utf-8", errors="replace") as fh:
                new = fh.readlines()[BEFORE["events_lines"]:]
            leaked = [l for l in new if any(m in l for m in TEMP_MARKERS)]
            self.assertEqual(leaked, [],
                             "rows naming a temp directory reached the real ~/Netradio/cache/events.jsonl")
