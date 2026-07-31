"""align-tool Pass 1 (matchconv): the MATCH-seed + PHAT-refine converter.

All hermetic: synthetic audio, fabricated MATCH paths, no external binaries. The
end-to-end test is the load-bearing one -- it rebuilds the gate-0 situation in
miniature (original embedded in a longer 'mix' at a DJ-pitched rate, polarity
inverted, MATCH seed off by seconds with the WRONG rate) and requires the converter
to recover the true rate, the true seats, and the inversion anyway.
"""

import os
import re
import sys
import tempfile
import unittest
import wave

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))

from streamalign import groundtruth as gt          # noqa: E402
from streamalign import hints                      # noqa: E402
from streamalign import matchconv as mc            # noqa: E402

SR = mc.SR

# The emitted grammar, pinned exactly as labels/sort_tsv.py parses it.
SYNC_RE = re.compile(r"((track)(\d+)?|(orig)(\d+))\s+sync:\s+(.)(.*)")
ORIG_ROW_RE = re.compile(r"orig(\d+)\s+(start|end|note):\s*(.*)")

# One synthetic world, built once. Band-limited noise, not white: white noise has zero
# autocorrelation beyond one sample, so even the 3e-4 rate residual left by the sweep's
# grid annihilates its correlation -- real music correlates over milliseconds and is what
# the thresholds are tuned for. A ~2 ms smoothing kernel gives the synthetic the same
# forgiveness without changing the math under test.
TRUE_RATE = 1.02          # original runs 2% fast in the mix (DJ pitched up)
TRUE_DELTA = 14.0         # original-native seconds already elapsed at stream t=0
ORIG_LEN_S = 80.0
STREAM_LEN_S = 100.0
_rng = np.random.default_rng(1998)
_kernel = np.hanning(33).astype(np.float32)
_kernel /= _kernel.sum()
ORIG = np.convolve(_rng.standard_normal(int(ORIG_LEN_S * SR)).astype(np.float32),
                   _kernel, mode="same").astype(np.float32)


def _build_stream():
    """A quiet noise bed with the rate-warped, polarity-INVERTED original mixed in."""
    stream = (0.05 * _rng.standard_normal(int(STREAM_LEN_S * SR))).astype(np.float32)
    warped = mc.resample_by_rate(ORIG, TRUE_RATE)
    start = int(round(-TRUE_DELTA / TRUE_RATE * SR))     # orig local 0 on the stream clock
    lo = max(0, start)
    hi = min(len(stream), start + len(warped))
    stream[lo:hi] += -0.7 * warped[lo - start:hi - start]
    return stream


STREAM = _build_stream()


def _fake_match_pairs():
    """A MATCH a_b path the way gate 0 actually saw one: right neighbourhood, wrong detail.

    b = a + delta with the WRONG slope (0.98 vs the true 1.02), +-1.5 s of wander, and a
    garbage head (the forced (0,0) start). The converter must survive all three.
    """
    rows = []
    for i in range(0, int(STREAM_LEN_S * 50)):
        a = i * 0.02
        if a < 8.0:
            b = a * 0.4                                   # forced-origin garbage head
        else:
            b = TRUE_DELTA + a * 0.98 + 1.5 * np.sin(a / 7.0)
        rows.append((a, max(0.0, b)))
    return rows


class TestParseAbCsv(unittest.TestCase):

    def test_parses_sonic_annotator_format(self):
        # exactly as sonic-annotator -w csv writes it: 9-decimal time, plain value
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as f:
            f.write("0.020000000,0\n0.040000000,0.02\n\n69.480000000,63.2\n")
            path = f.name
        try:
            pairs = mc.parse_ab_csv(path)
        finally:
            os.unlink(path)
        self.assertEqual(len(pairs), 3)
        self.assertAlmostEqual(pairs[2][0], 69.48)
        self.assertAlmostEqual(pairs[2][1], 63.2)

    def test_too_short_a_path_is_refused_by_coarse_map(self):
        with self.assertRaises(ValueError):
            mc.coarse_map([(0.0, 0.0)] * 5)


class TestCoarseMap(unittest.TestCase):

    def test_recovers_offset_and_rate_despite_garbage_head(self):
        offset, rate = mc.coarse_map(_fake_match_pairs(), orig_len_s=ORIG_LEN_S)
        # The seed's whole contract is to land inside the sweep's capture range
        # (offset +-SWEEP_RADIUS_S, rate +-RATE_SPAN of true) -- not to reproduce the
        # path's numbers, which are WRONG by construction (slope 0.98 vs true 1.02).
        self.assertLess(abs(offset - TRUE_DELTA), mc.SWEEP_RADIUS_S * 0.5)
        self.assertLess(abs(rate - TRUE_RATE), mc.RATE_SPAN * 0.8)


class TestEndToEnd(unittest.TestCase):
    """The gate-0 situation in miniature: recover rate, seats, and polarity from a bad seed."""

    @classmethod
    def setUpClass(cls):
        cls.result = mc.convert(STREAM, ORIG, _fake_match_pairs(), anchor_count=5)

    def test_recovers_the_true_rate(self):
        self.assertAlmostEqual(self.result["rate"], TRUE_RATE, delta=0.003)

    def test_detects_the_polarity_inversion(self):
        self.assertTrue(self.result["inverted"])

    def test_anchor_seats_are_sample_tight(self):
        # every selected anchor's implied original position must sit within 10 ms of truth:
        # b_native(a) = TRUE_DELTA + a * TRUE_RATE
        self.assertTrue(self.result["anchors"])
        for a, off, conf, _inv, _out in self.result["anchors"]:
            b_native = (a - off) * self.result["rate"]
            self.assertAlmostEqual(b_native, TRUE_DELTA + a * TRUE_RATE, delta=0.010)
            self.assertGreaterEqual(conf, mc.MIN_CONF)

    def test_anchors_are_spread_not_clustered(self):
        anchors = self.result["anchors"]
        self.assertGreaterEqual(len(anchors), 3)
        span = anchors[-1][0] - anchors[0][0]
        self.assertGreater(span, (STREAM_LEN_S - TRUE_DELTA) * 0.4)


class TestOutlierMarking(unittest.TestCase):

    def test_a_loop_skip_is_marked_and_deselected(self):
        # a smooth curve with one confident-but-wrong excursion (the DnB loop trap)
        anchors = [(10.0 * i, -27.5 - 0.001 * i, 0.5, True) for i in range(9)]
        anchors[4] = (40.0, -28.2, 0.45, True)
        marked = mc.mark_outliers(anchors)
        self.assertTrue(marked[4][4])
        self.assertFalse(any(m[4] for i, m in enumerate(marked) if i != 4))
        picked = mc.select_anchors(marked, 9)
        self.assertNotIn(40.0, [p[0] for p in picked])

    def test_low_confidence_is_never_selected(self):
        anchors = [(10.0 * i, -27.5, 0.05, False) for i in range(5)]
        self.assertEqual(mc.select_anchors(mc.mark_outliers(anchors), 5), [])


class TestEmission(unittest.TestCase):
    """Rows are existing grammar, marked HINT, and can never masquerade as labels."""

    def _rows(self, off0=-27.5):
        anchors = [(60.0, off0, 0.72, True, False), (200.0, off0 + 0.2, 0.65, True, False)]
        return mc.build_rows(72, anchors, 1.0218, True,
                             orig_native_len_s=369.31, stream_len_s=1200.0)

    def test_sync_rows_parse_as_the_existing_grammar(self):
        stream_rows, orig_rows = self._rows()
        stream_syncs = [t for _, _, t in stream_rows if " sync:" in t]
        orig_syncs = [t for _, _, t in orig_rows if " sync:" in t]
        self.assertEqual(len(stream_syncs), 2)
        self.assertEqual(len(orig_syncs), 2)
        for text in stream_syncs:
            m = SYNC_RE.match(text)
            self.assertIsNotNone(m, text)
            self.assertEqual(m.group(1), "track")
        for text in orig_syncs:
            m = SYNC_RE.match(text)
            self.assertIsNotNone(m, text)
            self.assertEqual(m.group(5), "072")

    def test_every_row_is_marked_hint_with_confidence_on_syncs(self):
        for rows in self._rows():
            for _, _, text in rows:
                self.assertTrue(text.endswith(" HINT"), text)
            for _, _, text in rows:
                if " sync:" in text:
                    self.assertIn("confidence", text)

    def test_paired_markers_match_across_the_two_files(self):
        stream_rows, orig_rows = self._rows()
        s_marks = [SYNC_RE.match(t).group(6) for _, _, t in stream_rows if " sync:" in t]
        o_marks = [SYNC_RE.match(t).group(6) for _, _, t in orig_rows if " sync:" in t]
        self.assertEqual(s_marks, o_marks)

    def test_orig_times_are_native_seconds(self):
        _, orig_rows = self._rows()
        sync_times = [a for a, _, t in orig_rows if " sync:" in t]
        # b_native = (a - off) * rate for the first anchor
        self.assertAlmostEqual(sync_times[0], (60.0 - -27.5) * 1.0218, places=4)

    def test_start_before_capture_becomes_a_question(self):
        stream_rows, _ = self._rows(off0=-27.5)
        questions = [t for _, _, t in stream_rows if "QUESTION" in t]
        self.assertEqual(len(questions), 1)
        self.assertIn("BEFORE", questions[0])
        # and no bare `orig072 start:` row is proposed in that case
        self.assertFalse([t for _, _, t in stream_rows if re.match(r"orig072 start:", t)])

    def test_start_inside_capture_is_a_native_shape_row(self):
        stream_rows, _ = self._rows(off0=12.0)
        starts = [(a, t) for a, _, t in stream_rows if ORIG_ROW_RE.match(t)
                  and t.startswith("orig072 start:")]
        self.assertEqual(len(starts), 1)
        self.assertAlmostEqual(starts[0][0], 12.0, places=4)

    def test_emitted_names_are_invisible_to_the_pipeline(self):
        self.assertFalse(gt.is_pipeline_label_file("d376-395.orig072.match.hints.tsv"))
        self.assertFalse(gt.is_pipeline_label_file("orig072.match.hints.tsv"))

    def test_rows_go_out_through_the_write_guard(self):
        stream_rows, _ = self._rows()
        with self.assertRaises(ValueError):
            hints.write_hints(stream_rows, "/tmp/d376-395.labels.tsv")


class TestNccRadiusFix(unittest.TestCase):
    """refine_offset's confidence must survive a search radius wider than the window.

    _ncc_at used to truncate the a-segment by the found shift as if both segments were
    equal length; with a widened b-segment (radius > win) every peak beyond one window
    length got confidence exactly 0.0 even when the offset itself was found dead-on.
    The converter's rate sweep runs at radius >> win, so this is load-bearing for it.
    """

    def test_conf_survives_radius_wider_than_window(self):
        from streamalign.align import refine_offset
        rng = np.random.default_rng(7)
        raw = rng.standard_normal(SR * 60).astype(np.float32)
        # band-limited + a white floor: pure smoothed noise has exact spectral comb
        # nulls, and PHAT's whitening amplifies shared-null bins into peak-burying
        # noise -- an artefact of synthetic material, not of the fix under test
        base = np.convolve(raw, _kernel, mode="same") + 0.1 * raw
        true_off = -SR * 13                       # a[i] pairs with b[i + 13 s]
        a = base[-true_off:-true_off + 8 * SR].copy()
        off, conf = refine_offset(a, base, around=true_off + 800,
                                  radius=12 * SR, win=6 * SR)
        self.assertAlmostEqual(off, true_off, delta=1.0)
        self.assertGreater(conf, 0.9)


class TestRunnerSeam(unittest.TestCase):

    def test_sonic_annotator_argv_is_fixed_shape(self):
        argv = mc.sonic_annotator_argv("/a/s.wav", "/b/o.wav", "/out", exe="/bin/sa")
        self.assertEqual(argv[0], "/bin/sa")
        self.assertIn("-m", argv)
        self.assertIn("vamp:match-vamp-plugin:match:a_b", argv)
        self.assertEqual(argv[argv.index("--csv-basedir") + 1], "/out")
        # inputs ride as discrete argv entries, stream first (units depend on it)
        self.assertLess(argv.index("/a/s.wav"), argv.index("/b/o.wav"))

    def test_write_wav16_roundtrips(self):
        arr = np.sin(np.arange(SR) * 0.1).astype(np.float32) * 0.5
        with tempfile.TemporaryDirectory() as tmp:
            path = mc.write_wav16(os.path.join(tmp, "t.wav"), arr)
            with wave.open(path, "rb") as w:
                self.assertEqual(w.getframerate(), SR)
                self.assertEqual(w.getnchannels(), 1)
                self.assertEqual(w.getnframes(), len(arr))
                back = np.frombuffer(w.readframes(len(arr)), dtype="<i2") / 32767.0
        self.assertLess(float(np.max(np.abs(back - arr))), 1e-3)


if __name__ == "__main__":
    unittest.main()
