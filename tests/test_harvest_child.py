"""The per-file decode child, the stop path, and the memory rows.

Nothing here touches the network and nothing decodes real audio: `ffmpeg` is a fake object,
every decode child is a fake `Popen`, and the two things worth being careful about are pinned:

  * **A partial decode must never become a signature.** A signature is written once per key and
    never decoded again, so a truncated or wrong-length one is not a transient error -- it is a
    permanently wrong answer, cached locally and uploaded to the pool. Several tests exist only
    to show that each way a decode can end badly produces no signature at all.
  * **The child must not look like the harvester.** The supervisor discovers a live harvester by
    looking for a `harvest.py` command line containing `--run`. If a decode child's argv ever
    gained that flag, the supervisor would adopt a child that lives for one file.
"""

import contextlib
import io
import json
import os
import shutil
import signal
import subprocess
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

import cache_budget                     # noqa: E402  (the machine's one cache policy)

SR = 16000
LONG_ENOUGH = int(60 * SR)              # comfortably over chroma_recipe.MIN_SECONDS

# The cache-policy names the landing test saves and restores (the same set
# tests/test_cache_budget.py uses).
CACHE_ENV = ("NETRADIO_CACHE_ROOT", "NETRADIO_DOWNLOAD_ROOT", "NETRADIO_DISK_MAX_PCT",
             "NETRADIO_CACHE_EVENTS_DAYS")


def _pcm(n_samples):
    """Decoded PCM as ffmpeg would write it: mono float32 little-endian."""
    return (np.arange(n_samples, dtype="float32") % 7.0 - 3.0).tobytes()


def _key(url):
    return "u" + __import__("hashlib").sha1(url.encode()).hexdigest()[:20]


def _job_of(argv):
    """The job directory from a spawned child's argv, however the flag is spelled."""
    for flag in ("--sign-job", "--job"):
        if flag in argv:
            return argv[argv.index(flag) + 1]
    raise AssertionError("no job directory on %r" % (argv,))


class _FakeProc:
    """Just enough of `Popen` for the decode path: exit code, stderr, and a stop record."""

    def __init__(self, argv, returncode=0, stderr=b"", alive=False, order=None):
        self.argv = argv
        self.returncode = returncode
        self.stdout = io.BytesIO()          # never read; the code closes it
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


def fake_decode(pcm=b"", rc=0, stderr=b"", alive=False, order=None, vanish=None):
    """A `subprocess.Popen` stand-in for the one ffmpeg the child spawns.

    The fake writes `pcm` straight into the spool file it is handed as `stdout`, which is
    exactly what the real one does -- the point of the change being tested is that Python never
    holds the decoded audio. `vanish` unlinks that path while "decoding".
    """
    made = {}

    def _popen(argv, **kwargs):
        proc = _FakeProc(argv, returncode=rc, stderr=stderr, alive=alive, order=order)
        if pcm:
            kwargs["stdout"].write(pcm)
        made["ff"] = proc
        if vanish:
            os.unlink(vanish)
        return proc

    _popen.made = made
    return _popen


def _feed(case, key, url="https://example.invalid/watch?v=abc", duration_s=60.0):
    """One audio file + its sidecar in the case's directory, the contract's own shape."""
    path = os.path.join(case.audio, key + ".mp3")
    with open(path, "wb") as fh:
        fh.write(b"")
    with open(os.path.join(case.audio, key + ".json"), "w") as fh:
        json.dump({"key": key, "url": url, "duration_s": duration_s,
                   "fed_at": "2026-09-19T00:00:00+00:00"}, fh)
    return path


@unittest.skipUnless(harvest is not None, "harvest.py needs numpy -- not this test's job")
class ChildBoundary(unittest.TestCase):
    """`sign_file` keeps its contract while the decode happens in another process."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="child-")
        self.audio = os.path.join(self.tmp, "audio")
        os.makedirs(self.audio)
        self.jobs = os.path.join(self.tmp, "jobs")
        self._paths = (harvest.JOBS, harvest.HARVEST_DIRS, harvest.LEDGER, harvest.STATE)
        harvest.JOBS = self.jobs
        harvest.HARVEST_DIRS = self.audio
        harvest.LEDGER = os.path.join(self.tmp, "ledger.json")
        harvest.STATE = os.path.join(self.tmp, "state.json")
        self.key = _key("https://example.invalid/watch?v=abc")
        self.path = _feed(self, self.key)
        self.addCleanup(self._restore)
        # The child path (NETRADIO_HARVEST_CHILD unset), so the spawn is what runs.
        os.environ.pop("NETRADIO_HARVEST_CHILD", None)
        self._child_env = dict(os.environ)

    def _restore(self):
        (harvest.JOBS, harvest.HARVEST_DIRS, harvest.LEDGER, harvest.STATE) = self._paths
        harvest._STOP.update({"signum": 0, "child": None, "procs": [], "part": None})
        for k in [k for k in list(os.environ) if k.startswith("NETRADIO_HARVEST_")]:
            os.environ.pop(k, None)
        os.environ.clear()
        os.environ.update(self._child_env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _child_writes(self, result, chroma=None, pcm=b""):
        """A fake decode child: it drops the files a real one leaves, then exits."""

        def _popen(argv, **kwargs):
            job = _job_of(argv)
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
            job = _job_of(argv)
            # Read it here: the job directory is unlinked the moment sign_file returns.
            with open(os.path.join(job, "sign.json")) as fh:
                seen["handoff"] = json.load(fh)
            with open(os.path.join(job, "result.json"), "w") as fh:
                json.dump({"ok": False, "error": "nope"}, fh)
            return _FakeChild(argv)

        with mock.patch.object(harvest.subprocess, "Popen", _popen):
            harvest.sign_file(self.path)

        self.assertEqual(seen["argv"][0], sys.executable)
        self.assertTrue(seen["argv"][1].endswith("harvest.py"))
        self.assertEqual(seen["argv"][2], "--sign-job")
        self.assertTrue(seen["argv"][3].startswith(self.jobs))
        # The audio path is NOT on the command line -- it travels in the job directory.
        self.assertNotIn(self.path, " ".join(seen["argv"]))
        self.assertEqual(seen["handoff"]["path"], self.path)
        self.assertEqual(seen["handoff"]["expect_s"], 60.0)
        self.assertEqual(seen["kwargs"]["cwd"], harvest.HOME)
        self.assertEqual(seen["kwargs"]["env"]["MallocLargeCache"], "0")
        # Same process group as the parent, so the supervisor's killpg reaches the whole family.
        self.assertNotIn("start_new_session", seen["kwargs"])

    def _spawned_command_line(self, path):
        """What `ps -axo command=` would show for the child -- which is what discovery reads."""
        seen = {}

        def _popen(argv, **kwargs):
            seen["argv"] = argv
            job = _job_of(argv)
            os.makedirs(job, exist_ok=True)
            with open(os.path.join(job, "result.json"), "w") as fh:
                json.dump({"ok": False, "error": "nope"}, fh)
            return _FakeChild(argv)

        with mock.patch.object(harvest.subprocess, "Popen", _popen):
            harvest.sign_file(path)
        return " ".join(seen["argv"])

    def test_the_child_command_line_never_says_run(self):
        """The supervisor matches `--run` as a SUBSTRING of the whole ps line, so that is how
        this has to be checked. Asserting `"--run" not in argv` on the LIST tests element
        membership, which no command line ever satisfies -- it passes while the invariant it
        claims to protect is broken."""
        self.assertNotIn("--run", self._spawned_command_line(self.path))

    def test_a_path_that_contains_the_flag_cannot_be_read_as_it(self):
        """A key is 20 hex characters with a `u` in front, but the PATH around it is not ours
        to promise: a directory named to contain `--run` would put the flag on the command
        line, and the supervisor would adopt a process that lives for one file."""
        hostile_dir = os.path.join(self.tmp, "dir--runXY9z")
        os.makedirs(hostile_dir)
        harvest.HARVEST_DIRS = hostile_dir
        hostile = os.path.join(hostile_dir, self.key + ".mp3")
        with open(hostile, "wb") as fh:
            fh.write(b"")
        with open(os.path.join(hostile_dir, self.key + ".json"), "w") as fh:
            json.dump({"key": self.key, "fed_at": "2026-09-19T00:00:00+00:00"}, fh)
        line = self._spawned_command_line(hostile)
        self.assertNotIn("--run", line)
        self.assertNotIn(hostile, line)

    def test_a_command_line_that_would_be_misread_is_never_spawned(self):
        """The interpreter path and the repo path are not ours to promise. If either carried
        the flag, the decode runs in this process rather than as a child that will be misread."""
        called = []
        with mock.patch.object(harvest, "_spawn_argv",
                               lambda job: ["/opt/py--run/bin/python", "harvest.py",
                                            "--sign-job", job]), \
                mock.patch.object(harvest.subprocess, "Popen",
                                  lambda *a, **k: called.append(a)), \
                mock.patch.object(harvest, "_decode_and_sign",
                                  return_value={"ok": False, "error": "ran in process"}) as dec:
            err = harvest.sign_file(self.path)
        self.assertEqual(err, (None, None))
        self.assertEqual(dec.call_count, 1)
        self.assertEqual(called, [])

    def test_an_existing_malloc_setting_is_not_overridden(self):
        """`setdefault`, so an operator can run an experiment without editing the code."""
        seen = {}

        def _popen(argv, **kwargs):
            seen["env"] = kwargs["env"]
            job = _job_of(argv)
            os.makedirs(job, exist_ok=True)
            with open(os.path.join(job, "result.json"), "w") as fh:
                json.dump({"ok": False, "error": "nope"}, fh)
            return _FakeChild(argv)

        with mock.patch.dict(os.environ, {"MallocLargeCache": "1"}), \
                mock.patch.object(harvest.subprocess, "Popen", _popen):
            harvest.sign_file(self.path)
        self.assertEqual(seen["env"]["MallocLargeCache"], "1")

    def test_success_returns_a_memmap_and_leaves_no_files_behind(self):
        chroma = np.arange(12 * 5, dtype="float32").reshape(12, 5)
        pcm = _pcm(4000)
        with mock.patch.object(harvest.subprocess, "Popen",
                               self._child_writes({"ok": True, "error": None, "reason": None,
                                                   "seconds": 0.25, "peak_mb": 901.5,
                                                   "footprint_mb": 590.0},
                                                  chroma=chroma, pcm=pcm)):
            c, samples = harvest.sign_file(self.path)

        self.assertTrue(np.array_equal(c, chroma))
        self.assertEqual(c.dtype, np.dtype("float32"))
        # The memmap still reads the right samples although the job dir is already gone.
        self.assertFalse(os.path.exists(self.jobs) and os.listdir(self.jobs))
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
                               self._child_writes({"ok": True, "error": None, "reason": None},
                                                  chroma=chroma, pcm=pcm)):
            _c, samples = harvest.sign_file(self.path)

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
        err = "ffmpeg: pipe:0: Invalid data found"
        with mock.patch.object(harvest.subprocess, "Popen",
                               self._child_writes({"ok": False, "reason": "decode_failed",
                                                   "error": err})):
            c, samples = harvest.sign_file(self.path)
        self.assertEqual((c, samples), (None, None))
        self.assertEqual(harvest._LAST_CHILD["reason"], "decode_failed")

    def test_a_crashed_child_is_reported_not_swallowed(self):
        def _popen(argv, **kwargs):
            return _FakeChild(argv, returncode=1,
                              stderr=b"Traceback (most recent call last):\nValueError: boom\n")
        with mock.patch.object(harvest.subprocess, "Popen", _popen):
            c, samples = harvest.sign_file(self.path)
        self.assertEqual((c, samples), (None, None))
        self.assertEqual(harvest._LAST_CHILD["error"], "child failed (exit 1): ValueError: boom")

    def test_a_child_killed_by_a_signal_reads_as_stopped(self):
        def _popen(argv, **kwargs):
            return _FakeChild(argv, returncode=143)
        with mock.patch.object(harvest.subprocess, "Popen", _popen):
            self.assertEqual(harvest.sign_file(self.path), (None, None))
        self.assertEqual(harvest._LAST_CHILD["error"], harvest.STOPPED)

    def test_a_child_signalled_ALONE_still_raises_the_parents_flag(self):
        """The flag and the exit code are two routes for the same event, and the callers' guards
        read the flag. The supervisor signals the whole group, so normally both fire -- but a
        `kill` aimed at the child alone, or our own handler not having run yet, would leave the
        flag down and the stop looking like an ordinary failure."""
        self.assertFalse(harvest._stop_requested())
        with mock.patch.object(harvest.subprocess, "Popen",
                              lambda argv, **k: _FakeChild(argv, returncode=143)):
            harvest.sign_file(self.path)
        self.assertTrue(harvest._stop_requested())
        self.assertEqual(harvest._stop_name(), "SIGTERM")

    def test_a_stop_before_the_spawn_starts_no_decode(self):
        called = []
        harvest._STOP["signum"] = signal.SIGTERM
        with mock.patch.object(harvest.subprocess, "Popen",
                              lambda *a, **k: called.append(a)):
            self.assertEqual(harvest.sign_file(self.path), (None, None))
        self.assertEqual(called, [])

    def test_a_signal_between_the_spawn_and_the_registration_still_reaches_the_child(self):
        """The handler passes a signal to `_STOP["child"]`, which is assigned after `Popen`
        returns. A signal in that window found nothing to pass itself to, and a whole decode
        then ran unwatched while this process sat in communicate()."""
        terminated = []

        class _Racing(_FakeChild):
            """Still running when the parent looks, so `_end` has something to terminate."""

            def __init__(self, argv):
                super().__init__(argv, returncode=130)
                self.alive = True

            def poll(self):
                return None if self.alive else self.returncode

            def terminate(self):
                terminated.append(True)
                self.alive = False

            def wait(self, timeout=None):
                self.alive = False
                return self.returncode

            def communicate(self, timeout=None):
                self.alive = False
                return b"", b""

        def _popen(argv, **kwargs):
            harvest._STOP["signum"] = signal.SIGINT      # the handler ran during the spawn
            return _Racing(argv)

        with mock.patch.object(harvest.subprocess, "Popen", _popen):
            self.assertEqual(harvest.sign_file(self.path), (None, None))
        self.assertEqual(terminated, [True])

    def test_a_missing_result_file_is_a_failure_not_a_success(self):
        def _popen(argv, **kwargs):
            return _FakeChild(argv, returncode=0, stderr=b"segmentation fault\n")
        with mock.patch.object(harvest.subprocess, "Popen", _popen):
            harvest.sign_file(self.path)
        self.assertTrue(harvest._LAST_CHILD["error"].startswith("child failed (exit 0): "))

    def test_the_escape_hatch_runs_the_decode_in_process(self):
        """NETRADIO_HARVEST_CHILD=0 is for diagnosing the child's environment, so it must not
        spawn one."""
        called = []
        with mock.patch.dict(os.environ, {"NETRADIO_HARVEST_CHILD": "0"}), \
                mock.patch.object(harvest.subprocess, "Popen",
                                  lambda *a, **k: called.append(a) or _FakeChild(["x"])), \
                mock.patch.object(harvest, "_decode_and_sign",
                                  return_value={"ok": False, "error": "in-process"}) as dec:
            err = harvest.sign_file(self.path)
        self.assertEqual(err, (None, None))
        self.assertEqual(dec.call_count, 1)
        self.assertEqual(called, [])

    def test_stale_job_directories_are_swept_but_recent_ones_are_left(self):
        """A crashed child's spool is dead weight; a decode child still mid-decode is not ours."""
        os.makedirs(os.path.join(self.jobs, "old"))
        os.makedirs(os.path.join(self.jobs, "recent"))
        old = os.path.join(self.jobs, "old")
        os.utime(old, (0, harvest.time.time() - 2 * 3600))
        self.assertEqual(harvest.sweep_job_dirs(), 1)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(os.path.join(self.jobs, "recent")))


class _FakeChild:
    """A fake decode child: it has already exited by the time the parent looks."""

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


@unittest.skipUnless(harvest is not None, "harvest.py needs numpy -- not this test's job")
class NoSignatureFromABadDecode(unittest.TestCase):
    """Every way the decode can end badly, and none of them writes a signature.

    A signature is written once per key, uploaded to the pool, and never decoded again. A
    truncated one is not a transient error; it is a wrong answer that outlives the run.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="child-decode-")
        self.audio = os.path.join(self.tmp, "audio")
        os.makedirs(self.audio)
        self.job = os.path.join(self.tmp, "job")
        self.key = _key("https://example.invalid/watch?v=partial")
        self.url = "https://example.invalid/watch?v=partial"
        self.saved = []
        self.put = []
        self._paths = (harvest.JOBS, harvest.HARVEST_DIRS, harvest.LEDGER, harvest.STATE)
        harvest.JOBS = os.path.join(self.tmp, "harvest-tmp")
        harvest.HARVEST_DIRS = self.audio
        harvest.LEDGER = os.path.join(self.tmp, "ledger.json")
        harvest.STATE = os.path.join(self.tmp, "state.json")
        self.path = _feed(self, self.key, url=self.url)
        self._cache = harvest.CACHE
        harvest.CACHE = os.path.join(self.tmp, "cache")
        self.addCleanup(self._restore)
        os.environ.pop("NETRADIO_HARVEST_CHILD", None)

    def _restore(self):
        harvest.CACHE = self._cache
        (harvest.JOBS, harvest.HARVEST_DIRS, harvest.LEDGER, harvest.STATE) = self._paths
        harvest._STOP.update({"signum": 0, "child": None, "procs": [], "part": None})
        for k in [k for k in list(os.environ) if k.startswith("NETRADIO_HARVEST_")]:
            os.environ.pop(k, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, popen, **patches):
        real_save = np.save

        def _save(path, arr, *a, **k):
            # np.save is handed a file handle for the cache's .tmp write, so take the handle's
            # name: what is recorded is what a write aimed at, whichever side of the rename.
            self.saved.append(os.path.basename(str(getattr(path, "name", path))))
            return real_save(path, arr, *a, **k)

        with mock.patch.object(harvest.subprocess, "Popen", popen), \
                mock.patch.object(harvest.np, "save", _save), \
                mock.patch.object(harvest.chroma_recipe, "compute_chroma",
                                  lambda y, sr=None: np.zeros((12, 4), dtype="float32")), \
                mock.patch.object(harvest.sigstore, "enabled", lambda: True), \
                mock.patch.object(harvest.sigstore, "put",
                                  lambda path, key: self.put.append(key) or "etag"):
            for name, value in patches.items():
                setattr(harvest, name, value)
            os.environ["NETRADIO_HARVEST_CHILD"] = "0"     # the decode runs right here
            return harvest.sign_file(self.path)

    def _assert_no_signature(self, result, error_contains):
        self.assertEqual(result, (None, None))
        self.assertEqual([n for n in self.saved if n.startswith(self.key)], [],
                         "no signature was written")
        self.assertNotIn("chroma32.npy", self.saved)
        self.assertEqual(self.put, [])
        self.assertFalse(os.path.exists(os.path.join(self.job, "pcm.f32le.part")))

    def test_ffmpeg_failed(self):
        result = self._run(fake_decode(pcm=_pcm(LONG_ENOUGH), rc=1,
                                       stderr=b"pipe:0: Invalid data found\n"))
        self._assert_no_signature(result, "ffmpeg")
        self.assertEqual(harvest._LAST_CHILD["reason"], "decode_failed")

    def test_nothing_decoded_at_all(self):
        result = self._run(fake_decode(pcm=b"", stderr=b"moov atom not found\n"))
        self._assert_no_signature(result, "moov atom")
        self.assertEqual(harvest._LAST_CHILD["reason"], "decode_failed")

    def test_too_short_to_trust(self):
        result = self._run(fake_decode(pcm=_pcm(SR * 5)))
        self._assert_no_signature(result, "too short")
        self.assertEqual(harvest._LAST_CHILD["reason"], "decode_failed")

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
            os.environ["NETRADIO_HARVEST_CHILD"] = "0"
            result = harvest.sign_file(self.path)
        self.assertEqual(result, (None, None))
        self.assertEqual(harvest._LAST_CHILD["error"], "stopped")

    def test_a_clean_decode_writes_the_signature_once(self):
        result = self._run(fake_decode(pcm=_pcm(LONG_ENOUGH)))
        self.assertEqual(result[0].shape, (12, 4))
        self.assertEqual(len([n for n in self.saved if n.startswith(self.key)]), 1)
        self.assertIn("chroma32.npy", self.saved)
        # the signature, and the sidecar beside it -- the two uploads of one sign
        self.assertEqual(self.put, [self.key + ".npy", self.key + ".json"])
        self.assertEqual(harvest._LAST_CHILD["seconds"], 60.0)
        self.assertIn("peak_mb", harvest._LAST_CHILD)

    def test_a_flood_of_stderr_does_not_deadlock_the_decode(self):
        """ffmpeg's stderr pipe holds 64 KB. Nobody read it until the decode exited, so a chatty
        source and a long decode could wedge each other."""
        noise = (b"[parse] progress line\n" * 12000)[:256 * 1024]
        result = self._run(fake_decode(pcm=_pcm(LONG_ENOUGH), stderr=noise))
        self.assertEqual(result[0].shape, (12, 4))

    def test_the_child_never_writes_the_state_or_the_ledger(self):
        """The decode child's whole job: decode, signature, upload -- and NOT ONE ROW of the
        ledger or the state, which are the parent's and sit under the writer's lock."""
        missing = os.path.join(self.tmp, "nowhere", "state.json")
        with mock.patch.object(harvest, "STATE", missing), \
                mock.patch.object(harvest, "LEDGER", missing), \
                mock.patch.object(harvest.subprocess, "Popen",
                                  fake_decode(pcm=_pcm(LONG_ENOUGH))), \
                mock.patch.object(harvest.chroma_recipe, "compute_chroma",
                                  lambda y, sr=None: np.zeros((12, 4), dtype="float32")), \
                mock.patch.object(harvest.sigstore, "enabled", lambda: True), \
                mock.patch.object(harvest.sigstore, "put", lambda *a: "etag"):
            result = harvest._decode_and_sign(self.path, self.job, 60.0)
        self.assertTrue(result["ok"], result)
        self.assertFalse(os.path.exists(os.path.dirname(missing)))


@unittest.skipUnless(harvest is not None, "harvest.py needs numpy -- not this test's job")
class TheChildEntryPoint(unittest.TestCase):
    """`--sign-job` really runs, and it runs before anything that could touch shared state."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="child-main-")
        self.job = os.path.join(self.tmp, "job")
        os.makedirs(self.job)
        self.audio = os.path.join(self.tmp, "audio")
        os.makedirs(self.audio)
        self.path = os.path.join(self.audio, "u" + "a" * 20 + ".mp3")
        with open(self.path, "wb") as fh:
            fh.write(b"")
        with open(os.path.join(self.audio, "u" + "a" * 20 + ".json"), "w") as fh:
            json.dump({"key": "u" + "a" * 20, "fed_at": "2026-09-19T00:00:00+00:00"}, fh)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_main_dispatches_the_child_before_taking_the_writer_lock(self):
        argv = ["harvest.py", "--sign-job", self.job]
        with open(os.path.join(self.job, "sign.json"), "w") as fh:
            json.dump({"path": self.path, "expect_s": 60}, fh)
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(harvest, "acquire_writer_lock") as lock, \
                mock.patch.object(harvest, "_decode_and_sign",
                                  return_value={"ok": True, "error": None}) as dec:
            rc = harvest.main()
        self.assertEqual(rc, 0)
        # The job file describes the job: the path and, when the sidecar made one, its length.
        # A job file that does not say otherwise publishes (an ordinary sign).
        dec.assert_called_once_with(self.path, self.job, 60, True)
        lock.assert_not_called()
        with open(os.path.join(self.job, "result.json")) as fh:
            self.assertTrue(json.load(fh)["ok"])

    def test_the_canary_re_sign_crosses_the_fork_unpublished(self):
        """`publish: false` in the job file reaches the decode: the canary's re-sign must not
        land in the cache or the bucket whichever process runs it."""
        with open(os.path.join(self.job, "sign.json"), "w") as fh:
            json.dump({"path": self.path, "expect_s": 60, "publish": False}, fh)
        with mock.patch.object(sys, "argv", ["harvest.py", "--sign-job", self.job]), \
                mock.patch.object(harvest, "_decode_and_sign",
                                  return_value={"ok": True, "error": None}) as dec:
            self.assertEqual(harvest.main(), 0)
        dec.assert_called_once_with(self.path, self.job, 60, False)

    def test_a_job_without_a_path_is_refused(self):
        with mock.patch.object(sys, "argv", ["harvest.py", "--sign-job", self.job]):
            self.assertEqual(harvest.main(), 2)

    def test_a_crash_in_the_child_exits_nonzero(self):
        with open(os.path.join(self.job, "sign.json"), "w") as fh:
            json.dump({"path": self.path, "expect_s": None}, fh)
        with mock.patch.object(sys, "argv", ["harvest.py", "--sign-job", self.job]), \
                mock.patch.object(harvest, "_decode_and_sign", side_effect=ValueError("boom")):
            self.assertEqual(harvest.main(), 1)
        self.assertFalse(os.path.exists(os.path.join(self.job, "result.json")))

    def test_a_real_child_process_signs_a_real_decode(self):
        """End to end through a genuine subprocess, with a stand-in ffmpeg on PATH.

        This is the only test that runs the whole child -- argv, spool, recipe, result.json --
        as the parent will. No network: the stand-in `ffmpeg` reads the file it is given and
        writes the PCM the real one would have produced.
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
        self._stub(bindir, "ffmpeg", 'cat "$4" > /dev/null 2>&1; cat %s\n' % pcm_src)

        key = _key("https://example.invalid/real")
        path = os.path.join(self.audio, key + ".mp3")
        with open(path, "wb") as fh:
            fh.write(b"")
        with open(os.path.join(self.audio, key + ".json"), "w") as fh:
            json.dump({"key": key, "duration_s": 50.0,
                       "fed_at": "2026-09-19T00:00:00+00:00"}, fh)
        # The signature cache has no directory while the cache policy is dark, so the child
        # gets a throwaway root and puts its one signature under that -- nothing lands in any
        # real directory, and nothing needs putting back afterwards.
        cache_root = os.path.join(self.tmp, "root")
        jobs = os.path.join(self.tmp, "harvest-jobs")
        env = dict(os.environ,
                   PATH=bindir + os.pathsep + os.environ.get("PATH", ""),
                   PYTHONPATH=SCRIPTS,
                   NETRADIO_SIG_BUCKET="",          # sigstore dark: no upload, no credentials
                   NETRADIO_CACHE_ROOT=cache_root,
                   NETRADIO_DISK_MAX_PCT="100")     # the host's floor must not refuse the sign
        env.pop("NETRADIO_HARVEST_CHILD", None)
        with open(os.path.join(self.job, "sign.json"), "w") as fh:
            json.dump({"path": path, "expect_s": 50}, fh)
        out = subprocess.run(
            [sys.executable, os.path.join(SCRIPTS, "harvest.py"),
             "--sign-job", self.job],
            capture_output=True, text=True, env=env, timeout=600,
            cwd=os.path.dirname(SCRIPTS))
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
        # the signature landed in the throwaway root, under the key
        self.assertTrue(os.path.isfile(os.path.join(cache_root, "chroma", key + ".npy")))

    @staticmethod
    def _stub(bindir, name, body):
        path = os.path.join(bindir, name)
        with open(path, "w") as fh:
            fh.write("#!/bin/sh\n" + body)
        os.chmod(path, 0o755)


@unittest.skipUnless(harvest is not None, "harvest.py needs numpy -- not this test's job")
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

    def test_the_child_stops_its_decode(self):
        ff = _FakeProc(["ffmpeg"], alive=True)
        part = tempfile.mktemp()
        open(part, "wb").close()
        harvest._STOP.update({"procs": [ff], "part": part})
        exits = []
        with mock.patch.object(harvest.os, "_exit", exits.append):
            harvest._child_stop(signal.SIGTERM, None)
        self.assertFalse(ff.alive, "the one child the stop exists for was left running")
        self.assertFalse(os.path.exists(part), "the spool file was left behind")
        self.assertEqual(exits, [128 + int(signal.SIGTERM)])

    def test_run_stops_cleanly_and_leaves_the_file_unsigned(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        state_path = os.path.join(tmp, "state.json")
        state = {"current": "u" + "a" * 20, "issues": []}
        harvest._STOP["signum"] = signal.SIGTERM
        out = io.StringIO()
        with mock.patch.object(harvest, "STATE", state_path), \
                contextlib.redirect_stdout(out):
            harvest._stopped(state)
        with open(state_path) as fh:
            saved = json.load(fh)
        self.assertIsNone(saved["current"], "nothing is left claiming to be in flight")
        self.assertIn("no row", out.getvalue())
        self.assertIn("a later pass", out.getvalue())


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
        """A watchdog is optional; the run is not.

        Both platform probes are broken here, and the platform is pinned for each -- the suite
        runs on Linux in CI and on macOS on the development machine, and a test that only breaks
        the probe of whichever platform it happens to be on proves half of what it claims.
        """
        with mock.patch.object(memwatch.sys, "platform", "darwin"), \
                mock.patch.object(memwatch.ctypes, "CDLL", side_effect=OSError("no")):
            self.assertEqual(memwatch.footprint_mb(), (None, None))
        with mock.patch.object(memwatch.sys, "platform", "linux"), \
                mock.patch("builtins.open", side_effect=OSError("no")):
            self.assertEqual(memwatch.footprint_mb(), (None, None))
        with mock.patch.object(memwatch.sys, "platform", "sunos5"):
            self.assertEqual(memwatch.footprint_mb(), (None, None))   # no probe at all

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


@unittest.skipUnless(harvest is not None, "harvest.py needs numpy -- not this test's job")
class MemoryRowsAndCeiling(unittest.TestCase):
    def setUp(self):
        self.state = {"issues": []}
        self.key = "u" + "a" * 20

    def _at(self, parent_mb):
        return mock.patch.object(harvest.memwatch, "footprint_mb",
                                 lambda: (parent_mb, parent_mb))

    def test_a_row_is_written_for_a_candidate_with_no_child_numbers(self):
        with self._at(310.0):
            row = harvest.record_memory(self.state, self.key, None)
        self.assertEqual(row["parent_mb"], 310.0)
        self.assertIsNone(row["child_peak_mb"])
        self.assertEqual(row["key"], self.key)
        self.assertEqual(self.state["mem"], row)
        self.assertEqual(self.state["mem_log"], [row])

    def test_a_row_carries_the_childs_peak_when_one_ran(self):
        child = {"peak_mb": 905.2, "footprint_mb": 591.0, "seconds": 7020.0}
        with self._at(310.0):
            row = harvest.record_memory(self.state, self.key, child)
        self.assertEqual((row["child_peak_mb"], row["child_after_mb"]), (905.2, 591.0))
        self.assertEqual(row["seconds"], 7020.0)

    def test_the_log_keeps_the_last_fifty_rows(self):
        with self._at(100.0):
            for i in range(60):
                harvest.record_memory(self.state, "%s%d" % ("u", i) * 1, None)
        self.assertEqual(len(self.state["mem_log"]), harvest.MEM_LOG_KEEP)
        self.assertTrue(self.state["mem_log"][-1]["key"].endswith("59"))

    def test_the_ceiling_is_off_unless_it_is_set(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NETRADIO_HARVEST_MEM_CEILING_MB", None)
            with self._at(9000.0):
                self.assertFalse(harvest.check_memory(self.state, self.key, None))
        self.assertEqual(self.state["issues"], [])

    def test_a_parent_over_the_ceiling_stands_down(self):
        with mock.patch.dict(os.environ, {"NETRADIO_HARVEST_MEM_CEILING_MB": "3000"}):
            with self._at(3001.0):
                self.assertTrue(harvest.check_memory(self.state, self.key, None))
        self.assertIn("standing down", self.state["issues"][-1]["issue"])
        self.assertIn("3000", self.state["issues"][-1]["issue"])

    def test_a_child_over_the_ceiling_only_reports(self):
        """The child's memory left with the child, so restarting the parent would fix nothing."""
        with mock.patch.dict(os.environ, {"NETRADIO_HARVEST_MEM_CEILING_MB": "3000"}):
            with self._at(300.0):
                self.assertFalse(harvest.check_memory(self.state, self.key, {"peak_mb": 5000.0}))
        self.assertIn("decode child peaked at 5000 MB", self.state["issues"][-1]["issue"])
        self.assertNotIn("session", self.state)

    def test_a_nonsense_ceiling_is_ignored_rather_than_crashing_the_run(self):
        with mock.patch.dict(os.environ, {"NETRADIO_HARVEST_MEM_CEILING_MB": "lots"}):
            self.assertEqual(harvest.mem_ceiling_mb(), 0.0)


@unittest.skipUnless(harvest is not None, "harvest.py needs numpy -- not this test's job")
class TheInProcessEscapeHatchAlsoStops(unittest.TestCase):
    """NETRADIO_HARVEST_CHILD=0 runs ffmpeg from THIS process, and the parent handler has to
    stop it. Leaving it decoding against a parent that has gone is the exact behaviour the
    handlers were added to end."""

    def setUp(self):
        self.addCleanup(lambda: harvest._STOP.update(
            {"signum": 0, "child": None, "procs": [], "part": None}))

    def test_the_parent_handler_stops_the_decode(self):
        ff = _FakeProc(["ffmpeg"], alive=True)
        harvest._STOP["procs"] = [ff]
        harvest._parent_stop(signal.SIGTERM, None)
        self.assertTrue(harvest._stop_requested())
        self.assertFalse(ff.alive)
        self.assertEqual(harvest._STOP["procs"], [])


@unittest.skipUnless(harvest is not None, "harvest.py needs numpy -- not this test's job")
class ASignatureEvictedBetweenRenameAndCommit(unittest.TestCase):
    """The rename and the commit are two calls, and a bounded cache may lose any entry at any
    time: an eviction run that starts between them takes a signature the policy has not
    recorded yet. The writer must never report a success whose entry is not there -- the
    decode reports `no_space`, and nothing is uploaded for a signature that is not on disk."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="child-landing-")
        self.audio = os.path.join(self.tmp, "audio")
        os.makedirs(self.audio)
        self.key = _key("https://example.invalid/watch?v=landing")
        self.path = _feed(self, self.key, url="https://example.invalid/watch?v=landing")
        self.put = []
        self._saved = {k: os.environ.get(k) for k in list(os.environ)
                       if k.startswith("NETRADIO_") and ("CACHE" in k or k in CACHE_ENV)}
        for k in self._saved:
            os.environ.pop(k, None)
        self.addCleanup(self._restore)
        # The policy ON, with the chroma cache registered over this test's own directory:
        # its cap (2000 bytes) admits the ~400-byte signature, and another writer asking
        # for 1000 bytes of room must take the just-published, not-yet-recorded entry.
        os.environ["NETRADIO_CACHE_ROOT"] = os.path.join(self.tmp, "root")
        os.environ["NETRADIO_CHROMA_CACHE_DIR"] = os.path.join(self.tmp, "cache")
        os.environ["NETRADIO_CHROMA_CACHE_GB"] = "0.000002"
        os.environ["NETRADIO_HARVEST_CHILD"] = "0"
        # The disk floor is the host's, not the test's: a machine whose cache root volume
        # is past the default 82% refuses on the floor before the cap this case exercises,
        # turning a "the entry was evicted between rename and commit" test into a plain
        # `no_space`. Pin the floor out of the way so the cap is the only thing refusing.
        os.environ["NETRADIO_DISK_MAX_PCT"] = "100"
        self._paths = harvest.LEDGER, harvest.STATE, harvest.JOBS
        harvest.LEDGER = os.path.join(self.tmp, "ledger.json")
        harvest.STATE = os.path.join(self.tmp, "state.json")
        harvest.JOBS = os.path.join(self.tmp, "jobs")
        self._registry = dict(cache_budget._REGISTRY), dict(cache_budget._STATS)
        cache_budget._REGISTRY.clear()
        cache_budget._STATS.clear()
        harvest.register_caches()

    def _restore(self):
        cache_budget._REGISTRY.clear()
        cache_budget._REGISTRY.update(self._registry[0])
        cache_budget._STATS.clear()
        cache_budget._STATS.update(self._registry[1])
        (harvest.LEDGER, harvest.STATE, harvest.JOBS) = self._paths
        for k in [k for k in list(os.environ)
                  if k.startswith("NETRADIO_") and ("CACHE" in k or k in CACHE_ENV)]:
            os.environ.pop(k, None)
        os.environ.update(self._saved)
        harvest._STOP.update({"signum": 0, "child": None, "procs": [], "part": None})
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_decode_reports_no_space_and_nothing_is_uploaded(self):
        real_replace = os.replace

        def racing_replace(a, b):
            real_replace(a, b)
            # another writer asks for room, between the signature's rename and its commit
            cache_budget.reserve("chroma", 1000)

        with mock.patch.object(harvest.subprocess, "Popen",
                               fake_decode(pcm=_pcm(LONG_ENOUGH))), \
                mock.patch.object(harvest.chroma_recipe, "compute_chroma",
                                  lambda y, sr=None: np.zeros((12, 60), dtype="float32")), \
                mock.patch.object(harvest.sigstore, "enabled", lambda: True), \
                mock.patch.object(harvest.sigstore, "put",
                                  lambda path, key: self.put.append(key) or "etag"), \
                mock.patch("os.replace", side_effect=racing_replace):
            c, samples = harvest.sign_file(self.path)
        self.assertEqual((c, samples), (None, None))
        self.assertEqual(harvest._LAST_CHILD["reason"], "no_space")
        self.assertIn("did not survive its own landing", harvest._LAST_CHILD["error"])
        self.assertEqual(self.put, [], "nothing uploaded for a signature that is not there")
        self.assertFalse(os.path.exists(os.path.join(harvest._chroma_dir(), self.key + ".npy")))


if __name__ == "__main__":
    unittest.main()
