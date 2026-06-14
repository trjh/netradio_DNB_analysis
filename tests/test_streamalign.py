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

from streamalign import align, audio, clips, graph, groundtruth, score, skips, solve  # noqa: E402

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


class ClipTests(unittest.TestCase):
    def test_skip_ahead_clip_construction(self):
        import numpy as np
        sr = audio.SR
        ref = np.sin(np.arange(int(60 * sr)) * 0.01).astype(float)
        skp = ref.copy()  # content irrelevant for length/annotation checks
        # skip-ahead 1s at skipper-local 20s; offset goes -5 -> -6 (more negative).
        walk = ([(float(t), -5.0, 1.0) for t in range(10, 20)]
                + [(float(t), -6.0, 1.0) for t in range(20, 30)])
        skip = {"at_s": 20.0, "before_s": 19.0, "after_s": 21.0, "delta_s": -1.0}
        made = clips.make_skip_clip(skp, ref, skip, walk, pad_s=2.0)
        self.assertIsNotNone(made)
        clip, ann = made
        self.assertAlmostEqual(len(clip) / sr, 5.0, delta=0.2)  # pad + gap + pad
        self.assertIn("AHEAD", " ".join(a["label"] for a in ann))

    def test_manifest_append_dedups_by_id(self):
        import tempfile
        d = tempfile.mkdtemp()
        clips._append_manifest(d, [{"id": "x", "audio": "x.mp3"}])
        clips._append_manifest(d, [{"id": "x", "audio": "x2.mp3"}, {"id": "y", "audio": "y.mp3"}])
        data = json.load(open(os.path.join(d, "manifest.json")))
        by_id = {c["id"]: c for c in data["clips"]}
        self.assertEqual(len(by_id), 2)
        self.assertEqual(by_id["x"]["audio"], "x2.mp3")  # replaced, not duplicated


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


if __name__ == "__main__":
    unittest.main()
