"""The harvester analyses the PLAYER'S copy of a candidate's audio, and asks the web only when
there is none.

Nothing here touches the network, ffmpeg, ffprobe or the aws CLI: every subprocess is a fake
behind the seams the code already has (`subprocess.Popen`, `harvest._probe_seconds`,
`audiostore._run`). What is pinned:

  * **Audio is addressed by queue id, found local-first, then in the bucket** -- and a file the
    player holds is opened in place, with no copy and no CLI call.
  * **A cached candidate comes first.** `pick_next` prefers an entry with audio over one without,
    whatever the host pacing says; the web is asked only when nothing pending is cached, and not
    at all when the fallback is off.
  * **"No audio" is a non-verdict.** The URL stays `pending`; it never reaches `done`, which is
    never re-fetched.
  * **A cached chunk is checked against its label, never cut to it.** No `-ss`, no `-t`; a file
    that misses its span is refused as `span_mismatch`.
  * **The harvester deletes nothing** under the player's root, and sends the CLI nothing but reads.
"""

import io
import json
import os
import shutil
import signal
import sys
import tempfile
import time
import unittest
from unittest import mock

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)

import numpy as np                      # noqa: E402

try:
    import harvest                      # noqa: E402
    import harvester                    # noqa: E402
    import audiostore                   # noqa: E402
except Exception:                       # librosa/numba absent -> not this test's job
    harvest = harvester = audiostore = None

SR = 16000
LONG_ENOUGH_S = 60                      # comfortably over chroma_recipe.MIN_SECONDS


def _pcm(seconds):
    return np.zeros(int(seconds * SR), dtype="float32").tobytes()


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


class Recorder:
    def __init__(self):
        self.calls, self.results = [], []

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        if not self.results:
            return FakeProc()
        nxt = self.results.pop(0)
        return nxt(cmd) if callable(nxt) else nxt


def _listing(keys):
    return FakeProc(stdout=json.dumps({"Contents": [{"Key": k, "Size": 4} for k in keys]}))


class _FakeFF:
    """Just enough of `Popen` for a file decode: writes `pcm` into the spool, exits `rc`."""

    def __init__(self, argv, pcm, rc, stdout):
        self.argv, self.returncode = argv, rc
        self.stderr = io.BytesIO(b"")
        self.stdout = io.BytesIO()
        if pcm:
            stdout.write(pcm)

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self.returncode

    def terminate(self):
        pass

    def kill(self):
        pass


@unittest.skipIf(harvest is None, "harvest.py needs the librosa venv (.venv) -- skipping")
class Base(unittest.TestCase):
    URL_LOCAL = "https://y/watch?v=local"
    URL_BUCKET = "https://y/watch?v=bucket#t=0,600"
    URL_NONE = "https://s/none"
    URL_STRANGER = "https://y/watch?v=not-in-the-queue"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = os.path.join(self.tmp, "queue-root")
        os.makedirs(os.path.join(self.root, "unplayed"))
        self.local_file = os.path.join(self.root, "unplayed", "id-local.m4a")
        with open(self.local_file, "wb") as fh:
            fh.write(b"m4a" * 100)
        with open(os.path.join(self.root, "index.json"), "w") as fh:
            json.dump({"entries": {"id-local": {"bucket": "unplayed",
                                                "file": "unplayed/id-local.m4a"}}}, fh)
        self._queue([{"id": "id-local", "url": self.URL_LOCAL, "duration": 300},
                     {"id": "id-bucket", "url": self.URL_BUCKET, "duration": 7200},
                     {"id": "id-none", "url": self.URL_NONE, "duration": 200}])
        # every module-level cache and path this exercise touches, reset on both ends
        self._paths = (harvest.STATE, harvest.QUEUE, harvest.WRITER_LOCK, harvest.JOBS,
                       harvest.CACHE, harvest.LISTEN_QUEUE)
        harvest.STATE = os.path.join(self.tmp, "state.json")
        harvest.QUEUE = os.path.join(self.tmp, "queue.json")
        harvest.WRITER_LOCK = os.path.join(self.tmp, "writer.lock")
        harvest.JOBS = os.path.join(self.tmp, "jobs")
        harvest.CACHE = os.path.join(self.tmp, "cache")
        self.addCleanup(self._restore)
        self._reset_caches()
        self.rec = Recorder()
        self.addCleanup(setattr, audiostore, "_run", audiostore._run)
        audiostore._run = self.rec
        self._env = {}
        for k in ("NETRADIO_AUDIO_BUCKET", "NETRADIO_AWS_CLI", "NETRADIO_DOWNLOAD_ROOT",
                  "NETRADIO_HARVEST_FETCH_FALLBACK", "NETRADIO_HARVEST_CHILD",
                  "NETRADIO_AUDIO_S3_ENDPOINT", "NETRADIO_AUDIO_AWS_PROFILE"):
            self._env[k] = os.environ.pop(k, None)
        os.environ["NETRADIO_DOWNLOAD_ROOT"] = self.root
        aws = os.path.join(self.tmp, "aws")
        with open(aws, "w") as fh:
            fh.write("#!/bin/sh\n")
        os.chmod(aws, 0o755)
        os.environ["NETRADIO_AWS_CLI"] = aws
        os.environ["NETRADIO_AUDIO_BUCKET"] = "audio-test"
        self.rec.results = [_listing(["audio/id-bucket.opus"])]

    def _queue(self, items):
        path = os.path.join(self.tmp, "listen_queue.json")
        with open(path, "w") as fh:
            json.dump({"items": items}, fh)
        harvest.LISTEN_QUEUE = path

    def _reset_caches(self):
        harvest._DURATIONS.update({"at": 0.0, "by_url": None})
        harvest._IDS["by_url"] = {}
        harvest._AVAILABLE.update({"at": 0.0, "ids": None, "local": {}})
        harvest._NO_AUDIO.clear()
        harvest._LAST_CHILD.clear()
        harvest._STOP.update({"signum": 0, "child": None, "procs": [], "part": None})
        audiostore._LIST.update({"at": 0.0, "ids": None})

    def _restore(self):
        (harvest.STATE, harvest.QUEUE, harvest.WRITER_LOCK, harvest.JOBS,
         harvest.CACHE, harvest.LISTEN_QUEUE) = self._paths
        self._reset_caches()
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _root_untouched(self):
        self.assertTrue(os.path.exists(self.local_file), "the player's file was removed")
        self.assertTrue(os.path.exists(os.path.join(self.root, "index.json")))
        for cmd in self.rec.calls:
            joined = " ".join(cmd) + " "
            for forbidden in (" rm ", "delete-object", " mv ", "put-object"):
                self.assertNotIn(forbidden, joined)
            self.assertFalse(any(a.startswith("s3://") for a in cmd[:-1]
                                 if cmd[cmd.index(a) - 1] == "cp" and cmd[-1].startswith("s3://")),
                             "an upload: local path then s3:// -- %r" % cmd)


class Availability(Base):
    def test_audio_is_found_by_queue_id_local_first_then_bucket(self):
        self.assertEqual(harvest.audio_for(self.URL_LOCAL),
                         {"id": "id-local", "path": os.path.realpath(self.local_file)})
        self.assertEqual(harvest.audio_for(self.URL_BUCKET), {"id": "id-bucket", "path": None})
        self.assertIsNone(harvest.audio_for(self.URL_NONE))
        self.assertIsNone(harvest.audio_for(self.URL_STRANGER),
                          "a URL that is not a queue entry has no id and therefore no audio")

    def test_one_listing_answers_the_whole_pending_list(self):
        for url in (self.URL_LOCAL, self.URL_BUCKET, self.URL_NONE) * 50:
            harvest.has_audio(url)
        lists = [c for c in self.rec.calls if "list-objects-v2" in c]
        self.assertEqual(len(lists), 1)

    def test_a_trashed_file_is_still_audio(self):
        os.makedirs(os.path.join(self.root, "trash"))
        with open(os.path.join(self.root, "trash", "id-none.m4a"), "wb") as fh:
            fh.write(b"x" * 10)
        with open(os.path.join(self.root, "index.json"), "w") as fh:
            json.dump({"entries": {"id-none": {"bucket": "trash", "file": "trash/id-none.m4a"}}},
                      fh)
        self.assertIsNotNone(harvest.audio_for(self.URL_NONE))

    def test_a_no_audio_answer_holds_the_url_back_for_a_while(self):
        self.assertTrue(harvest.has_audio(self.URL_LOCAL))
        harvest.hold_no_audio(self.URL_LOCAL)
        self.assertFalse(harvest.has_audio(self.URL_LOCAL))
        harvest.hold_no_audio(self.URL_LOCAL, hold_s=-1)          # the hold has passed
        self.assertTrue(harvest.has_audio(self.URL_LOCAL))


class PickNext(Base):
    def _state(self, **hosts):
        return {"hosts": hosts}

    def test_a_cached_entry_is_picked_before_an_uncached_one_whose_host_is_ready(self):
        pending = [self.URL_NONE, self.URL_LOCAL]
        far = {"y": {"next_ok": 4102444800.0}}             # the cached entry's host is NOT ready
        idx = harvest.pick_next(pending, self._state(**far), has_audio=harvest.has_audio)
        self.assertEqual(pending[idx], self.URL_LOCAL,
                         "reading a cached file asks nothing of the host, so pacing is moot")

    def test_a_blocked_host_does_not_block_its_cached_entries(self):
        pending = [self.URL_LOCAL]
        idx = harvest.pick_next(pending, self._state(y={"blocked": True}),
                                has_audio=harvest.has_audio)
        self.assertEqual(idx, 0)

    def test_the_web_is_asked_only_when_nothing_pending_is_cached(self):
        pending = [self.URL_NONE, self.URL_STRANGER]
        idx = harvest.pick_next(pending, self._state(), has_audio=harvest.has_audio)
        self.assertIsNotNone(idx, "the fallback is on: today's selection over the rest")
        pending = [self.URL_NONE, self.URL_LOCAL]
        idx = harvest.pick_next(pending, self._state(), has_audio=harvest.has_audio)
        self.assertEqual(pending[idx], self.URL_LOCAL)

    def test_with_the_fallback_off_an_uncached_entry_waits(self):
        os.environ["NETRADIO_HARVEST_FETCH_FALLBACK"] = "0"
        self.assertIsNone(harvest.pick_next([self.URL_NONE, self.URL_STRANGER], self._state(),
                                            has_audio=harvest.has_audio))
        self.assertEqual(harvest.pick_next([self.URL_NONE, self.URL_LOCAL], self._state(),
                                           has_audio=harvest.has_audio), 1)

    def test_a_held_url_belongs_to_neither_pass(self):
        """The hold means "wait for the player's copy", not "fetch it from the web instead"."""
        harvest.hold_no_audio(self.URL_LOCAL)
        self.assertIsNone(harvest.pick_next([self.URL_LOCAL], self._state(),
                                            has_audio=harvest.has_audio),
                          "fallback on, the only pending URL held: nothing to pick")
        pending = [self.URL_LOCAL, self.URL_NONE]
        idx = harvest.pick_next(pending, self._state(), has_audio=harvest.has_audio)
        self.assertEqual(pending[idx], self.URL_NONE, "the fallback pass skips the held URL")
        harvest.hold_no_audio(self.URL_LOCAL, hold_s=-1)
        self.assertEqual(harvest.pick_next(pending, self._state(), has_audio=harvest.has_audio),
                         0, "and picks it again once the hold has passed")

    def test_without_the_predicate_the_old_selection_is_unchanged(self):
        pending = ["https://a/1", "https://b/2"]
        state = self._state(a={"next_ok": 4102444800.0})
        self.assertEqual(harvest.pick_next(pending, state), 1)


class TheParentDecides(Base):
    """`stream_chroma` writes where the audio is, and whether the web may be asked, into the job
    file; the child carries it out."""

    def _spawn(self, url):
        seen = {}

        def _popen(argv, **kwargs):
            job = argv[argv.index("--fetch-job") + 1]
            seen["spec"] = json.load(open(os.path.join(job, "url.json")))
            with open(os.path.join(job, "result.json"), "w") as fh:
                json.dump({"ok": False, "error": "nope"}, fh)
            proc = mock.Mock()
            proc.communicate.return_value = (b"", b"")
            proc.returncode = 0
            return proc

        with mock.patch.object(harvest.subprocess, "Popen", _popen):
            harvest.stream_chroma(url, harvest.queue_duration(url))
        return seen["spec"]

    def test_a_local_entry_travels_with_its_path_and_no_permission_to_fetch(self):
        spec = self._spawn(self.URL_LOCAL)
        self.assertEqual(spec["audio"], {"id": "id-local",
                                        "path": os.path.realpath(self.local_file)})
        self.assertFalse(spec["fetch"])

    def test_a_bucket_entry_travels_with_its_id_alone(self):
        spec = self._spawn(self.URL_BUCKET)
        self.assertEqual(spec["audio"], {"id": "id-bucket", "path": None})
        self.assertFalse(spec["fetch"])

    def test_an_uncached_entry_may_fetch_only_while_the_fallback_is_on(self):
        self.assertEqual(self._spawn(self.URL_NONE)["fetch"], True)
        self.assertIsNone(self._spawn(self.URL_NONE)["audio"])
        os.environ["NETRADIO_HARVEST_FETCH_FALLBACK"] = "0"
        self.assertEqual(self._spawn(self.URL_NONE)["fetch"], False)


class TheChild(Base):
    """`_decode_and_sign` on a cached file."""

    def setUp(self):
        Base.setUp(self)
        self.job = os.path.join(self.tmp, "job")
        self.popens = []
        self.saved = []

    def _decode(self, url, duration, audio, fetch, pcm_s=LONG_ENOUGH_S, probe=None, rc=0,
                enabled_remote=False):
        def _popen(argv, **kwargs):
            self.popens.append(argv)
            if os.path.basename(argv[0]) != "ffmpeg":
                raise AssertionError("a cached decode spawned %r" % argv[0])
            return _FakeFF(argv, _pcm(pcm_s), rc, kwargs["stdout"])

        real_save = np.save

        def _save(path, arr, *a, **k):
            self.saved.append(os.path.basename(str(path)))
            return real_save(path, arr, *a, **k)

        with mock.patch.object(harvest.subprocess, "Popen", _popen), \
                mock.patch.object(harvest, "_probe_seconds", lambda p: probe), \
                mock.patch.object(harvest.np, "save", _save), \
                mock.patch.object(harvest.sigstore, "enabled", lambda: enabled_remote), \
                mock.patch.object(harvest.chroma_recipe, "compute_chroma",
                                  lambda y, sr=None: np.zeros((12, 4), dtype="float32")):
            return harvest._fetch_and_sign(url, self.job, duration, audio, fetch)

    def _ff_argv(self):
        self.assertEqual(len(self.popens), 1, self.popens)
        return self.popens[0]

    def test_a_local_file_is_decoded_in_place_with_no_cut_and_no_copy(self):
        audio = {"id": "id-local", "path": self.local_file}
        result = self._decode(self.URL_LOCAL + "#t=0,60", 300, audio, False, pcm_s=60,
                              probe=60.0)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["source"], "file")
        argv = self._ff_argv()
        self.assertEqual(argv[argv.index("-i") + 1], self.local_file)
        self.assertNotIn("-ss", argv)
        self.assertNotIn("-t", argv)
        self.assertEqual([c for c in self.rec.calls if "cp" in c], [],
                         "a file the player holds is opened where it is, never copied")
        self.assertIn(os.path.basename(harvest.sig_path(self.URL_LOCAL + "#t=0,60")), self.saved)
        self._root_untouched()

    def test_a_file_that_moved_since_the_parent_looked_is_found_again(self):
        """The player moves a file from unplayed/ to keep/ when it is loved. The parent's path
        is a snapshot; the child asks the index again before giving up."""
        moved = os.path.join(self.root, "keep", "id-local.m4a")
        os.makedirs(os.path.dirname(moved))
        os.replace(self.local_file, moved)
        with open(os.path.join(self.root, "index.json"), "w") as fh:
            json.dump({"entries": {"id-local": {"bucket": "keep",
                                                "file": "keep/id-local.m4a"}}}, fh)
        audio = {"id": "id-local", "path": self.local_file}          # the stale snapshot
        result = self._decode(self.URL_LOCAL, 60, audio, False, pcm_s=60)
        self.assertTrue(result["ok"], result)
        self.assertEqual(self._ff_argv()[self._ff_argv().index("-i") + 1],
                         os.path.realpath(moved))

    def test_a_bucket_entry_is_copied_into_the_job_and_decoded_from_there(self):
        def _cp(cmd):
            with open(cmd[-2], "wb") as fh:
                fh.write(b"opus")
            return FakeProc()
        self.rec.results = [FakeProc(stdout=json.dumps(
            {"Contents": [{"Key": "audio/id-bucket.opus", "Size": 4}]})), _cp]
        audio = {"id": "id-bucket", "path": None}
        result = self._decode(self.URL_BUCKET, 7200, audio, False, pcm_s=600, probe=600.0)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["source"], "bucket")
        src = self._ff_argv()[self._ff_argv().index("-i") + 1]
        self.assertEqual(src, os.path.join(self.job, "audio.opus"))
        self._root_untouched()

    def test_a_chunk_whose_file_misses_its_span_is_refused_before_the_decode(self):
        audio = {"id": "id-local", "path": self.local_file}
        result = self._decode(self.URL_LOCAL + "#t=0,600", 7200, audio, False, probe=650.0)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "span_mismatch")
        self.assertEqual(self.popens, [], "refused on the container's length: nothing decoded")
        self.assertEqual(self.saved, [])

    def test_a_chunk_whose_decode_misses_its_span_is_refused_after_it(self):
        """No ffprobe (None): the decoded length is the check of record, at the player's own
        two-second tolerance -- not the 2 %/10 s a downloaded stream is allowed."""
        audio = {"id": "id-local", "path": self.local_file}
        result = self._decode(self.URL_LOCAL + "#t=0,600", 7200, audio, False, pcm_s=596,
                              probe=None)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "span_mismatch")
        self.assertEqual(self.saved, [])
        self.assertFalse(os.path.exists(os.path.join(self.job, "pcm.f32le.part")))
        ok = self._decode(self.URL_LOCAL + "#t=0,600", 7200, audio, False, pcm_s=599, probe=None)
        self.assertTrue(ok["ok"], "one second inside the tolerance is a good hand-over")

    def test_a_whole_entry_is_held_to_its_declared_length(self):
        audio = {"id": "id-local", "path": self.local_file}
        result = self._decode(self.URL_LOCAL, 300, audio, False, pcm_s=200)
        self.assertFalse(result["ok"])
        self.assertIn("length mismatch", result["error"])
        self.assertNotIn("reason", result)

    def test_audio_expected_but_gone_is_no_audio_not_a_failure(self):
        os.unlink(self.local_file)
        os.environ.pop("NETRADIO_AUDIO_BUCKET")            # and no bucket to fall back on
        audio = {"id": "id-local", "path": self.local_file}
        result = self._decode(self.URL_LOCAL, 300, audio, True)
        self.assertFalse(result["ok"])
        self.assertTrue(result["no_audio"])
        self.assertEqual(self.popens, [], "no decode, and above all no yt-dlp: `fetch` is not "
                                          "a licence to go around a cache that has moved on")

    def test_no_cached_audio_and_no_fallback_is_no_audio(self):
        result = self._decode(self.URL_NONE, 200, None, False)
        self.assertFalse(result["ok"])
        self.assertTrue(result["no_audio"])
        self.assertIn("fallback is off", result["error"])
        self.assertEqual(self.popens, [])

    def test_no_cached_audio_with_the_fallback_on_fetches_as_before(self):
        spawned = []

        def _popen(argv, **kwargs):
            spawned.append(os.path.basename(argv[0]))
            proc = _FakeFF(argv, _pcm(200) if "ffmpeg" in argv[0] else b"", 0,
                           kwargs.get("stdout") or io.BytesIO())
            return proc

        with mock.patch.object(harvest.subprocess, "Popen", _popen), \
                mock.patch.object(harvest.np, "save", lambda *a, **k: None), \
                mock.patch.object(harvest.sigstore, "enabled", lambda: False), \
                mock.patch.object(harvest.chroma_recipe, "compute_chroma",
                                  lambda y, sr=None: np.zeros((12, 4), dtype="float32")):
            result = harvest._fetch_and_sign(self.URL_NONE, self.job, 200, None, True)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["source"], "fetch")
        self.assertEqual(spawned, ["yt-dlp", "ffmpeg"])

    def test_the_child_entry_point_reads_audio_and_fetch_from_the_job_file(self):
        os.makedirs(self.job)
        harvest._save(os.path.join(self.job, "url.json"),
                      {"url": self.URL_LOCAL, "duration": 300,
                       "audio": {"id": "id-local", "path": self.local_file}, "fetch": False})
        with mock.patch.object(sys, "argv", ["harvest.py", "--fetch-job", self.job]), \
                mock.patch.object(harvest, "_fetch_and_sign",
                                  return_value={"ok": False, "error": "x"}) as fetch:
            self.assertEqual(harvest.main(), 0)
        fetch.assert_called_once_with(self.URL_LOCAL, self.job, 300,
                                      {"id": "id-local", "path": self.local_file}, False)


class NoAudioIsNotAVerdict(Base):
    """`run()` and `harvester.work_once()` both leave a no-audio URL pending."""

    def setUp(self):
        Base.setUp(self)
        self.child_runs = 0
        self.naps = []

    def _no_audio(self, url, duration=None):
        self.child_runs += 1
        if self.child_runs > 25:                     # a bounded stub, never a hung test
            harvest._STOP["signum"] = signal.SIGTERM
            return None, None, harvest.STOPPED
        harvest._LAST_CHILD.clear()
        harvest._LAST_CHILD.update({"ok": False, "no_audio": True,
                                    "error": "no audio: entry id-none has no local file and no "
                                             "object in the bucket"})
        return None, None, harvest._LAST_CHILD["error"]

    def _stop_on_nap(self, seconds):
        self.naps.append(seconds)
        harvest._STOP["signum"] = signal.SIGTERM

    def _run(self, queue, fetch=None, cm_match=None, audio_for=None):
        """Run the loop over `queue`; the first nap (or the 26th child) stops it."""
        harvest._save(harvest.QUEUE, queue)
        qs = [(4, np.zeros((12, 8), dtype="float32"), "4:f00")]
        with mock.patch.object(harvest, "queries", lambda state=None: qs), \
                mock.patch.object(harvest, "sweep_excerpts", lambda: None), \
                mock.patch.object(harvest, "sweep_job_dirs", lambda *a, **k: 0), \
                mock.patch.object(harvest, "recover_missing_sigs_at_start", lambda *a: None), \
                mock.patch.object(harvest, "stamp_pool", lambda state: False), \
                mock.patch.object(harvest, "listen_queue_split", lambda issues=None: ([], [])), \
                mock.patch.object(harvest, "check_memory", lambda *a, **k: False), \
                mock.patch.object(harvest, "_load_sig", lambda url: None), \
                mock.patch.object(harvest, "audio_for", audio_for or harvest.audio_for), \
                mock.patch.object(harvest, "stream_chroma", fetch or self._no_audio), \
                mock.patch.object(harvest, "_nap", self._stop_on_nap), \
                mock.patch.object(harvest._cm, "match", cm_match or harvest._cm.match), \
                mock.patch.object(harvest.sigstore, "enabled", lambda: False), \
                mock.patch.object(harvest.selftest, "offline", lambda: {"why": "test"}), \
                mock.patch.object(harvest.selftest, "due_for_live", lambda: False), \
                mock.patch.object(harvest.memwatch, "allocator_canary",
                                  lambda *a, **k: (0, 0, None)):
            harvest.run(None)
        return harvest._load(harvest.QUEUE, {}), harvest._load(harvest.STATE, {})

    def _assert_left_pending(self, q, state):
        self.assertEqual(q["pending"], [self.URL_NONE])
        self.assertEqual(q["done"], [], "`done` is never re-fetched")
        self.assertEqual(q.get("retry_later", []), [])
        rows = [r for r in state["issues"] if r.get("url") == self.URL_NONE]
        self.assertEqual([r["reason"] for r in rows], ["no_audio"])
        self.assertEqual(state["errors"], 0, "nothing failed")
        self.assertIn(self.URL_NONE, harvest._NO_AUDIO)

    def test_run_leaves_the_url_pending_and_says_so_once(self):
        os.environ["NETRADIO_HARVEST_FETCH_FALLBACK"] = "0"
        # `has_audio` says yes once (the snapshot), the child says no; after the hold the loop
        # finds nothing pickable, naps, and the nap is where the test stops it.
        q, state = self._run({"pending": [self.URL_NONE], "done": []},
                             audio_for=lambda url: {"id": "id-none", "path": None})
        self._assert_left_pending(q, state)
        self.assertEqual(self.child_runs, 1)
        self.assertEqual(len(self.naps), 1)

    def test_with_the_fallback_on_a_held_url_is_not_fetched_from_the_web_instead(self):
        """The hold is "wait for the player's copy". With the fallback ON the second pass used to
        pick the held URL straight back, the pacing wait was skipped, the child was told
        `fetch=False`, said `no_audio` again, and the loop went round with no sleep: one child
        per iteration for as long as the hold lasted."""
        os.environ["NETRADIO_HARVEST_FETCH_FALLBACK"] = "1"
        q, state = self._run({"pending": [self.URL_NONE], "done": []},
                             audio_for=lambda url: {"id": "id-none", "path": None})
        self._assert_left_pending(q, state)
        self.assertEqual(self.child_runs, 1, "one child, then the hold; never a second")
        self.assertEqual(len(self.naps), 1, "nothing pickable -> the loop naps")

    def test_a_failed_web_fetch_still_paces_its_host(self):
        """A failure result carries no `source`; it must spend the host's turn all the same, or
        a run of dead links on one host is fetched back to back."""
        def _dead(url, duration=None):
            harvest._LAST_CHILD.clear()
            harvest._LAST_CHILD.update({"ok": False, "error": "too short (12s)"})
            return None, None, "too short (12s)"

        before = time.time()
        q, state = self._run({"pending": [self.URL_NONE], "done": []}, fetch=_dead)
        self.assertEqual(q["done"], [self.URL_NONE], "an ordinary failure is still a verdict")
        self.assertGreater(state["hosts"]["s"]["next_ok"], before)

    def test_a_cached_read_spends_no_host_turn(self):
        def _signed(url, duration=None):
            harvest._LAST_CHILD.clear()
            harvest._LAST_CHILD.update({"ok": True, "source": "file"})
            return np.zeros((12, 4), dtype="float32"), np.zeros(SR, dtype="float32"), None

        q, state = self._run({"pending": [self.URL_LOCAL], "done": []}, fetch=_signed,
                             cm_match=lambda qc, c: (1.0, 0, 0.0))
        self.assertEqual(q["done"], [self.URL_LOCAL])
        self.assertNotIn("next_ok", state["hosts"].get("y", {}),
                         "a file the player holds asks the host for nothing")


@unittest.skipIf(harvester is None, "harvester.py needs the librosa venv (.venv) -- skipping")
class TheSplitRuntime(Base):
    def setUp(self):
        Base.setUp(self)
        for attr in ("HSTATE", "JOBS", "RESULTS", "QUEUE"):
            p = mock.patch.object(harvester, attr, os.path.join(self.tmp, attr.lower()))
            p.start()
            self.addCleanup(p.stop)

    def _spooled(self):
        return sorted(os.listdir(harvester.RESULTS)) if os.path.isdir(harvester.RESULTS) else []

    def test_no_audio_submits_nothing_and_leaves_the_url_pending(self):
        def _no_audio(url, duration=None):
            harvest._LAST_CHILD.clear()
            harvest._LAST_CHILD.update({"ok": False, "no_audio": True, "error": "no audio: x"})
            return None, None, "no audio: x"

        q = {"pending": [self.URL_LOCAL], "done": []}
        hstate = harvester.blank_hstate()
        with mock.patch.object(harvest, "stream_chroma", _no_audio):
            self.assertEqual(harvester.work_once(hstate, q), "waiting")
        self.assertEqual(self._spooled(), [], "a spooled result is folded to `done` by the "
                                              "collector, which is never re-fetched")
        self.assertEqual(q["pending"], [self.URL_LOCAL])
        self.assertEqual(hstate["errors"], 0)
        self.assertFalse(harvest.has_audio(self.URL_LOCAL), "held back for a while")

    def test_a_cached_candidate_is_picked_and_spends_no_host_turn(self):
        def _signed(url, duration=None):
            harvest._LAST_CHILD.clear()
            harvest._LAST_CHILD.update({"ok": True, "source": "file"})
            os.makedirs(harvest.CACHE, exist_ok=True)
            np.save(harvest.sig_path(url), np.zeros((12, 4), dtype="float16"))
            return np.zeros((12, 4), dtype="float32"), np.zeros(SR, dtype="float32"), None

        q = {"pending": [self.URL_NONE, self.URL_LOCAL], "done": []}
        hstate = harvester.blank_hstate()
        hstate["hosts"]["y"] = {"next_ok": 4102444800.0}            # the cached host: not ready
        with mock.patch.object(harvest, "stream_chroma", _signed), \
                mock.patch.object(harvester, "already_held", lambda u: False), \
                mock.patch.object(harvester.sf, "write",
                                  lambda path, *a, **k: open(path, "wb").close()):
            self.assertEqual(harvester.work_once(hstate, q), "fetched")
        self.assertEqual(hstate["hosts"]["y"]["next_ok"], 4102444800.0,
                         "a cache read neither waited for the host nor spent its turn")
        self.assertEqual(len(self._spooled()), 1)
        self.assertEqual(json.load(open(os.path.join(harvester.RESULTS, self._spooled()[0])))["url"],
                         self.URL_LOCAL)
        self._root_untouched()

    def test_a_held_url_is_not_fetched_from_the_web_by_the_split_runtime_either(self):
        harvest.hold_no_audio(self.URL_LOCAL)
        q = {"pending": [self.URL_LOCAL], "done": []}
        hstate = harvester.blank_hstate()
        with mock.patch.object(harvest, "stream_chroma",
                               lambda *a: self.fail("fetched a held URL")):
            self.assertEqual(harvester.work_once(hstate, q), "idle")

    def test_with_the_fallback_off_and_nothing_cached_it_idles(self):
        os.environ["NETRADIO_HARVEST_FETCH_FALLBACK"] = "0"
        q = {"pending": [self.URL_NONE], "done": []}
        hstate = harvester.blank_hstate()
        with mock.patch.object(harvest, "stream_chroma",
                               lambda *a: self.fail("fetched with the fallback off")):
            self.assertEqual(harvester.work_once(hstate, q), "idle")


if __name__ == "__main__":
    unittest.main()
