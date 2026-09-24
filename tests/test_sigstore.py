"""sigstore: dark-by-default, verified puts, safe eviction, etags for the ledger. All offline.

The aws CLI never runs: the module's one subprocess seam (`sigstore._run`) is swapped for a
scripted recorder — the same reason the seam exists in the module. The fakes speak the JSON the
real CLI prints, built with `json.dumps` in the fixtures so the quoting is never hand-escaped:
S3 quotes its etags inside JSON, and one hand-escaped quote is exactly the kind of fixture that
passes while the real thing fails.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))

import sigstore  # noqa: E402
import cache_budget  # noqa: E402  (evict_cold deletes through the chroma cache's policy)


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class Recorder:
    def __init__(self):
        self.calls = []
        self.results = []            # popped per call; empty -> success, no output

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        return self.results.pop(0) if self.results else FakeProc()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.aws = os.path.join(self.tmp.name, "aws")
        with open(self.aws, "w") as fh:
            fh.write("#!/bin/sh\n")
        os.chmod(self.aws, 0o755)
        self.rec = Recorder()
        self._orig_run = sigstore._run
        sigstore._run = self.rec
        self.addCleanup(setattr, sigstore, "_run", self._orig_run)
        sigstore._verified.clear()
        self._env = {}
        for k in ("NETRADIO_SIG_BUCKET", "NETRADIO_SIG_S3_ENDPOINT",
                  "NETRADIO_SIG_AWS_PROFILE", "NETRADIO_AWS_CLI"):
            self._env[k] = os.environ.pop(k, None)
        self.addCleanup(self._restore)
        os.environ["NETRADIO_SIG_BUCKET"] = "test-bucket"
        os.environ["NETRADIO_AWS_CLI"] = self.aws

    def _restore(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _sig(self, name="u" + "0" * 20 + ".npy", size=100):
        path = os.path.join(self.tmp.name, name)
        with open(path, "wb") as fh:
            fh.write(b"x" * size)
        return path, name


class TestDark(Base):
    def test_dark_without_bucket(self):
        del os.environ["NETRADIO_SIG_BUCKET"]
        self.assertFalse(sigstore.enabled())
        path, key = self._sig()
        self.assertIsNone(sigstore.put(path, key))
        self.assertIsNone(sigstore.list_objects())
        self.assertFalse(sigstore.have_remote(key))
        self.assertEqual(self.rec.calls, [])

    def test_dark_never_evicts(self):
        del os.environ["NETRADIO_SIG_BUCKET"]
        path, key = self._sig()
        n, freed = sigstore.evict_cold(self.tmp.name, {"q1": [key]}, ["q1"])
        self.assertEqual(n, 0)
        self.assertTrue(os.path.exists(path))


class TestPut(Base):
    def test_put_verifies_size_and_returns_the_etag(self):
        # The etag is what the harvester's ledger row records: a put is not just "it landed",
        # it is "this is the object it landed as". S3 quotes its etags inside the JSON; the
        # quotes come off once, in the module.
        path, key = self._sig(size=100)
        self.rec.results = [FakeProc(), FakeProc(stdout=json.dumps([100, '"etag-1"']))]
        self.assertEqual(sigstore.put(path, key), "etag-1")
        self.assertIn("s3://test-bucket/chroma/" + key, self.rec.calls[0])

    def test_put_fails_on_size_mismatch(self):
        path, key = self._sig(size=100)
        self.rec.results = [FakeProc(), FakeProc(stdout=json.dumps([99, "etag-1"]))]
        self.assertFalse(sigstore.put(path, key))

    def test_put_fails_on_cp_error(self):
        path, key = self._sig()
        self.rec.results = [FakeProc(returncode=1, stderr="denied")]
        self.assertIsNone(sigstore.put(path, key))
        self.assertEqual(len(self.rec.calls), 1)                       # no HEAD after failed cp

    def test_a_put_without_a_bucket_returns_none(self):
        # None (not False) is the contract: a caller reading the result as a bool still works,
        # and a caller recording it in a ledger row writes an honest absence, not a boolean.
        del os.environ["NETRADIO_SIG_BUCKET"]
        path, key = self._sig()
        self.assertIsNone(sigstore.put(path, key))
        self.assertEqual(self.rec.calls, [])


class TestRemote(Base):
    def test_head_caches_per_session(self):
        _, key = self._sig()
        self.rec.results = [FakeProc(stdout=json.dumps([55, "e-55"]))]
        self.assertEqual(sigstore.remote_size(key), 55)
        self.assertEqual(sigstore.remote_size(key), 55)                # cached
        self.assertEqual(len(self.rec.calls), 1)

    def test_absent_object_is_none_and_not_cached(self):
        _, key = self._sig()
        self.rec.results = [FakeProc(returncode=254, stderr="Not Found"),
                            FakeProc(stdout=json.dumps([55, "e-55"]))]
        self.assertIsNone(sigstore.remote_size(key))
        self.assertEqual(sigstore.remote_size(key), 55)                # re-asked, now present

    def test_a_head_without_a_size_is_none(self):
        # A shape the contract does not describe (a torn [null, ...]) is an absence, not a
        # crash -- and it is never cached, so the next ask re-asks.
        _, key = self._sig()
        self.rec.results = [FakeProc(stdout=json.dumps([None, "e"])),
                            FakeProc(stdout=json.dumps([7, "e"]))]
        self.assertIsNone(sigstore.remote_size(key))
        self.assertEqual(sigstore.remote_size(key), 7)

    def test_the_listing_carries_the_etags(self):
        # The ledger's rows record the etags, and one listing answers both questions: what is
        # in the pool, and which object each row points at.
        k1 = "u" + "1" * 20 + ".npy"
        k2 = "u" + "2" * 20 + ".npy"
        page1 = json.dumps([[["chroma/" + k1, '"e-1"'],
                             ["chroma/_recipe.json", '"x"'],
                             ["chroma/_canary/manifest.json", '"x"'],
                             ["chroma/upload.npy", '"x"'],
                             ["chroma/u123.npy", '"x"']], "TOK"])
        page2 = json.dumps([[["chroma/" + k2, "e-2"]], None])
        self.rec.results = [FakeProc(stdout=page1), FakeProc(stdout=page2)]
        objects = sigstore.list_objects()
        self.assertEqual(objects, {k1: "e-1", k2: "e-2"})
        self.assertIn("--starting-token", self.rec.calls[1])

    def test_the_listing_admits_only_keys_shapes(self):
        # The scan refuses any stem that is not `u` + 20 hex, so an object named any other way
        # could never be satisfied by a local file: admitting it would seed a ledger row that
        # no feeder can cover, and the pool's count would disagree with the scan.
        page = json.dumps([[["chroma/upload.npy", '"x"'],
                            ["chroma/u123.npy", '"x"'],
                            ["chroma/" + "u" + "g" * 20 + ".npy", '"x"'],
                            ["chroma/_recipe.json", '"x"']], None])
        self.rec.results = [FakeProc(stdout=page)]
        self.assertEqual(sigstore.list_objects(), {})


# The cache-policy names only (the same set tests/test_cache_budget.py saves and restores):
# the sigstore settings Base.setUp has already placed stay.
CACHE_ENV = ("NETRADIO_CACHE_ROOT", "NETRADIO_DOWNLOAD_ROOT", "NETRADIO_DISK_MAX_PCT",
             "NETRADIO_CACHE_EVENTS_DAYS")


class TestEvict(Base):
    def setUp(self):
        super().setUp()
        # evict_cold deletes cache entries, so it goes through the cache policy's door: the
        # `chroma` cache must be registered over this test's directory for a removal to pass.
        # The policy's root must live OUTSIDE it (no cache may hold the root), so a second
        # throwaway directory serves.
        self._saved_env = {k: os.environ.get(k) for k in list(os.environ)
                           if k.startswith("NETRADIO_") and ("CACHE" in k or k in CACHE_ENV)}
        for k in self._saved_env:
            os.environ.pop(k, None)
        self._root = tempfile.mkdtemp(prefix="sigstore-policy-root-")
        os.environ["NETRADIO_CACHE_ROOT"] = self._root
        os.environ["NETRADIO_CHROMA_CACHE_DIR"] = self.tmp.name
        self._registry = dict(cache_budget._REGISTRY), dict(cache_budget._STATS)
        cache_budget._REGISTRY.clear()
        cache_budget._STATS.clear()
        cache_budget.register("chroma", dir=self.tmp.name)
        self.addCleanup(self._restore)
        # An empty volume: the disk floor never trips, so a near-cap eviction is what the
        # tests ask for and nothing else.
        self.addCleanup(setattr, cache_budget, "_disk_usage", cache_budget._disk_usage)
        cache_budget._disk_usage = lambda _p: (100 * 1000 * 1000, 0, 100 * 1000 * 1000)

    def _restore(self):
        import shutil
        cache_budget._REGISTRY.clear()
        cache_budget._REGISTRY.update(self._registry[0])
        cache_budget._STATS.clear()
        cache_budget._STATS.update(self._registry[1])
        for k in [k for k in list(os.environ)
                  if k.startswith("NETRADIO_") and ("CACHE" in k or k in CACHE_ENV)]:
            os.environ.pop(k, None)
        os.environ.update(self._saved_env)
        shutil.rmtree(self._root, ignore_errors=True)

    def test_evicts_only_verified_and_fully_scored(self):
        p1, k1 = self._sig("u" + "1" * 20 + ".npy", size=10)    # scored both, verified -> goes
        p2, k2 = self._sig("u" + "2" * 20 + ".npy", size=10)    # missing one mystery -> stays
        p3, k3 = self._sig("u" + "3" * 20 + ".npy", size=10)    # scored, NOT verified -> stays
        scored = {"qA": [k1, k2, k3], "qB": [k1, k3]}
        # HEADs happen in sorted(name) order for eligible files: k1 verified(10); k3 size-mismatch
        self.rec.results = [FakeProc(stdout=json.dumps([10, "e"])),
                            FakeProc(stdout=json.dumps([11, "e"]))]
        n, freed = sigstore.evict_cold(self.tmp.name, scored, ["qA", "qB"])
        self.assertEqual((n, freed), (1, 10))
        self.assertFalse(os.path.exists(p1))
        self.assertTrue(os.path.exists(p2))
        self.assertTrue(os.path.exists(p3))

    def test_no_mysteries_means_no_eviction(self):
        path, key = self._sig()
        n, _ = sigstore.evict_cold(self.tmp.name, {"qA": [key]}, [])
        self.assertEqual(n, 0)
        self.assertTrue(os.path.exists(path))

    def test_an_active_download_survives_an_eviction_run(self):
        """A bucket pull lands inside the registered `chroma` cache under a name ending
        `.part` -- the policy's write-in-progress mark -- so the run another writer's
        `reserve` triggers evicts old entries, never an active download out from under its
        own atomic replace."""
        old1, _ = self._sig("u" + "7" * 20 + ".npy", size=300 * 1000)  # entries to give up
        old2, _ = self._sig("u" + "8" * 20 + ".npy", size=300 * 1000)
        key = "u" + "9" * 20 + ".npy"
        part = os.path.join(self.tmp.name, key + ".abc123.part")    # fetch()'s temp, fresh
        with open(part, "wb") as fh:
            fh.write(b"x" * 100)
        os.environ["NETRADIO_CHROMA_CACHE_GB"] = "0.0005"     # 500 KB: the cache is over
        cache_budget.register("chroma", dir=self.tmp.name)   # re-read: the lowered cap applies
        self.assertTrue(cache_budget.reserve("chroma", 250 * 1000), "room was made")
        self.assertFalse(os.path.exists(old1), "the oldest entry went")
        self.assertFalse(os.path.exists(old2), "and the next oldest, until it fit")
        self.assertTrue(os.path.exists(part),
                        "the active download is held by the policy, never evicted")

    def test_fetch_downloads_via_temp_then_renames(self):
        dest_dir = os.path.join(self.tmp.name, "cache")
        key = "u" + "4" * 20 + ".npy"

        def fake_cp(cmd, **kw):
            self.rec.calls.append(cmd)
            with open(cmd[-2], "wb") as fh:            # cp writes the TEMP destination arg
                fh.write(b"z")
            return FakeProc()
        sigstore._run = fake_cp
        out = sigstore.fetch(key, dest_dir)
        self.assertEqual(out, os.path.join(dest_dir, key))
        self.assertTrue(os.path.exists(out))
        self.assertIn(".part", self.rec.calls[0][-2])              # download went via a temp name
        self.assertTrue(self.rec.calls[0][-2].endswith(".part"),
                        "the temp is the cache policy's write-in-progress mark, so no eviction "
                        "run takes an active download")
        self.assertEqual([n for n in os.listdir(dest_dir)], [key]) # no temp left behind

    def test_failed_fetch_leaves_no_partial_and_retry_succeeds(self):
        """The P2 regression: an interrupted copy must not poison the cache entry."""
        dest_dir = os.path.join(self.tmp.name, "cache")
        key = "u" + "5" * 20 + ".npy"
        state = {"n": 0}

        def flaky_cp(cmd, **kw):
            self.rec.calls.append(cmd)
            state["n"] += 1
            with open(cmd[-2], "wb") as fh:
                fh.write(b"par")                        # partial bytes hit the disk either way
            if state["n"] == 1:
                return FakeProc(returncode=1, stderr="timeout")     # ...but the copy FAILED
            return FakeProc()
        sigstore._run = flaky_cp
        self.assertIsNone(sigstore.fetch(key, dest_dir))
        self.assertEqual(os.listdir(dest_dir), [])      # nothing under the final name, no temp
        out = sigstore.fetch(key, dest_dir)             # the retry is not suppressed
        self.assertEqual(out, os.path.join(dest_dir, key))

    def test_overlapping_fetches_of_one_key_both_succeed(self):
        """The P2 regression: two threads, one key, unique temps, no crash, one final file."""
        import threading
        dest_dir = os.path.join(self.tmp.name, "cache")
        key = "u" + "6" * 20 + ".npy"
        gate = threading.Barrier(2)
        temps, results, errors = [], [], []

        def slow_cp(cmd, **kw):
            temps.append(cmd[-2])
            with open(cmd[-2], "wb") as fh:
                fh.write(b"z")
            gate.wait(timeout=5)               # both copies "finish" at the same moment
            return FakeProc()
        sigstore._run = slow_cp

        def go():
            try:
                results.append(sigstore.fetch(key, dest_dir))
            except Exception as exc:            # the old bug: FileNotFoundError from replace
                errors.append(exc)
        ts = [threading.Thread(target=go) for _ in range(2)]
        [t.start() for t in ts]
        [t.join(timeout=10) for t in ts]
        self.assertEqual(errors, [])
        self.assertEqual(results, [os.path.join(dest_dir, key)] * 2)
        self.assertEqual(len(set(temps)), 2)                     # genuinely unique temp paths
        self.assertEqual(os.listdir(dest_dir), [key])            # one file, no leftovers

    def test_no_endpoint_flag_unless_configured(self):
        """The neutrality P2: this public module names no provider."""
        path, key = self._sig()
        self.rec.results = [FakeProc(), FakeProc(stdout=json.dumps([100, "e"]))]
        sigstore.put(path, key)
        self.assertNotIn("--endpoint-url", self.rec.calls[0])
        os.environ["NETRADIO_SIG_S3_ENDPOINT"] = "https://s3.example.test"
        sigstore._verified.clear()
        self.rec.results = [FakeProc(), FakeProc(stdout=json.dumps([100, "e"]))]
        sigstore.put(path, key)
        cmd = self.rec.calls[-2]
        self.assertIn("--endpoint-url", cmd)
        self.assertEqual(cmd[cmd.index("--endpoint-url") + 1], "https://s3.example.test")


if __name__ == "__main__":
    unittest.main()
