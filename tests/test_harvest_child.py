"""The per-candidate fetch child, the stop path, and the memory rows.

Nothing here touches the network. Every `yt-dlp` and `ffmpeg` is a fake object, every fetch child
is a fake `Popen`, and the two things worth being careful about are pinned:

  * **A partial decode must never become a signature.** A signature is written once per URL and
    never fetched again, so a truncated one is not a transient error -- it is a permanently wrong
    answer, cached locally and uploaded to the pool. Several tests exist only to show that each
    way a fetch can end badly produces no signature at all.
  * **The child must not look like the harvester.** The player's supervisor discovers a live
    harvester by looking for a `harvest.py` command line containing `--run`. If a fetch child's
    argv ever gained that flag, the supervisor would adopt a child that lives for one track.
"""

import io
import json
import os
import shutil
import signal
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)

import numpy as np                      # noqa: E402

try:
    import harvest                      # noqa: E402
    import memwatch                     # noqa: E402
except Exception:                       # a dependency this test does not own
    harvest = memwatch = None

SR = 16000
LONG_ENOUGH = int(60 * SR)              # comfortably over chroma_recipe.MIN_SECONDS


def _pcm(n_samples):
    """Decoded PCM as ffmpeg would write it: mono float32 little-endian."""
    return (np.arange(n_samples, dtype="float32") % 7.0 - 3.0).tobytes()


class _FakeProc:
    """Just enough of `Popen` for the decode path: exit code, stderr, and a stop record."""

    def __init__(self, argv, returncode=0, stderr=b"", alive=False, order=None):
        self.argv = argv
        self.returncode = returncode
        self.stdout = io.BytesIO()          # yt-dlp's; the code closes it and never reads it
        self.stderr = io.BytesIO(stderr)
        self.alive = alive
        self.order = order if order is not None else []
        self.killed = False

    def name(self):
        return os.path.basename(self.argv[0])

    def wait(self, timeout=None):
        self.alive = False
        return self.returncode

    def poll(self):
        return None if self.alive else self.returncode

    def terminate(self):
        self.order.append(self.name())
        self.alive = False

    def kill(self):
        self.killed = True
        self.alive = False


def fake_decode(pcm=b"", yt_rc=0, yt_err=b"", ff_rc=0, ff_err=b"", alive=False, order=None):
    """A `subprocess.Popen` stand-in for the yt-dlp | ffmpeg pair.

    The fake ffmpeg writes `pcm` straight into the spool file it is handed as `stdout`, which is
    exactly what the real one does -- the point of the change being tested is that Python never
    holds the decoded audio.
    """
    made = {}

    def _popen(argv, **kwargs):
        proc = _FakeProc(argv, order=order, alive=alive)
        if "ffmpeg" in argv[0]:
            proc.returncode, proc.stderr = ff_rc, io.BytesIO(ff_err)
            if pcm:
                kwargs["stdout"].write(pcm)
            made["ff"] = proc
        else:
            proc.returncode, proc.stderr = yt_rc, io.BytesIO(yt_err)
            made["yt"] = proc
        return proc

    _popen.made = made
    return _popen


@unittest.skipUnless(harvest is not None, "harvest.py needs librosa/numpy -- not this test's job")
class ChildBoundary(unittest.TestCase):
    """`stream_chroma` keeps its three-tuple contract while the work moves to another process."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.jobs = os.path.join(self.tmp, "tmp")
        self._jobs = harvest.JOBS
        harvest.JOBS = self.jobs
        self.url = "https://example.invalid/watch?v=abc"
        self.addCleanup(self._restore)

    def _restore(self):
        harvest.JOBS = self._jobs
        harvest._STOP.update({"signum": 0, "child": None, "procs": [], "part": None})
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _child_writes(self, result, chroma=None, pcm=b""):
        """A fake fetch child: it drops the files a real one leaves, then exits."""
        def _popen(argv, **kwargs):
            job = argv[argv.index("--job") + 1]
            os.makedirs(job, exist_ok=True)
            if chroma is not None:
                np.save(os.path.join(job, "chroma32.npy"), chroma)
            if pcm:
                with open(os.path.join(job, "pcm.f32le"), "wb") as fh:
                    fh.write(pcm)
            if result is not None:
                with open(os.path.join(job, "result.json"), "w") as fh:
                    json.dump(result, fh)
            return _FakeChild(argv)
        return _popen

    def test_child_spawn_shape(self):
        """The argv, the working directory, the process group, and the allocator variable."""
        seen = {}

        def _popen(argv, **kwargs):
            seen["argv"], seen["kwargs"] = argv, kwargs
            job = argv[argv.index("--job") + 1]
            os.makedirs(job, exist_ok=True)
            with open(os.path.join(job, "result.json"), "w") as fh:
                json.dump({"ok": False, "error": "nope"}, fh)
            return _FakeChild(argv)

        with mock.patch.object(harvest.subprocess, "Popen", _popen):
            harvest.stream_chroma(self.url)

        self.assertEqual(seen["argv"][0], sys.executable)
        self.assertTrue(seen["argv"][1].endswith("harvest.py"))
        self.assertEqual(seen["argv"][2:4], ["--fetch-one", self.url])
        self.assertEqual(seen["argv"][4], "--job")
        self.assertTrue(seen["argv"][5].startswith(self.jobs))
        self.assertEqual(seen["kwargs"]["cwd"], harvest.HOME)
        self.assertEqual(seen["kwargs"]["env"]["MallocLargeCache"], "0")
        # Same process group as the parent, so the supervisor's killpg reaches the whole family.
        self.assertNotIn("start_new_session", seen["kwargs"])

    def test_the_child_argv_never_says_run(self):
        """`--run` in the child's argv would make the player's supervisor adopt it."""
        seen = {}

        def _popen(argv, **kwargs):
            seen["argv"] = argv
            job = argv[argv.index("--job") + 1]
            os.makedirs(job, exist_ok=True)
            with open(os.path.join(job, "result.json"), "w") as fh:
                json.dump({"ok": False, "error": "nope"}, fh)
            return _FakeChild(argv)

        with mock.patch.object(harvest.subprocess, "Popen", _popen):
            harvest.stream_chroma(self.url)
        self.assertNotIn("--run", seen["argv"])

    def test_an_existing_malloc_setting_is_not_overridden(self):
        """`setdefault`, so an operator can run an experiment without editing the code."""
        seen = {}

        def _popen(argv, **kwargs):
            seen["env"] = kwargs["env"]
            job = argv[argv.index("--job") + 1]
            os.makedirs(job, exist_ok=True)
            with open(os.path.join(job, "result.json"), "w") as fh:
                json.dump({"ok": False, "error": "nope"}, fh)
            return _FakeChild(argv)

        with mock.patch.dict(os.environ, {"MallocLargeCache": "1"}), \
                mock.patch.object(harvest.subprocess, "Popen", _popen):
            harvest.stream_chroma(self.url)
        self.assertEqual(seen["env"]["MallocLargeCache"], "1")

    def test_success_returns_a_memmap_and_leaves_no_files_behind(self):
        chroma = np.arange(12 * 5, dtype="float32").reshape(12, 5)
        pcm = _pcm(4000)
        with mock.patch.object(harvest.subprocess, "Popen",
                               self._child_writes({"ok": True, "error": None, "seconds": 0.25,
                                                   "peak_mb": 901.5, "footprint_mb": 590.0},
                                                  chroma=chroma, pcm=pcm)):
            c, samples, err = harvest.stream_chroma(self.url)

        self.assertIsNone(err)
        self.assertTrue(np.array_equal(c, chroma))
        self.assertEqual(c.dtype, np.dtype("float32"))
        # The memmap still reads the right samples although the file is already gone.
        self.assertFalse(os.path.exists(os.path.join(self.jobs)) and
                         os.listdir(self.jobs))
        self.assertTrue(np.array_equal(np.asarray(samples),
                                       np.frombuffer(pcm, dtype="float32")))
        # ...and it slices like the array it replaced, which is all write_excerpt needs.
        self.assertTrue(np.array_equal(np.asarray(samples[10:20], dtype="float32"),
                                       np.frombuffer(pcm, dtype="float32")[10:20]))
        self.assertEqual(harvest._LAST_CHILD["peak_mb"], 901.5)

    def test_write_excerpt_from_a_memmap_matches_an_in_memory_array(self):
        """`write_excerpt` is the one place the samples are used for something other than
        counting, and its hard cap is the project's copyright posture in code."""
        sf = self._soundfile()
        chroma = np.zeros((12, 3), dtype="float32")
        pcm = _pcm(SR * 40)
        with mock.patch.object(harvest.subprocess, "Popen",
                               self._child_writes({"ok": True, "error": None},
                                                  chroma=chroma, pcm=pcm)):
            _c, samples, _e = harvest.stream_chroma(self.url)

        a = os.path.join(self.tmp, "a.wav")
        b = os.path.join(self.tmp, "b.wav")
        with mock.patch.object(harvest, "_write_provenance", lambda: None):
            harvest.write_excerpt(samples, 20.0, a)
            harvest.write_excerpt(np.frombuffer(pcm, dtype="float32"), 20.0, b)
        self.assertTrue(np.array_equal(sf.read(a)[0], sf.read(b)[0]))

    @staticmethod
    def _soundfile():
        try:
            import soundfile
        except ImportError:
            raise unittest.SkipTest("soundfile unavailable")
        return soundfile

    def test_a_handled_error_comes_back_as_the_string_callers_already_know(self):
        wall = "ERROR: Sign in to confirm you're not a bot"
        with mock.patch.object(harvest.subprocess, "Popen",
                               self._child_writes({"ok": False, "error": wall})):
            c, samples, err = harvest.stream_chroma(self.url)
        self.assertEqual((c, samples), (None, None))
        self.assertEqual(err, wall)
        self.assertTrue(harvest.is_bot_wall(err))      # the halt path still recognises it

    def test_a_crashed_child_is_reported_not_swallowed(self):
        def _popen(argv, **kwargs):
            return _FakeChild(argv, returncode=1,
                              stderr=b"Traceback (most recent call last):\nValueError: boom\n")
        with mock.patch.object(harvest.subprocess, "Popen", _popen):
            c, samples, err = harvest.stream_chroma(self.url)
        self.assertEqual((c, samples), (None, None))
        self.assertEqual(err, "child failed (exit 1): ValueError: boom")

    def test_a_child_killed_by_a_signal_reads_as_stopped(self):
        def _popen(argv, **kwargs):
            return _FakeChild(argv, returncode=143)
        with mock.patch.object(harvest.subprocess, "Popen", _popen):
            self.assertEqual(harvest.stream_chroma(self.url)[2], "stopped")

    def test_a_missing_result_file_is_a_failure_not_a_success(self):
        def _popen(argv, **kwargs):
            return _FakeChild(argv, returncode=0, stderr=b"segmentation fault\n")
        with mock.patch.object(harvest.subprocess, "Popen", _popen):
            err = harvest.stream_chroma(self.url)[2]
        self.assertTrue(err.startswith("child failed (exit 0): "), err)

    def test_the_escape_hatch_runs_the_fetch_in_process(self):
        """NETRADIO_HARVEST_CHILD=0 is for diagnosing the child's environment, so it must not
        spawn one."""
        called = []
        with mock.patch.dict(os.environ, {"NETRADIO_HARVEST_CHILD": "0"}), \
                mock.patch.object(harvest.subprocess, "Popen",
                                  lambda *a, **k: called.append(a) or _FakeChild(["x"])), \
                mock.patch.object(harvest, "_fetch_and_sign",
                                  return_value={"ok": False, "error": "in-process"}) as fetch:
            err = harvest.stream_chroma(self.url)[2]
        self.assertEqual(err, "in-process")
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(called, [])

    def test_stale_job_directories_are_swept_but_recent_ones_are_left(self):
        """A crashed child's spool is dead weight; a live split-harvester fetch is not ours."""
        os.makedirs(os.path.join(self.jobs, "old"))
        os.makedirs(os.path.join(self.jobs, "recent"))
        old = os.path.join(self.jobs, "old")
        os.utime(old, (0, harvest.time.time() - 2 * 3600))
        self.assertEqual(harvest.sweep_job_dirs(), 1)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(os.path.join(self.jobs, "recent")))


class _FakeChild:
    """A fake fetch child: it has already exited by the time the parent looks."""

    def __init__(self, argv, returncode=0, stderr=b""):
        self.argv = argv
        self.returncode = returncode
        self._stderr = stderr

    def communicate(self, timeout=None):
        return b"", self._stderr

    def poll(self):
        return self.returncode

    def terminate(self):
        pass


@unittest.skipUnless(harvest is not None, "harvest.py needs librosa/numpy -- not this test's job")
class NoSignatureFromAPartialDecode(unittest.TestCase):
    """Every way the decode can end badly, and none of them writes a signature.

    A signature is written once per URL, uploaded to the pool, and never fetched again. A
    truncated one is not a transient error; it is a wrong answer that outlives the run.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.job = os.path.join(self.tmp, "job")
        self.url = "https://example.invalid/watch?v=partial"
        self.saved = []
        self.put = []
        self._cache = harvest.CACHE
        harvest.CACHE = os.path.join(self.tmp, "cache")
        self.addCleanup(self._restore)

    def _restore(self):
        harvest.CACHE = self._cache
        harvest._STOP.update({"signum": 0, "child": None, "procs": [], "part": None})
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, popen, **patches):
        real_save = np.save

        def _save(path, arr, *a, **k):
            self.saved.append(os.path.basename(str(path)))
            return real_save(path, arr, *a, **k)

        with mock.patch.object(harvest.subprocess, "Popen", popen), \
                mock.patch.object(harvest.np, "save", _save), \
                mock.patch.object(harvest.sigstore, "enabled", lambda: True), \
                mock.patch.object(harvest.sigstore, "put",
                                  lambda *a: self.put.append(a) or True), \
                mock.patch.object(harvest.chroma_recipe, "compute_chroma",
                                  lambda y, sr=None: np.zeros((12, 4), dtype="float32")):
            for name, value in patches.items():
                setattr(harvest, name, value)
            return harvest._fetch_and_sign(self.url, self.job)

    def _assert_no_signature(self, result, error_contains):
        self.assertFalse(result["ok"])
        self.assertIn(error_contains, result["error"])
        self.assertNotIn(os.path.basename(harvest.sig_path(self.url)), self.saved)
        self.assertNotIn("chroma32.npy", self.saved)
        self.assertEqual(self.put, [])
        self.assertFalse(os.path.exists(os.path.join(self.job, "pcm.f32le.part")))

    def test_ytdlp_killed_mid_stream(self):
        """yt-dlp dies, ffmpeg sees EOF and exits 0 on what it has. That is the trap."""
        result = self._run(fake_decode(pcm=_pcm(LONG_ENOUGH), yt_rc=-15,
                                       yt_err=b"ERROR: interrupted\n"))
        self._assert_no_signature(result, "interrupted")

    def test_ffmpeg_failed(self):
        result = self._run(fake_decode(pcm=_pcm(LONG_ENOUGH), ff_rc=1,
                                       ff_err=b"pipe:0: Invalid data found\n"))
        self._assert_no_signature(result, "ffmpeg: pipe:0: Invalid data found")

    def test_nothing_decoded_at_all(self):
        result = self._run(fake_decode(pcm=b"", yt_err=b"ERROR: video unavailable\n"))
        self._assert_no_signature(result, "video unavailable")

    def test_too_short_to_trust(self):
        result = self._run(fake_decode(pcm=_pcm(SR * 5)))
        self._assert_no_signature(result, "too short (5s)")

    def test_a_stop_between_the_decode_and_the_recipe(self):
        """The flag can go up after the decode finished cleanly. Nothing is written."""
        popen = fake_decode(pcm=_pcm(LONG_ENOUGH))

        def _stop_after_decode(y, sr=None):
            raise AssertionError("compute_chroma must not run after a stop")

        with mock.patch.object(harvest.subprocess, "Popen", popen), \
                mock.patch.object(harvest.chroma_recipe, "compute_chroma", _stop_after_decode), \
                mock.patch.object(harvest, "_wait",
                                  side_effect=lambda p, **k: (harvest._STOP.__setitem__(
                                      "signum", signal.SIGTERM), p.wait())[1]):
            result = harvest._fetch_and_sign(self.url, self.job)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "stopped")

    def test_a_clean_decode_writes_the_signature_once(self):
        result = self._run(fake_decode(pcm=_pcm(LONG_ENOUGH)))
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.saved.count(os.path.basename(harvest.sig_path(self.url))), 1)
        self.assertIn("chroma32.npy", self.saved)
        self.assertEqual(len(self.put), 1)
        self.assertEqual(result["n_samples"], LONG_ENOUGH)
        self.assertEqual(result["seconds"], 60.0)
        self.assertIn("peak_mb", result)
        self.assertTrue(os.path.exists(os.path.join(self.job, "pcm.f32le")))

    def test_a_flood_of_stderr_does_not_deadlock_the_decode(self):
        """yt-dlp's stderr pipe holds 64 KB. Nobody read it until ffmpeg exited, so a chatty
        yt-dlp and a long decode could wedge each other."""
        noise = (b"[download] progress line\n" * 12000)[:256 * 1024]
        result = self._run(fake_decode(pcm=_pcm(LONG_ENOUGH), yt_err=noise))
        self.assertTrue(result["ok"], result)

    def test_the_child_never_writes_the_state_or_the_queue(self):
        missing = os.path.join(self.tmp, "nowhere", "state.json")
        with mock.patch.object(harvest, "STATE", missing), \
                mock.patch.object(harvest, "QUEUE", missing):
            result = self._run(fake_decode(pcm=_pcm(LONG_ENOUGH)))
        self.assertTrue(result["ok"], result)
        self.assertFalse(os.path.exists(os.path.dirname(missing)))


@unittest.skipUnless(harvest is not None, "harvest.py needs librosa/numpy -- not this test's job")
class TheChildEntryPoint(unittest.TestCase):
    """`--fetch-one` really runs, and it runs before anything that could touch shared state."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.job = os.path.join(self.tmp, "job")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_main_dispatches_the_child_before_taking_the_writer_lock(self):
        argv = ["harvest.py", "--fetch-one", "https://example.invalid/x", "--job", self.job]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(harvest, "acquire_writer_lock") as lock, \
                mock.patch.object(harvest, "_fetch_and_sign",
                                  return_value={"ok": True, "error": None}) as fetch:
            rc = harvest.main()
        self.assertEqual(rc, 0)
        fetch.assert_called_once_with("https://example.invalid/x", self.job)
        lock.assert_not_called()
        with open(os.path.join(self.job, "result.json")) as fh:
            self.assertTrue(json.load(fh)["ok"])

    def test_a_crash_in_the_child_exits_nonzero(self):
        argv = ["harvest.py", "--fetch-one", "https://example.invalid/x", "--job", self.job]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(harvest, "_fetch_and_sign", side_effect=ValueError("boom")):
            self.assertEqual(harvest.main(), 1)
        self.assertFalse(os.path.exists(os.path.join(self.job, "result.json")))

    def test_a_real_child_process_signs_a_real_decode(self):
        """End to end through a genuine subprocess, with stand-in binaries on PATH.

        This is the only test that runs the whole child -- argv, spool, recipe, result.json -- as
        the parent will. No network: `yt-dlp` writes nothing and the stand-in `ffmpeg` writes the
        PCM the real one would have produced.
        """
        try:
            import librosa            # noqa: F401
        except ImportError:
            self.skipTest("librosa unavailable -- see requirements-streamalign.txt")

        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir)
        pcm_src = os.path.join(self.tmp, "pcm.raw")
        with open(pcm_src, "wb") as fh:
            fh.write(_pcm(SR * 50))
        self._stub(bindir, "yt-dlp", "exit 0\n")
        self._stub(bindir, "ffmpeg", "cat %s\n" % pcm_src)

        url = "https://example.invalid/real"
        # The signature cache is the repo's own gitignored directory; put the one file this test
        # creates back afterwards.
        self.addCleanup(lambda: os.path.exists(harvest.sig_path(url))
                        and os.unlink(harvest.sig_path(url)))
        env = dict(os.environ,
                   PATH=bindir + os.pathsep + os.environ.get("PATH", ""),
                   PYTHONPATH=SCRIPTS,
                   NETRADIO_SIG_BUCKET="")          # sigstore dark: no upload, no credentials
        import subprocess
        out = subprocess.run(
            [sys.executable, os.path.join(SCRIPTS, "harvest.py"),
             "--fetch-one", url, "--job", self.job],
            capture_output=True, text=True, env=env, timeout=600)
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        with open(os.path.join(self.job, "result.json")) as fh:
            result = json.load(fh)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["n_samples"], SR * 50)
        self.assertEqual(result["seconds"], 50.0)
        chroma = np.load(os.path.join(self.job, "chroma32.npy"))
        self.assertEqual(chroma.shape[0], 12)
        self.assertEqual(chroma.dtype, np.dtype("float32"))
        self.assertEqual(os.path.getsize(os.path.join(self.job, "pcm.f32le")), SR * 50 * 4)

    @staticmethod
    def _stub(bindir, name, body):
        path = os.path.join(bindir, name)
        with open(path, "w") as fh:
            fh.write("#!/bin/sh\n" + body)
        os.chmod(path, 0o755)


@unittest.skipUnless(harvest is not None, "harvest.py needs librosa/numpy -- not this test's job")
class Stopping(unittest.TestCase):
    def setUp(self):
        self.addCleanup(lambda: harvest._STOP.update(
            {"signum": 0, "child": None, "procs": [], "part": None}))

    def test_the_parent_handler_raises_a_flag_and_passes_the_signal_on(self):
        child = mock.Mock()
        child.poll.return_value = None
        harvest._STOP["child"] = child
        harvest._parent_stop(signal.SIGTERM, None)
        self.assertTrue(harvest._stop_requested())
        self.assertEqual(harvest._stop_name(), "SIGTERM")
        child.terminate.assert_called_once_with()

    def test_the_nap_comes_back_early(self):
        import threading
        started = harvest.time.time()
        threading.Timer(0.2, lambda: harvest._STOP.__setitem__("signum",
                                                               signal.SIGINT)).start()
        self.assertTrue(harvest._nap(30))
        self.assertLess(harvest.time.time() - started, 5)

    def test_the_child_stops_ffmpeg_before_ytdlp(self):
        """Killing yt-dlp first makes ffmpeg exit 0 on a truncated stream."""
        order = []
        ff = _FakeProc(["ffmpeg"], alive=True, order=order)
        yt = _FakeProc(["yt-dlp"], alive=True, order=order)
        part = tempfile.mktemp()
        open(part, "wb").close()
        harvest._STOP.update({"procs": [ff, yt], "part": part})
        exits = []
        with mock.patch.object(harvest.os, "_exit", exits.append):
            harvest._child_stop(signal.SIGTERM, None)
        self.assertEqual(order, ["ffmpeg", "yt-dlp"])
        self.assertFalse(os.path.exists(part))
        self.assertEqual(exits, [128 + int(signal.SIGTERM)])

    def test_run_stops_cleanly_and_leaves_the_url_pending(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        state_path = os.path.join(tmp, "state.json")
        state = {"session": {"phase": "working", "until": 0}, "current": "https://x.invalid/1"}
        harvest._STOP["signum"] = signal.SIGTERM
        with mock.patch.object(harvest, "STATE", state_path):
            harvest._stopped(state)
        with open(state_path) as fh:
            saved = json.load(fh)
        self.assertEqual(saved["session"]["phase"], "stopped (SIGTERM)")
        self.assertIsNone(saved["current"])


@unittest.skipUnless(memwatch is not None, "memwatch needs the scripts path")
class Footprint(unittest.TestCase):
    def test_the_probe_reports_a_plausible_number_and_a_peak(self):
        current, peak = memwatch.footprint_mb()
        if current is None:
            self.skipTest("no footprint probe on %s" % sys.platform)
        self.assertGreater(current, 1)
        self.assertGreaterEqual(peak, current)
        block = np.ones(50_000_000, dtype="float32")     # 200 MB
        after, after_peak = memwatch.footprint_mb()
        del block
        self.assertGreaterEqual(after - current, 150)
        self.assertGreaterEqual(after_peak, after)

    @unittest.skipUnless(sys.platform == "darwin", "the footprint CLI is macOS only")
    def test_the_probe_agrees_with_the_footprint_cli(self):
        import re
        import subprocess
        try:
            out = subprocess.run(["footprint", "-p", str(os.getpid())],
                                 capture_output=True, text=True, timeout=30).stdout
        except (OSError, subprocess.SubprocessError):
            self.skipTest("the footprint CLI is not available")
        m = re.search(r"Footprint:\s+([\d.]+)\s*(KB|MB|GB)", out)
        if not m:
            self.skipTest("could not parse the footprint CLI's output")
        cli = float(m.group(1)) * {"KB": 1 / 1024.0, "MB": 1.0, "GB": 1024.0}[m.group(2)]
        current = memwatch.footprint_mb()[0]
        self.assertLess(abs(current - cli) / max(cli, 1.0), 0.10)

    def test_a_broken_probe_returns_nothing_rather_than_raising(self):
        with mock.patch.object(memwatch.ctypes, "CDLL", side_effect=OSError("no")):
            self.assertEqual(memwatch.footprint_mb(), (None, None))

    def test_the_allocator_canary_flags_retention(self):
        readings = iter([(20.0, 20.0), (800.0, 800.0)])
        _b, _a, retained = memwatch.allocator_canary(sampler=lambda: next(readings))
        self.assertEqual(retained, 780.0)
        self.assertIn("allocator retention back", memwatch.canary_issue(retained))
        self.assertIn("MallocLargeCache", memwatch.canary_line(retained))

    def test_a_clean_allocator_earns_no_issue(self):
        readings = iter([(20.0, 20.0), (36.0, 36.0)])
        _b, _a, retained = memwatch.allocator_canary(sampler=lambda: next(readings))
        self.assertEqual(retained, 16.0)
        self.assertIsNone(memwatch.canary_issue(retained))


@unittest.skipUnless(harvest is not None, "harvest.py needs librosa/numpy -- not this test's job")
class MemoryRowsAndCeiling(unittest.TestCase):
    def setUp(self):
        self.state = {"issues": []}
        self.url = "https://example.invalid/watch?v=mem"

    def _at(self, parent_mb):
        return mock.patch.object(harvest.memwatch, "footprint_mb",
                                 lambda: (parent_mb, parent_mb))

    def test_a_row_is_written_for_a_cached_candidate_with_no_child_numbers(self):
        with self._at(310.0):
            row = harvest.record_memory(self.state, self.url, None)
        self.assertEqual(row["parent_mb"], 310.0)
        self.assertIsNone(row["child_peak_mb"])
        self.assertEqual(self.state["mem"], row)
        self.assertEqual(self.state["mem_log"], [row])

    def test_a_row_carries_the_childs_peak_when_one_ran(self):
        child = {"peak_mb": 905.2, "footprint_mb": 591.0, "seconds": 7020.0}
        with self._at(310.0):
            row = harvest.record_memory(self.state, self.url, child)
        self.assertEqual((row["child_peak_mb"], row["child_after_mb"]), (905.2, 591.0))
        self.assertEqual(row["seconds"], 7020.0)

    def test_the_log_keeps_the_last_fifty_rows(self):
        with self._at(100.0):
            for i in range(60):
                harvest.record_memory(self.state, "%s#%d" % (self.url, i), None)
        self.assertEqual(len(self.state["mem_log"]), harvest.MEM_LOG_KEEP)
        self.assertTrue(self.state["mem_log"][-1]["url"].endswith("#59"))

    def test_the_ceiling_is_off_unless_it_is_set(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NETRADIO_HARVEST_MEM_CEILING_MB", None)
            with self._at(9000.0):
                self.assertFalse(harvest.check_memory(self.state, self.url, None))
        self.assertEqual(self.state["issues"], [])

    def test_a_parent_over_the_ceiling_stands_down(self):
        with mock.patch.dict(os.environ, {"NETRADIO_HARVEST_MEM_CEILING_MB": "3000"}):
            with self._at(3001.0):
                self.assertTrue(harvest.check_memory(self.state, self.url, None))
        self.assertEqual(self.state["session"]["phase"], "restarting: memory ceiling")
        self.assertIn("3000", self.state["issues"][-1]["issue"])

    def test_a_child_over_the_ceiling_only_reports(self):
        """The child's memory left with the child, so restarting the parent would fix nothing."""
        with mock.patch.dict(os.environ, {"NETRADIO_HARVEST_MEM_CEILING_MB": "3000"}):
            with self._at(300.0):
                self.assertFalse(harvest.check_memory(self.state, self.url, {"peak_mb": 5000.0}))
        self.assertIn("fetch child peaked at 5000 MB", self.state["issues"][-1]["issue"])
        self.assertNotIn("session", self.state)

    def test_a_nonsense_ceiling_is_ignored_rather_than_crashing_the_run(self):
        with mock.patch.dict(os.environ, {"NETRADIO_HARVEST_MEM_CEILING_MB": "lots"}):
            self.assertEqual(harvest.mem_ceiling_mb(), 0.0)


if __name__ == "__main__":
    unittest.main()
