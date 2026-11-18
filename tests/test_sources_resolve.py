"""The one originals resolver: the rule, the `sources_flac` cache, and the pull on demand.

Everything runs over `tests/bucket_fake.py` and its cache isolation helper: no network, no
`aws`, no ffmpeg. The rule (2026-10-28): the uncompressed file, else the lossless master, else
the flac, else a lossy file, and two files at the winning rung refused with both named.
"""

import test_suite_env  # noqa: F401  (no test reaches a real audio bucket, however it is run)
import contextlib
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))
sys.path.insert(0, HERE)

import cache_budget  # noqa: E402
import missing_sources  # noqa: E402
import sources_resolve  # noqa: E402
from bucket_fake import AUDIO_BUCKET, FakeAws, isolate_caches  # noqa: E402
from optional_imports import import_or_skip  # noqa: E402

np = import_or_skip("numpy")
identify_by_chroma = import_or_skip("identify_by_chroma") if np else None

FLAC = b"fLaC" + b"\x03" * 300


def touch(directory, name, body=b"x" * 32):
    path = os.path.join(directory, name)
    with open(path, "wb") as fh:
        fh.write(body)
    return path


class TheRule(unittest.TestCase):
    def pick(self, *names):
        return sources_resolve.choose(names, "original 072")

    def test_the_uncompressed_file_wins(self):
        self.assertEqual(self.pick("072-a.flac", "072-a.wv", "072-a.wav", "072-a.mp3"),
                         "072-a.wav")
        self.assertEqual(self.pick("072-a.flac", "072-a.au"), "072-a.au")

    def test_then_the_lossless_master(self):
        self.assertEqual(self.pick("072-a.flac", "072-a.wv", "072-a.mp3"), "072-a.wv")
        self.assertEqual(self.pick("072-a.flac", "072-a.aif"), "072-a.aif")

    def test_then_the_flac_then_a_lossy_file(self):
        self.assertEqual(self.pick("072-a.mp3", "072-a.flac"), "072-a.flac")
        self.assertEqual(self.pick("072-a.opus"), "072-a.opus")

    def test_two_at_the_winning_rung_stop_naming_both(self):
        with self.assertRaises(sources_resolve.Ambiguous) as caught:
            self.pick("072-b.mp3", "072-a.m4a")
        self.assertEqual(caught.exception.names, ["072-a.m4a", "072-b.mp3"])
        self.assertIn("072-a.m4a", str(caught.exception))
        self.assertIn("072-b.mp3", str(caught.exception))
        self.assertIsInstance(caught.exception, ValueError)

    def test_two_at_a_losing_rung_do_not_matter(self):
        self.assertEqual(self.pick("072-a.flac", "072-b.mp3", "072-c.mp3"), "072-a.flac")

    def test_a_file_that_is_not_an_original_is_never_a_candidate(self):
        self.assertIsNone(self.pick("072-a.jpg", "072-a.mp4", "072-a.null"))
        self.assertIsNone(sources_resolve.stem_key("folder.jpg"))

    def test_stems(self):
        self.assertEqual(sources_resolve.stem_key("072-x.flac"), ("track", 72))
        self.assertEqual(sources_resolve.stem_key("Mystery Track 4.wv"), ("mystery", 4))
        self.assertEqual(sources_resolve.stem_key("promo.mp3"), ("name", "promo"))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sources-resolve-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.src = os.path.join(self.tmp, "sources")
        os.makedirs(self.src)
        self.fake = isolate_caches(self, os.path.join(self.tmp, "root"), fake=FakeAws())
        os.environ["NETRADIO_SOURCES_FLAC_CACHE_DIR"] = self.src
        self.assertIsNotNone(sources_resolve.register_cache())
        sources_resolve._verified.clear()
        self.addCleanup(sources_resolve._verified.clear)

    def add(self, name, body=FLAC, sha256=True):
        self.fake.add(AUDIO_BUCKET, "sources/" + name, body, sha256=sha256)

    def quiet(self):
        return contextlib.redirect_stdout(io.StringIO())


class ThePull(Base):
    def test_a_local_candidate_is_returned_with_no_bucket_call(self):
        touch(self.src, "072-a.flac")
        self.add("072-a.flac")
        self.assertEqual(sources_resolve.resolve(72), os.path.join(self.src, "072-a.flac"))
        self.assertEqual(self.fake.calls, [])

    def test_a_missing_original_is_pulled_checked_and_admitted(self):
        self.add("073-b.flac")
        path = sources_resolve.resolve(73)
        self.assertEqual(path, os.path.join(self.src, "073-b.flac"))
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), FLAC)
        self.assertEqual(self.fake.ops("cp-down"), ["cp-down"])
        with open(cache_budget.events_path(), encoding="utf-8") as fh:
            self.assertIn("073-b.flac", fh.read())

    def test_a_master_in_the_bucket_is_pulled_as_its_flac(self):
        self.add("074-c.wv", b"wavpack")
        self.add("074-c.flac")
        self.assertEqual(sources_resolve.resolve(74), os.path.join(self.src, "074-c.flac"))
        self.assertFalse(os.path.exists(os.path.join(self.src, "074-c.wv")))

    def test_two_files_at_one_rung_in_the_bucket_are_refused(self):
        self.add("075-a.opus", b"o1")
        self.add("075-b.opus", b"o2")
        with self.assertRaises(sources_resolve.Ambiguous):
            sources_resolve.resolve(75)
        self.assertEqual(self.fake.ops("cp-down"), [])

    def test_a_hash_mismatch_lands_nothing(self):
        self.add("076-d.flac", sha256="0" * 64)
        with self.quiet():
            self.assertIsNone(sources_resolve.resolve(76))
        self.assertEqual(os.listdir(self.src), [])

    def test_pull_false_never_pulls(self):
        self.add("077-e.flac")
        self.assertIsNone(sources_resolve.resolve(77, pull=False))
        self.assertTrue(sources_resolve.nameable(77))
        self.assertEqual(self.fake.ops("cp-down"), [])

    def test_no_audio_bucket_pulls_nothing_and_reports_missing(self):
        os.environ.pop("NETRADIO_AUDIO_BUCKET")
        self.add("078-f.flac")
        self.assertIsNone(sources_resolve.resolve(78))
        self.assertFalse(sources_resolve.nameable(78))
        path, failed = sources_resolve.fetch("078-f.flac")
        self.assertIsNone(path)
        self.assertEqual(failed[0], 404)
        self.assertIn("no audio bucket", failed[1])
        self.assertEqual(self.fake.calls, [])

    def test_read_only_pulls_nothing(self):
        os.environ["NETRADIO_READ_ONLY"] = "1"
        self.add("079-g.flac")
        self.assertIsNone(sources_resolve.resolve(79))
        self.assertEqual(self.fake.calls, [])

    def test_a_directory_that_is_not_the_cache_is_never_pulled_into(self):
        other = os.path.join(self.tmp, "elsewhere")
        os.makedirs(other)
        self.add("080-h.flac")
        self.assertIsNone(sources_resolve.resolve(80, other))
        self.assertEqual(os.listdir(other), [])
        self.assertEqual(self.fake.calls, [])

    def test_the_originals_listing_joins_the_bucket(self):
        touch(self.src, "081-here.wv")
        touch(self.src, "081-here.flac")
        self.add("082-there.wv", b"wavpack")
        self.add("082-there.flac")
        self.add("083-two.mp3", b"a")
        self.add("083-two2.mp3", b"b")
        chosen, refused = sources_resolve.originals()
        self.assertEqual(chosen[("track", 81)], "081-here.wv")
        self.assertEqual(chosen[("track", 82)], "082-there.flac")
        self.assertEqual(refused[("track", 83)], ["083-two.mp3", "083-two2.mp3"])


class TheCache(Base):
    """Nothing the bucket does not hold verified is ever evicted."""

    def setUp(self):
        super().setUp()
        os.environ["NETRADIO_SOURCES_FLAC_CACHE_GB"] = "0.0000001"     # 100 bytes: over cap
        sources_resolve.register_cache()

    def test_an_unpushed_directory_loses_nothing(self):
        for n in range(3):
            touch(self.src, "%03d-x.flac" % (90 + n), b"y" * 200)
        cache_budget.run_eviction(sources_resolve.CACHE)
        self.assertEqual(len(os.listdir(self.src)), 3)

    def test_with_no_bucket_every_file_is_pinned(self):
        os.environ.pop("NETRADIO_AUDIO_BUCKET")
        body = b"z" * 200
        touch(self.src, "093-x.flac", body)
        self.add("093-x.flac", body)
        cache_budget.run_eviction(sources_resolve.CACHE)
        self.assertEqual(os.listdir(self.src), ["093-x.flac"])

    def test_only_a_file_the_bucket_holds_verified_is_evicted(self):
        body = b"v" * 200
        touch(self.src, "094-verified.flac", body)
        touch(self.src, "095-wrong-hash.flac", body)
        touch(self.src, "096-no-meta.flac", body)
        touch(self.src, "097-other-size.flac", body)
        self.add("094-verified.flac", body)
        self.add("095-wrong-hash.flac", body, sha256=hashlib.sha256(b"else").hexdigest())
        self.add("096-no-meta.flac", body, sha256=False)
        self.add("097-other-size.flac", body + b"!")
        cache_budget.run_eviction(sources_resolve.CACHE)
        self.assertEqual(sorted(os.listdir(self.src)),
                         ["095-wrong-hash.flac", "096-no-meta.flac", "097-other-size.flac"])

    def test_a_file_being_served_is_pinned(self):
        body = b"s" * 200
        path = touch(self.src, "098-served.flac", body)
        self.add("098-served.flac", body)
        with sources_resolve.pinned(path):
            cache_budget.run_eviction(sources_resolve.CACHE)
            self.assertTrue(os.path.exists(path))
        cache_budget.run_eviction(sources_resolve.CACHE)
        self.assertFalse(os.path.exists(path))

    def test_the_verdict_is_held_until_the_file_changes(self):
        os.environ["NETRADIO_SOURCES_FLAC_CACHE_GB"] = "none"
        sources_resolve.register_cache()
        body = b"h" * 200
        path = touch(self.src, "099-held.flac", body)
        self.add("099-held.flac", body)
        self.assertTrue(sources_resolve.backed(path))
        self.assertTrue(sources_resolve.backed(path))
        self.assertEqual(self.fake.ops("head-object"), ["head-object"])
        touch(self.src, "099-held.flac", b"H" * 200)
        self.assertFalse(sources_resolve.backed(path))


class TheInventory(unittest.TestCase):
    """missing_sources.py reads this disk by the resolver's rule, and never pulls."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="inventory-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.src = os.path.join(self.tmp, "sources")
        os.makedirs(self.src)
        self.fake = isolate_caches(self, os.path.join(self.tmp, "root"), fake=FakeAws())
        os.environ["NETRADIO_SOURCES_FLAC_CACHE_DIR"] = self.src
        sources_resolve.register_cache()
        self.meta = os.path.join(self.tmp, "track-metadata.json")
        with open(self.meta, "w") as fh:
            json.dump({"tracks": {
                "65": {"artist": "Klute", "title": "Make Me Feel"},
                "66": {"artist": "Somebody", "title": "Elsewhere"}}}, fh)

    def test_a_master_only_stem_is_held_and_nothing_is_pulled(self):
        touch(self.src, "065-Klute - Make Me Feel.wv")
        self.fake.add(AUDIO_BUCKET, "sources/066-Somebody - Elsewhere.flac", FLAC, sha256=True)
        inv = missing_sources.inventory(self.meta, self.src)
        by = {r["track"]: r for r in inv["tracks"]}
        self.assertEqual(by[65]["status"], "have")
        self.assertEqual(by[65]["ext"], "wv")
        self.assertEqual(by[66]["status"], "missing")
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(sorted(os.listdir(self.src)), ["065-Klute - Make Me Feel.wv"])

    def test_the_better_rung_is_the_file_reported(self):
        touch(self.src, "065-Klute - Make Me Feel.flac", b"f" * 64)
        touch(self.src, "065-Klute - Make Me Feel.wv", b"w" * 32)
        inv = missing_sources.inventory(self.meta, self.src)
        self.assertEqual(inv["tracks"][0]["file"], "065-Klute - Make Me Feel.wv")


@unittest.skipUnless(identify_by_chroma, "needs numpy and the audio stack")
class ThePool(Base):
    """The chroma pool holds one original per stem, chosen by the resolver."""

    def test_one_per_stem_the_bucket_joins_and_two_at_a_rung_are_left_out(self):
        touch(self.src, "070-pair.wv")
        touch(self.src, "070-pair.flac")
        touch(self.src, "Mystery Track 4.wav")
        touch(self.src, "071-a.mp3")
        touch(self.src, "071-b.mp3")
        self.add("072-only-there.flac")
        loaded = []

        def fake_load(path, **_kw):
            loaded.append(os.path.basename(path))
            return np.zeros(int(61 * identify_by_chroma._audio.SR), dtype="float32")
        with mock.patch.object(identify_by_chroma._audio, "load_audio", fake_load), \
                mock.patch.object(identify_by_chroma, "chroma", lambda y: "c"), \
                self.quiet():
            pool = identify_by_chroma.load_pool(self.src)
        self.assertEqual([n for n, _ in pool], ["070-pair.wv", "072-only-there.flac"])
        self.assertEqual(sorted(loaded), ["070-pair.wv", "072-only-there.flac"])


class TheAcoustidListing(Base):
    def test_one_file_per_stem_by_the_resolver(self):
        import acoustid_check
        touch(self.src, "070-pair.wv")
        touch(self.src, "070-pair.flac")
        self.add("072-only-there.flac")
        seen = []

        def fake_fingerprint(path):
            seen.append(os.path.basename(path))
            return None
        os.environ["ACOUSTID_DEV_API_KEY"] = "test-key"
        self.addCleanup(os.environ.pop, "ACOUSTID_DEV_API_KEY", None)
        with mock.patch.object(acoustid_check, "fingerprint", fake_fingerprint), self.quiet():
            acoustid_check.main([])
        self.assertEqual(seen, ["070-pair.wv", "072-only-there.flac"])


if __name__ == "__main__":
    unittest.main()
