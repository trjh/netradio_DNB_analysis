"""The align server's capture identifiers and its MP3 pull (`scripts/align_ids.py`).

`list_streams` names the stems of this disk's MP3s and of the audio bucket's `stream/mp3/`
(one held listing a minute); `ensure_stream` pulls a stem only the bucket holds, whole and
checked by the object's SHA-256 metadata, before the review page's stream audio is served.
All over `tests/bucket_fake.py`: no network, no `aws`.
"""

import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
sys.path.insert(0, SCRIPTS)
sys.path.insert(0, HERE)

import align_ids  # noqa: E402
import bucketio  # noqa: E402
import cache_budget  # noqa: E402
import review_audio  # noqa: E402
from bucket_fake import AUDIO_BUCKET, FakeAws, isolate_caches  # noqa: E402

BODY = b"ID3" + b"\x07" * 3000


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="align-ids-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.fake = isolate_caches(self, os.path.join(self.tmp, "root"), fake=FakeAws())
        self.mp3 = os.path.join(self.tmp, "captures", "mp3")
        os.makedirs(self.mp3)
        with open(os.path.join(self.mp3, "d000-018.mp3"), "wb") as fh:
            fh.write(b"ID3local")
        self.fake.add(AUDIO_BUCKET, "stream/mp3/d100-119.mp3", BODY, sha256=True)
        align_ids._failures.clear()
        self.addCleanup(align_ids._failures.clear)

    def path(self, stem="d100-119"):
        return os.path.join(self.mp3, stem + ".mp3")


class TheStemListing(Base):
    def test_names_a_stem_the_bucket_holds_and_this_disk_does_not(self):
        self.assertEqual(align_ids.list_streams(self.mp3), ["d000-018", "d100-119"])
        self.assertEqual(self.fake.ops("cp-down"), [], "naming a stem never pulls it")

    def test_lists_only_the_disk_under_read_only(self):
        with mock.patch.dict(os.environ, {"NETRADIO_READ_ONLY": "1"}):
            self.assertEqual(align_ids.list_streams(self.mp3), ["d000-018"])
        self.assertEqual(self.fake.calls, [])

    def test_lists_only_the_disk_with_the_bucket_unset(self):
        os.environ.pop("NETRADIO_AUDIO_BUCKET")
        self.assertEqual(align_ids.list_streams(self.mp3), ["d000-018"])
        self.assertEqual(self.fake.calls, [])

    def test_many_requests_within_a_minute_cost_one_listing(self):
        for _ in range(25):
            align_ids.list_streams(self.mp3)
        self.assertEqual(self.fake.ops("list-objects-v2"), ["list-objects-v2"])

    def test_a_bucket_name_that_fails_the_stem_pattern_is_not_named(self):
        self.fake.add(AUDIO_BUCKET, "stream/mp3/..evil.mp3", b"x", sha256=True)
        self.fake.add(AUDIO_BUCKET, "stream/mp3/sub/d1.mp3", b"x", sha256=True)
        self.assertEqual(align_ids.list_streams(self.mp3), ["d000-018", "d100-119"])

    def test_a_symlinked_mp3_is_not_named(self):
        os.symlink(self.path("d000-018"), self.path("d001-019"))
        self.assertNotIn("d001-019", align_ids.list_streams(self.mp3))


class TheStreamDelivery(Base):
    def test_pulls_once_then_serves(self):
        status, result = review_audio.stream_delivery("d100-119", self.mp3)
        self.assertEqual(status, 200)
        self.assertEqual(result["path"], self.path())
        with open(self.path(), "rb") as fh:
            self.assertEqual(fh.read(), BODY)
        status, _result = review_audio.stream_delivery("d100-119", self.mp3)
        self.assertEqual(status, 200)
        self.assertEqual(self.fake.ops("cp-down"), ["cp-down"])

    def test_a_file_on_disk_is_served_with_no_bucket_call_beyond_the_listing(self):
        status, _ = review_audio.stream_delivery("d000-018", self.mp3)
        self.assertEqual(status, 200)
        self.assertEqual(self.fake.ops("cp-down") + self.fake.ops("head-object"), [])

    def test_two_concurrent_requests_share_one_pull(self):
        real = bucketio.fetch_verified

        def slow(*args, **kwargs):
            time.sleep(0.2)
            return real(*args, **kwargs)

        results = []
        with mock.patch.object(bucketio, "fetch_verified", slow):
            threads = [threading.Thread(
                target=lambda: results.append(review_audio.stream_delivery("d100-119", self.mp3)[0]))
                for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(results, [200, 200])
        self.assertEqual(self.fake.ops("cp-down"), ["cp-down"])

    def test_a_hash_mismatch_is_refused_the_partial_removed_nothing_served(self):
        self.fake.served[(AUDIO_BUCKET, "stream/mp3/d100-119.mp3")] = b"ID3" + b"\x08" * 3000
        status, result = review_audio.stream_delivery("d100-119", self.mp3)
        self.assertEqual(status, 502)
        self.assertTrue(result["error"].startswith("stream d100-119: "))
        self.assertIn("sha256 mismatch", result["error"])
        self.assertFalse(os.path.exists(self.path()))
        self.assertEqual([f for f in os.listdir(self.mp3) if f.endswith(".part")], [])

    def test_a_size_mismatch_is_refused(self):
        self.fake.served[(AUDIO_BUCKET, "stream/mp3/d100-119.mp3")] = BODY[:-1]
        status, result = review_audio.stream_delivery("d100-119", self.mp3)
        self.assertEqual(status, 502)
        self.assertIn("size mismatch", result["error"])
        self.assertFalse(os.path.exists(self.path()))

    def test_an_object_with_no_sha256_metadata_is_refused(self):
        self.fake.add(AUDIO_BUCKET, "stream/mp3/d100-119.mp3", BODY, sha256=False)
        status, result = review_audio.stream_delivery("d100-119", self.mp3)
        self.assertEqual(status, 502)
        self.assertIn("no sha256", result["error"])
        self.assertEqual(self.fake.ops("cp-down"), [])

    def test_a_failed_pull_is_answered_from_memory_for_a_minute(self):
        self.fake.served[(AUDIO_BUCKET, "stream/mp3/d100-119.mp3")] = BODY[:-1]
        review_audio.stream_delivery("d100-119", self.mp3)
        review_audio.stream_delivery("d100-119", self.mp3)
        self.assertEqual(self.fake.ops("cp-down"), ["cp-down"])

    def test_nothing_is_pulled_under_read_only(self):
        with mock.patch.dict(os.environ, {"NETRADIO_READ_ONLY": "1"}):
            status, _ = review_audio.stream_delivery("d100-119", self.mp3)
        self.assertEqual(status, 404)
        self.assertEqual(self.fake.calls, [])


class TheMp3Cache(Base):
    def test_registered_where_no_other_process_holds_the_directory_and_the_pull_admitted(self):
        self.assertIsNotNone(align_ids.register_mp3_cache(self.mp3))
        self.assertTrue(align_ids.holds_mp3_dir(self.mp3))
        with mock.patch.object(cache_budget, "reserve", wraps=cache_budget.reserve) as reserve, \
                mock.patch.object(cache_budget, "commit", wraps=cache_budget.commit) as commit:
            self.assertIsNone(align_ids.ensure_stream("d100-119", self.mp3))
        reserve.assert_called_once_with(align_ids.MP3_CACHE, len(BODY))
        commit.assert_called_once_with(align_ids.MP3_CACHE, self.path())

    def test_not_registered_where_another_process_holds_it_and_the_pull_lands_whole(self):
        other = [{"pid": 1, "process": "other", "caches": [{"name": "stream_mp3",
                                                            "dir": self.mp3}]}]
        with mock.patch.object(cache_budget, "other_processes", return_value=other):
            self.assertIsNone(align_ids.register_mp3_cache(self.mp3))
        self.assertFalse(align_ids.holds_mp3_dir(self.mp3))
        with mock.patch.object(cache_budget, "reserve") as reserve:
            self.assertIsNone(align_ids.ensure_stream("d100-119", self.mp3))
        reserve.assert_not_called()
        self.assertTrue(os.path.isfile(self.path()))


class TheBoundary(unittest.TestCase):
    def test_nothing_reads_the_tier_manifest_or_imports_the_audio_cache_module(self):
        for name in ("align_ids.py", "align_inspect.py", "review_audio.py", "audio_bucket.py",
                     "extract_tracks.py", os.path.join("streamalign", "audio.py")):
            with open(os.path.join(SCRIPTS, name), encoding="utf-8") as fh:
                text = fh.read()
            self.assertNotIn("stream-tier.json", text, name)
            self.assertNotIn("import audio_cache", text, name)
            self.assertNotIn("stream_tier", text, name)


if __name__ == "__main__":
    unittest.main()
