"""Tests for the A2 starter-file emitter + the seed-only exclusion.

`emit_starter` carries the owner's labels at/after each `file_<other>:` link onto the
neighbour's local timeline as a seed `<other>.starter.labels.tsv`, with the anchor offset
derived from `groundtruth.resolve_starts`. Starter files must be excluded everywhere the
pipeline reads labels (groundtruth/solve, track_mix, build_track_metadata, Code.js).

These run anywhere — pure file I/O, no audio/ffmpeg (the captures live on Tim's disk).
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

from streamalign import emit_labels, groundtruth  # noqa: E402


def write_labels(path, rows):
    with open(path, "w", encoding="utf-8") as handle:
        for a, b, text in rows:
            handle.write("%.6f\t%.6f\t%s\n" % (a, b, text))


# An owner capture (d336-355) anchored at master 1000.0 that homes a neighbour (d356-375)
# starting at local 1200.0, with labels on either side of the link.
OWNER = "d336-355"
OTHER = "d356-375"
OWNER_ROWS = [
    (0.0, 0.0, "file start sync: d336-355.wav 1000.000000 verified d328-342"),
    (500.0, 500.0, "orig069 note: before the link"),
    (1200.0, 1200.0, "file_d356-375: file start sync: d356-375.wav verified by 067"),
    (1300.0, 1305.0, "orig070 note: after the link"),
    (1400.0, 1400.0, "track sync: A"),
]


class ExclusionPredicateTests(unittest.TestCase):
    def test_hand_and_auto_included_starter_excluded(self):
        self.assertTrue(groundtruth.is_pipeline_label_file("d336-355.labels.tsv"))
        self.assertTrue(groundtruth.is_pipeline_label_file("d356-375.auto.labels.tsv"))
        self.assertFalse(groundtruth.is_pipeline_label_file("d356-375.starter.labels.tsv"))


class EmitStarterTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        write_labels(os.path.join(self.dir, OWNER + ".labels.tsv"), OWNER_ROWS)

    def _read(self, path):
        with open(path, "r", encoding="utf-8") as handle:
            return [ln.rstrip("\n").split("\t", 2) for ln in handle if ln.strip()]

    def test_writes_starter_with_derived_anchor(self):
        written = emit_labels.emit_starter(OWNER, labels_dir=self.dir)
        self.assertIn(OTHER, written)
        rows = self._read(written[OTHER])
        # anchor at local 0.0, master derived as owner(1000) + link(1200) = 2200
        self.assertEqual(rows[0][0], "0.000000")
        self.assertIn("file start sync: d356-375.wav 2200.000000", rows[0][2])
        self.assertTrue(rows[0][2].endswith(" STARTER"))

    def test_carries_after_link_shifted_and_drops_before(self):
        written = emit_labels.emit_starter(OWNER, labels_dir=self.dir)
        texts = [r[2] for r in self._read(written[OTHER])]
        starts = {r[2]: r[0] for r in self._read(written[OTHER])}
        # after-link label carried, shifted by 1200, suffixed
        self.assertIn("orig070 note: after the link STARTER", texts)
        self.assertEqual(starts["orig070 note: after the link STARTER"], "100.000000")
        self.assertIn("track sync: A STARTER", texts)
        # before-link label dropped; originating link row not duplicated
        self.assertFalse(any("before the link" in t for t in texts))
        self.assertFalse(any(t.startswith("file_d356-375:") for t in texts))

    def test_no_links_returns_empty(self):
        write_labels(os.path.join(self.dir, "d000-018.labels.tsv"),
                     [(0.0, 0.0, "file start sync: d000-018.wav 0.000000 verified x")])
        self.assertEqual(emit_labels.emit_starter("d000-018", labels_dir=self.dir), {})


class StarterExcludedFromSolveTests(unittest.TestCase):
    def test_resolve_starts_ignores_starter_files(self):
        d = tempfile.mkdtemp()
        write_labels(os.path.join(d, OWNER + ".labels.tsv"), OWNER_ROWS)
        # a stray starter file with a bogus anchor must NOT leak into resolve_starts
        write_labels(os.path.join(d, "zzz.starter.labels.tsv"),
                     [(0.0, 0.0, "file start sync: zzz.wav 999.000000 STARTER")])
        starts = groundtruth.resolve_starts(d)
        self.assertIn(OWNER, starts)
        self.assertNotIn("zzz", starts)


if __name__ == "__main__":
    unittest.main()
