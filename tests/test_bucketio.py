"""bucketio: the bucket value, listing, head, fetch_verified, put and the timeouts. All offline.

The aws CLI never runs: `bucketio._run`, the module's one subprocess seam, is swapped for
`bucket_fake.FakeAws`, which holds objects in memory and speaks the JSON the real CLI prints.
"""

import copy
import errno
import hashlib
import json
import os
import pickle
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))
sys.path.insert(0, HERE)

import bucketio  # noqa: E402
from bucket_fake import FakeAws, FakeProc, timeout_error  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.cli = os.path.join(self.tmp, "aws")
        with open(self.cli, "w") as fh:
            fh.write("#!/bin/sh\nexit 1\n")
        os.chmod(self.cli, 0o755)
        self.aws = FakeAws().install(self)
        bucketio._digests.clear()
        self.addCleanup(bucketio._digests.clear)
        self.bucket = bucketio.Bucket("b", aws_cli=self.cli)

    def file(self, name, body):
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as fh:
            fh.write(body)
        return path


class TestBucketValue(Base):
    def test_no_name_is_refused(self):
        for name in ("", "   ", None):
            with self.assertRaises(bucketio.BucketError):
                bucketio.Bucket(name, aws_cli=self.cli)

    def test_no_cli_is_refused(self):
        with self.assertRaises(bucketio.BucketError):
            bucketio.Bucket("b", aws_cli=os.path.join(self.tmp, "missing"))
        with mock.patch.object(bucketio.shutil, "which", return_value=None):
            with self.assertRaises(bucketio.BucketError):
                bucketio.Bucket("b")

    def test_the_cli_on_the_path_is_used_when_none_is_named(self):
        with mock.patch.dict(os.environ, {"PATH": self.tmp}):
            self.assertEqual(bucketio.Bucket("b").cli, self.cli)

    def test_defining_a_bucket_runs_no_command(self):
        bucketio.Bucket("b", endpoint="https://s3.example.test", profile="p", aws_cli=self.cli)
        self.assertEqual(self.aws.calls, [])

    def test_endpoint_and_profile_flags_only_when_set(self):
        self.assertEqual(self.bucket.argv(["x"]), [self.cli, "x"])
        full = bucketio.Bucket("b", endpoint="https://s3.example.test", profile="p",
                               aws_cli=self.cli)
        self.assertEqual(full.argv(["x"]), [self.cli, "--endpoint-url",
                                            "https://s3.example.test", "--profile", "p", "x"])


class TestListing(Base):
    def test_keys_and_sizes_across_pages(self):
        self.aws.page_size = 2
        for i in range(5):
            self.aws.add("b", "p/k%d" % i, b"x" * i)
        self.aws.add("b", "other/k", b"x")
        listed, err = bucketio.listing(self.bucket, "p/")
        self.assertIsNone(err)
        self.assertEqual({k: v.size for k, v in listed.items()},
                         {"p/k%d" % i: i for i in range(5)})
        self.assertEqual(listed["p/k1"].etag, hashlib.md5(b"x").hexdigest())  # quotes gone
        self.assertEqual(len(self.aws.ops("list-objects-v2")), 3)
        self.assertIn("--starting-token", self.aws.calls[1][0])
        first = self.aws.calls[0][0]
        # one page per command: the CLI stops after --max-items and prints a NextToken
        self.assertEqual(first[first.index("--max-items") + 1], str(bucketio.LIST_PAGE_KEYS))

    def test_one_list_per_prefix_per_minute(self):
        self.aws.add("b", "p/a", b"1")
        clock = [1000.0]
        with mock.patch.object(bucketio.time, "monotonic", lambda: clock[0]):
            bucketio.listing(self.bucket, "p/")
            bucketio.listing(self.bucket, "p/")
            clock[0] += 30
            bucketio.listing(self.bucket, "p/")
            self.assertEqual(len(self.aws.ops("list-objects-v2")), 1)
            bucketio.listing(self.bucket, "q/")                     # another prefix asks
            self.assertEqual(len(self.aws.ops("list-objects-v2")), 2)
            clock[0] += 31
            bucketio.listing(self.bucket, "p/")                     # past the minute: asks
            self.assertEqual(len(self.aws.ops("list-objects-v2")), 3)

    def test_max_age_zero_always_asks(self):
        bucketio.listing(self.bucket, "p/", max_age=0)
        bucketio.listing(self.bucket, "p/", max_age=0)
        self.assertEqual(len(self.aws.ops("list-objects-v2")), 2)

    def test_max_age_zero_holds_nothing(self):
        self.aws.add("b", "p/a", b"1")
        listed, err = bucketio.listing(self.bucket, "fresh/", max_age=0)
        self.assertEqual((listed, err), ({}, None))
        self.assertNotIn((self.bucket, "fresh/"), bucketio._listings)
        self.assertNotIn((self.bucket, "fresh/"), bucketio._listing_locks)

    def test_a_listing_that_never_ends_is_an_error(self):
        self.aws.page_size = 1
        for i in range(5):
            self.aws.add("b", "p/k%d" % i, b"x")
        with mock.patch.object(bucketio, "LIST_MAX_PAGES", 3):
            listed, err = bucketio.listing(self.bucket, "p/")
        self.assertIsNone(listed)
        self.assertIn("stopped after 3 pages", err)
        self.assertEqual(len(self.aws.ops("list-objects-v2")), 3)
        self.assertNotIn((self.bucket, "p/"), bucketio._listings)   # not held

    def test_a_failed_listing_is_reported_and_not_held(self):
        self.aws.fail_next("list-objects-v2", FakeProc(returncode=255, stderr="AccessDenied"))
        listed, err = bucketio.listing(self.bucket, "p/")
        self.assertIsNone(listed)
        self.assertIn("AccessDenied", err)
        listed, err = bucketio.listing(self.bucket, "p/")
        self.assertEqual((listed, err), ({}, None))                 # asked again, now empty

    def test_a_put_drops_the_held_listing(self):
        bucketio.listing(self.bucket, "p/")
        bucketio.put(self.bucket, self.file("f", b"abc"), "p/f")
        listed, _ = bucketio.listing(self.bucket, "p/")
        self.assertIn("p/f", listed)


class TestHead(Base):
    def test_size_sha256_and_etag(self):
        self.aws.add("b", "k", b"abc", meta={"SHA256": "ABCDEF"})
        got, err = bucketio.head(self.bucket, "k")
        self.assertIsNone(err)
        self.assertEqual(got, bucketio.Head(3, "abcdef", hashlib.md5(b"abc").hexdigest()))

    def test_absent_is_none_none(self):
        self.assertEqual(bucketio.head(self.bucket, "k"), (None, None))

    def test_only_the_head_404_means_absent(self):
        # Other errors that mention a 404 or "Not Found" (a wrong endpoint behind a proxy, an
        # error from another operation) are no answer, never "the object is not there".
        for stderr in ("Not Found", "NoSuchKey", "proxy: 404 Not Found",
                       "An error occurred (404) when calling the ListObjectsV2 operation"):
            self.aws.fail_next("head-object", FakeProc(returncode=254, stderr=stderr))
            got, err = bucketio.head(self.bucket, "k")
            self.assertIsNone(got)
            self.assertTrue(err, stderr)
            self.aws.add("b", "k", b"audio", sha256=True)
            self.aws.fail_next("head-object", FakeProc(returncode=254, stderr=stderr))
            self.assertNotIn("no such object",
                             bucketio.fetch_verified(self.bucket, "k",
                                                     os.path.join(self.tmp, "f")))
            del self.aws.objects[("b", "k")]

    def test_no_answer_is_a_detail(self):
        self.aws.fail_next("head-object", FakeProc(returncode=255, stderr="Could not connect"))
        got, err = bucketio.head(self.bucket, "k")
        self.assertIsNone(got)
        self.assertIn("Could not connect", err)
        self.aws.fail_next("head-object", timeout_error())
        got, err = bucketio.head(self.bucket, "k")
        self.assertIsNone(got)
        self.assertTrue(err)

    def test_no_sha256_metadata_is_empty(self):
        self.aws.add("b", "k", b"abc")
        self.assertEqual(bucketio.head(self.bucket, "k")[0].sha256, "")


class TestFetchVerified(Base):
    def dest(self):
        return os.path.join(self.tmp, "out", "f.wav")

    def leftovers(self):
        folder = os.path.join(self.tmp, "out")
        return [n for n in os.listdir(folder) if n.endswith(".part")] if os.path.isdir(folder) else []

    def test_nothing_passed_heads_once_then_copies(self):
        self.aws.add("b", "k", b"audio", sha256=True)
        self.assertIsNone(bucketio.fetch_verified(self.bucket, "k", self.dest()))
        self.assertEqual(self.aws.ops(), ["head-object", "cp-down"])
        with open(self.dest(), "rb") as fh:
            self.assertEqual(fh.read(), b"audio")
        self.assertEqual(self.leftovers(), [])

    def test_the_copy_lands_in_a_part_file_beside_dest(self):
        self.aws.add("b", "k", b"audio", sha256=True)
        bucketio.fetch_verified(self.bucket, "k", self.dest())
        target = self.aws.calls[-1][0][3]
        self.assertEqual(os.path.dirname(target), os.path.dirname(self.dest()))
        self.assertTrue(target.endswith(".part"))
        self.assertTrue(os.path.basename(target).startswith("f.wav.bucketio-"))

    def test_passed_size_and_hash_need_no_head(self):
        self.aws.add("b", "k", b"audio")
        err = bucketio.fetch_verified(self.bucket, "k", self.dest(), size=5,
                                      sha256=hashlib.sha256(b"audio").hexdigest())
        self.assertIsNone(err)
        self.assertEqual(self.aws.ops(), ["cp-down"])

    def test_size_mismatch_refuses_and_removes_the_part(self):
        self.aws.add("b", "k", b"audio", sha256=True)
        self.aws.served[("b", "k")] = b"audi"
        err = bucketio.fetch_verified(self.bucket, "k", self.dest())
        self.assertIn("size mismatch", err)
        self.assertFalse(os.path.exists(self.dest()))
        self.assertEqual(self.leftovers(), [])

    def test_hash_mismatch_refuses_and_removes_the_part(self):
        self.aws.add("b", "k", b"audio", sha256=True)
        self.aws.served[("b", "k")] = b"AUDIO"
        err = bucketio.fetch_verified(self.bucket, "k", self.dest())
        self.assertIn("sha256 mismatch", err)
        self.assertFalse(os.path.exists(self.dest()))
        self.assertEqual(self.leftovers(), [])

    def test_no_sha256_metadata_is_refused_by_default(self):
        self.aws.add("b", "k", b"audio")
        err = bucketio.fetch_verified(self.bucket, "k", self.dest())
        self.assertIn("no sha256", err)
        self.assertEqual(self.aws.ops(), ["head-object"])            # nothing copied
        self.assertFalse(os.path.exists(self.dest()))

    def test_no_sha256_metadata_is_checked_by_size_when_allowed(self):
        self.aws.add("b", "k", b"audio")
        self.assertIsNone(bucketio.fetch_verified(self.bucket, "k", self.dest(),
                                                  require_sha256=False))
        os.remove(self.dest())
        self.aws.served[("b", "k")] = b"aud"
        err = bucketio.fetch_verified(self.bucket, "k", self.dest(), require_sha256=False)
        self.assertIn("size mismatch", err)

    def test_the_hash_is_still_checked_when_not_required_but_present(self):
        self.aws.add("b", "k", b"audio", sha256=True)
        self.aws.served[("b", "k")] = b"AUDIO"
        err = bucketio.fetch_verified(self.bucket, "k", self.dest(), require_sha256=False)
        self.assertIn("sha256 mismatch", err)

    def test_an_empty_hash_from_the_caller_is_the_callers_fault(self):
        self.aws.add("b", "k", b"audio", sha256=True)
        err = bucketio.fetch_verified(self.bucket, "k", self.dest(), sha256="")
        self.assertIn("caller passed an empty sha256", err)
        self.assertEqual(self.aws.calls, [])                          # nothing asked or copied

    def test_the_clis_own_temp_file_goes_with_a_killed_copy(self):
        # `aws s3 cp` downloads to `<target>.<random>` and renames at the end; a copy killed
        # at its timeout leaves that file, which does not end `.part`.
        self.aws.add("b", "k", b"audio", sha256=True)
        real = self.aws._cp_down

        def killed(args):
            with open(args[3] + ".3fA9c0D1", "wb") as fh:
                fh.write(b"aud")
            raise timeout_error()
        self.aws._cp_down = killed
        err = bucketio.fetch_verified(self.bucket, "k", self.dest())
        self.assertTrue(err)
        self.assertEqual(os.listdir(os.path.join(self.tmp, "out")), [])
        self.aws._cp_down = real
        self.assertIsNone(bucketio.fetch_verified(self.bucket, "k", self.dest()))

    def test_absent_object_is_refused(self):
        err = bucketio.fetch_verified(self.bucket, "k", self.dest())
        self.assertIn("no such object", err)

    def test_a_failed_copy_leaves_nothing(self):
        self.aws.add("b", "k", b"audio", sha256=True)
        self.aws.fail_next("cp-down", FakeProc(returncode=1, stderr="reset by peer"))
        err = bucketio.fetch_verified(self.bucket, "k", self.dest())
        self.assertIn("reset by peer", err)
        self.assertFalse(os.path.exists(self.dest()))
        self.assertEqual(self.leftovers(), [])


class TestPut(Base):
    def test_put_always_writes_the_sha256_metadata(self):
        path = self.file("f", b"abc")
        got, err = bucketio.put(self.bucket, path, "p/f")
        self.assertIsNone(err)
        digest = hashlib.sha256(b"abc").hexdigest()
        self.assertEqual(got.sha256, digest)
        self.assertEqual(self.aws.objects[("b", "p/f")]["meta"], {"sha256": digest})
        cp = self.aws.calls[0][0]
        self.assertEqual(cp[cp.index("--metadata") + 1], "sha256=" + digest)
        self.assertEqual(self.aws.ops(), ["cp-up", "head-object"])

    def test_the_hash_is_always_the_uploaded_bytes(self):
        with self.assertRaises(TypeError):
            bucketio.put(self.bucket, self.file("f", b"abc"), "p/f", sha256="not-a-sha256")

    def test_other_metadata_rides_beside_the_hash_percent_encoded(self):
        path = self.file("f", b"abc")
        got, err = bucketio.put(self.bucket, path, "p/f",
                                meta={"Title": "a, b = c \u00e9", "sha256": "forged",
                                      "bad name": "x"})
        self.assertIsNone(err)
        digest = hashlib.sha256(b"abc").hexdigest()
        meta = self.aws.objects[("b", "p/f")]["meta"]
        self.assertEqual(meta, {"sha256": digest, "title": "a%2C b %3D c %C3%A9"})
        self.assertEqual(got.sha256, digest, "a caller cannot replace the hash")

    def test_the_etag_proves_the_bytes(self):
        # The SHA-256 the head returns is only the metadata the put sent; an upload whose bytes
        # arrived changed (same size) is caught by the ETag.
        self.aws.stored[("b", "p/f")] = b"abd"
        got, err = bucketio.put(self.bucket, self.file("f", b"abc"), "p/f")
        self.assertIsNone(got)
        self.assertIn("etag mismatch", err)

    def test_a_multipart_etag_is_checked_part_by_part(self):
        body = os.urandom(bucketio.MULTIPART_PART_BYTES * 2 + 5)
        got, err = bucketio.put(self.bucket, self.file("big", body), "p/big")
        self.assertIsNone(err)
        self.assertTrue(got.etag.endswith("-3"))
        bad = bytearray(body)
        bad[-1] ^= 1
        self.aws.stored[("b", "p/big")] = bytes(bad)
        got, err = bucketio.put(self.bucket, self.file("big", body), "p/big")
        self.assertIn("etag mismatch", err)

    def test_a_part_size_it_cannot_know_is_refused_not_passed(self):
        body = os.urandom(bucketio.MULTIPART_PART_BYTES + 5)
        path = self.file("big", body)
        sha = hashlib.sha256(body).hexdigest()
        self.aws.fail_next("head-object", FakeProc(stdout=json.dumps(
            {"ContentLength": len(body), "ETag": '"0123-7"', "Metadata": {"sha256": sha}})))
        got, err = bucketio.put(self.bucket, path, "p/big")
        self.assertIsNone(got)
        self.assertIn("cannot prove the bytes", err)

    def test_another_part_size_is_proved_from_the_part_count(self):
        # A CLI set to 6 MiB parts uploads 13 MiB in 3 parts, where the default gives 2.
        mib = bucketio.MIB
        body = os.urandom(13 * mib)
        self.aws.part_bytes = 6 * mib
        got, err = bucketio.put(self.bucket, self.file("big", body), "p/big")
        self.assertIsNone(err)
        self.assertTrue(got.etag.endswith("-3"))
        bad = bytearray(body)
        bad[-1] ^= 1
        self.aws.stored[("b", "p/big")] = bytes(bad)
        got, err = bucketio.put(self.bucket, self.file("big", body), "p/big")
        self.assertIsNone(got)
        self.assertIn("etag mismatch", err)

    def test_a_file_changed_during_the_upload_proves_nothing(self):
        # The digests are taken before the upload; the file is rewritten at the same size
        # before the bytes go up. The second read sees the new bytes, which match the ETag but
        # not the SHA-256 the put stored, so the put is refused.
        mib = bucketio.MIB
        path = self.file("big", os.urandom(12 * mib))
        self.aws.part_bytes = 6 * mib
        real = self.aws._cp_up

        def rewritten(args):
            with open(path, "wb") as fh:
                fh.write(os.urandom(12 * mib))
            return real(args)
        self.aws._cp_up = rewritten
        got, err = bucketio.put(self.bucket, path, "p/big")
        self.assertIsNone(got)
        self.assertEqual(err.kind, bucketio.NOT_PROVED)
        self.assertIn("the file changed during the upload", err)

    def test_a_part_size_under_five_mib_is_not_tried(self):
        mib = bucketio.MIB
        body = os.urandom(12 * mib)
        self.aws.part_bytes = 3 * mib                                # 4 parts: below S3's floor
        got, err = bucketio.put(self.bucket, self.file("big", body), "p/big")
        self.assertIsNone(got)
        self.assertIn("cannot prove the bytes", err)

    def test_a_file_of_exactly_the_threshold_passes_in_either_form(self):
        body = os.urandom(bucketio.MULTIPART_PART_BYTES)
        path = self.file("edge", body)
        got, err = bucketio.put(self.bucket, path, "p/edge")         # plain MD5
        self.assertIsNone(err)
        self.assertNotIn("-", got.etag)
        self.aws.one_part_at_threshold = True                        # one-part multipart
        got, err = bucketio.put(self.bucket, path, "p/edge")
        self.assertIsNone(err)
        self.assertTrue(got.etag.endswith("-1"))
        self.aws.stored[("b", "p/edge")] = os.urandom(len(body))
        got, err = bucketio.put(self.bucket, path, "p/edge")
        self.assertIsNone(got)
        self.assertIn("etag mismatch", err)

    def test_part_size_candidates_are_whole_mib_and_bounded(self):
        mib = bucketio.MIB
        size = 40 * mib + 3                                          # 3 parts: 14..20 MiB
        self.assertEqual(bucketio.part_size_candidates(size, 3),
                         [m * mib for m in (16, 20, 14, 15, 17, 18, 19)])
        for p in bucketio.part_size_candidates(size, 3):
            self.assertEqual(-(-size // p), 3)
        # Two parts of a large file allow hundreds of sizes; the cap holds, round sizes first.
        many = bucketio.part_size_candidates(1000 * mib, 2)
        self.assertEqual(len(many), bucketio.MAX_PART_SIZE_CANDIDATES)
        self.assertEqual(many[:2], [512 * mib, 500 * mib])
        self.assertIn(100 * mib, bucketio.part_size_candidates(150 * mib, 2))
        self.assertEqual(bucketio.part_size_candidates(size, 1), [])
        self.assertEqual(bucketio.part_size_candidates(size, 50), [])  # parts under 5 MiB

    def test_other_part_sizes_cost_one_read(self):
        mib = bucketio.MIB
        body = os.urandom(13 * mib)
        path = self.file("big", body)
        self.aws.part_bytes = 6 * mib
        opened = []
        real_open = open

        def counting_open(p, *a, **kw):
            if p == path:
                opened.append(p)
            return real_open(p, *a, **kw)
        bucketio.digests(path)
        with mock.patch("builtins.open", counting_open):
            _got, err = bucketio.put(self.bucket, path, "p/big")
        self.assertIsNone(err)
        self.assertEqual(len(opened), 2)          # the upload, then one read for every size

    def test_expected_etag_is_the_default_part_size_etag(self):
        part = bucketio.MULTIPART_PART_BYTES
        small = b"abc"
        got = bucketio.digests(self.file("s", small))
        self.assertEqual(bucketio.expected_etag(len(small), got), hashlib.md5(small).hexdigest())
        big = os.urandom(part * 2 + 5)
        got = bucketio.digests(self.file("b", big))
        parts = [hashlib.md5(big[i:i + part]).digest() for i in range(0, len(big), part)]
        self.assertEqual(bucketio.expected_etag(len(big), got),
                         hashlib.md5(b"".join(parts)).hexdigest() + "-3")

    def test_an_unchanged_file_is_hashed_once(self):
        path = self.file("f", b"abc")
        calls = []
        real = bucketio._digest_file

        def counting(p):
            calls.append(p)
            return real(p)
        with mock.patch.object(bucketio, "_digest_file", counting):
            bucketio.sha256_file(path)
            bucketio.put(self.bucket, path, "p/f")
            bucketio.put(self.bucket, path, "p/f")
            self.assertEqual(len(calls), 1)
            with open(path, "ab") as fh:
                fh.write(b"d")                                       # changed: read again
            got, err = bucketio.put(self.bucket, path, "p/f")
            self.assertIsNone(err)
            self.assertEqual(got.sha256, hashlib.sha256(b"abcd").hexdigest())
            self.assertEqual(len(calls), 2)

    def test_a_failed_upload_is_reported_without_a_head(self):
        self.aws.fail_next("cp-up", FakeProc(returncode=1, stderr="denied"))
        got, err = bucketio.put(self.bucket, self.file("f", b"abc"), "p/f")
        self.assertIsNone(got)
        self.assertIn("denied", err)
        self.assertEqual(self.aws.ops(), ["cp-up"])

    def test_a_head_that_disagrees_fails_the_put(self):
        self.aws.fail_next("head-object", FakeProc(stdout='{"ContentLength": 2, "Metadata": {}}'))
        got, err = bucketio.put(self.bucket, self.file("f", b"abc"), "p/f")
        self.assertIsNone(got)
        self.assertIn("size mismatch", err)


class TestFailureKinds(Base):
    def test_a_failure_is_still_its_detail_text(self):
        err = bucketio.Failure(bucketio.NOT_COPIED, "denied")
        self.assertIsInstance(err, str)
        self.assertEqual(err, "denied")
        self.assertEqual(err.detail, "denied")
        self.assertIn("deni", err)
        self.assertEqual("x: %s" % err, "x: denied")
        with self.assertRaises(ValueError):
            bucketio.Failure("maybe", "denied")

    def test_a_failure_survives_pickle_and_copy(self):
        err = bucketio.Failure(bucketio.DIFFERS, "etag mismatch")
        for again in (pickle.loads(pickle.dumps(err)), copy.copy(err), copy.deepcopy(err)):
            self.assertEqual(again, "etag mismatch")
            self.assertEqual(again.kind, bucketio.DIFFERS)

    def test_a_copy_that_says_nothing_is_still_a_true_failure(self):
        self.aws.fail_next("cp-up", FakeProc(returncode=1, stderr="  \n"))
        got, err = bucketio.put(self.bucket, self.file("f", b"abc"), "p/f")
        self.assertIsNone(got)
        self.assertTrue(err)
        self.assertEqual(err, "aws exited 1")

    def test_not_copied_only_when_nothing_was_sent(self):
        _got, err = bucketio.put(self.bucket, os.path.join(self.tmp, "missing"), "p/f")
        self.assertEqual(err.kind, bucketio.NOT_COPIED)
        self.assertEqual(self.aws.calls, [])

    def test_a_failed_copy_may_have_left_an_object(self):
        self.aws.fail_next("cp-up", FakeProc(returncode=1, stderr="denied"))
        _got, err = bucketio.put(self.bucket, self.file("f", b"abc"), "p/f")
        self.assertEqual((err.kind, str(err)), (bucketio.NOT_PROVED, "denied"))
        self.aws.fail_next("cp-up", timeout_error())
        _got, err = bucketio.put(self.bucket, self.file("f", b"abc"), "p/f")
        self.assertEqual(err.kind, bucketio.NOT_PROVED)

    def test_a_failed_copy_drops_the_held_listing(self):
        self.assertEqual(bucketio.listing(self.bucket, "p/"), ({}, None))     # held: empty
        real = self.aws._cp_up

        def landed_then_failed(args):
            real(args)                          # the object lands; the answer is lost
            return FakeProc(returncode=1, stderr="connection reset")
        self.aws._cp_up = landed_then_failed
        _got, err = bucketio.put(self.bucket, self.file("f", b"abc"), "p/f")
        self.assertEqual(err.kind, bucketio.NOT_PROVED)
        listed, _ = bucketio.listing(self.bucket, "p/")
        self.assertIn("p/f", listed)

    def test_not_proved_when_the_head_gives_no_answer(self):
        self.aws.fail_next("head-object", FakeProc(returncode=255, stderr="Could not connect"))
        _got, err = bucketio.put(self.bucket, self.file("f", b"abc"), "p/f")
        self.assertEqual(err.kind, bucketio.NOT_PROVED)
        self.assertIn("Could not connect", err)

    def test_not_proved_when_the_cap_cuts_the_part_sizes(self):
        mib = bucketio.MIB
        body = os.urandom(20 * mib)
        path = self.file("big", body)
        sha = hashlib.sha256(body).hexdigest()
        # 20 MiB in 2 parts allows 10 to 19 MiB: 10 sizes, over a cap of 4.
        self.aws.fail_next("head-object", FakeProc(stdout=json.dumps(
            {"ContentLength": len(body), "ETag": '"%s-2"' % ("0" * 32),
             "Metadata": {"sha256": sha}})))
        with mock.patch.object(bucketio, "MAX_PART_SIZE_CANDIDATES", 4):
            _got, err = bucketio.put(self.bucket, path, "p/big")
        self.assertEqual(err.kind, bucketio.NOT_PROVED)
        self.assertIn("4 of 10 part sizes", err)

    def test_not_proved_when_no_whole_mib_size_fits(self):
        mib = bucketio.MIB
        self.aws.part_bytes = 3 * mib
        _got, err = bucketio.put(self.bucket, self.file("big", os.urandom(12 * mib)), "p/big")
        self.assertEqual(err.kind, bucketio.NOT_PROVED)

    def test_differs(self):
        self.aws.stored[("b", "p/f")] = b"abd"                       # same size, other bytes
        _got, err = bucketio.put(self.bucket, self.file("f", b"abc"), "p/f")
        self.assertEqual(err.kind, bucketio.DIFFERS)
        self.assertIn("etag mismatch", err)
        self.aws.fail_next("head-object", FakeProc(stdout='{"ContentLength": 2, "Metadata": {}}'))
        _got, err = bucketio.put(self.bucket, self.file("f", b"abc"), "p/f")
        self.assertEqual(err.kind, bucketio.DIFFERS)
        mib = bucketio.MIB
        body = os.urandom(13 * mib)                                  # 6 MiB parts, all tried
        self.aws.part_bytes = 6 * mib
        bad = bytearray(body)
        bad[0] ^= 1
        self.aws.stored[("b", "p/big")] = bytes(bad)
        _got, err = bucketio.put(self.bucket, self.file("big", body), "p/big")
        self.assertEqual(err.kind, bucketio.DIFFERS)

    def test_etag_verdict(self):
        got = bucketio.digests(self.file("f", b"abc"))
        md5 = hashlib.md5(b"abc").hexdigest()
        self.assertIsNone(bucketio.etag_verdict(md5, 3, got, None))
        self.assertEqual(bucketio.etag_verdict("0" * 32, 3, got, None), bucketio.DIFFERS)
        self.assertEqual(bucketio.etag_verdict(None, 3, got, None), bucketio.NOT_PROVED)
        self.assertEqual(bucketio.etag_verdict("abc-x", 3, got, None), bucketio.NOT_PROVED)


class TestEtagForm(Base):
    def test_one_bare_form_everywhere(self):
        md5 = hashlib.md5(b"abc").hexdigest()
        got, _ = bucketio.put(self.bucket, self.file("f", b"abc"), "p/f")
        self.assertEqual(got.etag, md5)
        self.assertEqual(bucketio.head(self.bucket, "p/f")[0].etag, md5)
        self.assertEqual(bucketio.listing(self.bucket, "p/")[0]["p/f"].etag, md5)

    def test_quoted_and_bare(self):
        self.assertEqual(bucketio.quoted_etag("abc"), '"abc"')
        self.assertEqual(bucketio.quoted_etag('"abc"'), '"abc"')
        self.assertEqual(bucketio.bare_etag('"abc"'), "abc")
        self.assertEqual(bucketio.bare_etag("abc"), "abc")
        self.assertIsNone(bucketio.quoted_etag(None))
        self.assertIsNone(bucketio.bare_etag('""'))
        self.assertEqual(bucketio.bare_etag(bucketio.quoted_etag("abc-2")), "abc-2")


class TestListingLastModified(Base):
    def test_each_entry_carries_its_last_modified_time(self):
        self.aws.add("b", "p/a", b"1", last_modified="2026-11-23T10:00:00+00:00")
        self.aws.add("b", "p/b", b"22")
        listed, err = bucketio.listing(self.bucket, "p/")
        self.assertIsNone(err)
        self.assertEqual(listed["p/a"], bucketio.Listed(1, hashlib.md5(b"1").hexdigest(),
                                                        "2026-11-23T10:00:00+00:00"))
        self.assertEqual(listed["p/b"].last_modified, FakeAws.LAST_MODIFIED)
        query = self.aws.calls[0][0]
        self.assertIn("LastModified", query[query.index("--query") + 1])

    def test_an_entry_without_one_is_none(self):
        self.aws.fail_next("list-objects-v2", FakeProc(stdout=json.dumps(
            [[["p/a", 1, '"x"']], None])))
        listed, _ = bucketio.listing(self.bucket, "p/")
        self.assertEqual(listed["p/a"], bucketio.Listed(1, "x", None))
        self.assertEqual(bucketio.Listed(1, "x"), bucketio.Listed(1, "x", None))


class TestStagingDir(Base):
    def setUp(self):
        super().setUp()
        self.stage = os.path.join(self.tmp, "stage")
        self.dest = os.path.join(self.tmp, "out", "f.wav")
        self.aws.add("b", "k", b"audio", sha256=True)

    def test_the_part_is_made_in_the_staging_dir(self):
        self.assertIsNone(bucketio.fetch_verified(self.bucket, "k", self.dest,
                                                  staging_dir=self.stage))
        target = self.aws.calls[-1][0][3]
        self.assertEqual(os.path.dirname(target), self.stage)
        self.assertTrue(target.endswith(".part"))
        self.assertTrue(os.path.basename(target).startswith("f.wav.bucketio-"))
        with open(self.dest, "rb") as fh:
            self.assertEqual(fh.read(), b"audio")
        self.assertEqual(os.listdir(self.stage), [])
        self.assertEqual(os.listdir(os.path.dirname(self.dest)), ["f.wav"])

    def test_a_refused_copy_leaves_both_directories_clean(self):
        self.aws.served[("b", "k")] = b"AUDIO"
        err = bucketio.fetch_verified(self.bucket, "k", self.dest, staging_dir=self.stage)
        self.assertIn("sha256 mismatch", err)
        self.assertEqual(os.listdir(self.stage), [])
        self.assertEqual(os.listdir(os.path.dirname(self.dest)), [])

    def test_another_file_system_is_copied_beside_dest_then_renamed(self):
        real = os.replace
        renames = []

        def replace(src, dst):
            renames.append((os.path.dirname(src), dst))
            if os.path.dirname(src) == self.stage:
                raise OSError(errno.EXDEV, "Invalid cross-device link")
            return real(src, dst)
        with mock.patch.object(bucketio.os, "replace", replace):
            self.assertIsNone(bucketio.fetch_verified(self.bucket, "k", self.dest,
                                                      staging_dir=self.stage))
        self.assertEqual(renames[-1], (os.path.dirname(self.dest), self.dest))
        with open(self.dest, "rb") as fh:
            self.assertEqual(fh.read(), b"audio")
        self.assertEqual(os.listdir(self.stage), [])
        self.assertEqual(os.listdir(os.path.dirname(self.dest)), ["f.wav"])


    def test_a_failed_cross_device_copy_is_a_failure(self):
        os.makedirs(os.path.dirname(self.dest))
        with open(self.dest, "wb") as fh:
            fh.write(b"older")                                       # same size, other bytes
        real = os.replace

        def replace(src, dst):
            if os.path.dirname(src) == self.stage:
                raise OSError(errno.EXDEV, "Invalid cross-device link")
            return real(src, dst)

        def full(_src, _dst):
            raise OSError(errno.ENOSPC, "No space left on device")
        with mock.patch.object(bucketio.os, "replace", replace), \
                mock.patch.object(bucketio.shutil, "copyfile", full):
            err = bucketio.fetch_verified(self.bucket, "k", self.dest, staging_dir=self.stage)
        self.assertIn("No space left", err)
        with open(self.dest, "rb") as fh:
            self.assertEqual(fh.read(), b"older")
        self.assertEqual(os.listdir(self.stage), [])
        self.assertEqual(os.listdir(os.path.dirname(self.dest)), ["f.wav"])


class TestFetchSmall(Base):
    def test_size_only_no_head_small_timeout(self):
        self.aws.add("b", "t.jpg", b"jpeg")                          # no sha256 metadata
        dest = os.path.join(self.tmp, "thumbs", "t.jpg")
        self.assertIsNone(bucketio.fetch_small(self.bucket, "t.jpg", dest, 4))
        self.assertEqual(self.aws.ops(), ["cp-down"])
        self.assertEqual(self.aws.calls[0][2], bucketio.TIMEOUTS["small"])
        with open(dest, "rb") as fh:
            self.assertEqual(fh.read(), b"jpeg")

    def test_a_wrong_size_is_refused_and_leaves_nothing(self):
        self.aws.add("b", "t.jpg", b"jpeg")
        dest = os.path.join(self.tmp, "thumbs", "t.jpg")
        self.assertIn("size mismatch", bucketio.fetch_small(self.bucket, "t.jpg", dest, 5))
        self.assertEqual(os.listdir(os.path.dirname(dest)), [])

    def test_a_large_or_unsized_object_is_refused_before_any_call(self):
        dest = os.path.join(self.tmp, "t")
        self.assertIn("over the", bucketio.fetch_small(
            self.bucket, "k", dest, bucketio.SMALL_FETCH_MAX_BYTES + 1))
        self.assertIn("needs the object's size", bucketio.fetch_small(self.bucket, "k", dest,
                                                                      None))
        self.assertEqual(self.aws.calls, [])

    def test_a_staging_dir_works_here_too(self):
        self.aws.add("b", "t.jpg", b"jpeg")
        stage = os.path.join(self.tmp, "stage")
        dest = os.path.join(self.tmp, "thumbs", "t.jpg")
        self.assertIsNone(bucketio.fetch_small(self.bucket, "t.jpg", dest, 4,
                                               staging_dir=stage))
        self.assertEqual(os.path.dirname(self.aws.calls[0][0][3]), stage)
        self.assertEqual(os.listdir(stage), [])


class TestPutTimeout(Base):
    def timeouts(self):
        return [(self.aws._operation(args), t) for args, _cmd, t in self.aws.calls]

    def test_a_small_upload_runs_under_the_small_timeout(self):
        got, err = bucketio.put(self.bucket, self.file("t.jpg", b"jpeg"), "t.jpg",
                                timeout="small")
        self.assertIsNone(err)
        self.assertEqual(self.timeouts(), [("cp-up", bucketio.TIMEOUTS["small"]),
                                           ("head-object", bucketio.TIMEOUTS["head"])])

    def test_the_default_is_the_copy_timeout(self):
        bucketio.put(self.bucket, self.file("f", b"abc"), "f")
        self.assertEqual(self.timeouts()[0], ("cp-up", bucketio.TIMEOUTS["copy"]))

    def test_an_unknown_timeout_raises_before_any_call(self):
        with mock.patch.object(bucketio, "digests") as digests:
            with self.assertRaises(ValueError):
                bucketio.put(self.bucket, self.file("f", b"abc"), "f", timeout="forever")
        digests.assert_not_called()
        self.assertEqual(self.aws.calls, [])


class TestTimeouts(Base):
    def test_each_kind_of_call_runs_under_its_own_timeout(self):
        self.aws.add("b", "k", b"audio", sha256=True)
        bucketio.listing(self.bucket, "", max_age=0)
        bucketio.head(self.bucket, "k")
        bucketio.fetch_verified(self.bucket, "k", os.path.join(self.tmp, "f"),
                                size=5, sha256=hashlib.sha256(b"audio").hexdigest())
        bucketio.put(self.bucket, self.file("g", b"x"), "g")
        seen = {}
        for args, _cmd, timeout in self.aws.calls:
            seen.setdefault(self.aws._operation(args), set()).add(timeout)
        self.assertEqual(seen, {"list-objects-v2": {bucketio.TIMEOUTS["list"]},
                                "head-object": {bucketio.TIMEOUTS["head"]},
                                "cp-down": {bucketio.TIMEOUTS["copy"]},
                                "cp-up": {bucketio.TIMEOUTS["copy"]}})
        self.assertGreater(bucketio.TIMEOUTS["copy"], bucketio.TIMEOUTS["head"])

    def test_run_reports_a_timeout_and_a_missing_binary(self):
        self.aws.fail_next("head-object", timeout_error())
        out, err = bucketio.run(self.bucket, ["s3api", "head-object"], "head")
        self.assertIsNone(out)
        self.assertTrue(err)
        self.aws.fail_next("head-object", FileNotFoundError("no such file: aws"))
        out, err = bucketio.run(self.bucket, ["s3api", "head-object"], "head")
        self.assertIn("no such file", err)

    def test_an_unknown_kind_is_a_programming_error(self):
        with self.assertRaises(KeyError):
            bucketio.run(self.bucket, ["s3", "ls"], "forever")


if __name__ == "__main__":
    unittest.main()
