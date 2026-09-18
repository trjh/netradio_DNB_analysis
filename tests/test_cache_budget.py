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
        self.assertEqual(mods, {"contextlib", "fcntl", "json", "os", "re", "shutil", "threading",
                                "time", "collections", "datetime"})

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
        with mock.patch.dict(os.environ, {"NETRADIO_ALIGN_CACHE": "", "NETRADIO_CACHE_ROOT": ""}):
            self.assertIsNone(cb.dir_of("streamalign"))               # dark: every load decodes
            self.assertIsNone(audio.cache_dir())
            self.assertEqual(cb.dir_of("chroma"), os.path.join(ROOT, ".harvest", "chroma"))

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

    def test_write_excerpt_says_whether_it_kept_the_excerpt(self):
        """The caller counts `kept` and records the lead's audio from this answer: a refused
        excerpt must not be counted, and must not name a file that is not there (review #146)."""
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
                path = os.path.join(tmp, "MT4-0.0500-abcd.wav")
                cb.disk_usage = lambda p: Usage(100, 90, 10)            # past the floor
                self.assertFalse(harvest.write_excerpt(
                    np.zeros(16000 * 40, dtype="float32"), 20.0, path))
                cb.disk_usage = lambda p: Usage(100, 10, 90)            # under it
                self.assertTrue(harvest.write_excerpt(
                    np.zeros(16000 * 40, dtype="float32"), 20.0, path))
                self.assertFalse(harvest.write_excerpt(np.zeros(0, dtype="float32"), 0.0, path))
            finally:
                cb.disk_usage = saved

    def test_an_owner_dir_cache_is_swept_with_the_root_unset(self):
        """Should fix 2 of #146 (the twin's Should fix 3): `sweep_excerpts` is a scoped run, and
        with NETRADIO_CACHE_ROOT unset the caches are live under `.harvest/` anyway. The run
        applies their age, taking a lock in each cache's own directory, and creates nothing
        under `~`."""
        import time
        tmp = tempfile.mkdtemp()
        old = os.path.join(tmp, "MT4-0.9000-old.wav")
        young = os.path.join(tmp, "MT4-0.1000-new.wav")
        for path, age_days in ((old, 40), (young, 1)):
            with open(path, "wb") as fh:
                fh.write(b"x" * 10)
            os.utime(path, (time.time() - age_days * 86400,) * 2)
        home = os.path.expanduser("~")
        before = set(os.listdir(home))
        registry = dict(cb._registry)
        saved = cb.disk_usage
        env = {k: os.environ.get(k) for k in os.environ if k.startswith("NETRADIO_")}
        try:
            for k in list(os.environ):
                if k.startswith("NETRADIO_"):
                    os.environ.pop(k)
            os.environ["NETRADIO_CANDIDATES_CACHE_DIR"] = tmp
            os.environ["NETRADIO_CHROMA_CACHE_DIR"] = tempfile.mkdtemp()   # keep the repo's
            cb.disk_usage = lambda p: Usage(100, 10, 90)                   # .harvest/ out of it
            import harvest                              # registers `candidates` at import
            self.assertIsNotNone(cb.record("candidates"))
            summary = harvest.cache_budget.run(names=("candidates", "chroma"))
            self.assertIsNone(summary["root"])
            self.assertEqual(summary["caches"][0]["aged"], 1)
            self.assertFalse(os.path.exists(old))
            self.assertTrue(os.path.exists(young))
            self.assertTrue(os.path.exists(os.path.join(tmp, cb.LOCK_FILE)))
        finally:
            cb.disk_usage = saved
            cb._registry.clear()
            cb._registry.update(registry)
            for k in list(os.environ):
                if k.startswith("NETRADIO_"):
                    os.environ.pop(k)
            os.environ.update({k: v for k, v in env.items() if v is not None})
        self.assertEqual(sorted(set(os.listdir(home)) - before), [],
                         "the scoped run created something under the real $HOME")


class TestFixtureIsolation(unittest.TestCase):
    """With NETRADIO_CACHE_ROOT unset the module and its owners must never touch the real home
    directory (the 2026-09-18 leak: a suite run created ~/Netradio/cache on the live machine).
    Runs against the REAL $HOME, imports every owner, calls every entry point, and FAILS (never
    skips) if anything appears there. The harvester's unset fallback is the repo's own
    .harvest/, which is asserted too."""

    def test_nothing_touches_the_real_home_with_the_root_unset(self):
        home = os.path.expanduser("~")
        saved = {k: os.environ.get(k) for k in os.environ if k.startswith("NETRADIO_")}
        before = set(os.listdir(home))
        target = os.path.join(home, "Netradio")
        existed = os.path.exists(target)
        registry = dict(cb._registry)
        try:
            for k in list(os.environ):
                if k.startswith("NETRADIO_"):
                    os.environ.pop(k)
            import sigstore                       # noqa: F401
            from streamalign import audio         # noqa: F401
            import extract_tracks                 # noqa: F401
            self.assertIsNone(cb.cache_root())
            self.assertIsNone(cb.dir_of("streamalign"))
            self.assertIsNone(cb.dir_of("stream_tracks"))
            self.assertTrue(cb.dir_of("chroma").startswith(ROOT))
            for rec in cb.registered():
                cb.reserve(rec.name, 10, os.path.join(home, "Netradio", "x"))
                cb.commit(rec.name, os.path.join(home, "Netradio", "x"))
                cb.remove(rec.name, os.path.join(home, "Netradio", "x"), "test")
            cb.log_event("chroma", "landed", "/nowhere", 1, "test")
            cb.run()
            cb.startup()
            cb.status(force=True)
            cb.prune_events()
        finally:
            cb._registry.clear()
            cb._registry.update(registry)
            for k in list(os.environ):
                if k.startswith("NETRADIO_"):
                    os.environ.pop(k)
            os.environ.update({k: v for k, v in saved.items() if v is not None})
        self.assertEqual(sorted(set(os.listdir(home)) - before), [],
                         "the suite created something under the real $HOME")
        if not existed:
            self.assertFalse(os.path.exists(target), "~/Netradio was created by the module")


class DarkRootCreatesNothing(unittest.TestCase):
    """A root that is named but absent is as dark as an unset one: no writer's makedirs may
    create the cache under it and then have reserve admit the write."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.absent = os.path.join(self.tmp, "absent-root")
        self._env = {k: os.environ.get(k) for k in os.environ
                     if k.startswith("NETRADIO_") and ("_CACHE_" in k or k in
                                                        ("NETRADIO_CACHE_ROOT", "NETRADIO_DISK_MAX_PCT",
                                                         "NETRADIO_ALIGN_CACHE"))}
        for k in self._env:
            os.environ.pop(k)
        os.environ["NETRADIO_CACHE_ROOT"] = self.absent
        self._du = cb.disk_usage
        cb.disk_usage = lambda p: Usage(100, 10, 90)

    def tearDown(self):
        cb.disk_usage = self._du
        for k in list(os.environ):
            if k.startswith("NETRADIO_") and ("_CACHE_" in k or k in ("NETRADIO_CACHE_ROOT",)):
                os.environ.pop(k)
        os.environ.update({k: v for k, v in self._env.items() if v is not None})

    def test_ensure_dir_refuses_under_an_absent_root(self):
        import sigstore    # noqa: F401
        for name in ("chroma", "streamalign", "stream_tracks", "candidates"):
            self.assertIsNone(cb.ensure_dir(name), name)
        self.assertFalse(os.path.exists(self.absent))
        os.makedirs(self.absent)
        self.assertEqual(cb.ensure_dir("stream_tracks"), os.path.join(self.absent, "stream_tracks"))

    def test_audio_decodes_without_caching_and_creates_nothing(self):
        import numpy as np
        from streamalign import audio
        src = os.path.join(self.tmp, "src.bin")
        open(src, "wb").write(b"x")
        with mock.patch.object(audio, "_ffmpeg_decode", lambda p, sr, mono: np.zeros(8, dtype="<f4")):
            self.assertEqual(len(audio.load_audio(src)), 8)
        self.assertFalse(os.path.exists(self.absent))

    def test_match_queue_returns_the_chroma_and_keeps_nothing(self):
        try:
            import numpy as np
            import librosa  # noqa: F401  (chroma_of imports it inline; CI has no librosa)
            import match_queue
        except ImportError as exc:
            self.skipTest("needs the venv: %s" % exc)
        src = os.path.join(self.tmp, "cand.wav")
        open(src, "wb").write(b"x")
        with mock.patch.object(match_queue._audio, "load_audio",
                               lambda p: np.zeros(60 * match_queue._audio.SR, dtype="float32")), \
                mock.patch.object(match_queue.chroma_recipe, "compute_chroma",
                                  lambda y: np.zeros((12, 4), dtype="float32")), \
                mock.patch.dict(os.environ, {}):
            c = match_queue.chroma_of(src)
        self.assertEqual(c.shape, (12, 4))
        self.assertFalse(os.path.exists(self.absent))

    def test_match_queue_with_the_root_unset_keeps_nothing(self):
        try:
            import numpy as np
            import librosa  # noqa: F401  (chroma_of imports it inline; CI has no librosa)
            import match_queue
        except ImportError as exc:
            self.skipTest("needs the venv: %s" % exc)
        os.environ.pop("NETRADIO_CACHE_ROOT")
        src = os.path.join(self.tmp, "cand.wav")
        open(src, "wb").write(b"x")
        local = os.path.join(ROOT, ".harvest", "chroma")
        before = set(os.listdir(local)) if os.path.isdir(local) else set()
        with mock.patch.object(match_queue._audio, "load_audio",
                               lambda p: np.zeros(60 * match_queue._audio.SR, dtype="float32")), \
                mock.patch.object(match_queue.chroma_recipe, "compute_chroma",
                                  lambda y: np.zeros((12, 4), dtype="float32")):
            c = match_queue.chroma_of(src)
        self.assertEqual(c.shape, (12, 4))
        after = set(os.listdir(local)) if os.path.isdir(local) else set()
        self.assertEqual(after - before, set())                      # the repo-local dir untouched

    def test_match_queue_writes_through_the_policy_when_live(self):
        try:
            import numpy as np
            import librosa  # noqa: F401  (chroma_of imports it inline; CI has no librosa)
            import match_queue
        except ImportError as exc:
            self.skipTest("needs the venv: %s" % exc)
        os.makedirs(self.absent)                                     # the root exists now
        src = os.path.join(self.tmp, "cand.wav")
        open(src, "wb").write(b"x")
        calls = []
        real_reserve, real_commit = cb.reserve, cb.commit
        with mock.patch.object(match_queue._audio, "load_audio",
                               lambda p: np.zeros(60 * match_queue._audio.SR, dtype="float32")), \
                mock.patch.object(match_queue.chroma_recipe, "compute_chroma",
                                  lambda y: np.zeros((12, 4), dtype="float32")), \
                mock.patch.object(cb, "reserve", lambda *a, **k: calls.append("reserve") or real_reserve(*a, **k)), \
                mock.patch.object(cb, "commit", lambda *a, **k: calls.append("commit") or real_commit(*a, **k)):
            match_queue.chroma_of(src)
        self.assertEqual(calls, ["reserve", "commit"])
        self.assertEqual(len(os.listdir(os.path.join(self.absent, "chroma"))), 1)

    def test_a_root_configured_after_import_is_where_every_writer_lands(self):
        """The writers were imported with no root (the TestFixtureIsolation case, and any script
        that loads .env late); a root set afterwards is where every path resolves, and every
        write is inside the policy's directory and accounted for."""
        try:
            import numpy as np
            import librosa  # noqa: F401  (chroma_of imports it inline; CI has no librosa)
            import harvest
            import match_queue
        except ImportError as exc:
            self.skipTest("needs the venv: %s" % exc)
        os.environ.pop("NETRADIO_CACHE_ROOT")
        self.assertIsNone(cb.cache_root())
        root = os.path.join(self.tmp, "late-root")
        os.makedirs(root)
        os.environ["NETRADIO_CACHE_ROOT"] = root
        self.assertEqual(harvest._chroma_dir(), os.path.join(root, "chroma"))
        self.assertEqual(harvest._keep_dir(), os.path.join(root, "candidates"))
        self.assertTrue(harvest.sig_path("https://x/y").startswith(os.path.join(root, "chroma")))
        self.assertEqual(match_queue.cache_dir(), os.path.join(root, "chroma"))
        src = os.path.join(self.tmp, "cand.wav")
        open(src, "wb").write(b"x")
        with mock.patch.object(match_queue._audio, "load_audio",
                               lambda p: np.zeros(60 * match_queue._audio.SR, dtype="float32")), \
                mock.patch.object(match_queue.chroma_recipe, "compute_chroma",
                                  lambda y: np.zeros((12, 4), dtype="float32")):
            match_queue.chroma_of(src)
        written = os.listdir(os.path.join(root, "chroma"))
        self.assertEqual(len(written), 1)
        landed = [e for e in cb.read_events() if e["event"] == "landed"]
        self.assertEqual(landed[0]["path"], os.path.join(root, "chroma", written[0]))

    def test_an_excerpt_under_an_absent_root_is_not_kept(self):
        try:
            import harvest
            import numpy as np
            import soundfile  # noqa: F401
        except ImportError as exc:
            self.skipTest("needs the venv: %s" % exc)
        path = os.path.join(self.absent, "candidates", "MT4-0.0500-abcd.wav")
        with mock.patch.object(harvest, "KEEP", os.path.dirname(path)):
            harvest.write_excerpt(np.zeros(16000 * 40, dtype="float32"), 20.0, path)
        self.assertFalse(os.path.exists(self.absent))


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
