"""The suite's temp hygiene: no test may leave a file or directory behind in the temp root.

Importing this module installs a guard over the whole run. `python -m unittest discover -s
tests` imports every test module before it runs a single test, so the guard is in place for
every test whichever module happens to declare it.

**Why it exists.** Many tests called `tempfile.mkdtemp()` or
`tempfile.NamedTemporaryFile(delete=False)` and never removed what they made. One full run
left 112 entries and about 200 MB in the temp root, most of it the fixture audio that
`tests/test_g4_missing_sources.py` builds. Every run added the same again, and a suite left
looping unattended fills the disk. The leak is fixed at every site; this guard is what stops
the next one.

**Two mechanics.**

* The suite gets a root of its own *inside* the user temp root, and `tempfile.tempdir` points
  at it. `tempfile.mkdtemp()` with no `dir=` therefore lands somewhere only this suite's own
  code writes, which is what lets the guard attribute a new entry to the test that made it
  instead of to whatever else on the machine is using the temp root at the same moment.
  `TMPDIR` points one level down, at a scratch the guard does not watch, so the subprocesses
  the tests run are contained without a tool's own scratch files failing a test. The root is
  removed at interpreter exit, so a leak that somehow slips past the guard is bounded to a
  single run rather than accumulating across many.
* `unittest.TestCase.run` is wrapped. The entries in that root are compared either side of
  every test, and a test that adds one fails, named, with the one-line fix in the message.
  The leaked entry is then removed, so a run does not keep filling the disk while it reports.

**What it does not attribute.** `setUpClass` and `setUpModule` run outside `TestCase.run`, so
an entry made there belongs to no test and the guard stays quiet about it. Those are cleaned
at their own level instead (`addClassCleanup`, `tearDownModule`, `tearDownClass`) and are
bounded by the suite root like everything else.
"""

import atexit
import os
import shutil
import sys
import tempfile
import unittest

# The suite's own temp root, inside the user temp root. Created before any test runs, and
# removed when the interpreter exits.
SUITE_TEMP_ROOT = tempfile.mkdtemp(prefix="analysis-suite-")
tempfile.tempdir = SUITE_TEMP_ROOT
atexit.register(shutil.rmtree, SUITE_TEMP_ROOT, True)

# Subprocesses the tests run (git, the MATCH tools, ffmpeg …) get a scratch of their own, one
# level down and outside what the guard watches. Their temp files are contained and go with
# the root, but a tool's own scratch is not a test's leak and must not fail one.
SUBPROCESS_TEMP = os.path.join(SUITE_TEMP_ROOT, "subprocesses")
os.makedirs(SUBPROCESS_TEMP, exist_ok=True)
os.environ["TMPDIR"] = SUBPROCESS_TEMP

_FIX_DIR = "self.addCleanup(shutil.rmtree, <dir>, True)"
_FIX_FILE = "self.addCleanup(os.unlink, <path>)"


def _entries():
    """The names directly under the suite temp root, bar the subprocess scratch."""
    try:
        names = os.listdir(SUITE_TEMP_ROOT)
    except OSError:                      # the root is gone: nothing to compare
        return set()
    return {n for n in names if n != os.path.basename(SUBPROCESS_TEMP)}


def _remove(path):
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path, ignore_errors=True)
    else:
        try:
            os.unlink(path)
        except OSError:
            pass


_original_run = unittest.TestCase.run


def _run_and_check_temp_hygiene(self, result=None):
    before = _entries()
    outcome = _original_run(self, result)
    result = outcome if outcome is not None else result
    leaked = sorted(_entries() - before)
    if not leaked:
        return outcome
    fixes = set()
    for name in leaked:                  # do not let one leak fill the disk for the rest
        path = os.path.join(SUITE_TEMP_ROOT, name)
        fixes.add(_FIX_DIR if os.path.isdir(path) else _FIX_FILE)
        _remove(path)
    if result is not None:
        try:
            raise AssertionError(
                "%s left %d temporary entr%s behind in the temp root (%s): register the "
                "cleanup on the line that creates it, %s"
                % (self, len(leaked), "y" if len(leaked) == 1 else "ies",
                   ", ".join(leaked), " or ".join(sorted(fixes))))
        except AssertionError:
            result.addFailure(self, sys.exc_info())
    return outcome


unittest.TestCase.run = _run_and_check_temp_hygiene


# The fixtures below are built inside functions on purpose: a module-level TestCase subclass
# is collected by the loader, and the leaking ones are meant to fail.

def _a_test_that_leaks():
    """A test that makes a directory and walks away: the defect this guard is here for."""
    class ATestThatLeaks(unittest.TestCase):
        made = None

        def runTest(self):
            ATestThatLeaks.made = tempfile.mkdtemp(prefix="hygiene-leak-")
    return ATestThatLeaks()


def _a_test_that_leaks_a_file():
    """The same defect with `NamedTemporaryFile(delete=False)`."""
    class ATestThatLeaksAFile(unittest.TestCase):
        made = None

        def runTest(self):
            with tempfile.NamedTemporaryFile(prefix="hygiene-leak-", delete=False) as fh:
                ATestThatLeaksAFile.made = fh.name
    return ATestThatLeaksAFile()


def _a_test_that_cleans_up():
    """The same things done right."""
    class ATestThatCleansUp(unittest.TestCase):
        def runTest(self):
            made = tempfile.mkdtemp(prefix="hygiene-tidy-")
            self.addCleanup(shutil.rmtree, made, True)
            with tempfile.NamedTemporaryFile(prefix="hygiene-tidy-", delete=False) as fh:
                self.addCleanup(os.unlink, fh.name)
    return ATestThatCleansUp()


class TheGuard(unittest.TestCase):
    def test_a_test_that_leaks_a_directory_fails_and_is_named(self):
        result = unittest.TestResult()
        case = _a_test_that_leaks()
        case.run(result)
        self.assertEqual(len(result.failures), 1, result.failures)
        failed, text = result.failures[0]
        self.assertIs(failed, case)
        self.assertIn("ATestThatLeaks", text)                   # the test is named
        self.assertIn(os.path.basename(case.made), text)        # and so is the directory
        self.assertIn("shutil.rmtree", text)                    # and the fix

    def test_a_test_that_leaks_a_file_fails_and_is_named(self):
        result = unittest.TestResult()
        case = _a_test_that_leaks_a_file()
        case.run(result)
        self.assertEqual(len(result.failures), 1, result.failures)
        _, text = result.failures[0]
        self.assertIn(os.path.basename(case.made), text)
        self.assertIn("os.unlink", text)
        self.assertFalse(os.path.exists(case.made))

    def test_the_leaked_directory_is_removed_so_the_run_stops_filling_the_disk(self):
        case = _a_test_that_leaks()
        case.run(unittest.TestResult())
        self.assertFalse(os.path.exists(case.made))

    def test_a_test_that_cleans_up_passes(self):
        result = unittest.TestResult()
        _a_test_that_cleans_up().run(result)
        self.assertTrue(result.wasSuccessful(), result.failures + result.errors)

    def test_the_guard_itself_leaves_nothing(self):
        before = _entries()
        result = unittest.TestResult()
        _a_test_that_leaks().run(result)
        _a_test_that_leaks_a_file().run(result)
        _a_test_that_cleans_up().run(result)
        self.assertEqual(_entries(), before)

    def test_an_unqualified_mkdtemp_lands_in_the_watched_root(self):
        # the point of the pin: every mkdtemp() in the suite lands where the guard is looking
        self.assertTrue(os.path.isdir(SUITE_TEMP_ROOT))
        self.assertEqual(tempfile.gettempdir(), SUITE_TEMP_ROOT)
        made = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, made, True)
        self.assertEqual(os.path.dirname(made), SUITE_TEMP_ROOT)

    def test_a_subprocess_scratch_entry_is_not_a_tests_leak(self):
        # TMPDIR is a level down and unwatched: a subprocess's own scratch is contained by
        # the root but never attributed to the test that ran it.
        self.assertEqual(os.environ["TMPDIR"], SUBPROCESS_TEMP)
        self.assertEqual(os.path.dirname(SUBPROCESS_TEMP), SUITE_TEMP_ROOT)
        before = _entries()
        tempfile.mkdtemp(dir=SUBPROCESS_TEMP)
        self.assertEqual(_entries(), before)


if __name__ == "__main__":
    unittest.main()
