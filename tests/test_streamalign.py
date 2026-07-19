"""Tests for the stream alignment engine (P0/P1).

Ground-truth parsing runs anywhere (labels are committed). Audio-dependent tests
skip gracefully when the capture files / ffmpeg aren't present (they live on Tim's
disk, not in the repo).
"""

import json
import os
import shutil
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

from streamalign import align, audio, emit_labels, graph, groundtruth, score, skips, solve, track_mix  # noqa: E402

# Known hand values from TIMELINE_GUIDE / the labels (master_start seconds).
SMOKE = {
    "d000-018": 0.0,
    "d019-040": 1102.848,
    "d026-073b": 1593.848006,
    "d088-107": 5267.520066,
    "d336-355": 19875.171068,  # chained: d328-342 start + 237.408
}


def _have_audio(*stems):
    return shutil.which("ffmpeg") and all(audio.find_audio_file(s) for s in stems)


class GroundTruthTests(unittest.TestCase):
    def setUp(self):
        self.starts = groundtruth.resolve_starts()
        if "d000-018" not in self.starts:
            self.skipTest("no labels resolved (empty labels dir)")

    def test_smoke_values(self):
        for stem, expected in SMOKE.items():
            self.assertIn(stem, self.starts, stem)
            self.assertAlmostEqual(self.starts[stem], expected, places=3,
                                   msg="%s: %r != %r" % (stem, self.starts[stem], expected))

    def test_anchor_is_zero(self):
        self.assertEqual(self.starts["d000-018"], 0.0)

    def test_edges_reference_real_files(self):
        edges = groundtruth.alignment_edges()
        self.assertTrue(edges)
        # every edge endpoint that's a 'd' tile should appear in the start table
        # at least for the source side (the verified side may be a non-synced ref)
        srcs = {a for a, _ in edges}
        self.assertIn("d001-026b", srcs)


class ScoreTests(unittest.TestCase):
    def test_pairwise_scoring_math(self):
        gt = {"a": 10.0, "b": 64.392}
        results = [{"a": "a", "b": "b", "offset_seconds": 54.392, "confidence": 0.99}]
        rows, summary = score.score_pairwise(results, gt)
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["error_ms"], 0.0, places=6)
        self.assertEqual(summary["n"], 1)

    def test_absolute_scoring_with_anchor(self):
        est = {"x": 5.0, "y": 105.0}
        gt = {"x": 0.0, "y": 100.0}
        rows, summary = score.score_absolute(est, gt, anchor="x")
        # after anchoring on x, both say y=100 -> zero error
        self.assertAlmostEqual(summary["max_samp"], 0.0, places=3)

    def test_consistency_residual(self):
        place = {"a": 0.0, "b": 54.392}
        rows, summary = score.consistency_report(place, [("a", "b", 54.392)])
        self.assertAlmostEqual(rows[0]["residual_ms"], 0.0, places=6)


class AlignTests(unittest.TestCase):
    def test_clean_overlap_within_one_ms(self):
        if not _have_audio("d000-018", "d001-026b"):
            self.skipTest("audio/ffmpeg not available")
        gt = groundtruth.resolve_starts()
        r = align.align_pair("d000-018", "d001-026b")
        expected = gt["d001-026b"] - gt["d000-018"]
        err_samples = abs(r["offset_seconds"] - expected) * audio.SR
        self.assertLess(err_samples, 16, "error %.1f samples" % err_samples)
        self.assertGreater(r["confidence"], 0.9)


class SkipDetectionTests(unittest.TestCase):
    def test_recovers_documented_skips(self):
        # d065-087.labels.tsv documents d084-103b in sync with d065-087 over local
        # [1169.6, 1380.4]s with 4 skips: 1.632, 0.672, 1.248, 1.248 (sum 4.8).
        if not _have_audio("d065-087", "d084-103b"):
            self.skipTest("audio/ffmpeg not available")
        # Call with DEFAULT tuning on purpose: the production defaults must be the
        # validated values, so this fails if they regress to the known-bad ones.
        r = skips.characterise_overlap(
            "d065-087", "d084-103b", 1170.0, 1378.0, 1169.592)
        self.assertEqual(len(r["skips"]), 4, r["skips"])
        self.assertAlmostEqual(sum(s["delta_s"] for s in r["skips"]), 4.8, places=2)
        got = sorted(round(s["delta_s"], 3) for s in r["skips"])
        for got_d, exp_d in zip(got, [0.672, 1.248, 1.248, 1.632]):
            self.assertAlmostEqual(got_d, exp_d, places=2)


class TrackMixTests(unittest.TestCase):
    def test_pairs_plain_and_numbered_track_sync(self):
        import tempfile
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "x.labels.tsv"), "w") as f:
            f.write("10.0\t10.0\torig041 sync: 2\n")        # plain `track sync`
            f.write("11.0\t11.0\ttrack sync: 2\n")
            f.write("20.0\t20.0\torig015 sync: A\n")         # numbered `track015 sync`
            f.write("21.0\t21.0\ttrack015 sync: A\n")
        pts = track_mix.parse_sync_points(d)
        self.assertEqual(len(pts.get(41, [])), 1)
        self.assertEqual(len(pts.get(15, [])), 1)

    def test_note_prefixed_sync_not_consumed(self):
        # A carried-forward note (`note <file>: track sync: N`) references ANOTHER
        # file and must NOT be paired as a current-file sync point.
        import tempfile
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "x.labels.tsv"), "w") as f:
            f.write("5.0\t5.0\torig065 sync: 9\n")
            f.write("100.0\t100.0\tnote d336-355: track sync: 9\n")
        pts = track_mix.parse_sync_points(d)
        self.assertEqual(pts.get(65, []), [])  # the only candidate is inside a note

    def test_AB_rate_is_per_file_not_cross_file(self):
        # f1 has a coherent A/B (rate 1.0); f2 has a stray earlier B from another
        # section. The rate must come from f1's A/B, never f1.A paired with f2.B.
        import tempfile
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "f1.labels.tsv"), "w") as f:
            f.write("100.0\t100.0\torig047 sync: A\n")
            f.write("101.0\t101.0\ttrack047 sync: A\n")
            f.write("200.0\t200.0\torig047 sync: B\n")
            f.write("201.0\t201.0\ttrack047 sync: B\n")  # f1: (201-101)/(200-100)=1.0
        with open(os.path.join(d, "f2.labels.tsv"), "w") as f:
            f.write("10.0\t10.0\torig047 sync: B\n")
            f.write("99.0\t99.0\ttrack047 sync: B\n")     # stray earlier B, must be ignored
        gt = track_mix.track_sync_groundtruth(d)
        self.assertAlmostEqual(gt[47]["rate"], 1.0, places=4)
        self.assertEqual(gt[47]["rate_method"], "AB")

    def test_rate_uses_AB_like_the_sheet(self):
        # track 015 lives in the committed d026-073b labels; rate must match the
        # sheet's (trackB-trackA)/(origB-origA), not a fit over all points.
        gt = track_mix.track_sync_groundtruth()
        if 15 not in gt or gt[15]["rate"] is None:
            self.skipTest("track 15 sync points not in committed labels")
        self.assertAlmostEqual(gt[15]["rate"], 1.010651, places=4)
        self.assertEqual(gt[15]["rate_method"], "AB")

    def _chroma_signal(self, seconds, sr):
        # A deterministic, non-repeating melody: a distinct pitch class every 0.5 s
        # walking up the chromatic scale. Many unique chroma columns give the
        # subsequence DTW a single unambiguous diagonal path (so the slope is a clean
        # rate and the offset is well-defined) — no audio files needed.
        import numpy as np
        t = np.arange(int(seconds * sr)) / sr
        step = 0.5
        y = np.zeros_like(t)
        n = int(seconds / step)
        for i in range(n):
            semitone = i % 12               # chromatic walk, one note per 0.5 s
            f0 = 220.0 * (2 ** (semitone / 12.0))
            m = (t >= i * step) & (t < (i + 1) * step)
            for h in (1, 2, 3):             # a few harmonics for chroma richness
                y[m] += np.sin(2 * np.pi * f0 * h * t[m]) / h
        return (y / 2).astype("float32")

    def test_reliability_gate_matches_real_validation(self):
        # The precision-first gate, fed the actual measured (confidence, norm_cost,
        # slope) from aligning tracks 8/10/13/16/23 to their mix regions. Only 13 and
        # 16 — the two whose recovered rate is within target of the sync ground truth
        # — must pass; 23 (wrong-match, low R²), 10 (degenerate slope, high cost) and
        # 8 (empty mix, NaN slope) must each be rejected. No audio needed.
        import math
        cases = {            # (confidence, norm_cost, slope) -> expected reliable
            8:  (0.0,     0.0104, math.nan),   # empty mix -> NaN slope, fails finite
            10: (0.99846, 0.0206, 1.0),        # degenerate slope -> fails confidence
            13: (1.0,     0.0112, 1.00216),    # clean match within target
            16: (1.0,     0.0130, 1.01130),    # clean match within target
            23: (0.76887, 0.0498, 1.23228),    # wrong-match -> fails conf AND cost
        }
        expect = {8: False, 10: False, 13: True, 16: True, 23: False}
        for trk, (conf, cost, slope) in cases.items():
            self.assertEqual(track_mix.is_reliable(conf, cost, slope), expect[trk],
                             "track %d gate" % trk)

    def test_chroma_dtw_recovers_rate_and_offset(self):
        # On a clean same-speed excerpt the warp path is a straight diagonal: rate ~1,
        # offset ~where the excerpt starts, and the match is reliable. The excerpt ends
        # mid-original (not at its last frame), so this is the regression for scoring
        # `norm_cost` at the SELECTED subsequence endpoint rather than dist[-1, -1] —
        # the latter inflated the cost and falsely flagged this clean match.
        try:
            import librosa  # noqa: F401
        except ImportError:
            self.skipTest("librosa not installed (core python)")
        import numpy as np
        sr = track_mix._audio.SR
        orig = self._chroma_signal(12.0, sr)
        mix = orig[int(2.0 * sr):int(10.0 * sr)]   # same speed, ends 2 s before orig end
        r = track_mix.chroma_dtw_rate(orig, mix, sr=sr, hop=512)
        self.assertAlmostEqual(r["rate"], 1.0, places=1)
        self.assertGreaterEqual(r["confidence"], track_mix._MIN_CONFIDENCE)
        self.assertAlmostEqual(r["offset_orig_s"], 2.0, delta=0.3)
        self.assertTrue(r["reliable"])             # not falsely flagged by end-cost bug
        rng = np.random.default_rng(0)
        noise = rng.standard_normal(int(6.0 * sr)).astype("float32") * 0.1
        rn = track_mix.chroma_dtw_rate(orig, noise, sr=sr, hop=512)
        self.assertGreater(rn["norm_cost"], r["norm_cost"])   # noise matches worse
        self.assertFalse(rn["reliable"])

    def test_chroma_dtw_flags_mix_longer_than_original(self):
        # When the mix region is longer than the whole original (a master span that
        # exceeds the source — track 40), subsequence DTW's premise is violated:
        # librosa reorients X/Y and indexing the endpoint would crash. Must flag, not
        # crash, and not emit a rate.
        try:
            import librosa  # noqa: F401
        except ImportError:
            self.skipTest("librosa not installed (core python)")
        sr = track_mix._audio.SR
        orig = self._chroma_signal(6.0, sr)
        mix = self._chroma_signal(10.0, sr)        # longer than the original
        r = track_mix.chroma_dtw_rate(orig, mix, sr=sr, hop=512)
        self.assertFalse(r["reliable"])
        self.assertIn("note", r)
        self.assertNotEqual(r["rate"], r["rate"])  # NaN

    def test_select_capture_picks_containing_capture(self):
        # source_files is ordered by overlap, not containment: srcs[0] may start after
        # (or end before) the track region. _select_capture must pick the capture whose
        # [start, start+len] fully covers [mb, me], not srcs[0]. Stub the audio layer
        # so no real files are needed (capture length comes from load_audio()).
        sr = track_mix._audio.SR
        lengths = {"capA": 100, "capB": 600, "capC": 200}   # seconds
        starts = {"capA": 0.0, "capB": 90.0, "capC": 700.0}

        class _Stub:
            SR = sr

            @staticmethod
            def find_audio_file(stem, audio_dir=None):
                return stem in lengths

            @staticmethod
            def load_audio(stem, audio_dir=None):
                import numpy as np
                return np.zeros(int(lengths[stem] * sr), dtype="float32")

        orig_audio = track_mix._audio
        track_mix._audio = _Stub
        try:
            # span [200, 540] sits inside capB (90..690), not capA (0..100, srcs[0]).
            srcs = ["capA.wav", "capB.wav", "capC.wav"]
            cap, cstart = track_mix._select_capture(srcs, 200.0, 540.0, starts)
            self.assertEqual(cap, "capB")
            self.assertEqual(cstart, 90.0)
            # span fully outside every capture -> best partial / None reason
            cap2, info2 = track_mix._select_capture(srcs, 2000.0, 2100.0, starts)
            self.assertIsNone(cap2)
        finally:
            track_mix._audio = orig_audio


class EmitLabelsTests(unittest.TestCase):
    def test_emit_roundtrips_and_is_auto_generated(self):
        import tempfile
        out = tempfile.mkdtemp()
        positions = {"d900-901": 100.5, "d902-903": 250.0}
        emit_labels.emit_labels(positions, out, {"d900-901": 60.0, "d902-903": 60.0})
        files = sorted(os.listdir(out))
        # programmatic output is ALWAYS <stem>.auto.labels.tsv
        self.assertEqual(files, ["d900-901.auto.labels.tsv", "d902-903.auto.labels.tsv"])
        for fn in files:
            with open(os.path.join(out, fn)) as f:
                for line in f:
                    self.assertTrue(line.rstrip("\n").endswith("AUTO GENERATED"), line)
        starts = groundtruth.resolve_starts(out)   # reads *.auto.labels.tsv too
        self.assertAlmostEqual(starts["d900-901"], 100.5, places=3)
        self.assertAlmostEqual(starts["d902-903"], 250.0, places=3)

    def test_always_auto_suffix_and_never_overwrites_hand(self):
        import tempfile
        out = tempfile.mkdtemp()
        # a hand <stem>.labels.tsv sitting in the SAME dir must be left untouched
        hand_path = os.path.join(out, "d900-901.labels.tsv")
        with open(hand_path, "w") as f:
            f.write("0\t0\thand label\n")
        emit_labels.emit_labels({"d900-901": 5.0}, out, {"d900-901": 10.0})
        # programmatic output goes to .auto.labels.tsv; the hand file is unchanged
        self.assertTrue(os.path.exists(os.path.join(out, "d900-901.auto.labels.tsv")))
        with open(hand_path) as f:
            self.assertEqual(f.read(), "0\t0\thand label\n")


class GraphTests(unittest.TestCase):
    def test_filename_range(self):
        self.assertEqual(graph.filename_range("d356-375"), (356, 375))
        self.assertEqual(graph.filename_range("d425-438b"), (425, 438))
        self.assertIsNone(graph.filename_range("dnb-14Nov02-a"))

    def test_candidate_pairs_prunes_distant(self):
        stems = ["d356-375", "d376-395", "d505-524"]
        pairs = graph.candidate_pairs(stems, max_gap_min=30)
        self.assertIn(("d356-375", "d376-395"), pairs)
        self.assertNotIn(("d356-375", "d505-524"), pairs)  # 149 min apart

    def test_connected_components(self):
        stems = ["a", "b", "c", "d"]
        edges = [{"a": "a", "b": "b"}, {"a": "b", "b": "c"}]
        comps = graph.connected_components(stems, edges)
        self.assertEqual(comps[0], {"a", "b", "c"})
        self.assertIn({"d"}, comps)

    def test_blind_offset_clean_pair(self):
        # Large, clean overlap: blind_offset locks to ±1 sample at high confidence.
        if not _have_audio("d000-018", "d006-025"):
            self.skipTest("audio/ffmpeg not available")
        gt = groundtruth.resolve_starts()
        off, conf = graph.blind_offset("d000-018", "d006-025")
        expected = gt["d006-025"] - gt["d000-018"]
        self.assertLess(abs(off - expected) * audio.SR, 16)
        self.assertGreater(conf, 0.9)

    def test_blind_offset_small_overlap_limitation(self):
        # Pins the documented limitation: blind_offset is UNRELIABLE on a small /
        # skip-heavy overlap (d084-103b overlaps d065-087 over only ~210 s, with 4
        # skips). It scores low here even though the files DO overlap, so callers
        # must not treat a low score as "no overlap". If a future detector fixes
        # this, update blind_offset's scope docs and this test together.
        if not _have_audio("d065-087", "d084-103b"):
            self.skipTest("audio/ffmpeg not available")
        _off, conf = graph.blind_offset("d065-087", "d084-103b")
        self.assertLess(conf, 0.8, "small-overlap limitation unexpectedly resolved "
                        "(conf=%.3f); update blind_offset scope + README" % conf)


# The skip-clip renderer (clips.py) was retired 2026-07-15 -> Archive/skip-clips/. Skip
# DETECTION (skips.py) and the decision store are still tested in test_skip_review.py.


class SolveTests(unittest.TestCase):
    def test_propagates_offsets_from_anchor(self):
        edges = [
            {"a": "x", "b": "y", "offset_s": 10.0, "conf": 0.99},
            {"a": "y", "b": "z", "offset_s": 20.0, "conf": 0.99},
        ]
        pos = solve.solve_positions(edges, anchor="x", anchor_master=0.0)
        self.assertAlmostEqual(pos["x"], 0.0)
        self.assertAlmostEqual(pos["y"], 10.0)
        self.assertAlmostEqual(pos["z"], 30.0)

    def test_prefers_higher_confidence_path(self):
        # y reachable from x directly (noisy, offset 99) or via the high-conf chain
        # x->w->y (offsets 10 then 5 = 15). Best-first should take the clean path.
        edges = [
            {"a": "x", "b": "y", "offset_s": 99.0, "conf": 0.50},
            {"a": "x", "b": "w", "offset_s": 10.0, "conf": 0.99},
            {"a": "w", "b": "y", "offset_s": 5.0, "conf": 0.99},
        ]
        pos = solve.solve_positions(edges, anchor="x")
        self.assertAlmostEqual(pos["y"], 15.0)

    def test_unconnected_files_omitted(self):
        edges = [{"a": "x", "b": "y", "offset_s": 1.0, "conf": 0.9}]
        pos = solve.solve_positions(edges, anchor="x")
        self.assertNotIn("orphan", pos)

    def test_solve_robust_drops_outlier_with_redundancy(self):
        # z is reached by three edges; two agree (z=15) and one (x->z=100) is a gross
        # outlier. With independent corroboration (>=3 edges at z), it's dropped.
        edges = [
            {"a": "x", "b": "y", "offset_s": 10.0, "conf": 0.95},
            {"a": "x", "b": "w", "offset_s": 20.0, "conf": 0.95},
            {"a": "y", "b": "z", "offset_s": 5.0, "conf": 0.95},   # z = 15
            {"a": "w", "b": "z", "offset_s": -5.0, "conf": 0.95},  # z = 15
            {"a": "x", "b": "z", "offset_s": 100.0, "conf": 0.99}, # bad (high conf)
        ]
        pos, dropped = solve.solve_robust(edges, anchor="x", max_residual_s=0.5)
        self.assertEqual(len(dropped), 1)
        self.assertEqual({dropped[0]["a"], dropped[0]["b"]}, {"x", "z"})
        self.assertAlmostEqual(pos["z"], 15.0)

    def test_solve_robust_keeps_ambiguous_triangle(self):
        # Bare triangle with a HIGH-conf bad edge: which edge is wrong is genuinely
        # ambiguous, so nothing is dropped (better than dropping a good edge) — the
        # inconsistency is left for placement_diagnostics to flag.
        edges = [
            {"a": "x", "b": "y", "offset_s": 100.0, "conf": 0.99},  # bad, but confident
            {"a": "x", "b": "z", "offset_s": 30.0, "conf": 0.90},
            {"a": "y", "b": "z", "offset_s": 20.0, "conf": 0.90},
        ]
        _pos, dropped = solve.solve_robust(edges, anchor="x", max_residual_s=0.5)
        self.assertEqual(dropped, [])  # no good edge wrongly dropped

    def test_placement_diagnostics(self):
        # y is corroborated by two agreeing edges; z is single-edge (uncorroborated).
        pos = {"x": 0.0, "y": 10.0, "z": 30.0}
        edges = [
            {"a": "x", "b": "y", "offset_s": 10.0, "conf": 0.9},
            {"a": "x", "b": "y", "offset_s": 10.02, "conf": 0.9},  # agrees (<0.1 s)
            {"a": "y", "b": "z", "offset_s": 20.0, "conf": 0.9},
        ]
        diag = solve.placement_diagnostics(pos, edges)
        self.assertTrue(diag["y"]["corroborated"])
        self.assertFalse(diag["z"]["corroborated"])  # only one edge
        self.assertEqual(diag["z"]["edges"], 1)

    def test_clean_region_solves_to_sub_ms(self):
        # The anchored clean overlap chain places files to ~1 sample. Use a small
        # known-clean edge set so loop/skip edges don't enter (those are the
        # documented edge-measurement limits, validated separately).
        if not _have_audio("d000-018", "d006-025", "d019-040"):
            self.skipTest("audio/ffmpeg not available")
        edges = solve.measure_edges(
            [("d000-018", "d006-025"), ("d006-025", "d019-040")], conf_min=0.7)
        pos = solve.solve_positions(edges, anchor="d000-018")
        gt = groundtruth.resolve_starts()
        for stem in ("d006-025", "d019-040"):
            self.assertIn(stem, pos)
            self.assertLess(abs(pos[stem] - gt[stem]) * audio.SR, 16)


class TailSolveTests(unittest.TestCase):
    def test_constants_are_consistent(self):
        from streamalign import tail
        # the orphan and bridge target are tail files, not part of Session B's solve
        sb_nodes = {n for e in tail.SESSION_B_EDGES for n in e}
        self.assertNotIn(tail.ORPHAN, sb_nodes)
        self.assertIn(tail.ANCHOR_REF, sb_nodes)
        # the clean anchor edges never use the two-offset pre-roll files
        anchor_targets = {b for _a, b in tail.WRAP_ANCHOR_EDGES}
        self.assertNotIn("d-25-000b", anchor_targets)
        self.assertNotIn("d-25-005b", anchor_targets)

    def test_tail_solve_corroborated(self):
        from streamalign import tail
        stems = {n for e in tail.SESSION_B_EDGES for n in e}
        stems |= {b for _a, b in tail.WRAP_ANCHOR_EDGES} | set(tail.BRIDGE_EDGE)
        if not _have_audio(*stems):
            self.skipTest("audio/ffmpeg not available")
        res = tail.solve_tail()
        # all 14 Session-B files placed and cross-checked to 0 residual
        self.assertEqual(len(res["absolute"]), 14)
        self.assertTrue(all(d["corroborated"] for d in res["diagnostics"].values()))
        # the 3 clean loop-wrap edges agree on one anchor
        self.assertLess(res["anchor_spread_s"], 0.01)
        self.assertAlmostEqual(res["s_star"], -6928.648, places=1)
        # d512-005 sits ~869 s before the d000-018 loop anchor (master 0)
        self.assertAlmostEqual(res["absolute"]["d512-005"], -869.061, places=1)
        self.assertEqual(res["orphan"], "d396-415")


if __name__ == "__main__":
    unittest.main()
