"""Tests for the A1 `LABELTRACK <name>` header support in labels/sort_tsv.py.

Covers the pure scoping helpers (stem reduction, the three name classes, the prefix
expansion rule) and the end-to-end read path: a multi-LABELTRACK export sorts into
correctly scoped rows with the markers stripped, and a file that uses the convention
but is missing a marker on a block boundary fails validation. Legacy marker-less files
must keep working unchanged.
"""

import io
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "labels"))

import sort_tsv  # noqa: E402


def line(ts, text, ts2=None):
    """Build one Audacity export line `start\tend\ttext`."""
    ts2 = ts if ts2 is None else ts2
    return "%.6f\t%.6f\t%s" % (ts, ts2, text)


def run(lines, primary=None):
    """Reset state, set the primary stem, feed `lines`, and assemble the output.

    Returns (write_lines, secondfiles, missing_count) where write_lines is the list of
    assembled label texts and missing_count is report_missing()'s effective error count.
    """
    sort_tsv.reset_state()
    sort_tsv.primary_stem = primary
    for text in lines:
        sort_tsv.process_line(text)
    missing = sort_tsv.report_missing()
    write_lines = sort_tsv.assemble_write_lines(do_adjustment=False)
    texts = [entry[2] for entry in write_lines]
    return texts, dict(sort_tsv.secondfiles), missing


class StemTests(unittest.TestCase):
    def test_strips_label_and_audio_suffixes(self):
        self.assertEqual(sort_tsv._stem("d336-355.labels.tsv"), "d336-355")
        self.assertEqual(sort_tsv._stem("d336-355.labels.txt"), "d336-355")
        self.assertEqual(sort_tsv._stem("path/to/d356-375.wav"), "d356-375")
        self.assertEqual(sort_tsv._stem("d356-375.starter.labels.tsv"), "d356-375")
        self.assertEqual(sort_tsv._stem("orig069"), "orig069")

    def test_capture_stem_detection(self):
        for name in ("d336-355", "d356-375", "d-25-000b", "d180_d-14Nov10-a", "d000-018"):
            self.assertTrue(sort_tsv.is_capture_stem(name), name)
        for name in ("orig069", "track", "track063", "mix", "light percussion"):
            self.assertFalse(sort_tsv.is_capture_stem(name), name)


class ClassifyTests(unittest.TestCase):
    def test_three_name_classes(self):
        self.assertEqual(sort_tsv.classify_track("d336-355", "d336-355"), "primary")
        self.assertEqual(sort_tsv.classify_track("d336-355.wav", "d336-355.labels.tsv"), "primary")
        self.assertEqual(sort_tsv.classify_track("d356-375", "d336-355"), "file")
        self.assertEqual(sort_tsv.classify_track("orig069", "d336-355"), "prefix")

    def test_no_primary_routes_capture_as_file(self):
        # stdin with no --stem: a capture-stem track has no primary to match, so it routes
        # as a secondary rather than the primary block.
        self.assertEqual(sort_tsv.classify_track("d336-355", None), "file")
        self.assertEqual(sort_tsv.classify_track("orig069", None), "prefix")


class ExpandLabelTests(unittest.TestCase):
    def test_idempotent_when_already_prefixed(self):
        self.assertEqual(sort_tsv.expand_label("orig069", "orig069 sync: 0"), "orig069 sync: 0")

    def test_grammar_match_keeps_keyword(self):
        self.assertEqual(sort_tsv.expand_label("orig069", "sync: 0"), "orig069 sync: 0")
        self.assertEqual(sort_tsv.expand_label("orig069", "note: rs1"), "orig069 note: rs1")

    def test_free_text_falls_back_to_note(self):
        self.assertEqual(sort_tsv.expand_label("orig069", "light percussion starts"),
                         "orig069 note: light percussion starts")


class ScopeLabelTests(unittest.TestCase):
    def test_primary_unchanged(self):
        self.assertEqual(
            sort_tsv.scope_label("d336-355", "d336-355", "file start sync: d336-355.wav 0"),
            "file start sync: d336-355.wav 0")

    def test_file_stem_rehomes(self):
        self.assertEqual(
            sort_tsv.scope_label("d356-375", "d336-355", "file end: d356-375.wav COMPLETE"),
            "file_d356-375: file end: d356-375.wav COMPLETE")

    def test_file_stem_idempotent(self):
        self.assertEqual(
            sort_tsv.scope_label("d356-375", "d336-355", "file_d356-375: file end: x COMPLETE"),
            "file_d356-375: file end: x COMPLETE")

    def test_prefix_case_expands(self):
        self.assertEqual(sort_tsv.scope_label("orig069", "d336-355", "light percussion starts"),
                         "orig069 note: light percussion starts")


PRIMARY = "d336-355"

# A three-label-track export: an orig prefix block, a neighbour-file block (capture stem),
# and the primary block. Each block is internally time-sorted and led by its LABELTRACK marker.
MULTITRACK = [
    line(1492.985, "LABELTRACK orig069"),
    line(1516.245, "light percussion starts"),
    line(1539.939, "sync: 0"),
    line(0.000, "LABELTRACK d356-375"),
    line(0.000, "file start sync: d356-375.wav 1203.135 verified by 067"),
    line(100.000, "file end: d356-375.wav COMPLETE"),
    line(0.000, "LABELTRACK d336-355"),
    line(0.000, "file start sync: d336-355.wav 19637.763 verified d328-342"),
    line(50.000, "track sync: A"),
]


class EndToEndTests(unittest.TestCase):
    def test_markers_stripped_and_labels_scoped(self):
        texts, secondfiles, missing = run(MULTITRACK, PRIMARY)
        self.assertEqual(missing, 0)
        # the LABELTRACK marker text never reaches the output
        self.assertFalse(any(t.startswith("LABELTRACK") for t in texts), texts)
        # orig block prefix-expanded
        self.assertIn("orig069 note: light percussion starts", texts)
        self.assertIn("orig069 sync: 0", texts)
        # primary block untouched
        self.assertIn("file start sync: d336-355.wav 19637.763 verified d328-342", texts)
        self.assertIn("track sync: A", texts)

    def test_neighbour_block_routes_to_secondary(self):
        texts, secondfiles, missing = run(MULTITRACK, PRIMARY)
        self.assertIn("d356-375", secondfiles)
        self.assertEqual(len(secondfiles["d356-375"]), 2)
        # the re-homed labels are emitted (with the file_ prefix peeled back off by the
        # existing secondary mechanism)
        self.assertIn("file end: d356-375.wav COMPLETE", texts)
        self.assertIn("file start sync: d356-375.wav 1203.135 verified by 067", texts)


class ValidationTests(unittest.TestCase):
    def test_missing_marker_on_block_boundary_is_an_error(self):
        # drop the d356-375 marker: its block now begins on a backwards timestamp jump
        # with no header -> flagged, and the file does use the convention -> an error.
        broken = [ln for ln in MULTITRACK if "LABELTRACK d356-375" not in ln]
        _texts, _sf, missing = run(broken, PRIMARY)
        self.assertGreater(missing, 0)

    def test_missing_marker_at_start_is_an_error(self):
        # first line is content, not a LABELTRACK, but a later block uses one.
        broken = MULTITRACK[1:]
        _texts, _sf, missing = run(broken, PRIMARY)
        self.assertGreater(missing, 0)

    def test_legacy_no_markers_processes_unchanged(self):
        legacy = [
            line(0.000, "file start sync: d336-355.wav 19637.763 verified d328-342"),
            line(10.000, "orig069 sync: 0"),
            line(20.000, "track sync: A"),
        ]
        texts, _sf, missing = run(legacy, PRIMARY)
        self.assertEqual(missing, 0)  # no convention in use -> not held to it
        self.assertIn("orig069 sync: 0", texts)  # labels untouched
        self.assertIn("track sync: A", texts)


if __name__ == "__main__":
    unittest.main()
