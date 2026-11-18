"""calibrate.py's `build_cases`: which clean extract a case reads.

The lookup is shared with `selftest.py` deliberately (see `build_cases`' own docstring), so
the choice here is the choice the canary makes too. No audio anywhere: nothing is decoded,
and the extract directory's listing is stubbed to a fixed order, so the choice is the only
thing under test.
"""

import test_suite_env  # noqa: F401  (no test reaches a real audio bucket, however it is run)
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # sibling test helpers
from optional_imports import import_or_skip  # noqa: E402

calibrate = import_or_skip("calibrate")  # None only when an audio library is missing


@unittest.skipIf(calibrate is None, "calibrate.py needs the librosa venv (.venv) — skipping")
class TheCleanExtractChoice(unittest.TestCase):
    """During the wav→flac cut-over both formats sit beside each other for a track: the old
    tool's `.wav` stays, and the next re-cut writes a `.flac` beside it. Raw `os.listdir`
    order is filesystem order, so the same calibration run could read one track's `.wav` and
    another's `.flac`. The content is the same cut either way -- the cost is reproducibility
    -- so the re-cut format wins, and the wav still serves a directory the re-cut has not
    reached yet."""

    def _case(self, files):
        """Run `build_cases` over one track, with the extract directory listing `files` in
        exactly that order (the filesystem's is arbitrary; the choice must not be).
        Returns (the case's extract path, the directory it came from)."""
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        src = os.path.join(tmp, "sources")
        exdir = os.path.join(tmp, "stream_tracks")
        os.makedirs(src)
        os.makedirs(exdir)
        # a path is all build_cases needs of the original: nothing is decoded here
        open(os.path.join(src, "004-Some Original.mp3"), "wb").close()
        tracks = {"4": {"master_begin_seconds": 0, "master_end_seconds": 90,
                       "artist": "A", "title": "B"}}
        real_listdir = os.listdir

        def fake_listdir(path):
            if os.path.basename(os.path.normpath(path)) == "stream_tracks":
                return list(files)
            return real_listdir(path)

        with mock.patch.dict(os.environ, {"NETRADIO_STREAM_TRACKS_CACHE_DIR": exdir}), \
                mock.patch("os.listdir", side_effect=fake_listdir):
            cases = calibrate.build_cases(tracks, {}, src)
        extracts = [c["extract"] for c in cases]
        return extracts, exdir

    def test_the_flac_re_cut_wins_over_a_wav_left_beside_it(self):
        # the wav first, deliberately: taking the first listdir match would read it
        extracts, exdir = self._case(["004 - A - B.wav", "004 - A - B.flac"])
        self.assertEqual(extracts, [os.path.join(exdir, "004 - A - B.flac")],
                         "the re-cut format is the one the tool writes now")

    def test_a_directory_of_only_wavs_still_reads_them(self):
        """An old cut directory the re-cut has not reached yet still serves its extracts."""
        extracts, exdir = self._case(["004 - A - B.wav"])
        self.assertEqual(extracts, [os.path.join(exdir, "004 - A - B.wav")])


if __name__ == "__main__":
    unittest.main()


@unittest.skipIf(calibrate is None, "calibrate.py needs the librosa venv (.venv) — skipping")
class ACutHeldOnlyInTheBucket(unittest.TestCase):
    """A machine that holds no cut for a track, while the audio bucket's `stream/tracks/` does,
    scores the bucket's cut: `build_cases` records it, and `mix_query` fetches it for that case
    alone (never every listed cut at start: --limit cuts the list after `build_cases`)."""

    def setUp(self):
        from bucket_fake import AUDIO_BUCKET, FakeAws, isolate_caches
        import extract_tracks
        self.et = extract_tracks
        self.tmp = tempfile.mkdtemp(prefix="calibrate-bucket-cut-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.fake = isolate_caches(self, os.path.join(self.tmp, "root"), fake=FakeAws())
        self.addCleanup(extract_tracks._bucket_cuts.clear)
        extract_tracks.register_cache()
        self.src = os.path.join(self.tmp, "sources")
        os.makedirs(self.src)
        open(os.path.join(self.src, "004-Some Original.mp3"), "wb").close()
        self.body = b"fLaC" * 300
        self.fake.add(AUDIO_BUCKET, "stream/tracks/004.flac", self.body, sha256=True)

    def test_the_cut_is_fetched_when_its_case_is_scored_and_lands_under_its_name(self):
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            self.et.refresh_cuts()
        tracks = {"4": {"master_begin_seconds": 0, "master_end_seconds": 90,
                        "artist": "Dead Calm", "title": "Urban Style"}}
        cases = calibrate.build_cases(tracks, {}, self.src)
        self.assertEqual(len(cases), 1)
        self.assertIsNone(cases[0]["extract"])
        self.assertTrue(cases[0]["extract_in_bucket"])
        self.assertEqual(self.fake.ops("cp-down"), [], "nothing is fetched to build the list")
        seen = []

        def load(path, **_kw):
            seen.append(path)
            return calibrate.np.zeros(100 * calibrate._audio.SR, dtype="float32")

        with mock.patch.object(calibrate._audio, "load_audio", load):
            mix = calibrate.mix_query(cases[0])
        self.assertIsNotNone(mix)
        want = os.path.join(calibrate.cache_budget.dir_of(self.et.STREAM_TRACKS_CACHE),
                            "004 - Dead Calm - Urban Style.flac")
        self.assertEqual(seen, [want])
        with open(want, "rb") as fh:
            self.assertEqual(fh.read(), self.body)
        self.assertTrue(os.path.isfile(want + self.et.OBJECT_META))
        self.assertEqual(self.fake.ops("cp-down"), ["cp-down"])
        self.assertIn("004 - Dead Calm - Urban Style.flac",
                      [os.path.basename(e["path"]) if isinstance(e, dict)
                       else os.path.basename(e[0])
                       for e in calibrate.cache_budget.entries(self.et.STREAM_TRACKS_CACHE)],
                      "the fetched cut is an entry of the tracks cache")

    def test_with_no_listing_held_no_case_is_made_from_the_bucket(self):
        tracks = {"4": {"master_begin_seconds": 0, "master_end_seconds": 90,
                        "artist": "A", "title": "B"}}
        self.assertEqual(calibrate.build_cases(tracks, {}, self.src), [])
        self.assertEqual(self.fake.calls, [])
