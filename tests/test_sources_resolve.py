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
track_mix = import_or_skip("streamalign.track_mix") if np else None
inspect_worker = import_or_skip("streamalign.inspect_worker") if np else None
from bucket_fake import FakeProc  # noqa: E402
from streamalign import mystery  # noqa: E402

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
        self.root_dir = os.path.join(self.tmp, "checkout")
        os.makedirs(self.root_dir)
        patcher = mock.patch.object(sources_resolve, "ROOT_DIR", self.root_dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.assertIsNotNone(sources_resolve.register_cache())
        sources_resolve._verified.clear()
        self.addCleanup(sources_resolve._verified.clear)

    def add(self, name, body=FLAC, sha256=True):
        self.fake.add(AUDIO_BUCKET, "sources/" + name, body, sha256=sha256)

    @contextlib.contextmanager
    def quiet(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            yield

    def record(self, choices):
        with open(os.path.join(self.root_dir, "source-versions.json"), "w") as fh:
            json.dump(choices, fh)


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


class TheVersions(Base):
    """Two files at the winning rung can be two real versions: the recorded choice picks one,
    and with none the refusal says exactly how to record it (2026-11-18)."""

    TIED = ("017-Erykah Badu - On & On (Blu Mar Ten Remix) - 6.41.FkIPqQEjxw.opus",
            "017-Erykah Badu - On & On (Blu Mar Ten Remix) - 7.52.JCv3Vs_CFjQ.opus")

    def setUp(self):
        super().setUp()
        for name in self.TIED:
            touch(self.src, name)

    def test_a_tie_with_a_recorded_choice_resolves_to_it(self):
        self.record({"017": self.TIED[1]})
        self.assertEqual(sources_resolve.resolve(17), os.path.join(self.src, self.TIED[1]))
        self.assertEqual(sources_resolve.originals()[0][("track", 17)], self.TIED[1])

    def test_a_tie_without_a_choice_refuses_saying_how_to_choose(self):
        with self.assertRaises(sources_resolve.Ambiguous) as caught:
            sources_resolve.resolve(17)
        self.assertEqual(
            str(caught.exception),
            "track 017 has 2 versions: %s, %s. Choose one: add \"017\": \"<file name>\" to "
            "source-versions.json in this repo" % self.TIED)

    def test_a_recorded_name_that_matches_no_file_is_an_error_naming_the_setting(self):
        self.record({"017": "017-not-here.opus"})
        with self.assertRaises(sources_resolve.VersionError) as caught:
            sources_resolve.resolve(17)
        self.assertIn("source-versions.json", str(caught.exception))
        self.assertIn("017-not-here.opus", str(caught.exception))
        self.assertIn('"017"', str(caught.exception))

    def test_listing_keeps_both_versions(self):
        chosen, refused = sources_resolve.originals()
        self.assertNotIn(("track", 17), chosen)
        self.assertEqual(refused[("track", 17)], sorted(self.TIED))
        self.assertEqual(sources_resolve.list_originals(),
                         [{"num": 17, "name": os.path.splitext(sorted(self.TIED)[0])[0][4:]}])

    def test_a_recorded_version_this_disk_lacks_is_pulled_never_its_sibling(self):
        os.remove(os.path.join(self.src, self.TIED[1]))
        self.record({"017": self.TIED[1]})
        self.add(self.TIED[1], b"seven-fifty-two")
        path = sources_resolve.resolve(17)
        self.assertEqual(path, os.path.join(self.src, self.TIED[1]))
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), b"seven-fifty-two")

    def test_a_record_that_does_not_read_is_an_error(self):
        with open(os.path.join(self.root_dir, "source-versions.json"), "w") as fh:
            fh.write("[1, 2")
        with self.assertRaises(sources_resolve.VersionError):
            sources_resolve.resolve(17)


class TheSeed(unittest.TestCase):
    def test_the_committed_record_names_the_7_52_version_of_017(self):
        with open(sources_resolve.versions_path(), encoding="utf-8") as fh:
            record = json.load(fh)
        self.assertEqual(record["017"],
                         "017-Erykah Badu - On & On (Blu Mar Ten Remix) - 7.52.JCv3Vs_CFjQ.opus")
        self.assertEqual(sources_resolve.stem_key(record["017"]), ("track", 17))


class TheMessages(Base):
    """A pull's failure is said on stderr, never stdout: in the inspector's DSP child stdout is
    the JSON channel. A master the bucket holds with no FLAC copy has its own reason."""

    def test_a_failed_pull_prints_to_stderr_only(self):
        self.add("084-x.flac")
        self.fake.fail_next("head-object", FakeProc(returncode=255, stderr="Could not connect"))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertIsNone(sources_resolve.resolve(84))
        self.assertEqual(out.getvalue(), "")
        self.assertIn("084-x.flac", err.getvalue())

    def test_a_master_with_no_flac_copy_is_not_called_absent(self):
        self.add("085-m.wv", b"wavpack")
        path, failed = sources_resolve.pull_stem(("track", 85))
        self.assertIsNone(path)
        self.assertEqual(failed[0], 503)
        self.assertIn("085-m.wv", failed[1])
        self.assertIn("no FLAC copy", failed[1])
        self.assertNotIn("not on this disk or in the audio bucket", failed[1])
        self.assertEqual(self.fake.ops("cp-down"), [])

    def test_a_refused_mystery_is_never_dropped_in_silence(self):
        touch(self.src, "Mystery Track 6.wav")
        touch(self.src, "Mystery Track 6 v2.wav")
        meta = os.path.join(self.tmp, "track-metadata.json")
        with open(meta, "w") as fh:
            json.dump({"tracks": {"70": {"title": "Mystery Track 6"}}}, fh)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(mystery.searchable(self.src, meta), [])
        self.assertIn("two clips, choose one", err.getvalue())
        self.assertIn("Mystery Track 6 v2.wav", err.getvalue())


@unittest.skipUnless(inspect_worker, "needs numpy and the audio stack")
class TheWorkerChannel(Base):
    def test_a_failed_pull_leaves_stdout_json_only_and_the_reply_carries_the_reason(self):
        self.add("086-w.flac")
        self.fake.fail_next("head-object", FakeProc(returncode=255, stderr="Could not connect"))
        req = json.dumps({"op": "slice", "stem": "d000-018", "orig": 86,
                          "stream_t": 1, "orig_t": 1}) + "\n"
        out = io.StringIO()
        with mock.patch.object(inspect_worker._audio, "capture_nameable", return_value=True), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            inspect_worker.serve(self.src, stdin=io.StringIO(req),
                                 cache=inspect_worker.PairCache(loader=lambda *a: None))
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        reply = json.loads(lines[0])
        self.assertIn("Could not connect", reply["error"])


@unittest.skipUnless(track_mix and inspect_worker, "needs numpy and the audio stack")
class TheDecodePin(Base):
    """An original is pinned while a reader decodes it."""

    def test_the_inspector_pins_the_original_while_its_pair_loads(self):
        path = touch(self.src, "087-p.flac")
        seen = []
        cache = inspect_worker.PairCache(
            loader=lambda s, o, r: seen.append(sources_resolve._in_use(o)))
        cache.get("d000-018", path, 1.0)
        self.assertEqual(seen, [True])
        self.assertFalse(sources_resolve._in_use(path))

    def test_track_mix_pins_the_original_while_it_decodes(self):
        path = touch(self.src, "088-p.flac")
        seen = []

        def fake_load(src, **_kw):
            if src == path:
                seen.append(sources_resolve._in_use(path))
            return np.zeros(16000 * 200, dtype="float32")
        meta = {"88": {"master_begin_seconds": 0.0, "master_end_seconds": 60.0,
                       "source_files": ["d000-018.wav"]}}
        with mock.patch.object(track_mix._audio, "load_audio", fake_load), \
                mock.patch.object(track_mix._gt, "resolve_starts", return_value={}), \
                mock.patch.object(track_mix, "_select_capture", return_value=("d000-018", 0.0)), \
                mock.patch.object(track_mix, "chroma_dtw_rate", return_value={"rate": 1.0}), \
                mock.patch.object(track_mix, "track_sync_groundtruth", return_value={}):
            track_mix.align_track(88, meta, self.src)
        self.assertEqual(seen, [True])


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

    def test_a_definite_mismatch_is_held_and_a_failed_head_is_not(self):
        os.environ["NETRADIO_SOURCES_FLAC_CACHE_GB"] = "none"
        sources_resolve.register_cache()
        body = b"m" * 200
        path = touch(self.src, "089-mismatch.flac", body)
        self.add("089-mismatch.flac", body, sha256=hashlib.sha256(b"else").hexdigest())
        other = touch(self.src, "089-blip.flac", body)
        self.add("089-blip.flac", body)
        self.assertFalse(sources_resolve.backed(path))
        self.assertFalse(sources_resolve.backed(path))
        self.assertEqual(self.fake.ops("head-object"), ["head-object"])
        self.fake.fail_next("head-object", FakeProc(returncode=255, stderr="Could not connect"))
        self.assertFalse(sources_resolve.backed(other))
        self.assertTrue(sources_resolve.backed(other), "a failed head is asked again")

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
        self.assertEqual([n for n, _ in pool],
                         ["070-pair.wv", "071-a.mp3", "071-b.mp3", "072-only-there.flac"],
                         "two versions with no recorded choice are both in the pool")
        self.assertEqual(sorted(loaded),
                         ["070-pair.wv", "071-a.mp3", "071-b.mp3", "072-only-there.flac"])


class TheAcoustidListing(Base):
    def test_one_file_per_stem_by_the_resolver(self):
        import acoustid_check
        touch(self.src, "070-pair.wv")
        touch(self.src, "070-pair.flac")
        touch(self.src, "071-a.mp3")
        touch(self.src, "071-b.mp3")
        self.add("072-only-there.flac")
        seen = []

        def fake_fingerprint(path):
            seen.append(os.path.basename(path))
            return None
        os.environ["ACOUSTID_DEV_API_KEY"] = "test-key"
        self.addCleanup(os.environ.pop, "ACOUSTID_DEV_API_KEY", None)
        with mock.patch.object(acoustid_check, "fingerprint", fake_fingerprint), self.quiet():
            acoustid_check.main([])
        self.assertEqual(seen, ["070-pair.wv", "071-a.mp3", "071-b.mp3", "072-only-there.flac"],
                         "two versions with no recorded choice are both checked")


if __name__ == "__main__":
    unittest.main()
