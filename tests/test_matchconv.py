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

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "scripts"))
sys.path.insert(0, os.path.join(_REPO, "labels"))

import sort_tsv                                    # noqa: E402
from streamalign import groundtruth as gt          # noqa: E402
from streamalign import hints                      # noqa: E402
from streamalign import matchconv as mc            # noqa: E402
from streamalign import track_mix as tm            # noqa: E402

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
    """Rows are existing grammar and can never masquerade as labels. Anchor rows
    (sync/start/end) carry NO trailing ` HINT` (RC-1) but keep their in-row machine
    marks -- ` verified confidence` on sync rows; per-point start/end rows naming
    their anchor with `confidence n/10`; prose rows keep the `note HINT:`/`note QUESTION:`
    row-type grammar and stay suffix-marked."""

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

    def test_anchor_rows_carry_no_hint_suffix_and_syncs_their_confidence(self):
        # RC-1: sync/start/end anchor rows end at the confidence (or MATCH delta) --
        # never at ` HINT`; every other emitted row is a marked note row, unchanged.
        for rows in self._rows():
            for _, _, text in rows:
                if SYNC_RE.match(text) or ORIG_ROW_RE.match(text):
                    self.assertFalse(text.endswith(" HINT"), text)
                else:
                    self.assertRegex(text, r"^note (HINT|QUESTION): ")
                    self.assertTrue(text.endswith(" HINT"), text)
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
        # Per-point boundaries in the NATIVE CLIP FRAME (owner corrections
        # 2026-08-16/17): each start row is where the unstretched clip must be
        # SEATED to line up at that sync point — seat_k = a_k − b_native_k — so a
        # constant rate != 1.000 shifts the seat between points all by itself.
        # Fixture: rate 1.0218 → seats 10.9536 and 8.1060, a 2.85 s spread from
        # pure pitch (the rate-corrected zeros would sit ~0.2 s apart).
        stream_rows, _ = self._rows(off0=12.0)
        starts = [(a, t) for a, _, t in stream_rows if ORIG_ROW_RE.match(t)
                  and t.startswith("orig072 start:")]
        self.assertEqual(len(starts), 2)
        self.assertAlmostEqual(starts[0][0], 60.0 - (60.0 - 12.0) * 1.0218, places=4)
        self.assertAlmostEqual(starts[1][0], 200.0 - (200.0 - 12.2) * 1.0218, places=4)

    def test_end_rows_always_emit_even_past_the_capture_edge(self):
        # Owner rule 2026-08-16: a start before the capture may be omitted, but an
        # END row is ALWAYS emitted, even beyond the last sample — the end position
        # is what seats the record's continuation in the NEXT capture. The overhang
        # still gets the explanatory QUESTION note, but the rows themselves survive.
        stream_rows, _ = self._rows(off0=1100.0)   # native ends ~= 1489-1492 s > 1200 s
        ends = [(a, t) for a, _, t in stream_rows
                if t.startswith("orig072 end:")]
        self.assertEqual(len(ends), 2)             # one per sync point, none dropped
        for a, _t in ends:
            self.assertGreater(a, 1200.0)
        notes = [t for _, _, t in stream_rows if "AFTER this capture" in t]
        self.assertEqual(len(notes), 1)
        # the reported overhang keys on the MAXIMUM end across anchors — under
        # rate > 1 that is the FIRST anchor's end, not the last's (review finding)
        import re as _re
        overhang = float(_re.search(r"ends (\d+\.\d+) s AFTER", notes[0]).group(1))
        ends_calc = [60.0 - (60.0 - 1100.0) * 1.0218 + 369.31,
                     200.0 - (200.0 - 1100.2) * 1.0218 + 369.31]
        self.assertAlmostEqual(overhang, max(ends_calc) - 1200.0, places=3)

    def test_every_sync_point_has_its_own_boundary_rows(self):
        # Owner rule 2026-08-16 (refined): EACH sync point gets its own start and
        # end row, named with that point's marker number, at that anchor's own
        # implied placement — the anchor-to-anchor shift of the implied starts is
        # the drift, made visible on the label track. `confidence n/10` stays the
        # machine mark; `verified` and `?` never appear on a boundary.
        stream_rows, _ = self._rows(off0=12.0)
        anchor_count = sum(1 for _, _, t in stream_rows
                           if t.startswith("track sync: "))
        starts = [t for _, _, t in stream_rows if t.startswith("orig072 start:")]
        ends = [t for _, _, t in stream_rows if t.startswith("orig072 end:")]
        self.assertEqual(len(starts), anchor_count)
        self.assertEqual(len(ends), anchor_count)
        for k in range(1, anchor_count + 1):
            self.assertTrue(any(t.startswith("orig072 start: %d " % k)
                                for t in starts), k)
            self.assertTrue(any(t.startswith("orig072 end: %d " % k)
                                for t in ends), k)
        for text in starts + ends:
            self.assertRegex(text, r"^orig072 (start|end): \d+ confidence \d")
            self.assertNotIn("verified", text)
            self.assertNotIn("?", text)

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


class TestHintFilenames(unittest.TestCase):
    """Two captures aligning the SAME original must never share an output name.

    hint writes atomically replace their destination, so a stem-less
    original-side name meant run 2 silently destroyed run 1's original-side
    hints (review finding, 2026-07-31)."""

    def test_default_out_dir_is_labels_automated(self):
        # AP-30: script emissions land in labels/automated/ (committed); the
        # labels/ root stays hand-authoritative. --labels moves the whole
        # root and automated/ rides along; --out still overrides outright.
        self.assertEqual(gt.automated_dir("/x/labels"),
                         os.path.join("/x/labels", "automated"))
        self.assertTrue(gt.automated_dir().endswith(
            os.path.join("labels", "automated")))

    def test_orig_side_is_capture_specific(self):
        s1, o1 = mc.hint_filenames("d376-395", 72)
        s2, o2 = mc.hint_filenames("d356-375", 72)
        self.assertNotEqual(o1, o2)
        self.assertNotEqual(s1, s2)
        self.assertEqual(len({s1, o1, s2, o2}), 4)

    def test_names_stay_invisible_to_the_pipeline(self):
        for name in mc.hint_filenames("d376-395", 72):
            self.assertTrue(name.endswith(".hints.tsv"), name)
            self.assertFalse(gt.is_pipeline_label_file(name), name)


class TestTrim(unittest.TestCase):
    """AP-02: the pre-MATCH trim -- window math and the path shift back."""

    def test_window_spans_the_expected_overlap_with_margins(self):
        lo, hi = mc.trim_window(around_s=300.0, orig_len_s=369.31, stream_len_s=2400.0)
        self.assertAlmostEqual(lo, 300.0 - mc.TRIM_MARGIN_S)
        self.assertAlmostEqual(hi, 300.0 + 369.31 + mc.TRIM_MARGIN_S)

    def test_window_clamps_to_the_capture(self):
        lo, hi = mc.trim_window(around_s=14.7, orig_len_s=369.31, stream_len_s=400.0)
        self.assertEqual(lo, 0.0)                     # 14.7 - 60 clamps to the first sample
        self.assertEqual(hi, 400.0)                   # and the end clamps to the last

    def test_rate_guess_stretches_the_expected_end(self):
        # a slowed original (rate < 1: fewer original-seconds per stream-second)
        # occupies MORE stream time, so the window must reach further
        _, hi_neutral = mc.trim_window(100.0, 300.0, 10000.0, rate_guess=1.0)
        _, hi_slow = mc.trim_window(100.0, 300.0, 10000.0, rate_guess=0.9)
        self.assertGreater(hi_slow, hi_neutral)

    def test_apply_trim_offset_shifts_stream_times_only(self):
        pairs = [(0.0, 10.0), (1.0, 11.0)]
        shifted = mc.apply_trim_offset(pairs, 240.0)
        self.assertEqual(shifted, [(240.0, 10.0), (241.0, 11.0)])

    def test_derive_around_is_master_span_minus_capture_start(self):
        meta = {"72": {"master_begin_seconds": 22293.0, "master_end_seconds": 22564.0}}
        self.assertAlmostEqual(mc.derive_around(72, meta, 22278.306), 14.694, places=3)

    def test_derive_around_returns_none_when_unknowable(self):
        meta = {"72": {"master_begin_seconds": 22293.0}}
        self.assertIsNone(mc.derive_around(72, meta, None))       # unplaced capture
        self.assertIsNone(mc.derive_around(99, meta, 22278.306))  # no metadata entry
        self.assertIsNone(mc.derive_around(72, {"72": {}}, 22278.306))  # no span


class TestBatchWorklist(unittest.TestCase):
    """AP-16: which tracks a capture's master span covers."""

    META = {
        "70": {"master_begin_seconds": 21500.0, "master_end_seconds": 21900.0},
        "71": {"master_begin_seconds": 21900.0, "master_end_seconds": 22300.0},
        "72": {"master_begin_seconds": 22293.0, "master_end_seconds": 22564.0},
        "90": {"master_begin_seconds": 30000.0, "master_end_seconds": 30300.0},
        "no-span": {"title": "unplaced"},
        "not-a-number": {"master_begin_seconds": 0.0, "master_end_seconds": 1.0},
    }

    def test_overlapping_tracks_are_found_and_sorted(self):
        # capture d376-395: master 22278.3, ~2400 s long
        got = mc.tracks_overlapping(22278.306, 2400.0, self.META)
        self.assertEqual([n for n, _b, _e in got], [71, 72])

    def test_unplaced_capture_yields_nothing(self):
        self.assertEqual(mc.tracks_overlapping(None, 2400.0, self.META), [])

    def test_touching_spans_do_not_count_as_overlap(self):
        got = mc.tracks_overlapping(21900.0, 100.0, self.META)   # 70 ends exactly here
        self.assertEqual([n for n, _b, _e in got], [71])


class TestReferee(unittest.TestCase):
    """AP-03: the (trimmed) MATCH path referees the PHAT anchors."""

    PAIRS = [(a * 1.0, 14.0 + a * 1.02) for a in range(0, 100, 2)]   # b = 14 + 1.02*a

    def test_match_predict_interpolates_and_bounds(self):
        self.assertAlmostEqual(mc.match_predict(self.PAIRS, 10.0), 14.0 + 10.2, places=6)
        self.assertAlmostEqual(mc.match_predict(self.PAIRS, 11.0), 14.0 + 11.22, places=6)
        self.assertIsNone(mc.match_predict(self.PAIRS, -1.0))
        self.assertIsNone(mc.match_predict(self.PAIRS, 1e9))
        self.assertIsNone(mc.match_predict([], 10.0))

    def test_deltas_are_match_minus_phat(self):
        # PHAT anchor agreeing exactly with the path: delta 0; one seated 0.5 s off: 0.5
        agree = (50.0, 50.0 - (14.0 + 50 * 1.02) / 1.02, 0.9, False, False)
        offset = (60.0, 60.0 - (14.0 + 60 * 1.02 - 0.51) / 1.02, 0.9, False, False)
        deltas = mc.referee_deltas(self.PAIRS, [agree, offset], 1.02)
        self.assertAlmostEqual(deltas[0], 0.0, places=6)
        self.assertAlmostEqual(deltas[1], 0.51, places=6)

    def test_uncovered_anchor_gets_none(self):
        outside = (150.0, 0.0, 0.9, False, False)                 # beyond the path's span
        self.assertEqual(mc.referee_deltas(self.PAIRS, [outside], 1.02), [None])

    def _rows(self, deltas):
        anchors = [(60.0, -27.5, 0.72, True, False), (200.0, -27.3, 0.65, True, False)]
        return mc.build_rows(72, anchors, 1.0218, True,
                             orig_native_len_s=369.31, stream_len_s=1200.0,
                             match_deltas=deltas)

    def test_delta_is_appended_to_both_sync_rows(self):
        stream_rows, orig_rows = self._rows([0.031, -0.012])
        for rows in (stream_rows, orig_rows):
            syncs = [t for _, _, t in rows if " sync:" in t]
            self.assertIn("MATCH +0.031s", syncs[0])
            self.assertIn("MATCH -0.012s", syncs[1])

    def test_small_delta_earns_no_question(self):
        stream_rows, _ = self._rows([0.1, -0.2])
        self.assertFalse([t for _, _, t in stream_rows if "disagree" in t])

    def test_big_delta_earns_a_question_at_that_anchor(self):
        stream_rows, _ = self._rows([0.05, 0.9])
        questions = [(a, t) for a, _, t in stream_rows
                     if "QUESTION" in t and "disagree" in t]
        self.assertEqual(len(questions), 1)
        self.assertEqual(questions[0][0], 200.0)                  # at the offending anchor
        self.assertIn("+0.900", questions[0][1])

    def test_globally_off_path_yields_one_question_not_a_pile(self):
        # both anchors "disagree", but the MEDIAN delta says the whole MATCH path is
        # seated wrong (its forced-start failure): one global question, no per-anchor pile
        stream_rows, _ = self._rows([-40.1, -40.6])
        questions = [t for _, _, t in stream_rows if "QUESTION" in t and "disagree" in t]
        self.assertEqual(len(questions), 1)
        self.assertIn("median", questions[0])
        self.assertIn("-40.", questions[0])
        # the raw per-anchor deltas still ride the sync rows
        syncs = [t for _, _, t in stream_rows if " sync:" in t]
        self.assertIn("MATCH -40.100s", syncs[0])

    def test_none_delta_appends_nothing(self):
        stream_rows, _ = self._rows([None, None])
        for _, _, t in stream_rows:
            self.assertNotIn("MATCH ", t)

    def test_end_to_end_result_carries_the_deltas(self):
        result = getattr(TestEndToEnd, "result", None)
        if result is None:                       # run standalone: build the world once
            result = mc.convert(STREAM, ORIG, _fake_match_pairs(), anchor_count=5)
        self.assertEqual(len(result["match_deltas"]), len(result["anchors"]))


class TestVerifiedToken(unittest.TestCase):
    """AP-04: every emitted sync row carries ` verified` right after its marker, and the
    token is exactly what the Python-side rule (labels/sort_tsv.sync_verified) and the
    audit (track_mix.sync_row_verified) recognise -- while the FILE-sync `verified`
    keyword family stays a stranger to it in both directions."""

    def _rows(self):
        anchors = [(60.0, -27.5, 0.59, True, False)]
        return mc.build_rows(72, anchors, 1.0218, True,
                             orig_native_len_s=369.31, stream_len_s=1200.0)

    def test_token_sits_immediately_after_the_marker(self):
        stream_rows, orig_rows = self._rows()
        s = [t for _, _, t in stream_rows if " sync:" in t][0]
        o = [t for _, _, t in orig_rows if " sync:" in t][0]
        self.assertRegex(s, r"^track sync: 1 verified confidence \d")
        self.assertRegex(o, r"^orig072 sync: 1 verified confidence \d")

    def test_emitted_rows_satisfy_both_recognisers(self):
        # RC-1: the anchor rows go out suffix-free, so they satisfy the recognisers
        # exactly as emitted -- no strip needed before folding into a hand file.
        for rows in self._rows():
            for _, _, text in rows:
                if " sync:" not in text:
                    continue
                self.assertTrue(sort_tsv.sync_verified(text), text)
                self.assertTrue(tm.sync_row_verified(text), text)
                self.assertTrue(sort_tsv.parses(text), text)      # grammar-safe as folded

    def test_file_sync_verified_is_not_this_token(self):
        for text in ("file start sync: d336-355.wav 19637.763 verified d328-342",
                     "file sync: d356-375.wav 1203.135 verified by 067"):
            self.assertFalse(sort_tsv.sync_verified(text), text)
            self.assertFalse(tm.sync_row_verified(text), text)

    def test_hand_free_text_after_marker_is_not_the_token(self):
        for text in ("orig015 sync: C start of d065",
                     "track sync: A first four-note",
                     "orig066 sync: B end in mix"):
            self.assertFalse(sort_tsv.sync_verified(text), text)
            self.assertFalse(tm.sync_row_verified(text), text)


class TestSoloProbeSeeding(unittest.TestCase):
    """AP-13: solo-anchor moments become preferred rate-sweep probe positions."""

    def test_positions_come_from_solo_anchors_sorted(self):
        real = tm.solo_anchors
        tm.solo_anchors = lambda orig, cap, lo, hi, top=3: [
            {"mix_s": 44.0, "orig_s": 30.0, "score": 0.9, "run_s": 10.0},
            {"mix_s": 21.0, "orig_s": 7.0, "score": 0.8, "run_s": 8.0}]
        try:
            got = mc.solo_probe_positions(STREAM, ORIG, offset0=TRUE_DELTA)
        finally:
            tm.solo_anchors = real
        self.assertEqual(got, [21.0, 44.0])

    def test_failure_degrades_to_no_positions(self):
        real = tm.solo_anchors
        tm.solo_anchors = lambda *a, **k: (_ for _ in ()).throw(ImportError("no librosa"))
        try:
            got = mc.solo_probe_positions(STREAM, ORIG, offset0=TRUE_DELTA)
        finally:
            tm.solo_anchors = real
        self.assertEqual(got, [])

    def test_sweep_probes_the_seeded_positions(self):
        seen = []
        real = mc._refine_peaks

        def spy(stream, orig2, a_s, b2_s, win_s, radius_s, n_peaks=1):
            seen.append(round(a_s, 3))
            return real(stream, orig2, a_s, b2_s, win_s, radius_s, n_peaks=n_peaks)

        mc._refine_peaks = spy
        try:
            mc.sweep_rate(STREAM, ORIG, TRUE_DELTA, 1.0, span=0.0, step=0.01,
                          probe_positions=[33.25])
        except ValueError:
            pass                # only the probe *positions* are under test here
        finally:
            mc._refine_peaks = real
        self.assertIn(33.25, seen)

    def test_out_of_overlap_positions_are_dropped(self):
        seen = []
        real = mc._refine_peaks

        def spy(stream, orig2, a_s, b2_s, win_s, radius_s, n_peaks=1):
            seen.append(round(a_s, 3))
            return real(stream, orig2, a_s, b2_s, win_s, radius_s, n_peaks=n_peaks)

        mc._refine_peaks = spy
        try:
            mc.sweep_rate(STREAM, ORIG, TRUE_DELTA, 1.0, span=0.0, step=0.01,
                          probe_positions=[-50.0, 1e6])
        except ValueError:
            pass                # only the probe *positions* are under test here
        finally:
            mc._refine_peaks = real
        self.assertNotIn(-50.0, seen)
        self.assertNotIn(1e6, seen)


class TestBothAnchorFormsParse(unittest.TestCase):
    """RC-1 back-compat: every reader accepts anchor rows WITH and WITHOUT the trailing
    ` HINT`. Label files in the wild still carry suffixed rows (pasted from pre-RC-1
    hints), so the old form must keep parsing exactly like the new one."""

    BARE = ["track sync: 1 verified confidence 5.9/10",
            "orig072 sync: 1 verified confidence 5.9/10",
            "track sync: 2 verified confidence 4.1/10 MATCH +0.031s",
            "orig072 start: 1 confidence 5.9/10",
            "orig072 end: 2 confidence 4.1/10"]

    def test_grammar_accepts_both_forms(self):
        for bare in self.BARE:
            for text in (bare, bare + " HINT"):
                self.assertTrue(sort_tsv.parses(text), text)

    def test_verified_recognisers_accept_both_forms(self):
        for bare in self.BARE:
            if " sync:" not in bare:
                continue
            for text in (bare, bare + " HINT"):
                self.assertTrue(sort_tsv.sync_verified(text), text)
                self.assertTrue(tm.sync_row_verified(text), text)

    def test_sync_pairing_accepts_both_forms(self):
        # A hand label file holding one pre-RC-1 (suffixed) pair and one new (bare)
        # pair: track_mix must pair and mark BOTH as machine-checked.
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "d376-395.labels.tsv"), "w",
                      encoding="utf-8") as f:
                f.write("60.0\t60.0\ttrack sync: 1 verified confidence 5.9/10 HINT\n"
                        "60.0\t60.0\torig072 sync: 1 verified confidence 5.9/10 HINT\n"
                        "200.0\t200.0\ttrack sync: 2 verified confidence 4.1/10\n"
                        "200.0\t200.0\torig072 sync: 2 verified confidence 4.1/10\n")
            points = tm.parse_sync_points(tmp)
        self.assertEqual(len(points.get(72, [])), 2)
        self.assertEqual(sorted(p["label"] for p in points[72]), ["1", "2"])
        self.assertTrue(all(p["verified"] for p in points[72]))
