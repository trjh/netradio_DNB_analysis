"""The cache policy's twin half: `scripts/cache_budget.py` is byte-identical to the player's, the
four caches this repo writes register with the values the plan gives them, every deletion of a
cache entry goes through `cache_budget.remove`, and `.env.example` passes `make env-check`.

Hermetic: temp directories, the disk reading faked, no ffmpeg (the extract's argv is captured), no
network. The excerpt test needs soundfile and skips without it.
"""

import ast
import collections
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(ROOT, "scripts")
sys.path.insert(0, SCRIPTS)

import cache_budget as cb   # noqa: E402

Usage = collections.namedtuple("Usage", "total used free")


class TheTwin(unittest.TestCase):
    def test_byte_identical_when_the_player_repo_is_here(self):
        other = os.environ.get("NETRADIO_PLAYER_REPO", "").strip()
        twin = os.path.join(os.path.expanduser(other), "cache_budget.py") if other else ""
        if not twin or not os.path.exists(twin):
            self.skipTest("NETRADIO_PLAYER_REPO unset or has no cache_budget.py yet "
                          "(the `make sync` self-check compares the two on this machine)")
        with open(os.path.join(SCRIPTS, "cache_budget.py"), "rb") as a, open(twin, "rb") as b:
            self.assertEqual(a.read(), b.read(), "cache_budget.py drifted between the repos")

    def test_the_module_imports_only_the_standard_library(self):
        tree = ast.parse(open(os.path.join(SCRIPTS, "cache_budget.py"), encoding="utf-8").read())
        mods = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods.add(node.module.split(".")[0])
        self.assertEqual(mods, {"fcntl", "json", "os", "re", "shutil", "threading", "time",
                                "collections", "datetime"})

    def test_the_sync_self_check_covers_the_twin(self):
        sh = open(os.path.join(SCRIPTS, "tracklist_sync.sh"), encoding="utf-8").read()
        self.assertIn('TWIN_A="$ANALYSIS/scripts/cache_budget.py"', sh)
        self.assertIn('TWIN_P="$PLAYER/cache_budget.py"', sh)


class TheRegistrations(unittest.TestCase):
    """streamalign (audio.py), chroma (sigstore.py), candidates (harvest.py), stream_tracks
    (extract_tracks.py): the values of the plan's §5."""

    def setUp(self):
        self._env = {k: os.environ.get(k) for k in os.environ if "_CACHE_" in k}
        for k in self._env:
            os.environ.pop(k)

    def tearDown(self):
        os.environ.update({k: v for k, v in self._env.items() if v is not None})

    def test_streamalign_and_chroma(self):
        from streamalign import audio    # noqa: F401
        import sigstore                  # noqa: F401
        self.assertEqual(cb.cap_of("streamalign"), 4 * cb.GB)
        self.assertEqual(cb.max_age_of("streamalign"), 14)
        self.assertEqual(cb.record("streamalign").refill, "re-decode")
        self.assertEqual(cb.cap_of("chroma"), 4 * cb.GB)
        self.assertEqual(cb.max_age_of("chroma"), 14)
        self.assertEqual(cb.record("chroma").refill, "bucket:chroma/")
        with mock.patch.dict(os.environ, {"NETRADIO_ALIGN_CACHE": "/legacy", "NETRADIO_CACHE_ROOT": "/cr"}):
            self.assertEqual(cb.dir_of("streamalign"), "/legacy")
            self.assertEqual(cb.dir_of("chroma"), "/cr/chroma")

    def test_chroma_pins_what_the_bucket_has_not_verified(self):
        import sigstore
        rec = cb.record("chroma")
        with mock.patch.dict(os.environ, {"NETRADIO_SIG_BUCKET": ""}):
            self.assertTrue(rec.pinned(cb.Entry("/c/u1.npy", 10, 1)))      # store dark: pinned
        with mock.patch.dict(os.environ, {"NETRADIO_SIG_BUCKET": "b", "NETRADIO_AWS_CLI": "/bin/true"}), \
                mock.patch.object(sigstore, "remote_size", lambda key: 10):
            self.assertFalse(rec.pinned(cb.Entry("/c/u1.npy", 10, 1)))     # verified: evictable
        with mock.patch.dict(os.environ, {"NETRADIO_SIG_BUCKET": "b", "NETRADIO_AWS_CLI": "/bin/true"}), \
                mock.patch.object(sigstore, "remote_size", lambda key: 11):
            self.assertTrue(rec.pinned(cb.Entry("/c/u1.npy", 10, 1)))      # a size mismatch: pinned

    def test_candidates(self):
        try:
            import harvest
        except ImportError as exc:
            self.skipTest("harvest.py needs the venv: %s" % exc)
        rec = cb.record("candidates")
        self.assertEqual(cb.cap_of("candidates"), 250_000_000)
        self.assertEqual(cb.max_age_of("candidates"), 30)
        self.assertEqual(rec.order, "by-score")
        worst = cb.Entry("/k/MT4-0.0900-abcd.wav", 1, 1)
        best = cb.Entry("/k/MT4-0.0100-abcd.wav", 1, 1)
        self.assertGreater(rec.score(worst), rec.score(best))
        with mock.patch.object(harvest, "KEEP", "/patched/keep"):
            self.assertEqual(cb.dir_of("candidates"), "/patched/keep")
        self.assertTrue(rec.is_entry("/k/MT4-0.1-x.wav"))
        self.assertFalse(rec.is_entry("/k/PROVENANCE.txt"))

    def test_stream_tracks(self):
        import extract_tracks
        rec = cb.record("stream_tracks")
        self.assertEqual(cb.cap_of("stream_tracks"), 2 * cb.GB)
        self.assertEqual(cb.max_age_of("stream_tracks"), 14)
        self.assertEqual(rec.refill, "re-extract")
        self.assertEqual(extract_tracks.PCM_BYTES_PER_S, 44100 * 2 * 2)
        with mock.patch.dict(os.environ, {"NETRADIO_CACHE_ROOT": "/cr"}):
            self.assertEqual(cb.dir_of("stream_tracks"), "/cr/stream_tracks")


class ExtractWritesFlac(unittest.TestCase):
    def test_cut_argv_follows_the_extension_and_concat_encodes(self):
        import extract_tracks
        calls = []
        with mock.patch.object(extract_tracks._audio, "find_audio_file", lambda stem: "/cap.wav"), \
                mock.patch.object(extract_tracks.subprocess, "run",
                                  lambda argv, **kw: calls.append(argv)):
            extract_tracks.cut("d019-040", 100.0, 160.0, 90.0, "/out/001 - x.flac")
        self.assertEqual(calls[0][-1], "/out/001 - x.flac")
        self.assertIn("-ac", calls[0])
        src = open(extract_tracks.__file__, encoding="utf-8").read()
        self.assertIn('"-c:a", "flac", out', src)               # the reassembly encodes
        self.assertNotIn('"-c", "copy", out', src)
        self.assertIn('"%03d - %s.flac"', src)
        self.assertNotIn("netradio-tracks", src)

    def test_calibrate_reads_the_cache_directory(self):
        src = open(os.path.join(SCRIPTS, "calibrate.py"), encoding="utf-8").read()
        self.assertIn('cache_budget.dir_of("stream_tracks")', src)
        self.assertNotIn("NETRADIO_TRACKS_DIR", src)
        self.assertNotIn("netradio-tracks", src)


class EveryDeletionGoesThroughTheApi(unittest.TestCase):
    """A guard (plan §9): no os.remove/os.unlink of a cache entry outside cache_budget. The files
    that write a cache are named; every deletion call in them is listed with its enclosing
    function and its argument, and each listed one deletes something that is not an entry."""

    OWNERS = ("harvest.py", "sigstore.py", "extract_tracks.py", os.path.join("streamalign", "audio.py"))
    ALLOWED = {
        # harvest.py: the fetch child's spool helper (.harvest/tmp), the pause flag
        ("harvest.py", "_unlink", "path"), ("harvest.py", "main", "PAUSE"),
        # sigstore.py: a fetch's temporary file, never an entry
        ("sigstore.py", "fetch", "tmp"),
        # extract_tracks.py: the concat parts and their list, never the track
        ("extract_tracks.py", "main", "p"),
        # audio.py: the .part of a failed write
        (os.path.join("streamalign", "audio.py"), "load_audio", "tmp"),
    }

    def test_no_stray_deletions_in_the_cache_owners(self):
        stray = []
        for rel in self.OWNERS:
            tree = ast.parse(open(os.path.join(SCRIPTS, rel), encoding="utf-8").read())
            for fn in ast.walk(tree):
                if not isinstance(fn, ast.FunctionDef):
                    continue
                for node in ast.walk(fn):
                    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                            and node.func.attr in ("remove", "unlink")
                            and isinstance(node.func.value, ast.Name) and node.func.value.id == "os"):
                        arg = ast.unparse(node.args[0]) if node.args else ""
                        if (rel, fn.name, arg) not in self.ALLOWED:
                            stray.append("%s:%d %s(): os.%s(%s)" % (rel, node.lineno, fn.name,
                                                                    node.func.attr, arg))
        self.assertEqual(stray, [])


class ExcerptsRespectTheFloor(unittest.TestCase):
    def test_an_excerpt_is_not_kept_past_the_floor(self):
        try:
            import harvest
            import numpy as np
            import soundfile  # noqa: F401
        except ImportError as exc:
            self.skipTest("needs the venv: %s" % exc)
        tmp = tempfile.mkdtemp()
        root = tempfile.mkdtemp()
        saved = cb.disk_usage
        with mock.patch.dict(os.environ, {"NETRADIO_CACHE_ROOT": root,
                                          "NETRADIO_CANDIDATES_CACHE_DIR": tmp,
                                          "NETRADIO_DISK_MAX_PCT": "82"}):
            try:
                cb.disk_usage = lambda p: Usage(100, 90, 10)            # 90 %: past the floor
                path = os.path.join(tmp, "MT4-0.0500-abcd.wav")
                harvest.write_excerpt(np.zeros(16000 * 40, dtype="float32"), 20.0, path)
                self.assertFalse(os.path.exists(path))
                cb.disk_usage = lambda p: Usage(100, 10, 90)            # under it: kept
                harvest.write_excerpt(np.zeros(16000 * 40, dtype="float32"), 20.0, path)
                self.assertTrue(os.path.exists(path))
                self.assertEqual(cb.read_events()[0]["event"], "landed")
            finally:
                cb.disk_usage = saved


class EnvCheck(unittest.TestCase):
    def test_this_repos_example_passes(self):
        """CI holds the line: .env.example carries no dead name and every read name."""
        r = subprocess.run([sys.executable, os.path.join(SCRIPTS, "env_check.py"), "--example-only",
                            "--repo", ROOT], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_the_make_target_exists(self):
        mk = open(os.path.join(ROOT, "Makefile")).read()
        self.assertIn("\nenv-check:", mk)
        self.assertIn("scripts/env_check.py", mk)

    def test_the_old_fraction_variables_are_read_by_nothing(self):
        for dirpath, dirs, files in os.walk(SCRIPTS):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for f in files:
                if f.endswith(".py"):
                    text = open(os.path.join(dirpath, f), encoding="utf-8").read()
                    self.assertNotIn("NETRADIO_ALIGN_CACHE_MAX_FRAC", text, f)
                    self.assertNotIn("NETRADIO_ALIGN_CACHE_DISK_FULL_FRAC", text, f)
                    self.assertNotIn("NETRADIO_TRACKS_DIR", text, f)


if __name__ == "__main__":
    unittest.main()
