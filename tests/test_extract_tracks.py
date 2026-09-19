"""extract_tracks.py: flac cuts, the variable's directory, and the policy's door.

The cuts land in the `stream_tracks` cache as FLAC (ffmpeg picks the codec from the
extension; the argv names none), the output directory comes from
NETRADIO_STREAM_TRACKS_CACHE_DIR (else $NETRADIO_CACHE_ROOT/stream_tracks), and each cut
that lands inside the registered cache goes through the policy: `reserve` before the
write, `commit` after. All synthetic: ffmpeg never runs (the subprocess seam is stubbed),
so the whole thing works on a bare checkout.
"""

import json
import os
import sys
import tempfile
import unittest
import unittest.mock

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)

import cache_budget                                # noqa: E402
import extract_tracks                              # noqa: E402

CACHE_ENV = ("NETRADIO_CACHE_ROOT", "NETRADIO_DOWNLOAD_ROOT", "NETRADIO_DISK_MAX_PCT",
             "NETRADIO_CACHE_EVENTS_DAYS")


class TheTracksCache(unittest.TestCase):
    KB = 1000

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="stream-tracks-")
        self.addCleanup(self._cleanup)
        self._saved = {k: os.environ.get(k) for k in list(os.environ)
                       if k.startswith("NETRADIO_") and ("CACHE" in k or k in CACHE_ENV)}
        for k in self._saved:
            os.environ.pop(k, None)
        os.environ["NETRADIO_CACHE_ROOT"] = self.tmp
        self._registry = dict(cache_budget._REGISTRY), dict(cache_budget._STATS)
        cache_budget._REGISTRY.clear()
        cache_budget._STATS.clear()
        extract_tracks.register_cache()
        self.addCleanup(setattr, cache_budget, "_disk_usage", cache_budget._disk_usage)
        cache_budget._disk_usage = self._fake_volume

    def _cleanup(self):
        import shutil
        cache_budget._REGISTRY.clear()
        cache_budget._REGISTRY.update(self._registry[0])
        cache_budget._STATS.clear()
        cache_budget._STATS.update(self._registry[1])
        for k in [k for k in list(os.environ)
                  if k.startswith("NETRADIO_") and ("CACHE" in k or k in CACHE_ENV)]:
            os.environ.pop(k, None)
        os.environ.update(self._saved)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fake_volume(self, _path):
        return (100 * 1000 * self.KB, 0, 100 * 1000 * self.KB)

    def test_the_default_directory_is_the_variables(self):
        os.environ["NETRADIO_STREAM_TRACKS_CACHE_DIR"] = os.path.join(self.tmp, "elsewhere")
        self.assertEqual(extract_tracks.tracks_dir(), os.path.join(self.tmp, "elsewhere"))
        os.environ.pop("NETRADIO_STREAM_TRACKS_CACHE_DIR")
        self.assertEqual(extract_tracks.tracks_dir(), os.path.join(self.tmp, "stream_tracks"))
        os.environ.pop("NETRADIO_CACHE_ROOT")
        self.assertIsNone(extract_tracks.tracks_dir(),
                          "no directory at all while the policy is dark -- never a fallback")

    def test_the_registration(self):
        rec = cache_budget.register("stream_tracks",
                                    cap=int(extract_tracks.STREAM_TRACKS_CACHE_GB * cache_budget.GB),
                                    max_age=extract_tracks.STREAM_TRACKS_CACHE_MAX_AGE_DAYS,
                                    refill="re-extract", rank=8)  # re-read: returns the record
        self.assertEqual((rec["cap"], rec["max_age"], rec["refill"], rec["rank"]),
                         (2 * cache_budget.GB, 14, "re-extract", 8))
        self.assertEqual(rec["dir"], os.path.join(self.tmp, "stream_tracks"))

    def test_the_argv_carries_flac_and_names_no_codec(self):
        """The output's extension chooses the codec: the argv names none, and the cut's
        output path ends .flac."""
        argvs = []
        out = os.path.join(self.tmp, "stream_tracks", "001 - A - B.flac")
        os.makedirs(os.path.dirname(out), exist_ok=True)   # main() makes the directory first

        def fake_run(argv, **kwargs):
            argvs.append(argv)
            with open(argv[-1], "wb") as fh:      # ffmpeg's output, simulated
                fh.write(b"fLaC")

        with unittest.mock.patch.object(extract_tracks.subprocess, "run", fake_run), \
                unittest.mock.patch.object(extract_tracks._audio, "find_audio_file",
                                          lambda stem: os.path.join(self.tmp, "capture.wav")):
            self.assertTrue(extract_tracks.cut("d000-018", 0.0, 30.0, 0.0, out))
        self.assertEqual(len(argvs), 1, "one ffmpeg invocation per cut")
        argv = argvs[0]
        self.assertEqual(argv[:1], ["ffmpeg"])
        self.assertTrue(argv[-1].endswith(".flac"), "the cut's output is flac")
        self.assertNotIn("-c", argv, "the codec is picked from the extension, never named")
        self.assertNotIn("-f", argv, "the container too")

    def test_a_cut_inside_the_cache_reserves_and_commits(self):
        self.assertTrue(cache_budget.registered("stream_tracks"))
        out = os.path.join(self.tmp, "stream_tracks", "001 - A - B.flac")

        os.makedirs(os.path.dirname(out), exist_ok=True)

        def fake_run(argv, **kwargs):
            with open(argv[-1], "wb") as fh:
                fh.write(b"fLaC" * 1000)

        with unittest.mock.patch.object(extract_tracks.subprocess, "run", fake_run), \
                unittest.mock.patch.object(extract_tracks._audio, "find_audio_file",
                                          lambda stem: os.path.join(self.tmp, "capture.wav")):
            self.assertTrue(extract_tracks.cut("d000-018", 0.0, 30.0, 0.0, out))
        self.assertTrue(os.path.isfile(out))
        rows = cache_budget.status()["caches"]
        self.assertEqual([r["entries"] for r in rows if r["name"] == "stream_tracks"], [1],
                         "the cut is accounted as the cache's entry")
        path = cache_budget.events_path()
        with open(path) as fh:
            events = [json.loads(line) for line in fh]
        self.assertEqual([(e["event"], e["cache"], e["entry"]) for e in events
                          if e["event"] == "admit"],
                         [("admit", "stream_tracks", "001 - A - B.flac")])

    def test_a_cut_the_policy_refuses_room_for_is_not_made(self):
        cache_budget._disk_usage = lambda _p: (100 * self.KB, 50 * self.KB, 50 * self.KB)
        os.environ["NETRADIO_DISK_MAX_PCT"] = "0"      # past the floor: every reserve refuses
        extract_tracks.register_cache()
        out = os.path.join(self.tmp, "stream_tracks", "001 - A - B.flac")

        def boom(argv, **kwargs):
            self.fail("no ffmpeg must run for a refused cut")

        with unittest.mock.patch.object(extract_tracks.subprocess, "run", boom), \
                unittest.mock.patch.object(extract_tracks._audio, "find_audio_file",
                                          lambda stem: os.path.join(self.tmp, "capture.wav")):
            self.assertFalse(extract_tracks.cut("d000-018", 0.0, 30.0, 0.0, out))
        self.assertFalse(os.path.exists(out))

    def test_a_cut_outside_the_registered_cache_is_written_but_not_accounted(self):
        """--out naming another directory is the operator's own: the write works as before,
        and the policy is not asked to account a path outside its cache."""
        outside = os.path.join(self.tmp, "my-tracks")
        os.makedirs(outside)
        out = os.path.join(outside, "001 - A - B.flac")

        def fake_run(argv, **kwargs):
            with open(argv[-1], "wb") as fh:
                fh.write(b"fLaC")

        with unittest.mock.patch.object(extract_tracks.subprocess, "run", fake_run), \
                unittest.mock.patch.object(extract_tracks._audio, "find_audio_file",
                                          lambda stem: os.path.join(self.tmp, "capture.wav")):
            self.assertTrue(extract_tracks.cut("d000-018", 0.0, 30.0, 0.0, out))
        self.assertTrue(os.path.isfile(out))
        self.assertEqual(cache_budget.status()["caches"][0]["entries"], 0)


if __name__ == "__main__":
    unittest.main()
