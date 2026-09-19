"""`make env-check`: set-but-unread and read-but-unset `NETRADIO_*` names.

Runs `scripts/env_check.py` against scratch repos and `.env` files, and against the repo's
own `.env.example`, which must pass clean: the check runs in CI as this test, so an example
never carries a dead name.
"""

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import env_check  # noqa: E402

CODE = '''
import os
import cache_budget

ROOT = os.environ.get("NETRADIO_THING_ROOT", "/srv/thing")
PORT = int(os.environ.get("NETRADIO_THING_PORT", 8000))
TOKEN = os.environ.get("NETRADIO_THING_TOKEN")
REEXEC = os.environ.get("NETRADIO_TRACKLIST_SYNC_REEXEC")
SHEET = os.environ.get("NETRADIO_SHEET_WEBHOOK")


def f():
    """Retired: NETRADIO_OLD_BUDGET_GB is no longer read."""   # prose, not a read
    # NETRADIO_OLD_COMMENT likewise
    cache_budget.register("widgets", cap=None)
'''

SHELL = '''#!/bin/bash
# NETRADIO_SHELL_COMMENT is only mentioned
LOG="${NETRADIO_THING_LOG:-/tmp/thing.log}"
'''

# Prose that names a variable and a cache: neither counts as a read.
PROSE = '''# was: os.environ.get("NETRADIO_RETIRED", "5")


def g():
    """cache_budget.register("dead", cap=None)"""
    return 1
'''


class Scratch(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.repo, True)
        with open(os.path.join(self.repo, "thing.py"), "w") as fh:
            fh.write(CODE)
        os.makedirs(os.path.join(self.repo, "scripts"))
        with open(os.path.join(self.repo, "scripts", "run.sh"), "w") as fh:
            fh.write(SHELL)
        os.makedirs(os.path.join(self.repo, "tests"))
        with open(os.path.join(self.repo, "tests", "test_x.py"), "w") as fh:
            fh.write('X = "NETRADIO_ONLY_IN_TESTS"\n')

    def env(self, text, name=".env"):
        path = os.path.join(self.repo, name)
        with open(path, "w") as fh:
            fh.write(text)
        return path

    def prose(self):
        with open(os.path.join(self.repo, "prose.py"), "w") as fh:
            fh.write(PROSE)

    def test_a_set_but_unread_name_is_reported(self):
        unread, _ = env_check.check(self.env("NETRADIO_THING_ROOT=/x\nNETRADIO_TYPO_ROOT=/y\n"),
                                    self.repo)
        self.assertEqual(unread, ["NETRADIO_TYPO_ROOT"])

    def test_a_name_only_in_prose_or_tests_is_not_read(self):
        unread, _ = env_check.check(self.env(
            "NETRADIO_OLD_BUDGET_GB=20\nNETRADIO_OLD_COMMENT=1\nNETRADIO_SHELL_COMMENT=1\n"
            "NETRADIO_ONLY_IN_TESTS=1\n"), self.repo)
        self.assertEqual(unread, ["NETRADIO_OLD_BUDGET_GB", "NETRADIO_OLD_COMMENT",
                                  "NETRADIO_ONLY_IN_TESTS", "NETRADIO_SHELL_COMMENT"])

    def test_a_default_stated_only_in_a_comment_does_not_read_the_name(self):
        # "# was: get("NETRADIO_RETIRED", "5")" must not count the name as read: prose never
        # does.
        self.prose()
        unread, unset = env_check.check(self.env("NETRADIO_RETIRED=5\n"), self.repo)
        self.assertEqual(unread, ["NETRADIO_RETIRED"])
        self.assertNotIn("NETRADIO_RETIRED", dict(unset))

    def test_a_registration_mentioned_only_in_prose_adds_no_cache_family(self):
        # A `cache_budget.register("dead", ...)` in a docstring is not a registration; a real
        # one in code still is (test_a_registered_cache_reads_its_family).
        self.prose()
        unread, _ = env_check.check(self.env("NETRADIO_DEAD_CACHE_GB=1\n"), self.repo)
        self.assertEqual(unread, ["NETRADIO_DEAD_CACHE_GB"])

    def test_an_unset_name_is_reported_with_its_default(self):
        _, unset = env_check.check(self.env("NETRADIO_THING_TOKEN=secret\n"), self.repo)
        unset = dict(unset)
        self.assertEqual(unset["NETRADIO_THING_ROOT"], "/srv/thing")
        self.assertEqual(unset["NETRADIO_THING_PORT"], "8000")
        self.assertEqual(unset["NETRADIO_THING_LOG"], "/tmp/thing.log")
        self.assertNotIn("NETRADIO_THING_TOKEN", unset)

    def test_a_registered_cache_reads_its_family(self):
        env = self.env("NETRADIO_WIDGETS_CACHE_GB=1\nNETRADIO_WIDGETS_CACHE_DIR=/w\n")
        unread, unset = env_check.check(env, self.repo)
        self.assertEqual(unread, [])
        unset = dict(unset)
        self.assertIn("NETRADIO_WIDGETS_CACHE_HEADROOM_MB", unset)
        self.assertIn("NETRADIO_WIDGETS_CACHE_MAX_AGE_DAYS", unset)

    def test_names_listed_side_by_side_are_not_each_others_default(self):
        with open(os.path.join(self.repo, "keys.py"), "w") as fh:
            fh.write('KEYS = ("NETRADIO_A_ID", "NETRADIO_A_SECRET")\n')
        _, unset = env_check.check(self.env(""), self.repo)
        self.assertIsNone(dict(unset)["NETRADIO_A_ID"])

    def test_an_example_counts_its_commented_lines(self):
        env = self.env("# NETRADIO_THING_PORT=8000\n# NETRADIO_DEAD=1\n# prose NETRADIO_X\n",
                       ".env.example")
        unread, unset = env_check.check(env, self.repo)
        self.assertEqual(unread, ["NETRADIO_DEAD"])
        self.assertNotIn("NETRADIO_THING_PORT", dict(unset))

    def test_a_real_env_ignores_its_commented_lines(self):
        unread, unset = env_check.check(self.env("# NETRADIO_DEAD=1\n"), self.repo)
        self.assertEqual(unread, [])
        self.assertIn("NETRADIO_THING_PORT", dict(unset))

    def test_hand_off_names_are_left_out_of_the_list_but_still_count_as_read(self):
        # NETRADIO_TRACKLIST_SYNC_REEXEC is the sync script's own re-exec guard: not a
        # setting of the file being checked, so it never belongs in the informational list;
        # a `.env` that sets one is still not flagged.
        unread, unset = env_check.check(self.env(""), self.repo)
        self.assertNotIn("NETRADIO_TRACKLIST_SYNC_REEXEC", dict(unset))
        self.assertIn("NETRADIO_SHEET_WEBHOOK", dict(unset), "a real setting still shows")
        unread, _ = env_check.check(self.env("NETRADIO_TRACKLIST_SYNC_REEXEC=x\n"), self.repo)
        self.assertEqual(unread, [], "a name the code reads is never flagged")

    def test_values_are_never_printed(self):
        env = self.env("NETRADIO_THING_TOKEN=hunter2-secret\nNETRADIO_TYPO=also-secret\n")
        buf = io.StringIO()
        old = env_check.ROOT
        env_check.ROOT = self.repo
        self.addCleanup(setattr, env_check, "ROOT", old)
        with contextlib.redirect_stdout(buf):
            rc = env_check.main([env])
        self.assertEqual(rc, 1, "a set-but-unread name fails the check")
        self.assertIn("NETRADIO_TYPO", buf.getvalue())
        self.assertNotIn("secret", buf.getvalue())

    def test_the_output_names_the_left_out_names(self):
        buf = io.StringIO()
        old = env_check.ROOT
        env_check.ROOT = self.repo
        self.addCleanup(setattr, env_check, "ROOT", old)
        with contextlib.redirect_stdout(buf):
            rc = env_check.main([self.env("")])
        self.assertEqual(rc, 0)
        self.assertIn("NETRADIO_TRACKLIST_SYNC_REEXEC", buf.getvalue(),
                      "the left-out names are named, so the list explains itself")


class TheRepo(unittest.TestCase):

    def test_env_example_passes_clean(self):
        unread, _ = env_check.check(os.path.join(ROOT, ".env.example"))
        self.assertEqual(unread, [], "names in .env.example that no code reads")

    def test_the_script_runs_and_exits_zero_on_the_example(self):
        out = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "env_check.py"),
                              os.path.join(ROOT, ".env.example")],
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("set but read by no code (0)", out.stdout)

    def test_this_repos_registrations_are_read(self):
        # The four caches this repo registers, each with its variable family.
        read = env_check.read_names()
        for name in ("STREAMALIGN", "CHROMA", "CANDIDATES", "STREAM_TRACKS"):
            for suffix in ("GB", "HEADROOM_MB", "MAX_AGE_DAYS", "DIR"):
                self.assertIn("NETRADIO_%s_CACHE_%s" % (name, suffix), read)
        self.assertIn("NETRADIO_CACHE_ROOT", read)
        self.assertIn("NETRADIO_DISK_MAX_PCT", read)
        self.assertIn("NETRADIO_STREAM_TRACKS_CACHE_DIR", read,
                      "extract_tracks.py and calibrate.py read the directory name directly")
        self.assertNotIn("NETRADIO_TRACKS_DIR", read,
                         "the retired hard-coded path variable is gone from the code")

    def test_the_registered_caches_report_the_defaults_the_code_really_uses(self):
        # The informational list's contract: the default shown is the one the code falls
        # back to -- for a cache family, what the registration sets. Caches whose
        # registration departs from the generic 4 GB / no age are named in the tool's own
        # table, because the code scan cannot evaluate the registration's arguments.
        read = env_check.read_names()
        self.assertIn("0.25 GB / 250 MB", read["NETRADIO_CANDIDATES_CACHE_GB"],
                      "candidates registers a 250 MB cap, not the generic 4 GB")
        self.assertIn("2 GB", read["NETRADIO_STREAM_TRACKS_CACHE_GB"],
                      "stream_tracks registers a 2 GB cap, not the generic 4 GB")
        self.assertIn("30 days", read["NETRADIO_CANDIDATES_CACHE_MAX_AGE_DAYS"])
        self.assertIn("14 days", read["NETRADIO_CHROMA_CACHE_MAX_AGE_DAYS"])
        self.assertIn("14 days", read["NETRADIO_STREAM_TRACKS_CACHE_MAX_AGE_DAYS"])
        # and a cache whose registration sets no cap keeps the generic text
        self.assertIn("4 GB", read["NETRADIO_CHROMA_CACHE_GB"])
        self.assertIn("none", read["NETRADIO_STREAMALIGN_CACHE_MAX_AGE_DAYS"])

    def test_the_retired_old_cache_names_are_read_by_nothing(self):
        # The old rules are removed, not deprecated: nothing reads the names this repo
        # stopped using.
        read = env_check.read_names()
        self.assertNotIn("NETRADIO_ALIGN_CACHE", read)
        self.assertNotIn("NETRUDIO_ALIGN_CACHE_MAX_FRAC", read)
        self.assertNotIn("NETRADIO_ALIGN_CACHE_DISK_FULL_FRAC", read)
        self.assertNotIn("NETRADIO_TRACKS_DIR", read)


if __name__ == "__main__":
    unittest.main()
