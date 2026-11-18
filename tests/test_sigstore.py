"""sigstore: dark-by-default, verified puts, safe eviction, etags for the ledger. All offline.

The aws CLI never runs: every bucket call goes through `bucketio`, whose one subprocess seam
(`bucketio._run`) is swapped for `bucket_fake.FakeAws`. The fake holds objects in memory and
speaks the JSON the real CLI prints, built with `json.dumps` so the quoting is never
hand-escaped: S3 quotes its etags inside JSON, and one hand-escaped quote is exactly the kind
of fixture that passes while the real thing fails.
"""

import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))
sys.path.insert(0, HERE)

import sigstore  # noqa: E402
import cache_budget  # noqa: E402  (evict_cold deletes through the chroma cache's policy)
from bucket_fake import FakeAws, FakeProc  # noqa: E402

BUCKET = "test-bucket"


def md5(body):
    return hashlib.md5(body).hexdigest()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.aws_cli = os.path.join(self.tmp.name, "aws")
        with open(self.aws_cli, "w") as fh:
            fh.write("#!/bin/sh\n")
        os.chmod(self.aws_cli, 0o755)
        self.aws = FakeAws().install(self)
        sigstore._verified.clear()
        self._env = {}
        for k in ("NETRADIO_SIG_BUCKET", "NETRADIO_SIG_S3_ENDPOINT",
                  "NETRADIO_SIG_AWS_PROFILE", "NETRADIO_AWS_CLI"):
            self._env[k] = os.environ.pop(k, None)
        self.addCleanup(self._restore)
        os.environ["NETRADIO_SIG_BUCKET"] = BUCKET
        os.environ["NETRADIO_AWS_CLI"] = self.aws_cli

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

    def _remote(self, key, body, sha256=False):
        self.aws.add(BUCKET, sigstore.PREFIX + key, body, sha256=sha256)


class TestDark(Base):
    def test_dark_without_bucket(self):
        del os.environ["NETRADIO_SIG_BUCKET"]
        self.assertFalse(sigstore.enabled())
        path, key = self._sig()
        self.assertIsNone(sigstore.put(path, key))
        self.assertIsNone(sigstore.list_objects())
        self.assertIsNone(sigstore.fetch(key, self.tmp.name))
        self.assertEqual(self.aws.calls, [])

    def test_dark_without_a_cli(self):
        os.environ["NETRADIO_AWS_CLI"] = os.path.join(self.tmp.name, "missing")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(sigstore.enabled())

    def test_a_named_bucket_with_no_cli_is_an_error_printed_once(self):
        # The store stays dark, but a bucket the operator named and cannot reach is an ERROR,
        # never a silent local-only run.
        self.addCleanup(sigstore._reported.clear)
        sigstore._reported.clear()
        os.environ["NETRADIO_AWS_CLI"] = os.path.join(self.tmp.name, "missing")
        self.assertIn("NETRADIO_SIG_BUCKET is set", sigstore.store_error())
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertFalse(sigstore.enabled())
            self.assertFalse(sigstore.enabled())
        self.assertEqual(err.getvalue().count("ERROR: sigstore:"), 1)

    def test_no_bucket_named_is_no_error(self):
        del os.environ["NETRADIO_SIG_BUCKET"]
        os.environ["NETRADIO_AWS_CLI"] = os.path.join(self.tmp.name, "missing")
        self.assertIsNone(sigstore.store_error())
        with contextlib.redirect_stderr(io.StringIO()) as err:
            sigstore.enabled()
        self.assertEqual(err.getvalue(), "")
        os.environ["NETRADIO_AWS_CLI"] = self.aws_cli
        os.environ["NETRADIO_SIG_BUCKET"] = BUCKET
        self.assertIsNone(sigstore.store_error())

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
        # quotes come off once, in bucketio.
        path, key = self._sig(size=100)
        self.assertEqual(sigstore.put(path, key), md5(b"x" * 100))
        self.assertIn("s3://test-bucket/chroma/" + key, self.aws.calls[0][0])
        self.assertEqual(self.aws.ops(), ["cp-up", "head-object"])
        self.assertEqual(sigstore.verified_size(key), 100)

    def test_put_writes_the_sha256_metadata(self):
        path, key = self._sig(size=100)
        sigstore.put(path, key)
        self.assertEqual(self.aws.objects[(BUCKET, sigstore.PREFIX + key)]["meta"],
                         {"sha256": hashlib.sha256(b"x" * 100).hexdigest()})

    def test_put_fails_on_size_mismatch(self):
        path, key = self._sig(size=100)
        self.aws.fail_next("head-object", FakeProc(stdout=json.dumps(
            {"ContentLength": 99, "ETag": '"etag-1"', "Metadata": {}})))
        self.assertFalse(sigstore.put(path, key))
        self.assertIsNone(sigstore.verified_size(key))

    def test_put_fails_on_cp_error(self):
        path, key = self._sig()
        self.aws.fail_next("cp-up", FakeProc(returncode=1, stderr="denied"))
        self.assertIsNone(sigstore.put(path, key))
        self.assertEqual(self.aws.ops(), ["cp-up"])                    # no HEAD after failed cp

    def test_a_put_without_a_bucket_returns_none(self):
        # None (not False) is the contract: a caller reading the result as a bool still works,
        # and a caller recording it in a ledger row writes an honest absence, not a boolean.
        del os.environ["NETRADIO_SIG_BUCKET"]
        path, key = self._sig()
        self.assertIsNone(sigstore.put(path, key))
        self.assertEqual(self.aws.calls, [])


class TestRemote(Base):
    def test_head_caches_per_session(self):
        _, key = self._sig()
        self._remote(key, b"x" * 55)
        self.assertEqual(sigstore._head(key), (55, md5(b"x" * 55)))
        self.assertEqual(sigstore._head(key)[0], 55)                # cached
        self.assertEqual(len(self.aws.calls), 1)

    def test_absent_object_is_none_and_not_cached(self):
        _, key = self._sig()
        self.assertIsNone(sigstore._head(key)[0])
        self._remote(key, b"x" * 55)
        self.assertEqual(sigstore._head(key)[0], 55)                # re-asked, now present

    def test_a_head_without_a_size_is_none(self):
        # A shape the contract does not describe (no ContentLength) is an absence, not a
        # crash -- and it is never cached, so the next ask re-asks.
        _, key = self._sig()
        self._remote(key, b"x" * 7)
        self.aws.fail_next("head-object", FakeProc(stdout=json.dumps({"ETag": '"e"'})))
        self.assertIsNone(sigstore._head(key)[0])
        self.assertEqual(sigstore._head(key)[0], 7)

    def test_the_listing_carries_the_etags(self):
        # The ledger's rows record the etags, and one listing answers both questions: what is
        # in the pool, and which object each row points at.
        self.aws.page_size = 2
        k1 = "u" + "1" * 20 + ".npy"
        k2 = "u" + "2" * 20 + ".json"
        for name in (k1, k2, "_recipe.json", "_canary/manifest.json", "upload.npy",
                     "u123.npy"):
            self._remote(name, name.encode())
        objects = sigstore.list_objects()
        self.assertEqual(objects, {k1: md5(k1.encode()), k2: md5(k2.encode())})
        self.assertIn("--starting-token", self.aws.calls[1][0])

    def test_the_listing_is_asked_fresh_every_time(self):
        # The harvester holds its own copy of the listing for as long as it wants; a second
        # cache here would hide an object a put just landed.
        sigstore.list_objects()
        sigstore.list_objects()
        self.assertEqual(len(self.aws.ops("list-objects-v2")), 2)

    def test_the_listing_admits_only_keys_shapes(self):
        # The scan refuses any stem that is not `u` + 20 hex, so an object named any other way
        # could never be satisfied by a local file: admitting it would seed a ledger row that
        # no feeder can cover, and the pool's count would disagree with the scan.
        for name in ("upload.npy", "u123.npy", "u" + "g" * 20 + ".npy", "_recipe.json"):
            self._remote(name, b"x")
        self.assertEqual(sigstore.list_objects(), {})

    def test_a_failed_listing_is_none(self):
        self.aws.fail_next("list-objects-v2", FakeProc(returncode=255, stderr="AccessDenied"))
        self.assertIsNone(sigstore.list_objects())


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
        # The bucket holds k1 at its local size, and k3 at another size.
        self._remote(k1, b"x" * 10)
        self._remote(k3, b"x" * 11)
        n, freed = sigstore.evict_cold(self.tmp.name, scored, ["qA", "qB"])
        self.assertEqual((n, freed), (1, 10))
        self.assertFalse(os.path.exists(p1))
        self.assertTrue(os.path.exists(p2))
        self.assertTrue(os.path.exists(p3))
        # and the deletion went through the cache policy's one door, which recorded it with
        # the caller's reason: a bare os.remove would empty the cache behind the policy's
        # back, leaving its accounting and its event log describing entries that are gone.
        with open(cache_budget.events_path()) as fh:
            events = [json.loads(line) for line in fh]
        self.assertEqual([(e["cache"], e["entry"], e["reason"]) for e in events
                          if e["event"] == "remove"],
                         [("chroma", os.path.basename(p1), "cold-verified")])

    def test_a_signature_outside_the_registered_cache_is_never_deleted(self):
        """`remove` refuses a path outside the cache's directory, so a cache_dir the caller
        passes that is NOT the registered `chroma` directory deletes nothing at all -- the
        one door is also a guard."""
        other = tempfile.TemporaryDirectory()      # outside the registered cache entirely
        self.addCleanup(other.cleanup)
        elsewhere = other.name
        stray = os.path.join(elsewhere, "u" + "5" * 20 + ".npy")
        with open(stray, "wb") as fh:
            fh.write(b"x" * 10)
        key = os.path.basename(stray)
        self._remote(key, b"x" * 10)
        n, freed = sigstore.evict_cold(elsewhere, {"qA": [key]}, ["qA"])
        self.assertEqual((n, freed), (0, 0))
        self.assertTrue(os.path.exists(stray), "nothing outside the cache is deleted")

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
        self._remote(key, b"z")
        out = sigstore.fetch(key, dest_dir)
        self.assertEqual(out, os.path.join(dest_dir, key))
        self.assertTrue(os.path.exists(out))
        target = self.aws.calls[-1][0][3]                      # cp's destination argument
        self.assertTrue(target.endswith(".part"),
                        "the temp is the cache policy's write-in-progress mark, so no eviction "
                        "run takes an active download")
        self.assertEqual(os.path.dirname(target), dest_dir)
        self.assertEqual([n for n in os.listdir(dest_dir)], [key]) # no temp left behind

    def test_an_older_signature_with_no_sha256_is_checked_by_size(self):
        """Signatures uploaded before put stored the hash carry none. They are fetched, and
        checked by their size; a copy of another size is refused."""
        dest_dir = os.path.join(self.tmp.name, "cache")
        key = "u" + "3" * 20 + ".npy"
        self._remote(key, b"old-signature")                     # no sha256 metadata
        self.assertEqual(sigstore.fetch(key, dest_dir), os.path.join(dest_dir, key))
        os.remove(os.path.join(dest_dir, key))
        self.aws.served[(BUCKET, sigstore.PREFIX + key)] = b"old-signatur"
        self.assertIsNone(sigstore.fetch(key, dest_dir))
        self.assertEqual(os.listdir(dest_dir), [])

    def test_a_signature_with_a_sha256_is_checked_by_it(self):
        dest_dir = os.path.join(self.tmp.name, "cache")
        key = "u" + "2" * 20 + ".npy"
        self._remote(key, b"signature", sha256=True)
        self.aws.served[(BUCKET, sigstore.PREFIX + key)] = b"SIGNATURE"   # same size
        self.assertIsNone(sigstore.fetch(key, dest_dir))
        self.assertEqual(os.listdir(dest_dir), [])
        del self.aws.served[(BUCKET, sigstore.PREFIX + key)]
        self.assertEqual(sigstore.fetch(key, dest_dir), os.path.join(dest_dir, key))

    def test_failed_fetch_leaves_no_partial_and_retry_succeeds(self):
        """The P2 regression: an interrupted copy must not poison the cache entry."""
        dest_dir = os.path.join(self.tmp.name, "cache")
        key = "u" + "5" * 20 + ".npy"
        self._remote(key, b"z")
        self.aws.fail_next("cp-down", FakeProc(returncode=1, stderr="timeout"))
        self.assertIsNone(sigstore.fetch(key, dest_dir))
        self.assertEqual(os.listdir(dest_dir), [])      # nothing under the final name, no temp
        out = sigstore.fetch(key, dest_dir)             # the retry is not suppressed
        self.assertEqual(out, os.path.join(dest_dir, key))

    def test_overlapping_fetches_of_one_key_both_succeed(self):
        """The P2 regression: two threads, one key, unique temps, no crash, one final file."""
        import threading
        dest_dir = os.path.join(self.tmp.name, "cache")
        key = "u" + "6" * 20 + ".npy"
        self._remote(key, b"z")
        gate = threading.Barrier(2)
        temps, results, errors = [], [], []
        real = self.aws._cp_down

        def slow_cp(args):
            temps.append(args[3])
            proc = real(args)
            gate.wait(timeout=5)               # both copies "finish" at the same moment
            return proc
        self.aws._cp_down = slow_cp

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
        sigstore.put(path, key)
        self.assertNotIn("--endpoint-url", self.aws.calls[0][1])
        os.environ["NETRADIO_SIG_S3_ENDPOINT"] = "https://s3.example.test"
        os.environ["NETRADIO_SIG_AWS_PROFILE"] = "sig"
        sigstore.put(path, key)
        cmd = self.aws.calls[-2][1]
        self.assertEqual(cmd[cmd.index("--endpoint-url") + 1], "https://s3.example.test")
        self.assertEqual(cmd[cmd.index("--profile") + 1], "sig")

if __name__ == "__main__":
    unittest.main()
