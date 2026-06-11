"""Tests for the stream alignment engine (P0/P1).

Ground-truth parsing runs anywhere (labels are committed). Audio-dependent tests
skip gracefully when the capture files / ffmpeg aren't present (they live on Tim's
disk, not in the repo).
"""

import os
import shutil
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

from streamalign import align, audio, groundtruth, score, skips  # noqa: E402

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
        r = skips.characterise_overlap(
            "d065-087", "d084-103b", 1170.0, 1378.0, 1169.592,
            win_s=8.0, hop_s=1.0, radius_s=3.0)
        self.assertEqual(len(r["skips"]), 4, r["skips"])
        self.assertAlmostEqual(sum(s["delta_s"] for s in r["skips"]), 4.8, places=2)
        got = sorted(round(s["delta_s"], 3) for s in r["skips"])
        for got_d, exp_d in zip(got, [0.672, 1.248, 1.248, 1.632]):
            self.assertAlmostEqual(got_d, exp_d, places=2)


if __name__ == "__main__":
    unittest.main()
