"""The originals directory has one name: `NETRADIO_SOURCES_FLAC_CACHE_DIR`, with no fallback.

The old name, `NETRADIO_SOURCES_DIR`, is read by nothing. Two names for one directory could
disagree, and a tool would then read one directory while another tool keeps a different one in
step. So no tracked file names the old setting, and a reader given only the old name finds no
originals directory.

This file is the one tracked file that names the old setting, so that it can set it.
"""

import test_suite_env  # noqa: F401  (no test reaches a real audio bucket, however it is run)
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "scripts"))

import align_inspect  # noqa: E402
import sources_resolve  # noqa: E402
from streamalign import mystery  # noqa: E402

OLD = "NETRADIO_SOURCES" + "_DIR"
NEW = "NETRADIO_SOURCES_FLAC_CACHE_DIR"
THIS_FILE = os.path.relpath(os.path.abspath(__file__), REPO)


def _tracked_files():
    """The tracked files, relative to the checkout.

    From git when this is a checkout. The container run unpacks `git archive HEAD`, which holds
    only tracked files, so there a walk of the tree is the same list.
    """
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=REPO, capture_output=True,
                             timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        out = None
    if out is not None and out.returncode == 0 and out.stdout:
        return [p for p in out.stdout.decode("utf-8", "replace").split("\0") if p]
    found = []
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in (".git", ".venv", ".worktree", "__pycache__")]
        for name in files:
            if name.startswith(".env") and name != ".env.example":
                continue                  # a machine's own settings file, never tracked
            found.append(os.path.relpath(os.path.join(root, name), REPO))
    return found


class TheOldNameIsGone(unittest.TestCase):
    def test_no_tracked_file_names_the_old_setting(self):
        named = []
        for rel in _tracked_files():
            if rel == THIS_FILE:
                continue
            path = os.path.join(REPO, rel)
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            with open(path, "rb") as fh:
                if OLD.encode() in fh.read():
                    named.append(rel)
        self.assertEqual(named, [], "these files still name %s; use %s" % (OLD, NEW))


class OnlyTheOldNameFindsNoOriginals(unittest.TestCase):
    """A machine whose `.env` still says the old name: the readers find no originals."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sources-name-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.originals = os.path.join(self.tmp, "originals")
        os.mkdir(self.originals)
        open(os.path.join(self.originals, "Mystery Track 4.wav"), "wb").close()
        self.metadata = os.path.join(self.tmp, "track-metadata.json")
        with open(self.metadata, "w") as fh:
            json.dump({"tracks": {"68": {"title": "Mystery Track 4"}}}, fh)
        self.setUp_root()

    def setUp_root(self):
        # with the new name unset the readers fall back to the checkout's sources_local/;
        # point that at an empty directory, so a checkout that has one does not answer here
        patcher = mock.patch.object(sources_resolve, "ROOT_DIR", self.tmp)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _env(self, **names):
        env = {k: v for k, v in os.environ.items() if k not in (OLD, NEW)}
        env.update(names)
        return mock.patch.dict(os.environ, env, clear=True)

    def test_the_mystery_clips_are_not_found_by_the_old_name(self):
        with self._env(**{OLD: self.originals}):
            found = mystery.current(metadata_path=self.metadata)
        self.assertEqual([e["clip"] for e in found], [None])

    def test_the_mystery_clips_are_found_by_the_new_name(self):
        with self._env(**{NEW: self.originals}):
            found = mystery.current(metadata_path=self.metadata)
        self.assertEqual([e["clip"] for e in found],
                         [os.path.join(self.originals, "Mystery Track 4.wav")])

    def test_the_align_inspector_ignores_the_old_name(self):
        with self._env(**{OLD: self.originals}):
            self.assertNotEqual(align_inspect.sources_dir(), self.originals)
        with self._env(**{NEW: self.originals}):
            self.assertEqual(align_inspect.sources_dir(), self.originals)


if __name__ == "__main__":
    unittest.main()
