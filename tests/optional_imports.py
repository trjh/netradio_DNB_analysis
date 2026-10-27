"""Import a module a test file needs, telling a missing library apart from a broken module.

A clone without the audio libraries should skip the tests that need them. A module that
fails to import for any other reason (a `NameError`, a renamed local module, a bad
constant) is a bug, and its tests must fail, not skip. A bare `except Exception` around the
import treats both the same, so a harvester that cannot start would leave its suite green
with nearly every test skipped.

`import_or_skip(name)` imports `name` and returns the module. When the import fails with an
`ImportError` for one of the optional third-party libraries in `deps`, it returns None
instead, and the caller's `unittest.skipUnless(module, ...)` decorators skip. Every other
error is raised.
"""

import importlib

# The third-party libraries a test may do without: the audio stack and what it pulls in.
OPTIONAL_DEPS = ("numpy", "scipy", "librosa", "soundfile", "numba", "audioread")


def missing_optional(exc, deps=OPTIONAL_DEPS):
    """True when `exc` is an ImportError for one of `deps` (or a submodule of one)."""
    if not isinstance(exc, ImportError):
        return False
    missing = (getattr(exc, "name", None) or "").split(".")[0]
    return missing in deps


def import_or_skip(name, deps=OPTIONAL_DEPS):
    """The module `name`, or None when only an optional library in `deps` is missing.

    Any other exception from the import propagates, so the test file fails to load and the
    run reports the real error."""
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        if missing_optional(exc, deps):
            return None
        raise
