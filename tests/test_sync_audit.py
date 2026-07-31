"""sync-audit: grading hand sync points against the audio (align-tool calibration).

Hermetic: a fabricated mini-world (labels dir + capture audio + sources dir with
synthetic WAVs) planted with one correct seat, one seat 30 ms off (the audit must
find and measure the drift), and one wrong-by-20-seconds seat (must be NOT-FOUND,
never silently 'verified'). Skips without ffmpeg (the decode path is real).
"""

import os
import shutil
import sys
import tempfile
import unittest
import wave

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))

from streamalign import sync_audit as sa           # noqa: E402

SR = sa.SR
HAVE_FFMPEG = shutil.which("ffmpeg") is not None


def _wav(path, mono):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(mono, -1, 1) * 32767.0).astype("<i2").tobytes())
    return path


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not on PATH")
class TestAuditEndToEnd(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="sync-audit-")
        labels = os.path.join(cls.tmp, "labels")
        audio = os.path.join(cls.tmp, "audio")
        sources = os.path.join(cls.tmp, "sources")
        for d in (labels, audio, sources):
            os.makedirs(d)
        rng = np.random.default_rng(9)
        raw = rng.standard_normal(SR * 60).astype(np.float32)
        k = np.hanning(33).astype(np.float32)
        k /= k.sum()
        orig = (np.convolve(raw, k, "same") + 0.1 * raw) * 0.4
        # capture: quiet bed with the original at rate 1.0 starting at t=20 s
        cap = 0.01 * rng.standard_normal(SR * 100).astype(np.float32)
        cap[20 * SR:20 * SR + len(orig)] += 0.5 * orig
        _wav(os.path.join(audio, "dTEST-000.wav"), cap)
        _wav(os.path.join(sources, "042-Synthetic Test.wav"), orig)
        # labels: clip head at 20 s (orig local 0). A is seated exactly; B's START
        # row is bookkept 30 ms late (so its reconstructed seat is 30 ms off while
        # the A/B sync deltas -- and thus the rate -- stay exactly 1.0); C points
        # 20 s away from any true correspondence.
        rows = [
            (20.0, "orig042 start: A"),
            (30.0, "orig042 sync: A"), (30.0, "track sync: A"),
            (20.03, "orig042 start: B"),
            (55.0, "orig042 sync: B"), (55.0, "track sync: B"),
            (20.0, "orig042 start: C"),
            (45.0, "orig042 sync: C"), (65.0, "track sync: C"),
        ]
        with open(os.path.join(labels, "dTEST-000.labels.tsv"), "w") as f:
            for t, text in rows:
                f.write("%.6f\t%.6f\t%s\n" % (t, t, text))
        cls.result = sa.audit(labels_dir=labels, sources_dir=sources,
                              audio_dir=audio, background=3)
        cls.by_label = {p["label"]: p for p in cls.result["points"]}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_exact_seat_verifies_strong_with_tiny_hand_error(self):
        p = self.by_label["A"]
        self.assertEqual(p["verdict"], "STRONG", p)
        self.assertLess(abs(p["hand_err_ms"]), 5.0)
        self.assertLess(p["whole"], 60.0)

    def test_bookkeeping_drift_is_found_and_measured(self):
        p = self.by_label["B"]
        self.assertEqual(p["verdict"], "STRONG", p)
        # the start row placed the clip 30 ms late: the audit must land back on
        # the true correspondence and report ~+30 ms of bookkeeping drift
        self.assertGreater(abs(p["hand_err_ms"]), 20.0)
        self.assertLess(abs(p["hand_err_ms"]), 45.0)

    def test_wrong_seat_is_not_found_not_silently_verified(self):
        p = self.by_label["C"]
        self.assertEqual(p["verdict"], "NOT-FOUND", p)
        self.assertGreater(p["whole"], 110.0)

    def test_background_samples_cluster_at_sqrt2(self):
        bg = np.array(self.result["background"])
        self.assertGreaterEqual(len(bg), 3)
        self.assertGreater(float(np.median(bg)), 120.0)
        self.assertLess(float(np.median(bg)), 160.0)

    def test_render_summarises(self):
        text = sa.render(self.result)
        self.assertIn("STRONG", text)
        self.assertIn("NOT-FOUND", text)
        self.assertIn("background", text)


class TestHelpers(unittest.TestCase):

    def test_start_for_prefers_exact_label_else_nearest(self):
        starts = {("d", 42): [("A", 10.0), ("B", 55.0)]}
        self.assertEqual(sa.start_for(starts, "d", 42, "A", 99.0), 10.0)
        self.assertEqual(sa.start_for(starts, "d", 42, "Z", 50.0), 55.0)
        self.assertIsNone(sa.start_for(starts, "d", 7, "A", 0.0))

    def test_duplicate_exact_labels_take_the_nearest_not_the_first(self):
        # re-seating leaves stale duplicate `start: A` rows; the live bookkeeping is
        # the one nearest the sync row (real case: track 010 in d019-040, 5.4 s apart)
        starts = {("d", 10): [("A", 509.828182), ("A", 515.211983)]}
        self.assertEqual(sa.start_for(starts, "d", 10, "A", 517.492078), 515.211983)

    def test_inside_shifts_edge_probes_rather_than_skipping(self):
        got = sa._inside(1.0, 1.0, 100.0, 100.0)
        self.assertIsNotNone(got)
        self.assertGreaterEqual(min(got), sa.WIN_S / 2 + sa.SEARCH_S)
        self.assertIsNone(sa._inside(1.0, 1.0, 9.0, 9.0))


if __name__ == "__main__":
    unittest.main()
