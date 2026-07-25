"""sigstore: dark-by-default, verified puts, safe eviction. All offline.

The aws CLI never runs: the module's one subprocess seam (`sigstore._run`) is swapped for a
scripted recorder — the same reason the seam exists in the module.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))

import sigstore  # noqa: E402


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
        self.assertFalse(sigstore.put(path, key))
        self.assertIsNone(sigstore.list_keys())
        self.assertFalse(sigstore.have_remote(key))
        self.assertEqual(self.rec.calls, [])

    def test_dark_never_evicts(self):
        del os.environ["NETRADIO_SIG_BUCKET"]
        path, key = self._sig()
        n, freed = sigstore.evict_cold(self.tmp.name, {"q1": [key]}, ["q1"])
        self.assertEqual(n, 0)
        self.assertTrue(os.path.exists(path))


class TestPut(Base):
    def test_put_verifies_size(self):
        path, key = self._sig(size=100)
        self.rec.results = [FakeProc(), FakeProc(stdout="100\n")]      # cp ok, head says 100
        self.assertTrue(sigstore.put(path, key))
        self.assertIn("s3://test-bucket/chroma/" + key, self.rec.calls[0])

    def test_put_fails_on_size_mismatch(self):
        path, key = self._sig(size=100)
        self.rec.results = [FakeProc(), FakeProc(stdout="99\n")]       # cp ok, head DISAGREES
        self.assertFalse(sigstore.put(path, key))

    def test_put_fails_on_cp_error(self):
        path, key = self._sig()
        self.rec.results = [FakeProc(returncode=1, stderr="denied")]
        self.assertFalse(sigstore.put(path, key))
        self.assertEqual(len(self.rec.calls), 1)                       # no HEAD after failed cp


class TestRemote(Base):
    def test_head_caches_per_session(self):
        _, key = self._sig()
        self.rec.results = [FakeProc(stdout="55\n")]
        self.assertEqual(sigstore.remote_size(key), 55)
        self.assertEqual(sigstore.remote_size(key), 55)                # cached
        self.assertEqual(len(self.rec.calls), 1)

    def test_absent_object_is_none_and_not_cached(self):
        _, key = self._sig()
        self.rec.results = [FakeProc(returncode=254, stderr="Not Found"),
                            FakeProc(stdout="55\n")]
        self.assertIsNone(sigstore.remote_size(key))
        self.assertEqual(sigstore.remote_size(key), 55)                # re-asked, now present

    def test_list_keys_pages_and_filters(self):
        page1 = json.dumps([["chroma/u" + "1" * 20 + ".npy", "chroma/_recipe.json",
                             "chroma/_canary/manifest.json"], "TOK"])
        page2 = json.dumps([["chroma/u" + "2" * 20 + ".npy"], None])
        self.rec.results = [FakeProc(stdout=page1), FakeProc(stdout=page2)]
        keys = sigstore.list_keys()
        self.assertEqual(keys, {"u" + "1" * 20 + ".npy", "u" + "2" * 20 + ".npy"})
        self.assertIn("--starting-token", self.rec.calls[1])


class TestEvict(Base):
    def test_evicts_only_verified_and_fully_scored(self):
        p1, k1 = self._sig("u" + "1" * 20 + ".npy", size=10)    # scored both, verified -> goes
        p2, k2 = self._sig("u" + "2" * 20 + ".npy", size=10)    # missing one mystery -> stays
        p3, k3 = self._sig("u" + "3" * 20 + ".npy", size=10)    # scored, NOT verified -> stays
        scored = {"qA": [k1, k2, k3], "qB": [k1, k3]}
        # HEADs happen in sorted(name) order for eligible files: k1 verified(10); k3 size-mismatch
        self.rec.results = [FakeProc(stdout="10\n"), FakeProc(stdout="11\n")]
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
        self.assertIn(".part-", self.rec.calls[0][-2])             # download went via a temp name
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
        self.rec.results = [FakeProc(), FakeProc(stdout="100\n")]
        sigstore.put(path, key)
        self.assertNotIn("--endpoint-url", self.rec.calls[0])
        os.environ["NETRADIO_SIG_S3_ENDPOINT"] = "https://s3.example.test"
        sigstore._verified.clear()
        self.rec.results = [FakeProc(), FakeProc(stdout="100\n")]
        sigstore.put(path, key)
        cmd = self.rec.calls[-2]
        self.assertIn("--endpoint-url", cmd)
        self.assertEqual(cmd[cmd.index("--endpoint-url") + 1], "https://s3.example.test")


if __name__ == "__main__":
    unittest.main()
