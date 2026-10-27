"""`optional_imports.import_or_skip`: a missing audio library skips, any other import error fails.

The harvester's test files once caught every exception from `import harvest` and skipped, so
a module-level bug in the harvester skipped nearly all of its tests and left the run green.
These tests pin the helper's line between the two, and import `harvest` with no guard at all
whenever numpy is present, so a harvester that cannot import is one clear failure.
"""

import importlib
import os
import shutil
import sys
import tempfile
import unittest

from optional_imports import import_or_skip, missing_optional

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")


class ImportOrSkip(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="optional-imports-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        sys.path.insert(0, self.tmp)
        self.addCleanup(sys.path.remove, self.tmp)
        self.made = []
        self.addCleanup(lambda: [sys.modules.pop(n, None) for n in self.made])

    def _module(self, name, body):
        with open(os.path.join(self.tmp, name + ".py"), "w") as fh:
            fh.write(body)
        self.made.append(name)
        importlib.invalidate_caches()

    def test_a_missing_optional_library_returns_none(self):
        self._module("needs_audio_lib", "import not_installed_audio_lib\n")
        self.assertIsNone(import_or_skip("needs_audio_lib", deps=("not_installed_audio_lib",)))

    def test_a_missing_submodule_of_an_optional_library_returns_none(self):
        self._module("needs_audio_sub", "import not_installed_audio_lib.sub\n")
        self.assertIsNone(import_or_skip("needs_audio_sub", deps=("not_installed_audio_lib",)))

    def test_a_missing_module_that_is_not_optional_raises(self):
        """A renamed local module is a bug, not a missing library."""
        self._module("needs_local", "import renamed_local_module\n")
        with self.assertRaises(ImportError):
            import_or_skip("needs_local", deps=("numpy",))

    def test_a_missing_name_in_an_optional_library_raises(self):
        """`from numpy import x` for an x numpy lacks names numpy, but numpy is installed:
        the code is wrong, and the test file must fail."""
        self._module("bad_from_import", "from json import a_name_json_lacks\n")
        with self.assertRaises(ImportError):
            import_or_skip("bad_from_import", deps=("json",))

    def test_any_other_error_raises(self):
        self._module("broken_at_import", "raise RuntimeError('a bug at import time')\n")
        with self.assertRaises(RuntimeError):
            import_or_skip("broken_at_import")
        self._module("name_error_at_import", "undefined_name\n")
        with self.assertRaises(NameError):
            import_or_skip("name_error_at_import")

    def test_the_rule_reads_only_import_errors(self):
        self.assertFalse(missing_optional(RuntimeError("numpy")))
        self.assertFalse(missing_optional(ImportError("x", name="numpy")))
        self.assertTrue(missing_optional(ModuleNotFoundError("x", name="numpy.linalg")))
        self.assertFalse(missing_optional(ModuleNotFoundError("x", name="sigstore")))


class TheHarvesterImports(unittest.TestCase):
    """With numpy present, `harvest` must import: no guard, so a broken module fails here."""

    def test_harvest_imports_whenever_numpy_does(self):
        try:
            import numpy  # noqa: F401
        except ImportError:
            self.skipTest("numpy is not installed: the harvester's tests skip on this machine")
        if SCRIPTS not in sys.path:
            sys.path.insert(0, SCRIPTS)
            self.addCleanup(sys.path.remove, SCRIPTS)
        import harvest
        self.assertTrue(hasattr(harvest, "reconcile_ledger"))


if __name__ == "__main__":
    unittest.main()
