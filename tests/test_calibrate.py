"""calibrate.py's `build_cases`: which clean extract a case reads.

The lookup is shared with `selftest.py` deliberately (see `build_cases`' own docstring), so
the choice here is the choice the canary makes too. No audio anywhere: nothing is decoded,
and the extract directory's listing is stubbed to a fixed order, so the choice is the only
thing under test.
"""

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)

try:
    import calibrate
except Exception:                       # librosa/numba absent -> not this test's job
    calibrate = None


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
