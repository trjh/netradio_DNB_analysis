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

from streamalign import groundtruth as gt          # noqa: E402
from streamalign import sync_audit as sa           # noqa: E402
from streamalign import track_mix as tm            # noqa: E402

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


class TestVerifiedFlag(unittest.TestCase):
    """AP-04: the audit reports whether a point's row text carries the token, and the
    token never leaks into (or out of) the FILE-sync `verified` family."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sync-verified-")
        self.labels = os.path.join(self.tmp, "labels")
        os.makedirs(self.labels)
        rows = [
            (0.0, "file start sync: dV-000.wav 100.0 verified d998-999"),
            (30.0, "orig042 sync: A verified confidence 5.9/10"),
            (30.0, "track sync: A verified confidence 5.9/10"),
            (55.0, "orig042 sync: B"),
            (55.0, "track sync: B"),
        ]
        with open(os.path.join(self.labels, "dV-000.labels.tsv"), "w") as f:
            for t, text in rows:
                f.write("%.6f\t%.6f\t%s\n" % (t, t, text))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_parse_sync_points_carries_the_flag(self):
        pairs = tm.parse_sync_points(self.labels)[42]
        by_label = {p["label"]: p for p in pairs}
        self.assertTrue(by_label["A"]["verified"])
        self.assertFalse(by_label["B"]["verified"])

    def test_audit_reports_the_flag_even_on_skipped_points(self):
        # no start rows, no audio: both points skip -- but the flag is still there,
        # which is exactly what --only-unchecked filters on
        result = sa.audit(labels_dir=self.labels, sources_dir=self.tmp)
        by_label = {p["label"]: p for p in result["points"]}
        self.assertTrue(by_label["A"]["verified"])
        self.assertFalse(by_label["B"]["verified"])
        text = sa.render(result)
        self.assertIn("verified ", text)
        self.assertIn("unchecked", text)

    def test_token_rows_never_reach_the_file_sync_family(self):
        # direction 1: the token does not create a placement or an alignment edge
        starts = gt.resolve_starts(self.labels)
        self.assertEqual(list(starts), ["dV-000"])              # only the file sync row
        self.assertEqual(gt.alignment_edges(self.labels), [("dV-000", "d998-999")])

    def test_file_sync_verified_never_marks_a_point(self):
        # direction 2: a file row's `verified <neighbour>` is not a sync-point token
        self.assertFalse(tm.sync_row_verified(
            "file start sync: dV-000.wav 100.0 verified d998-999"))


class TestDeriveSeat(unittest.TestCase):
    """AP-17 seat derivation: the pure master-timeline shift + neighbour-correction math."""

    STARTS = {("dA-000", 42): [("A", 20.0), ("B", 20.03)]}
    MASTERS = {"dA-000": 0.0, "dB-001": 40.0}

    def _strong(self, errs):
        return [{"track": 42, "stem": "dA-000", "verdict": "STRONG", "hand_err_ms": e}
                for e in errs]

    def test_seat_is_the_neighbours_bookkeeping_shifted_onto_this_clock(self):
        got, why = sa.derive_seat(42, "dB-001", "D", 30.0, self.STARTS, self.MASTERS,
                                  self._strong([0.0, 30.0, 10.0]))
        self.assertIsNone(why)
        # nearest start row to the shifted sync (70.0 on dA's clock) is B at 20.03;
        # shifted here: 20.03 - 40 = -19.97, so b_native = 30 - (-19.97) = 49.97
        self.assertEqual(got["ref_stem"], "dA-000")
        self.assertAlmostEqual(got["start_t"], -19.97, places=3)
        self.assertAlmostEqual(got["b_native"], 49.97, places=3)
        # the correction is the STRONG neighbours' MEDIAN hand error (the 066-A method)
        self.assertAlmostEqual(got["correction_s"], 0.010, places=6)
        self.assertEqual(got["n_strong"], 3)

    def test_no_strong_neighbours_stays_skipped_with_a_reason(self):
        got, why = sa.derive_seat(42, "dB-001", "D", 30.0, self.STARTS, self.MASTERS, [])
        self.assertIsNone(got)
        self.assertIn("no STRONG neighbour", why)
        # NOT-FOUND neighbours don't count either
        weak = [dict(p, verdict="NOT-FOUND") for p in self._strong([0.0])]
        got, why = sa.derive_seat(42, "dB-001", "D", 30.0, self.STARTS, self.MASTERS, weak)
        self.assertIsNone(got)

    def test_unplaced_capture_cannot_derive(self):
        got, why = sa.derive_seat(42, "dZ-999", "D", 30.0, self.STARTS, self.MASTERS,
                                  self._strong([0.0]))
        self.assertIsNone(got)
        self.assertIn("not master-placed", why)

    def test_other_tracks_seatings_are_never_borrowed(self):
        starts = {("dA-000", 66): [("A", 20.0)]}                 # a DIFFERENT original
        got, why = sa.derive_seat(42, "dB-001", "D", 30.0, starts, self.MASTERS,
                                  self._strong([0.0]))
        self.assertIsNone(got)


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not on PATH")
class TestSeatDerivationEndToEnd(unittest.TestCase):
    """AP-17 end to end: a capture whose sync points have NO `origNNN start:` row is
    graded anyway, its seat derived from the STRONG-audited neighbour capture that
    shares the clip seating -- marked, never silently equal to a bookkept seat."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="seat-derive-")
        labels = os.path.join(cls.tmp, "labels")
        audio = os.path.join(cls.tmp, "audio")
        sources = os.path.join(cls.tmp, "sources")
        for d in (labels, audio, sources):
            os.makedirs(d)
        rng = np.random.default_rng(11)
        raw = rng.standard_normal(SR * 60).astype(np.float32)
        k = np.hanning(33).astype(np.float32)
        k /= k.sum()
        orig = (np.convolve(raw, k, "same") + 0.1 * raw) * 0.4
        # master timeline: capture dA at master 0 (100 s), dB at master 40 (60 s);
        # the original plays from master 20, so dB carries its 20..60 s stretch
        cap = 0.01 * rng.standard_normal(SR * 100).astype(np.float32)
        cap[20 * SR:20 * SR + len(orig)] += 0.5 * orig
        _wav(os.path.join(audio, "dA-000.wav"), cap)
        _wav(os.path.join(audio, "dB-001.wav"), cap[40 * SR:])
        _wav(os.path.join(sources, "042-Synthetic Test.wav"), orig)
        # dA: seated bookkeeping + A/B sync pairs (the STRONG neighbours-to-be)
        with open(os.path.join(labels, "dA-000.labels.tsv"), "w") as f:
            for t, text in [
                    (0.0, "file start sync: dA-000.wav 0.0 verified x"),
                    (20.0, "orig042 start: A"),
                    (30.0, "orig042 sync: A"), (30.0, "track sync: A"),
                    (55.0, "orig042 sync: B"), (55.0, "track sync: B")]:
                f.write("%.6f\t%.6f\t%s\n" % (t, t, text))
        # dB: sync pair only -- NO orig042 start: row anywhere in this file
        with open(os.path.join(labels, "dB-001.labels.tsv"), "w") as f:
            for t, text in [
                    (0.0, "file start sync: dB-001.wav 40.0 verified dA-000"),
                    (30.0, "orig042 sync: D"), (30.0, "track sync: D")]:
                f.write("%.6f\t%.6f\t%s\n" % (t, t, text))
        cls.result = sa.audit(labels_dir=labels, sources_dir=sources, audio_dir=audio)
        cls.by_key = {(p["stem"], p["label"]): p for p in cls.result["points"]}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_neighbour_points_still_grade_normally(self):
        for label in ("A", "B"):
            p = self.by_key[("dA-000", label)]
            self.assertEqual(p["verdict"], "STRONG", p)
            self.assertNotIn("seat_source", p)

    def test_startless_point_is_graded_not_skipped(self):
        p = self.by_key[("dB-001", "D")]
        self.assertEqual(p["verdict"], "STRONG", p)
        # dB local 30 is master 70 = orig-native 50; the derived seat must land there
        self.assertLess(abs(p["hand_err_ms"]), 45.0)

    def test_derived_seat_is_marked_with_its_provenance(self):
        p = self.by_key[("dB-001", "D")]
        self.assertEqual(p.get("seat_source"), "derived-from-neighbours")
        self.assertEqual(p.get("seat_ref"), "dA-000")
        self.assertEqual(p.get("seat_neighbours"), 2)
        text = sa.render(self.result)
        self.assertIn("seat<-dA-000", text)


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


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not on PATH")
class TestExhaustiveSweep(unittest.TestCase):
    """The closed form residual = sqrt(2-2|rho|) at every lag, against brute force."""

    def test_sweep_matches_brute_force_and_finds_the_seat(self):
        rng = np.random.default_rng(3)
        raw = rng.standard_normal(SR * 20).astype(np.float32)
        k = np.hanning(33).astype(np.float32)
        k /= k.sum()
        base = np.convolve(raw, k, "same") + 0.1 * raw
        stream = 0.05 * rng.standard_normal(SR * 20).astype(np.float32)
        stream += base * 0.5
        owin = base[8 * SR:9 * SR].copy()          # 1 s window whose seat is at 8 s
        curve = sa.exhaustive_sweep(stream, owin)
        self.assertEqual(len(curve), len(stream) + len(owin) - 1)
        # global minimum at the planted seat (lag index = seat + n - 1)
        self.assertEqual(int(np.argmin(curve)), 8 * SR + len(owin) - 1)
        # the bed noise alone bounds the null: 0.05 bed vs ~0.117 signal -> ~37%
        self.assertLess(float(curve.min()), 45.0)
        # closed form == the audit's own metric, spot-checked by brute force
        for lag_s in (3.0, 8.0, 14.5):
            i = int(lag_s * SR)
            seg = stream[i:i + len(owin)]
            rho = abs(float(np.dot(seg, owin)
                            / (np.linalg.norm(seg) * np.linalg.norm(owin))))
            expect = 100.0 * np.sqrt(2 - 2 * rho)
            self.assertAlmostEqual(float(curve[i + len(owin) - 1]), expect, delta=0.5)
        # and the mass of the curve sits at the sqrt(2) cluster
        self.assertGreater(float(np.median(curve)), 120.0)


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not on PATH")
class TestSweepEdgeWindows(TestAuditEndToEnd):
    """A --wins wider than the original-side context must stay geometrically honest."""

    def test_oversized_window_clips_consistently(self):
        # point A sits 10 s into a 60 s original; a 40 s window overhangs the front
        # by ~10 s: the clipped extract's stream-side start must shift accordingly
        # and the hand seat must still be found as the (near-)minimum
        r = sa.sweep_point(42, "A",
                           labels_dir=os.path.join(self.tmp, "labels"),
                           sources_dir=os.path.join(self.tmp, "sources"),
                           audio_dir=os.path.join(self.tmp, "audio"),
                           wins=(40.0,))
        w = r["windows"]["40"]
        self.assertIsNone(w.get("error"))
        self.assertAlmostEqual(w["front_clipped_s"], 10.0, delta=0.2)
        self.assertIsNotNone(w["true_residual"])
        self.assertTrue(w["true_is_min"], w)
        # clipped front => the true window start sits at the seat's orig-local 0
        self.assertAlmostEqual(w["true_at_s"], 20.0, delta=0.1)

    def test_zero_window_rejected(self):
        with self.assertRaises(ValueError):
            sa.sweep_point(42, "A",
                           labels_dir=os.path.join(self.tmp, "labels"),
                           sources_dir=os.path.join(self.tmp, "sources"),
                           audio_dir=os.path.join(self.tmp, "audio"),
                           wins=(0.0,))
