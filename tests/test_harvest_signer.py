"""The signer: directories, sidecars, the ledger, sign_file.

Nothing here touches the network and nothing decodes real audio: `ffmpeg` is a fake that
writes prepared PCM into the spool it is handed, `compute_chroma` is stubbed, and the two
things worth being careful about are pinned:

  * **The row is the verdict.** A file that decodes badly, or whose own sidecar disagrees
    with its length, or that the cache policy has no room for, is `delayed` with a reason --
    and a feeder reads that reason to decide what to do with the file. The three things that
    must write NO row are pinned just as hard: a stop, a file that vanished mid-sign, and a
    sidecar that went between the scan and the sign.
  * **The directories are someone else's.** The harvester reads their top level and writes
    nothing there: no deletion, no rename, no move. A test feeds it a directory and demands
    every file back afterwards.
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
import time
import unittest
from unittest import mock

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)

import numpy as np                      # noqa: E402

try:
    import harvest                      # noqa: E402
except Exception:                       # a dependency this test does not own
    harvest = None

import cache_budget                     # noqa: E402  (the machine's one cache policy)
import sigstore                         # noqa: E402  (the seam every bucket test fakes)

SR = 16000
LONG_ENOUGH = int(60 * SR)              # comfortably over chroma_recipe.MIN_SECONDS

# The cache-policy names the landing tests save and restore (the same set
# tests/test_cache_budget.py uses).
CACHE_ENV = ("NETRADIO_CACHE_ROOT", "NETRADIO_DOWNLOAD_ROOT", "NETRADIO_DISK_MAX_PCT",
             "NETRADIO_CACHE_EVENTS_DAYS")


def _pcm(n_samples):
    """Decoded PCM as ffmpeg would write it: mono float32 little-endian."""
    return (np.arange(n_samples, dtype="float32") % 7.0 - 3.0).tobytes()


def _key(url):
    return "u" + __import__("hashlib").sha1(url.encode()).hexdigest()[:20]


class _FakeProc:
    """Just enough of `Popen` for the decode path: exit code and stderr."""

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
    """A `subprocess.Popen` stand-in for the one ffmpeg the signer spawns.

    The fake writes `pcm` straight into the spool file it is handed as `stdout`, which is
    exactly what the real one does. `vanish` unlinks that path while "decoding", so a test
    can take the file away mid-sign.
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


def fake_slow_decode(chunks, order=None, rc=0):
    """An ffmpeg that takes several polls to finish, so a stop can land mid-decode."""
    made = {}

    class _Slow(_FakeProc):
        def __init__(self, argv, spool):
            _FakeProc.__init__(self, argv, returncode=rc, alive=True, order=order)
            self.spool, self.left = spool, list(chunks)

        def wait(self, timeout=None):
            if self.left and timeout is not None:
                self.spool.write(self.left.pop(0))
                self.spool.flush()
                raise subprocess.TimeoutExpired(self.argv, timeout)
            self.alive = False
            return self.returncode

        def terminate(self):
            self.left = []
            _FakeProc.terminate(self)

    def _popen(argv, **kwargs):
        made["ff"] = _Slow(argv, kwargs["stdout"])
        return made["ff"]

    _popen.made = made
    return _popen


class _SignerCase(unittest.TestCase):
    """A lit cache policy, a throwaway ledger and one directory of audio."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="signer-")
        self.audio = os.path.join(self.tmp, "audio")
        os.makedirs(self.audio)
        self.root = os.path.join(self.tmp, "root")      # the policy's root, OUTSIDE the audio
        self._paths = (harvest.LEDGER, harvest.STATE, harvest.JOBS, harvest.WRITER_LOCK,
                       harvest.RULINGS, harvest.STATE_DIR, harvest.HARVEST_DIRS)
        harvest.STATE_DIR = os.path.join(self.tmp, ".harvest")
        harvest.LEDGER = os.path.join(self.tmp, ".harvest", "ledger.json")
        harvest.STATE = os.path.join(self.tmp, ".harvest", "state.json")
        harvest.JOBS = os.path.join(self.tmp, ".harvest", "tmp")
        harvest.WRITER_LOCK = os.path.join(self.tmp, ".harvest", "writer.lock")
        harvest.RULINGS = os.path.join(self.tmp, ".harvest", "rulings.json")
        harvest.HARVEST_DIRS = self.audio
        self._env = {k: os.environ.get(k) for k in list(os.environ) if k.startswith("NETRADIO_")}
        for k in self._env:
            os.environ.pop(k, None)
        os.environ["NETRADIO_CACHE_ROOT"] = self.root
        os.environ["NETRADIO_HARVEST_CHILD"] = "0"      # the decode runs in this process
        self._registry = dict(cache_budget._REGISTRY), dict(cache_budget._STATS)
        cache_budget._REGISTRY.clear()
        cache_budget._STATS.clear()
        harvest.register_caches()
        self.put = []
        self.chroma_dir = os.path.join(self.root, "chroma")
        self.addCleanup(self._restore)

    def _restore(self):
        (harvest.LEDGER, harvest.STATE, harvest.JOBS, harvest.WRITER_LOCK,
         harvest.RULINGS, harvest.STATE_DIR, harvest.HARVEST_DIRS) = self._paths
        cache_budget._REGISTRY.clear()
        cache_budget._REGISTRY.update(self._registry[0])
        cache_budget._STATS.clear()
        cache_budget._STATS.update(self._registry[1])
        for k in [k for k in list(os.environ) if k.startswith("NETRADIO_")]:
            os.environ.pop(k, None)
        os.environ.update(self._env)
        harvest._STOP.update({"signum": 0, "child": None, "procs": [], "part": None})
        harvest._REMOTE_OBJECTS.update({"at": 0.0, "objects": None})
        harvest._LAST_CHILD.clear()
        harvest._said_vanished.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- the world a feeder builds -----------------------------------------------------------

    def _feed(self, key, url="https://y/x", pcm=_pcm(LONG_ENOUGH), sidecar=None, name=None,
              mtime=None):
        """One audio file + its sidecar, the way the contract says a feeder writes them."""
        path = os.path.join(self.audio, name or (key + ".mp3"))
        with open(path, "wb") as fh:
            fh.write(pcm if isinstance(pcm, bytes) else b"")
        sc = {"key": key, "url": url, "title": "a set", "artist": "someone",
              "duration_s": len(pcm) / 4.0 / SR, "fed_at": "2026-09-19T00:00:00+00:00"}
        sc.update(sidecar or {})
        with open(os.path.join(self.audio, key + ".json"), "w") as fh:
            json.dump(sc, fh)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def _store_on(self, etag="etag-abc"):
        p = mock.patch.object(harvest.sigstore, "enabled", lambda: True), \
            mock.patch.object(harvest.sigstore, "put",
                              lambda path, key: self.put.append(
                                  (os.path.basename(path), key)) or etag)
        for patch in p:
            patch.start()
            self.addCleanup(patch.stop)

    def _decode_patches(self, popen, chroma=None):
        chroma = chroma if chroma is not None else np.zeros((12, 8), dtype="float32")
        return [mock.patch.object(harvest.subprocess, "Popen", popen),
                mock.patch.object(harvest.chroma_recipe, "compute_chroma",
                                  lambda y, sr=None: chroma)]

    def _run_patches(self, popen, chroma=None):
        patches = self._decode_patches(popen, chroma)
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)


@unittest.skipUnless(harvest, "harvest.py needs numpy -- not this test's job")
class SignFileWritesTheRow(_SignerCase):
    """`sign_file` on a file: decode, upload, and the row -- or the delayed reason."""

    def test_a_file_is_signed_and_the_row_names_every_field(self):
        key = _key("https://y/one")
        path = self._feed(key, url="https://y/one#t=0,60")
        self._store_on()
        self._run_patches(fake_decode(pcm=_pcm(LONG_ENOUGH)))
        c, samples = harvest.sign_file(path)

        self.assertIsNotNone(c)                       # the chroma, for scoring
        self.assertEqual(len(samples), LONG_ENOUGH)  # the memmap, for an excerpt
        row = harvest._load(harvest.LEDGER, {})[key]
        self.assertEqual(row["key"], key)
        self.assertEqual(row["status"], "signed")
        self.assertIsNone(row["reason"])
        self.assertEqual(row["size"], os.path.getsize(path))
        self.assertEqual(row["mtime"], os.stat(path).st_mtime)
        self.assertEqual(row["uploaded_etag"], "etag-abc")
        self.assertIn("signed_at", row)
        # the sidecar's fields are carried, never read for meaning
        self.assertEqual(row["url"], "https://y/one#t=0,60")
        self.assertEqual((row["title"], row["artist"]), ("a set", "someone"))
        self.assertEqual(row["duration_s"], 60.0)
        # the signature landed in the working cache, under the key
        sig = os.path.join(self.chroma_dir, key + ".npy")
        self.assertTrue(os.path.isfile(sig))
        self.assertTrue(np.array_equal(np.load(sig),
                                       np.zeros((12, 8), dtype="float32").astype("float16")))
        # the uploads: the signature, and the sidecar BESIDE it
        self.assertEqual(self.put, [(key + ".npy", key + ".npy"), (key + ".json", key + ".json")])

    def test_the_length_mismatch_is_a_delayed_verdict(self):
        key = _key("https://y/mismatch")
        path = self._feed(key, sidecar={"duration_s": 3600})
        self._store_on()
        self._run_patches(fake_decode(pcm=_pcm(LONG_ENOUGH)))   # 60s decoded, 3600 declared
        c, samples = harvest.sign_file(path)
        self.assertEqual((c, samples), (None, None))
        row = harvest._load(harvest.LEDGER, {})[key]
        self.assertEqual((row["status"], row["reason"]), ("delayed", "length_mismatch"))
        self.assertIsNone(row["uploaded_etag"])
        self.assertEqual(self.put, [], "nothing is uploaded for a refused file")
        self.assertFalse(os.path.exists(os.path.join(self.chroma_dir, key + ".npy")))

    def test_a_length_within_tolerance_is_signed(self):
        key = _key("https://y/close")
        path = self._feed(key)                     # duration_s matches the bytes
        self._store_on()
        self._run_patches(fake_decode(pcm=_pcm(LONG_ENOUGH - SR)))   # one second short of 60
        c, _ = harvest.sign_file(path)
        self.assertIsNotNone(c)
        self.assertEqual(harvest._load(harvest.LEDGER, {})[key]["status"], "signed")

    def test_a_duration_that_is_not_a_length_makes_no_claim(self):
        """The sidecar's `duration_s` is unbounded JSON: a bool (which `isinstance(x, int)`
        accepts), a string, a zero, a negative -- none of them is evidence of length, and
        taken literally `0` is how a source with no length to declare reads, which would
        refuse every file over ten seconds. The sign validates the claim it weighs: a value
        that is not a length is no claim at all, and no claim is never a mismatch."""
        self._store_on()
        self._run_patches(fake_decode(pcm=_pcm(LONG_ENOUGH)))
        for bad in (True, "an hour", 0, -30):
            with self.subTest(bad=bad):
                key = _key("https://y/bad-duration-%r" % (bad,))
                path = self._feed(key, sidecar={"duration_s": bad})
                c, _samples = harvest.sign_file(path)
                self.assertIsNotNone(c)
                self.assertEqual(harvest._load(harvest.LEDGER, {})[key]["status"], "signed")

    def test_the_four_hour_cap_is_refused_before_anything_is_spawned(self):
        """A declared length over the cap is refused on the sidecar's own claim: four hours of
        decode is four hours of CPU paid for a refusal, and the claim was in hand first."""
        key = _key("https://y/master")
        path = self._feed(key, sidecar={"duration_s": 5 * 3600})
        spawned = []
        self._run_patches(lambda argv, **kw: spawned.append(argv) or _FakeProc(argv))
        c, samples = harvest.sign_file(path)
        self.assertEqual((c, samples), (None, None))
        self.assertEqual(spawned, [], "ffmpeg never ran")
        row = harvest._load(harvest.LEDGER, {})[key]
        self.assertEqual((row["status"], row["reason"]), ("delayed", "too_long"))

    def test_an_undeclared_four_hour_file_is_refused_on_its_decode(self):
        """No `duration_s`, no claim -- but the decode's own length still answers the backstop,
        and the signature is the thing that must never exist."""
        key = _key("https://y/undeclared")
        path = self._feed(key, sidecar={"duration_s": None})
        self._store_on()
        self._run_patches(fake_decode(pcm=_pcm((4 * 3600 + 60) * SR)))
        c, samples = harvest.sign_file(path)
        self.assertEqual((c, samples), (None, None))
        row = harvest._load(harvest.LEDGER, {})[key]
        self.assertEqual((row["status"], row["reason"]), ("delayed", "too_long"))
        self.assertEqual(self.put, [])
        self.assertFalse(os.path.exists(os.path.join(self.chroma_dir, key + ".npy")))

    def test_an_undeclared_over_long_file_is_stopped_at_the_cap_not_decoded_whole(self):
        """The spool's own backstop: a sidecar that declares no length closes the first door,
        so the decode in full is the only thing that can answer the four-hour backstop -- and
        before the bound, a 20-hour file spooled every hour of itself (4.6 GB) before the
        refusal. Once the spool passes the cap the verdict is made, so ffmpeg is stopped and
        the measured-mark refusal speaks it: nothing is truncated, and a file with no claim
        costs the cap to refuse, not its whole length."""
        key = _key("https://y/spooling")
        path = self._feed(key, sidecar={"duration_s": None})
        order = []
        self._store_on()
        self._run_patches(fake_slow_decode([_pcm(2 * SR), _pcm(2 * SR)], order=order))
        with mock.patch.object(harvest, "MAX_DURATION_S", 3):   # a three-second cap, for the test
            c, samples = harvest.sign_file(path)
        self.assertEqual((c, samples), (None, None))
        self.assertIn("ffmpeg", order, "the decode was stopped once the spool passed the cap")
        row = harvest._load(harvest.LEDGER, {})[key]
        self.assertEqual((row["status"], row["reason"]), ("delayed", "too_long"))
        self.assertIn("too long", harvest._LAST_CHILD["error"])
        self.assertFalse(os.path.exists(harvest.JOBS) and os.listdir(harvest.JOBS),
                         "the spool was swept with the job")

    def test_a_failed_decode_is_a_verdict_not_a_crash(self):
        key = _key("https://y/corrupt")
        path = self._feed(key)
        self._store_on()
        self._run_patches(fake_decode(rc=1, stderr=b"pipe:0: Invalid data found\n"))
        c, samples = harvest.sign_file(path)
        self.assertEqual((c, samples), (None, None))
        row = harvest._load(harvest.LEDGER, {})[key]
        self.assertEqual((row["status"], row["reason"]), ("delayed", "decode_failed"))

    def test_no_room_for_the_signature_is_a_delayed_no_space(self):
        key = _key("https://y/full-disk")
        path = self._feed(key)
        self._store_on()
        self._run_patches(fake_decode(pcm=_pcm(LONG_ENOUGH)))
        with mock.patch.object(harvest.cache_budget, "reserve", lambda *a, **k: False):
            c, samples = harvest.sign_file(path)
        self.assertEqual((c, samples), (None, None))
        row = harvest._load(harvest.LEDGER, {})[key]
        self.assertEqual((row["status"], row["reason"]), ("delayed", "no_space"))

    def test_a_failed_upload_writes_no_row_and_is_retried(self):
        """A `signed` row is the promise that both objects are in the bucket, and the scan
        treats the row as covering the file. A half-landed sign recorded as signed would
        never be retried -- the pool would hold a signature with no sidecar beside it for
        good, or a sidecar beside no signature. No row: the file is still wanted, and the
        next pass signs it again."""
        key = _key("https://y/sidecar-blip")
        path = self._feed(key)
        issues = []
        self._run_patches(fake_decode(pcm=_pcm(LONG_ENOUGH)))
        # the signature upload lands, the sidecar's fails
        self._store_on()
        def _flaky_sidecar(p, k):
            self.put.append((os.path.basename(p), k))
            return "etag-abc" if k.endswith(".npy") else None
        with mock.patch.object(harvest.sigstore, "put", _flaky_sidecar):
            c, samples = harvest.sign_file(path, issues=issues)
        self.assertEqual((c, samples), (None, None))
        self.assertEqual(harvest._load(harvest.LEDGER, {}), {}, "no row was written")
        self.assertTrue(any("did not upload" in r["issue"] for r in issues))
        # the scan still wants the file -- the retry is the point
        todo, _covered = harvest.scan_directories(harvest._load(harvest.LEDGER, {}))
        self.assertEqual([r["key"] for r in todo], [key])
        # and the next sign, with both uploads landing, writes the row
        c, _samples = harvest.sign_file(path, issues=issues)
        self.assertIsNotNone(c)
        row = harvest._load(harvest.LEDGER, {})[key]
        self.assertEqual(row["status"], "signed")
        self.assertEqual(row["uploaded_etag"], "etag-abc")
        self.assertEqual(self.put[-1], (key + ".json", key + ".json"))

    def test_a_failed_signature_upload_writes_no_row_either(self):
        """The same completeness rule, the other half: the decode succeeded but the
        signature never reached the bucket -- recording it `signed` would cover the file
        forever with the pool holding nothing for it."""
        key = _key("https://y/sig-blip")
        path = self._feed(key)
        issues = []
        self._store_on()
        self._run_patches(fake_decode(pcm=_pcm(LONG_ENOUGH)))
        with mock.patch.object(harvest.sigstore, "put", lambda p, k: None):
            c, samples = harvest.sign_file(path, issues=issues)
        self.assertEqual((c, samples), (None, None))
        self.assertEqual(harvest._load(harvest.LEDGER, {}), {})
        self.assertTrue(any("the signature did not upload" in r["issue"] for r in issues))
        todo, _covered = harvest.scan_directories({})
        self.assertEqual([r["key"] for r in todo], [key])

    def test_a_dark_store_signs_locally_with_no_etag(self):
        """No bucket configured, no uploads promised: the row is `signed` with no etag, and
        the feeder's rule for that shape (feed the key again) is the contract's, not this
        side's -- nothing feeds against a dark store."""
        key = _key("https://y/local-only")
        path = self._feed(key)
        self._run_patches(fake_decode(pcm=_pcm(LONG_ENOUGH)))
        with mock.patch.object(harvest.sigstore, "enabled", lambda: False):
            c, samples = harvest.sign_file(path)
        self.assertIsNotNone(c)
        row = harvest._load(harvest.LEDGER, {})[key]
        self.assertEqual((row["status"], row["uploaded_etag"]), ("signed", None))
        self.assertEqual(self.put, [])

    def test_a_file_that_vanishes_mid_sign_gets_no_row(self):
        """The cache policy, not the harvester, owns the directories' space; a file it took
        away mid-sign goes back on the feeder's list, and a `decode_failed` row would be a
        final verdict on bytes nobody can re-feed."""
        key = _key("https://y/evicted")
        path = self._feed(key)
        issues = []
        self._run_patches(fake_decode(vanish=path, rc=1))
        c, samples = harvest.sign_file(path, issues=issues)
        self.assertEqual((c, samples), (None, None))
        self.assertEqual(harvest._load(harvest.LEDGER, {}), {}, "no row was written")
        self.assertEqual(self.put, [])
        self.assertTrue(any("left while it was being signed" in r["issue"] for r in issues))

    def test_a_vanished_files_issue_is_news_once_per_file(self):
        key = _key("https://y/evicted-twice")
        path = self._feed(key)
        issues = []
        for _ in range(2):
            self._feed(key, name=key + ".mp3")      # the feeder puts it back...
            self._run_patches(fake_decode(vanish=os.path.join(self.audio, key + ".mp3"), rc=1))
            harvest.sign_file(os.path.join(self.audio, key + ".mp3"), issues=issues)
        self.assertEqual(len([r for r in issues if "left while" in r["issue"]]), 1)

    def test_a_stop_is_never_a_verdict(self):
        key = _key("https://y/stopped")
        path = self._feed(key)
        self._run_patches(fake_slow_decode([_pcm(SR), _pcm(SR)]))
        harvest._STOP["signum"] = signal.SIGTERM     # the flag, as the handler raises it
        try:
            c, samples = harvest.sign_file(path)
        finally:
            harvest._STOP["signum"] = 0
        self.assertEqual((c, samples), (None, None))
        self.assertEqual(harvest._load(harvest.LEDGER, {}), {}, "no row was written")

    def test_a_file_whose_sidecar_goes_is_skipped_not_signed(self):
        key = _key("https://y/pulled")
        path = self._feed(key)
        os.unlink(os.path.join(self.audio, key + ".json"))
        spawned = []
        self._run_patches(lambda argv, **kw: spawned.append(argv) or _FakeProc(argv))
        c, samples = harvest.sign_file(path)
        self.assertEqual((c, samples), (None, None))
        self.assertEqual(spawned, [])
        self.assertEqual(harvest._load(harvest.LEDGER, {}), {})


@unittest.skipUnless(harvest, "harvest.py needs numpy -- not this test's job")
class TheScan(_SignerCase):
    """The top level, the completeness rule, and what a row already covers."""

    def test_a_file_with_no_sidecar_is_neither_read_nor_logged(self):
        key = _key("https://y/incomplete")
        with open(os.path.join(self.audio, key + ".mp3"), "wb") as fh:
            fh.write(_pcm(SR))
        todo, covered = harvest.scan_directories({})
        self.assertEqual(todo, [])
        self.assertEqual(covered, [])
        self.assertEqual(harvest._load(harvest.LEDGER, {}), {})

    def test_a_part_file_is_skipped_silently(self):
        """A `<key>.mp3.part` is a download still in progress -- routine feeder state, not a
        feeder bug. The stem-shape check would otherwise refuse it by name, once per run,
        for as long as the download takes."""
        key = _key("https://y/in-progress")
        with open(os.path.join(self.audio, key + ".mp3.part"), "wb") as fh:
            fh.write(_pcm(SR))
        with open(os.path.join(self.audio, key + ".json"), "w") as fh:
            json.dump({"key": key, "fed_at": "now"}, fh)
        issues = []
        todo, covered = harvest.scan_directories({}, issues=issues)
        self.assertEqual((todo, covered), ([], []))
        self.assertEqual(issues, [], "an unfinished download earns no refusal")

    def test_a_subdirectory_file_is_never_read(self):
        key = _key("https://y/buried")
        sub = os.path.join(self.audio, "scratch")
        os.makedirs(sub)
        with open(os.path.join(sub, key + ".mp3"), "wb") as fh:
            fh.write(_pcm(SR))
        with open(os.path.join(self.audio, key + ".json"), "w") as fh:
            json.dump({"key": key}, fh)
        todo, _ = harvest.scan_directories({})
        self.assertEqual(todo, [], "a sidecar beside the DIRECTORY is not a sidecar beside "
                                   "the file, and no subdirectory is ever read")

    def test_a_sidecar_whose_key_differs_from_the_stem_is_refused(self):
        key = _key("https://y/mislabelled")
        self._feed(key, sidecar={"key": _key("https://y/someone-else")})
        issues = []
        todo, _ = harvest.scan_directories({}, issues=issues)
        self.assertEqual(todo, [])
        self.assertTrue(any("differs from" in r["issue"] for r in issues))
        self.assertEqual(harvest._load(harvest.LEDGER, {}), {})

    def test_a_sidecar_without_fed_at_is_refused(self):
        """`fed_at` is the one field the contract requires of every sidecar -- the record of
        when the hand-over was made. A feeder that forgot it must hear about the refusal,
        not watch its file sit unsigned with no row and no reason."""
        key = _key("https://y/no-fed-at")
        self._feed(key, sidecar={"fed_at": None})
        issues = []
        todo, _ = harvest.scan_directories({}, issues=issues)
        self.assertEqual(todo, [])
        self.assertTrue(any("fed_at" in r["issue"] for r in issues))
        self.assertEqual(harvest._load(harvest.LEDGER, {}), {})

    def test_the_hand_tool_refuses_the_same_sidecar(self):
        """--sign-one goes through sign_file's own check, so the hand tool cannot sign past
        the rule the scan enforces -- and its refusal is VISIBLE: the hand tool must not
        tell the operator to consult an issues list it never wrote to."""
        key = _key("https://y/hand-fed-at")
        path = self._feed(key, sidecar={"fed_at": None})
        issues = []
        spawned = []
        self._run_patches(lambda argv, **kw: spawned.append(argv) or _FakeProc(argv))
        c, samples = harvest.sign_file(path, issues=issues)
        self.assertEqual((c, samples), (None, None))
        self.assertEqual(spawned, [])
        self.assertEqual(harvest._load(harvest.LEDGER, {}), {})
        self.assertTrue(any("no fed_at" in r["issue"] for r in issues),
                        "the refusal lands in the issues list, like the scan's")

    def test_find_file_wants_a_complete_sidecar(self):
        """The hand tool's finder asks the same of a sidecar as everything else: a key that
        matches and the one required field. A file that fails either is not a candidate for
        --sign-one, and the scan's refusal (not a silent skip) is what names it."""
        key = _key("https://y/findable")
        path = self._feed(key)
        self.assertEqual(harvest.find_file(key), path)
        os.unlink(os.path.join(self.audio, key + ".json"))
        self._feed(key, sidecar={"fed_at": None})
        self.assertIsNone(harvest.find_file(key))

    def test_a_relative_directory_is_refused_visibly(self):
        """The contract's paths are absolute. A relative entry would resolve against
        whatever directory the process started from -- a hand-off that signs a different
        directory than the configuration names -- so it is refused with an issue row, not
        silently resolved."""
        harvest.HARVEST_DIRS = "not-absolute"
        issues = []
        todo, covered = harvest.scan_directories({}, issues=issues)
        self.assertEqual((todo, covered), ([], []))
        self.assertTrue(any("not an absolute path" in r["issue"] for r in issues))

    def test_a_stem_that_is_not_a_keys_shape_is_refused(self):
        """The pool's own listing admits only `u` + 20 hex, so a signature filed under any
        other stem would be invisible to the pool -- refused where the refusal can name it."""
        for name in ("notes.txt", "u123.mp3", "u" + "g" * 20 + ".mp3"):
            with self.subTest(name=name):
                with open(os.path.join(self.audio, name), "wb") as fh:
                    fh.write(_pcm(SR))
                stem = name.rsplit(".", 1)[0]
                with open(os.path.join(self.audio, stem + ".json"), "w") as fh:
                    json.dump({"key": stem, "fed_at": "now"}, fh)
        issues = []
        todo, _ = harvest.scan_directories({}, issues=issues)
        self.assertEqual(todo, [])
        self.assertEqual(len(issues), 3, "each bad stem is refused, and named")

    def test_a_missing_directory_is_skipped_with_an_issue_row(self):
        harvest.HARVEST_DIRS = os.path.join(self.audio, "not-there")
        issues = []
        todo, covered = harvest.scan_directories({}, issues=issues)
        self.assertEqual((todo, covered), ([], []))
        self.assertTrue(any("not there" in r["issue"] for r in issues))

    def test_a_file_with_no_row_is_wanted_oldest_first(self):
        old = self._feed(_key("https://y/old"), mtime=1000)
        new = self._feed(_key("https://y/new"), mtime=2000)
        todo, covered = harvest.scan_directories({})
        self.assertEqual([r["key"] for r in todo],
                         [_key("https://y/old"), _key("https://y/new")])
        self.assertEqual(covered, [])

    def test_one_key_in_two_directories_is_proposed_once_oldest_copy_first(self):
        """A row covers only one file's bytes, so two differing copies of one key can never
        both be satisfied; the scan proposes one candidate per key -- the oldest copy, so a
        pass's choice is deterministic -- and the feeder that leaves two copies must take
        one of them away."""
        other = os.path.join(self.tmp, "more-audio")
        os.makedirs(other)
        harvest.HARVEST_DIRS = self.audio + os.pathsep + other
        key = _key("https://y/twice")
        old = self._feed(key, mtime=1000)
        new = os.path.join(other, key + ".mp3")
        with open(new, "wb") as fh:
            fh.write(_pcm(SR))
        os.utime(new, (2000, 2000))
        with open(os.path.join(other, key + ".json"), "w") as fh:
            json.dump({"key": key, "fed_at": "now"}, fh)
        todo, covered = harvest.scan_directories({})
        self.assertEqual([r["key"] for r in todo], [key])
        self.assertEqual(todo[0]["path"], old)

    def test_a_row_that_covers_the_file_means_no_sign(self):
        key = _key("https://y/done")
        path = self._feed(key)
        row = harvest._row(key, os.path.getsize(path), os.stat(path).st_mtime,
                           "signed", None, "then", "etag", {})
        todo, covered = harvest.scan_directories({key: row})
        self.assertEqual(todo, [])
        self.assertEqual(covered, [key])

    def test_a_changed_file_is_signed_again_whatever_the_row_said(self):
        key = _key("https://y/re-cut")
        path = self._feed(key)
        st = os.stat(path)
        for status, reason in (("signed", None), ("delayed", "decode_failed"),
                               ("delayed", "too_long"), ("delayed", "length_mismatch")):
            with self.subTest(status=status, reason=reason):
                # the row's size/mtime are the OLD file's: the re-cut differs in both
                row = harvest._row(key, st.st_size - 1, st.st_mtime - 1,
                                   status, reason, None, "etag", {})
                todo, _ = harvest.scan_directories({key: row})
                self.assertEqual([r["key"] for r in todo], [key])

    def test_a_no_space_delay_is_wanted_again_unchanged(self):
        """The one delayed row that is retried in place: `no_space` means the machine was
        full, not that the file was judged."""
        key = _key("https://y/no-room")
        path = self._feed(key)
        st = os.stat(path)
        row = harvest._row(key, st.st_size, st.st_mtime, "delayed", "no_space",
                           None, None, {})
        todo, covered = harvest.scan_directories({key: row})
        self.assertEqual([r["key"] for r in todo], [key])
        self.assertEqual(covered, [])

    def test_only_the_top_level_of_each_directory_is_read(self):
        """`:`-separated directories, each its own top level."""
        other = os.path.join(self.tmp, "more-audio")
        os.makedirs(other)
        harvest.HARVEST_DIRS = self.audio + os.pathsep + other
        a = self._feed(_key("https://y/a"))
        key_b = _key("https://y/b")
        with open(os.path.join(other, key_b + ".mp3"), "wb") as fh:
            fh.write(_pcm(SR))
        with open(os.path.join(other, key_b + ".json"), "w") as fh:
            json.dump({"key": key_b, "fed_at": "now"}, fh)
        todo, _ = harvest.scan_directories({})
        self.assertEqual(sorted(r["key"] for r in todo),
                         sorted([_key("https://y/a"), key_b]))


@unittest.skipUnless(harvest, "harvest.py needs numpy -- not this test's job")
class TheLedger(_SignerCase):
    """Seeded from the bucket at the first start; reconciled against it on every start."""

    def _objects(self, *names, etag="e-%s"):
        return {name: etag % name[:6] for name in names}

    def test_the_first_start_seeds_one_signed_row_per_bucket_key(self):
        objects = self._objects("u" + "a" * 20 + ".npy", "u" + "b" * 20 + ".npy")
        with mock.patch.object(harvest, "_remote_objects", lambda max_age_s=900: objects):
            res = harvest.reconcile_ledger({"issues": []})
        self.assertEqual(res["seeded"], 2)
        rows = harvest._load(harvest.LEDGER, {})
        for key in ("u" + "a" * 20, "u" + "b" * 20):
            with self.subTest(key=key):
                row = rows[key]
                self.assertEqual((row["status"], row["key"]), ("signed", key))
                self.assertIsNone(row["size"], "the seed has no file to name")
                self.assertIsNone(row["mtime"])
                self.assertIsNone(row["signed_at"], "when it was signed is not known")
                self.assertEqual(row["uploaded_etag"], "e-%s" % key[:6],
                                  "the object is there: the seed's evidence is the listing")
                # and nothing the sidecar would have carried either: a seeded row has never
                # seen a sidecar, and the contract says so
                for field in ("url", "title", "artist", "duration_s"):
                    self.assertIsNone(row[field])

    def test_a_gone_object_loses_its_etag(self):
        key = "u" + "a" * 20
        gone = "u" + "b" * 20
        filler = [("u" + ("%02d" % i) * 10) for i in range(12)]
        rows = {k: harvest._row(k, 1, 1.0, "signed", None, "then", "e-%s" % k, {})
                for k in filler}
        rows[key] = harvest._row(key, 1, 1.0, "signed", None, "then", "keep", {})
        rows[gone] = harvest._row(gone, 1, 1.0, "signed", None, "then", "lost", {})
        harvest._save(harvest.LEDGER, rows)
        objects = {k + ".npy": "e-%s" % k[:6]
                   for k in list(rows) if k != gone}
        with mock.patch.object(harvest, "_remote_objects",
                               lambda max_age_s=900: objects):
            res = harvest.reconcile_ledger({"issues": []})
        self.assertEqual(res["dropped"], 1)
        saved = harvest._load(harvest.LEDGER, {})
        self.assertEqual(saved[key]["uploaded_etag"], "keep")
        self.assertIsNone(saved[gone]["uploaded_etag"])
        self.assertEqual(len(saved), len(rows), "the drop touched nothing else")

    def test_a_landed_upload_gains_its_etag_back(self):
        """A signed row with no etag whose object IS in the listing: the feeder's re-feed
        rule must not keep firing for a key the bucket already holds."""
        key = "u" + "a" * 20
        harvest._save(harvest.LEDGER,
                      {key: harvest._row(key, 1, 1.0, "signed", None, "then", None, {})})
        with mock.patch.object(harvest, "_remote_objects",
                               lambda max_age_s=900: self._objects(key + ".npy")):
            res = harvest.reconcile_ledger({"issues": []})
        self.assertEqual(res["restored"], 1)
        self.assertEqual(harvest._load(harvest.LEDGER, {})[key]["uploaded_etag"],
                         "e-%s" % key[:6])

    def test_an_unlistable_bucket_touches_nothing(self):
        """'Unknown' is never 'gone': with the listing dark, a bucket-held signature and a
        missing one are indistinguishable, and dropping etags on a guess would empty the
        feeder's list of every key it should not feed."""
        key = "u" + "a" * 20
        harvest._save(harvest.LEDGER,
                      {key: harvest._row(key, 1, 1.0, "signed", None, "then", "e", {})})
        with mock.patch.object(harvest, "_remote_objects", lambda max_age_s=900: None):
            res = harvest.reconcile_ledger({"issues": []})
        self.assertEqual((res["seeded"], res["dropped"], res["restored"]), (0, 0, 0))
        self.assertEqual(harvest._load(harvest.LEDGER, {})[key]["uploaded_etag"], "e")

    def test_a_mass_loss_reports_and_touches_nothing(self):
        keys = [("u" + ("%02d" % i) * 10) for i in range(10)]
        rows = {k: harvest._row(k, 1, 1.0, "signed", None, "then", "e-%s" % k, {})
                for k in keys}
        harvest._save(harvest.LEDGER, rows)
        state = {"issues": []}
        # the listing holds only one of the ten: the store broke, not the rows
        with mock.patch.object(harvest, "_remote_objects",
                               lambda max_age_s=900: self._objects(keys[0] + ".npy")):
            res = harvest.reconcile_ledger(state)
        self.assertTrue(res["reported"])
        self.assertEqual((res["seeded"], res["dropped"]), (0, 0))
        self.assertEqual(harvest._load(harvest.LEDGER, {}), rows,
                         "nothing was dropped over a loss that size")
        self.assertIn("sig_alert", state)
        self.assertTrue(any(r.get("issue", "").startswith("ledger:") for r in state["issues"]))

    def test_a_healed_store_stands_the_alert_down(self):
        """The alert is standing, not permanent: a start with a mis-listed bucket raises it,
        and the store that heals must bring it down -- or the page reports a broken store
        forever, and an operator re-fixes a configuration that is already fixed. Any reconcile
        that does not report clears it."""
        keys = [("u" + ("%02d" % i) * 10) for i in range(10)]
        rows = {k: harvest._row(k, 1, 1.0, "signed", None, "then", "e-%s" % k, {})
                for k in keys}
        harvest._save(harvest.LEDGER, rows)
        state = {"issues": []}
        # the listing holds only one of the ten: the store broke, not the rows
        with mock.patch.object(harvest, "_remote_objects",
                               lambda max_age_s=900: self._objects(keys[0] + ".npy")):
            first = harvest.reconcile_ledger(state)
        self.assertTrue(first["reported"])
        self.assertIn("sig_alert", state)
        # the configuration is fixed: the next start's listing holds every object again
        with mock.patch.object(harvest, "_remote_objects",
                               lambda max_age_s=900:
                               self._objects(*(k + ".npy" for k in keys))):
            second = harvest.reconcile_ledger(state)
        self.assertFalse(second["reported"])
        self.assertTrue(second["cleared"], "the loss is gone, and the alert went with it")
        self.assertNotIn("sig_alert", state)

    def test_the_cap_is_env_overridable_for_a_deliberate_drop(self):
        key = "u" + "a" * 20
        harvest._save(harvest.LEDGER,
                      {key: harvest._row(key, 1, 1.0, "signed", None, "then", "e", {})})
        with mock.patch.dict(os.environ, {"NETRADIO_RECONCILE_DROP_CAP": "1"}), \
                mock.patch.object(harvest, "_remote_objects", lambda max_age_s=900: {}):
            res = harvest.reconcile_ledger({"issues": []})
        self.assertEqual(res["dropped"], 1)
        self.assertIsNone(harvest._load(harvest.LEDGER, {})[key]["uploaded_etag"])

    def test_a_delayed_row_is_neither_seeded_nor_reconciled(self):
        """The reconciliation's business is the `signed` rows: a delayed row is a verdict on
        a file, not a claim about the bucket."""
        key = "u" + "a" * 20
        harvest._save(harvest.LEDGER,
                      {key: harvest._row(key, 1, 1.0, "delayed", "too_long", None, None, {})})
        with mock.patch.object(harvest, "_remote_objects", lambda max_age_s=900: {}):
            res = harvest.reconcile_ledger({"issues": []})
        self.assertEqual((res["dropped"], res["restored"]), (0, 0))
        self.assertEqual(harvest._load(harvest.LEDGER, {})[key]["status"], "delayed")


@unittest.skipUnless(harvest, "harvest.py needs numpy -- not this test's job")
class TheCanaryRescore(_SignerCase):
    """The canary is re-scored every pass from its STORED signature, by its key. A failure
    raises `sig_alert` (kind `canary`); a pass clears only a canary alert, never a store-loss
    alert the ledger's reconcile raised. Unconfigured, it reports "not configured" and touches
    no alert, and the run carries on."""

    def _canary_sig(self, key, chroma=None):
        """Drop the canary's signature into the working cache so _load_sig finds it locally."""
        os.makedirs(self.chroma_dir, exist_ok=True)
        np.save(os.path.join(self.chroma_dir, key + ".npy"),
                chroma if chroma is not None else np.zeros((12, 8), dtype="float32"))

    def _canary_on(self, key, ok=True, why="matched"):
        """Patch selftest.live to answer `ok` for the canary, recording the chroma it was handed."""
        handed = []

        def fake_live(c_canary, mystery_queries=None):
            handed.append(c_canary)
            return {"kind": "live", "ok": ok, "when": "now", "why": why,
                    "track": 3, "name": "Jamie Myerson - Sky Blue",
                    "cost": 0.004, "rival": 0.06, "semitones": 0, "at_s": 30.0,
                    "took_s": 0.0}
        patcher = mock.patch.object(harvest.selftest, "live", fake_live)
        patcher.start()
        self.addCleanup(patcher.stop)
        return handed

    def test_unconfigured_is_a_noop_and_no_alert(self):
        # Without NETRADIO_CANARY_KEY, the re-score reports "not configured" and touches
        # nothing -- the run carries on, as it does today when the searched hit is refused.
        os.environ.pop("NETRADIO_CANARY_KEY", None)
        state = {"issues": []}
        changed = harvest.score_canary(state, qs=[])
        self.assertFalse(changed)
        self.assertNotIn("sig_alert", state)
        self.assertEqual(state["issues"], [])

    def test_a_passing_canary_raises_no_alert(self):
        key = "u" + "a" * 20
        os.environ["NETRADIO_CANARY_KEY"] = key
        self._canary_sig(key)
        self._canary_on(key, ok=True)
        state = {"issues": []}
        self.assertFalse(harvest.score_canary(state, qs=[]))
        self.assertNotIn("sig_alert", state)

    def test_a_failing_canary_raises_a_canary_kind_alert(self):
        key = "u" + "b" * 20
        os.environ["NETRADIO_CANARY_KEY"] = key
        self._canary_sig(key)
        self._canary_on(key, ok=False, why="the matcher is broken")
        state = {"issues": []}
        self.assertTrue(harvest.score_canary(state, qs=[]))
        self.assertEqual(state["sig_alert"]["kind"], "canary")
        self.assertIn("self-test failed", state["sig_alert"]["why"])
        self.assertTrue(any("self-test failed" in r["issue"] for r in state["issues"]))

    def test_a_passing_canary_clears_only_a_canary_alert(self):
        """A pass stands the CANARY alert down (the matcher is working), and only that
        alert: a store-loss alert the ledger's reconcile raised is a different break, and a
        canary already in the local cache passing while the bucket is still reporting a mass
        loss must not hide it."""
        key = "u" + "c" * 20
        os.environ["NETRADIO_CANARY_KEY"] = key
        self._canary_sig(key)
        self._canary_on(key, ok=True)
        state = {"issues": [], "sig_alert": {"at": "then", "kind": "canary",
                                              "why": "self-test failed: earlier"}}
        self.assertTrue(harvest.score_canary(state, qs=[]))
        self.assertNotIn("sig_alert", state, "the canary alert was cleared by the pass")

    def test_a_passing_canary_does_not_clear_a_store_loss_alert(self):
        """THE REGRESSION: a mass bucket loss reported by the ledger's reconcile raises a
        store-kind alert, and a passing canary (an in-cache signature, no bucket listing
        needed) must not hide it. The store alert survives the canary pass and is cleared
        only by the reconcile that raised it."""
        key = "u" + "d" * 20
        os.environ["NETRADIO_CANARY_KEY"] = key
        self._canary_sig(key)
        self._canary_on(key, ok=True)
        store_alert = {"at": "then", "kind": "store", "missing": 9, "corpus": 10,
                       "why": "9 of 10 signed rows point at objects the listing does not hold"}
        state = {"issues": [], "sig_alert": dict(store_alert)}
        self.assertFalse(harvest.score_canary(state, qs=[]),
                         "the canary pass changed nothing while a store alert is standing")
        self.assertEqual(state["sig_alert"], store_alert,
                         "the store-loss alert survives the passing canary")

    def test_a_failing_canary_does_not_overwrite_a_store_loss_alert(self):
        """A canary failure and a store loss are two different breaks; the canary does not
        clobber the store alert with its own -- both stay visible, the store alert the
        reconcile raised and the canary alert the re-score raised."""
        key = "u" + "e" * 20
        os.environ["NETRADIO_CANARY_KEY"] = key
        self._canary_sig(key)
        self._canary_on(key, ok=False, why="the matcher is broken")
        store_alert = {"at": "then", "kind": "store", "missing": 9, "corpus": 10,
                       "why": "9 of 10 signed rows point at objects the listing does not hold"}
        state = {"issues": [], "sig_alert": dict(store_alert)}
        # The canary fails, but the standing store alert is not the canary's to touch: the
        # canary sees a non-canary alert and leaves it alone (it does not overwrite, and it
        # does not duplicate an issues row either -- the store alert is the news already).
        self.assertFalse(harvest.score_canary(state, qs=[]),
                         "the canary did not overwrite the store alert")
        self.assertEqual(state["sig_alert"], store_alert,
                         "the store-loss alert stands unchanged")

    def test_a_store_loss_alert_is_cleared_only_by_reconcile(self):
        """The store alert the reconcile raises is the one that clears it: a healed store
        stands it down, and a passing canary in between does not."""
        keys = [("u" + ("%02d" % i) * 10) for i in range(10)]
        rows = {k: harvest._row(k, 1, 1.0, "signed", None, "then", "e-%s" % k, {})
                for k in keys}
        harvest._save(harvest.LEDGER, rows)
        canary_key = "u" + "f" * 20
        os.environ["NETRADIO_CANARY_KEY"] = canary_key
        self._canary_sig(canary_key)
        self._canary_on(canary_key, ok=True)
        state = {"issues": []}
        # the listing holds only one of ten: the store broke, the reconcile raises a store alert
        with mock.patch.object(harvest, "_remote_objects",
                               lambda max_age_s=900: {keys[0] + ".npy": "e-1"}):
            first = harvest.reconcile_ledger(state)
        self.assertTrue(first["reported"])
        self.assertEqual(state["sig_alert"]["kind"], "store")
        # the canary passes (in-cache signature); the store alert must survive it
        self.assertFalse(harvest.score_canary(state, qs=[]))
        self.assertEqual(state["sig_alert"]["kind"], "store",
                         "the store alert survived the passing canary")
        # the store heals: the next reconcile clears the store alert
        with mock.patch.object(harvest, "_remote_objects",
                               lambda max_age_s=900:
                               {k + ".npy": "e-%s" % k for k in keys}):
            second = harvest.reconcile_ledger(state)
        self.assertTrue(second["cleared"], "the healed store cleared its own alert")
        self.assertNotIn("sig_alert", state)

    def test_a_legacy_kindless_store_alert_is_healed_by_reconcile(self):
        """THE UPGRADE REGRESSION: a store alert written before the `kind` field landed has no
        `kind` but carries the store-loss shape (`missing`/`corpus`). After this change, a
        healthy reconcile must still clear it -- an upgrade that left a pre-existing store
        alarm standing forever would report a healed store as broken until another loss
        overwrote it. The canary path must leave it alone (it is not a canary alert)."""
        keys = [("u" + ("%02d" % i) * 10) for i in range(10)]
        rows = {k: harvest._row(k, 1, 1.0, "signed", None, "then", "e-%s" % k, {})
                for k in keys}
        harvest._save(harvest.LEDGER, rows)
        canary_key = "u" + "0" * 20
        os.environ["NETRADIO_CANARY_KEY"] = canary_key
        self._canary_sig(canary_key)
        self._canary_on(canary_key, ok=True)
        # a legacy alert, as written by the base branch: no `kind`, store-loss shape
        legacy = {"at": "then", "missing": 9, "corpus": 10,
                  "why": "9 of 10 signed rows point at objects the listing does not hold"}
        state = {"issues": [], "sig_alert": dict(legacy)}
        # the canary passes; the legacy store alert is not a canary alert, so it survives
        self.assertFalse(harvest.score_canary(state, qs=[]),
                         "the passing canary touched nothing")
        self.assertEqual(state["sig_alert"], legacy,
                         "the legacy store alert survived the passing canary")
        # the store heals: the reconcile clears the legacy store alert (kind absent, store shape)
        with mock.patch.object(harvest, "_remote_objects",
                               lambda max_age_s=900:
                               {k + ".npy": "e-%s" % k for k in keys}):
            res = harvest.reconcile_ledger(state)
        self.assertTrue(res["cleared"], "the healed store cleared the legacy alert")
        self.assertNotIn("sig_alert", state)


@unittest.skipUnless(harvest, "harvest.py needs numpy -- not this test's job")
class MatchRowsCarryTheKey(_SignerCase):
    """A match row joins on the key, and the old rows move onto it at first start."""

    def _score(self, url):
        key = _key(url)
        chroma = np.zeros((12, 8), dtype="float32")
        os.makedirs(self.chroma_dir, exist_ok=True)
        np.save(os.path.join(self.chroma_dir, key + ".npy"), chroma)
        state = {"matches": [], "kept": 0, "scored": {}}
        with mock.patch.object(harvest._cm, "match", return_value=(0.01, 0, 12.0)):
            return harvest.score_cached(state, 4, chroma, "4:fp", key), state

    def test_a_new_match_row_carries_the_key_and_not_the_url(self):
        hit, state = self._score("https://y/a-match")
        self.assertEqual(hit["key"], _key("https://y/a-match"))
        self.assertNotIn("url", hit)
        self.assertEqual(state["scored"]["4:fp"], [hit["key"] + ".npy"],
                         "the scored pairings keep the pool's own file naming")

    def test_an_existing_row_is_updated_by_its_key(self):
        key = _key("https://y/known")
        state = {"matches": [{"mystery": 4, "key": key, "cost": 0.09, "at_s": None,
                              "verdict": "near"}], "kept": 0, "scored": {}}
        os.makedirs(self.chroma_dir, exist_ok=True)
        np.save(os.path.join(self.chroma_dir, key + ".npy"),
                np.zeros((12, 8), dtype="float32"))
        with mock.patch.object(harvest._cm, "match", return_value=(0.012, 1, 30.0)):
            hit = harvest.score_cached(state, 4, np.zeros((12, 8), dtype="float32"),
                                      "4:fp", key)
        self.assertIs(hit, state["matches"][0], "no duplicate row was added")
        self.assertEqual((hit["cost"], hit["at_s"]), (0.012, 30.0))

    def test_a_fixture_of_url_only_rows_gains_a_key_on_every_row(self):
        state = {"matches": [
            {"at": "x", "mystery": 4, "cost": 0.02, "at_s": 1.0, "verdict": "MATCH",
             "url": "https://y/one"},
            {"at": "x", "mystery": 6, "cost": 0.06, "at_s": 2.0, "verdict": "near",
             "url": "https://y/two"},
            {"at": "x", "mystery": 7, "cost": 0.05, "verdict": "near",
             "url": "https://y/three"},          # old row: no at_s
        ]}
        moved = harvest.migrate_matches(state)
        self.assertEqual(moved, 3)
        self.assertEqual([m["key"] for m in state["matches"]],
                         [_key(u) for u in ("https://y/one", "https://y/two",
                                            "https://y/three")])
        for m in state["matches"]:
            self.assertNotIn("url", m)
            self.assertNotIn("--", m["key"])
        # and a second run moves nothing
        self.assertEqual(harvest.migrate_matches(state), 0)

    def test_a_row_with_neither_field_is_left_alone(self):
        state = {"matches": [{"mystery": 4, "cost": 0.02}]}
        self.assertEqual(harvest.migrate_matches(state), 0)
        self.assertEqual(state["matches"], [{"mystery": 4, "cost": 0.02}])


@unittest.skipUnless(harvest, "harvest.py needs numpy -- not this test's job")
class SignOneIsTheHandTool(_SignerCase):
    """`--sign-one <key>`: the lock, the reconcile, and the same path the loop uses."""

    def test_it_takes_the_lock_and_signs_through_the_same_path(self):
        key = _key("https://y/hand")
        path = self._feed(key)
        self._store_on()
        self._run_patches(fake_decode(pcm=_pcm(LONG_ENOUGH)))
        calls = []
        with mock.patch.object(harvest, "reconcile_ledger",
                               lambda state=None: calls.append("reconcile") or
                               {"seeded": 0, "dropped": 0, "restored": 0, "reported": False,
                                "cleared": False, "why": ""}), \
                mock.patch.object(harvest, "sign_file",
                                  side_effect=lambda p, issues=None:
                                      calls.append(p) or (None, None)) as sign:
            argv = ["harvest.py", "--sign-one", key]
            with mock.patch.object(sys, "argv", argv), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                harvest.main()
        sign.assert_called_once()
        self.assertEqual(sign.call_args[0][0], path)
        self.assertEqual(calls[0], "reconcile", "the hand tool reconciles first, like a run")
        self.assertEqual(harvest._load(harvest.LEDGER, {}), {},
                         "the patched sign_file wrote nothing; the lock was the point")

    def test_a_hand_sign_persists_the_alert_it_raises(self):
        """A hand sign reconciles like a run, and the alert a reconcile raises is standing
        state, not this command's output: it must reach the state file even when the key turns
        out to be absent and the tool returns early -- or a store that broke between runs
        leaves the page unwarned by the one writer that saw it."""
        keys = [("u" + ("%02d" % i) * 10) for i in range(10)]
        rows = {k: harvest._row(k, 1, 1.0, "signed", None, "then", "e-%s" % k, {})
                for k in keys}
        harvest._save(harvest.LEDGER, rows)
        objects = {keys[0] + ".npy": "e-1"}        # one of ten: the store broke, not the rows
        with mock.patch.object(harvest, "_remote_objects",
                               lambda max_age_s=900: objects), \
                mock.patch.object(sys, "argv",
                                  ["harvest.py", "--sign-one", _key("https://y/absent")]), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            harvest.main()
        self.assertIn("no file for key", out.getvalue())
        state = harvest._load(harvest.STATE, {})
        self.assertIn("sig_alert", state,
                      "the alert survived the early return that named the missing key")

    def test_it_refuses_while_a_writer_holds_the_lock(self):
        key = _key("https://y/hand")
        self._feed(key)
        first = harvest.acquire_writer_lock()
        self.assertIsNotNone(first)
        self.addCleanup(first.close)
        with mock.patch.object(sys, "argv", ["harvest.py", "--sign-one", key]), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            harvest.main()
        self.assertIn("writer is RUNNING", out.getvalue())

    def test_it_names_the_missing_key(self):
        with mock.patch.object(sys, "argv",
                               ["harvest.py", "--sign-one", _key("https://y/absent")]), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            harvest.main()
        self.assertIn("no file for key", out.getvalue())


@unittest.skipUnless(harvest, "harvest.py needs numpy -- not this test's job")
class TheHarvesterOwnsNothingButItsOwnFiles(_SignerCase):
    """The directories are someone else's: read, never written."""

    def test_a_full_sign_deletes_nothing_in_the_directories(self):
        key = _key("https://y/mine")
        path = self._feed(key)
        before = sorted(os.listdir(self.audio))
        self._store_on()
        self._run_patches(fake_decode(pcm=_pcm(LONG_ENOUGH)))
        c, samples = harvest.sign_file(path)
        samples = None
        self.assertEqual(sorted(os.listdir(self.audio)), before,
                         "the audio and its sidecar are exactly as the feeder left them")
        self.assertEqual(harvest._load(harvest.LEDGER, {})[key]["status"], "signed")

    def test_a_refused_sign_deletes_nothing_either(self):
        key = _key("https://y/still-mine")
        path = self._feed(key, sidecar={"duration_s": 3600})
        before = sorted(os.listdir(self.audio))
        self._store_on()
        self._run_patches(fake_decode(pcm=_pcm(LONG_ENOUGH)))
        harvest.sign_file(path)
        self.assertEqual(sorted(os.listdir(self.audio)), before)

    def test_no_code_path_builds_a_ytdlp_argv(self):
        """The fetch leg is gone; if it ever comes back through this file, the pool's
        politeness rules come back with it, and that is a decision, not a slip."""
        src = open(os.path.join(SCRIPTS, "harvest.py"), encoding="utf-8").read()
        self.assertNotIn("yt-dlp", src)
        self.assertNotIn("yt_dlp", src)

    def test_nothing_reads_a_queue_or_a_download_index(self):
        """The sidecar is the only notice the harvester takes: no queue, no download index,
        no other process's store is opened to decide what to work on."""
        src = open(os.path.join(SCRIPTS, "harvest.py"), encoding="utf-8").read()
        for name in ("NETRADIO_LISTEN_QUEUE", "listen_queue", "index.json",
                     "NETRADIO_YTDLP", "NETRADIO_CANARY_URL"):
            self.assertNotIn(name, src, "%s has no business in the signer" % name)


@unittest.skipUnless(harvest, "harvest.py needs numpy -- not this test's job")
class TheLoopSignsAndScores(_SignerCase):
    """One pass of `run()`, end to end: the seed, the scan, the sign, the rescan's row."""

    def _run(self, stop_after_naps=1):
        """Run the loop until it has napped `stop_after_naps` times, then stop it cleanly."""

        class _StopTheRun(Exception):
            pass

        naps = []

        def _nap(seconds):
            naps.append(seconds)
            if len(naps) >= stop_after_naps:
                harvest._STOP["signum"] = signal.SIGTERM
                return True
            return False

        return _StopTheRun, naps, _nap

    def test_a_file_is_signed_scored_and_the_state_saved(self):
        key = _key("https://y/looped")
        path = self._feed(key)
        harvest._save(harvest.RULINGS, {})
        qs = [(4, np.zeros((12, 8), dtype="float32"), "4:fp")]
        self._store_on()
        self._run_patches(fake_decode(pcm=_pcm(LONG_ENOUGH)))
        _exc, naps, _nap = self._run(stop_after_naps=1)
        with mock.patch.object(harvest, "queries", lambda state=None: qs), \
                mock.patch.object(harvest, "_remote_objects",
                                  lambda max_age_s=900: None), \
                mock.patch.object(harvest, "_nap", _nap), \
                mock.patch.object(harvest._cm, "match", return_value=(None, 0, None)), \
                mock.patch.object(harvest.selftest, "offline", lambda: {"why": "test"}), \
                mock.patch.object(harvest.memwatch, "allocator_canary",
                                  lambda *a, **k: (0, 0, None)):
            harvest.run(None)
        state = harvest._load(harvest.STATE, {})
        self.assertEqual(harvest._load(harvest.LEDGER, {})[key]["status"], "signed")
        self.assertEqual(state["analyzed"], 1)
        # the file was signed; the second pass's scan found it covered and the run stood down
        self.assertTrue(naps)
        self.assertEqual(sorted(os.listdir(self.audio)),
                         sorted([key + ".mp3", key + ".json"]))

    def test_a_sidecar_landed_between_the_scan_and_the_sign_is_the_one_weighed(self):
        """The re-offer race: the scan can list a directory in the window between a
        re-offered file's new bytes and its new sidecar, and the old sidecar's claim must not
        become the new file's verdict. The length check weighs the sidecar as it reads at
        sign time -- and a wrong `length_mismatch` would be worse than a wrong skip, because
        the row carries the new bytes' own size and mtime and would cover the file for good.
        (The contract's half of the rule: take the old sidecar away before the new audio
        lands, docs/HARVEST_FEED.md.)"""
        key = _key("https://y/re-offered")
        path = self._feed(key, sidecar={"duration_s": 3600})   # old sidecar, new bytes
        harvest._save(harvest.RULINGS, {})
        self._run_patches(fake_decode(pcm=_pcm(LONG_ENOUGH)))  # the 60 s the file really is
        real_scan = harvest.scan_directories

        def _scan_then_the_new_sidecar_lands(ledger, issues=None, said=None):
            todo, covered = real_scan(ledger, issues=issues, said=said)
            with open(os.path.join(self.audio, key + ".json"), "w") as fh:
                json.dump({"key": key, "url": "https://y/x", "title": "a set",
                           "artist": "someone", "duration_s": 60.0,
                           "fed_at": "2026-09-19T00:00:00+00:00"}, fh)
            return todo, covered

        _exc, naps, _nap = self._run(stop_after_naps=1)
        with mock.patch.object(harvest, "scan_directories",
                               _scan_then_the_new_sidecar_lands), \
                mock.patch.object(harvest, "queries", lambda state=None: []), \
                mock.patch.object(harvest, "_remote_objects",
                                  lambda max_age_s=900: None), \
                mock.patch.object(harvest, "_nap", _nap), \
                mock.patch.object(harvest.selftest, "offline", lambda: {"why": "test"}), \
                mock.patch.object(harvest.memwatch, "allocator_canary",
                                  lambda *a, **k: (0, 0, None)):
            harvest.run(None)
        row = harvest._load(harvest.LEDGER, {})[key]
        self.assertEqual((row["status"], row["reason"]), ("signed", None),
                         "the claim weighed was the one beside the file at sign time, not "
                         "the stale copy the scan read")
        self.assertEqual(row["duration_s"], 60.0,
                         "and the row carries the sidecar the sign weighed")

    def test_the_second_pass_counts_the_covered_file_once(self):
        key = _key("https://y/counted")
        self._feed(key)
        harvest._save(harvest.RULINGS, {})
        qs = []
        self._store_on()
        self._run_patches(fake_decode(pcm=_pcm(LONG_ENOUGH)))
        _exc, naps, _nap = self._run(stop_after_naps=2)
        with mock.patch.object(harvest, "queries", lambda state=None: qs), \
                mock.patch.object(harvest, "_remote_objects",
                                  lambda max_age_s=900: None), \
                mock.patch.object(harvest, "_nap", _nap), \
                mock.patch.object(harvest.selftest, "offline", lambda: {"why": "test"}), \
                mock.patch.object(harvest.memwatch, "allocator_canary",
                                  lambda *a, **k: (0, 0, None)):
            harvest.run(None)
        state = harvest._load(harvest.STATE, {})
        self.assertEqual(state["skipped_cached"], 1,
                         "counted once for the key, not once per pass that saw it")

    def test_the_run_names_a_relative_entry_in_its_refusal(self):
        """The gate at the run's start records WHICH entry was wrong, so an operator sees the
        refused path in the issues list and not only the message that the setting came up
        empty."""
        harvest.HARVEST_DIRS = "not-absolute"
        harvest._save(harvest.RULINGS, {})
        out = io.StringIO()
        with mock.patch.object(harvest, "queries", lambda state=None: []), \
                contextlib.redirect_stdout(out):
            harvest.run(None)
        text = out.getvalue()
        self.assertIn("no absolute directory", text)
        state = harvest._load(harvest.STATE, {})
        self.assertTrue(any("not-absolute" in r.get("dir", "") for r in state["issues"]),
                        "the refused entry is named in a persisted issues row")

    def test_the_hand_tool_names_a_relative_entry_in_its_refusal(self):
        harvest.HARVEST_DIRS = "not-absolute"
        with mock.patch.object(sys, "argv", ["harvest.py", "--sign-one", "u" + "a" * 20]), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            harvest.main()
        self.assertIn("no absolute directory", out.getvalue())
        state = harvest._load(harvest.STATE, {})
        self.assertTrue(any("not-absolute" in r.get("dir", "") for r in state["issues"]))

    def test_the_run_refuses_to_start_with_no_directories(self):
        harvest.HARVEST_DIRS = ""
        harvest._save(harvest.RULINGS, {})
        out = io.StringIO()
        with mock.patch.object(harvest, "queries", lambda state=None: []), \
                contextlib.redirect_stdout(out):
            harvest.run(None)
        self.assertIn("NETRADIO_HARVEST_DIRS", out.getvalue())
        self.assertIn("no directories", out.getvalue())

    def test_the_run_refuses_to_start_on_a_dark_cache(self):
        self._feed(_key("https://y/dark"))
        harvest._save(harvest.RULINGS, {})
        attrs = (harvest.CACHE, harvest.KEEP, harvest._CACHE_AT_IMPORT,
                 harvest._KEEP_AT_IMPORT)
        for k in [k for k in list(os.environ) if k.startswith("NETRADIO_")
                  and ("CACHE" in k or k in CACHE_ENV)]:
            os.environ.pop(k, None)
        cache_budget._REGISTRY.clear()
        cache_budget._STATS.clear()
        harvest.register_caches()         # the policy dark: nowhere to keep a signature
        try:
            out = io.StringIO()
            with mock.patch.object(harvest, "queries", lambda state=None: []), \
                    mock.patch.object(harvest.selftest, "offline",
                                      lambda: (_ for _ in ()).throw(
                                          AssertionError("refused before the canary"))), \
                    contextlib.redirect_stdout(out):
                harvest.run(None)
        finally:
            for name, value in zip(("CACHE", "KEEP", "_CACHE_AT_IMPORT",
                                    "_KEEP_AT_IMPORT"), attrs):
                setattr(harvest, name, value)
            cache_budget._REGISTRY.clear()
            cache_budget._REGISTRY.update(self._registry[0])
            cache_budget._STATS.clear()
            cache_budget._STATS.update(self._registry[1])
        self.assertIn("cache", out.getvalue())
        self.assertIn("NETRADIO_CACHE_ROOT", out.getvalue())

    def test_a_pass_with_no_room_signs_nothing_and_does_not_loop_the_decode(self):
        """The probe: one refusal per pass, no decode -- a full disk must not put every file
        through a multi-hour sign that ends in `no_space`."""
        key = _key("https://y/full")
        self._feed(key)
        harvest._save(harvest.RULINGS, {})
        spawned = []
        self._run_patches(lambda argv, **kw: spawned.append(argv) or _FakeProc(argv))
        _exc, naps, _nap = self._run(stop_after_naps=1)
        with mock.patch.object(harvest, "queries", lambda state=None: []), \
                mock.patch.object(harvest, "_nap", _nap), \
                mock.patch.object(harvest.cache_budget, "reserve",
                                  lambda *a, **k: False), \
                mock.patch.object(harvest.selftest, "offline", lambda: {"why": "test"}), \
                mock.patch.object(harvest.memwatch, "allocator_canary",
                                  lambda *a, **k: (0, 0, None)):
            harvest.run(None)
        self.assertEqual(spawned, [], "ffmpeg never ran")
        self.assertEqual(harvest._load(harvest.LEDGER, {}), {})


if __name__ == "__main__":
    unittest.main()
