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
    def setUp(self):
        sort_tsv.reset_state()

    def test_idempotent_when_already_prefixed(self):
        self.assertEqual(sort_tsv.expand_label("orig069", "orig069 sync: 0"), "orig069 sync: 0")

    def test_grammar_match_keeps_keyword(self):
        self.assertEqual(sort_tsv.expand_label("orig069", "sync: 0"), "orig069 sync: 0")
        self.assertEqual(sort_tsv.expand_label("orig069", "note: rs1"), "orig069 note: rs1")

    def test_free_text_falls_back_to_note(self):
        self.assertEqual(sort_tsv.expand_label("orig069", "light percussion starts"),
                         "orig069 note: light percussion starts")


class QualifierTests(unittest.TestCase):
    def test_track_name_yields_the_original_it_is_about(self):
        # the LABELTRACK name is a *track* name; the qualifier is the original it describes.
        # `069-dig` and `069.vinyl` are two tracks about the same original.
        self.assertEqual(sort_tsv.track_qualifier("070"), "orig070")
        self.assertEqual(sort_tsv.track_qualifier("069-dig"), "orig069")
        self.assertEqual(sort_tsv.track_qualifier(sort_tsv._stem("069.vinyl")), "orig069")
        self.assertEqual(sort_tsv.track_qualifier("orig069"), "orig069")
        self.assertEqual(sort_tsv.track_qualifier("mix"), "mix")

    def test_labels_suffix_is_stripped(self):
        self.assertEqual(sort_tsv._stem("070.labels"), "070")
        self.assertEqual(sort_tsv._stem("069-dig.labels"), "069-dig")
        self.assertEqual(sort_tsv._stem("d356-375.labels"), "d356-375")

    def test_scope_number(self):
        self.assertEqual(sort_tsv.scope_number("069-dig"), "069")
        self.assertIsNone(sort_tsv.scope_number("mix"))


class KeywordShapeTests(unittest.TestCase):
    """Free text is free; a label that *tried* to be a keyword and failed is an error."""

    def setUp(self):
        sort_tsv.reset_state()

    def test_free_text_is_not_keyword_shaped(self):
        for label in ("start overlap", "end drumline", "close next sync", "peak", "drum starts",
                      "big zero sync", "sync2.1-spectro", "audiofile align point for sync2",
                      "vocals: oh-ohh (rough sync with 1st)", "break: wah-oh-ah-ooh...",
                      "urban.style.music (sync1)", '"what are you doing here?"'):
            self.assertFalse(sort_tsv.is_keyword_shaped(label), label)

    def test_typos_are_keyword_shaped(self):
        for label in ("orig070 start", "orig069 end", "orig069: start", "s71e1",
                      "file start sync d376-395.wav MARK verified"):
            self.assertTrue(sort_tsv.is_keyword_shaped(label), label)
            self.assertFalse(sort_tsv.parses(label), label)

    def test_the_argument_after_the_colon_is_optional(self):
        # `orig070 start: A` anchors the start to sync point A; a bare `orig070 start:` just
        # says the original begins here -- the timestamp is the data, there is no sync point
        # to name. The rule used to demand an argument (`:\s+(.*)`) and rejected the latter.
        for label in ("orig070 start:", "orig069 end:", "orig071 start:", "orig069 note:"):
            self.assertTrue(sort_tsv.parses(label), label)
        for label in ("orig070 start: A", "orig069 note: rs1"):
            self.assertTrue(sort_tsv.parses(label), label)
        # the colon itself is still required
        self.assertFalse(sort_tsv.parses("orig070 start"))

    def test_a_bare_start_is_emitted_untouched_in_its_own_track(self):
        self.assertEqual(sort_tsv.scope_label("070.labels", "d356-375", "orig070 start:"),
                         "orig070 start:")
        self.assertEqual(sort_tsv.keyword_errors, [])
        self.assertEqual(sort_tsv.auto_notes, [])

    def test_free_text_auto_notes_silently(self):
        self.assertEqual(sort_tsv.expand_label("orig070", "close next sync"),
                         "orig070 note: close next sync")
        self.assertEqual(sort_tsv.expand_label(None, "start overlap"), "note: start overlap")
        self.assertEqual(sort_tsv.keyword_errors, [])
        self.assertEqual(len(sort_tsv.auto_notes), 2)

    def test_typod_keyword_errors_and_is_left_alone(self):
        # emitted as written -- we don't guess the fix -- but recorded as an error
        self.assertEqual(sort_tsv.expand_label("orig070", "orig070 start"), "orig070 start")
        self.assertEqual(sort_tsv.expand_label(None, "s71e1"), "s71e1")
        self.assertEqual(len(sort_tsv.keyword_errors), 2)
        self.assertEqual(sort_tsv.auto_notes, [])

    def test_valid_keyword_is_never_prefixed(self):
        # the bug: `orig070 sync: A` in LABELTRACK 070 became `070 note: orig070 sync: A`
        self.assertEqual(sort_tsv.scope_label("070.labels", "d356-375", "orig070 sync: A"),
                         "orig070 sync: A")
        self.assertEqual(sort_tsv.keyword_errors, [])

    def test_cross_scope_reference_is_an_error(self):
        # `orig017 sync: A` inside LABELTRACK 071 -- parses, but 017 is a typo for 071.
        # This one is invisible to any grammar check; only the scope catches it.
        self.assertEqual(sort_tsv.scope_label("071.labels", "d356-375", "orig017 sync: A"),
                         "orig017 sync: A")
        self.assertEqual(len(sort_tsv.keyword_errors), 1)
        self.assertIn("017", sort_tsv.keyword_errors[0][2])

    def test_matching_scope_reference_is_fine(self):
        self.assertEqual(sort_tsv.scope_label("071.labels", "d356-375", "orig071 sync: A"),
                         "orig071 sync: A")
        self.assertEqual(sort_tsv.keyword_errors, [])

    def test_note_body_may_mention_other_originals(self):
        # the scope check reads the label's *head*, not a note's free-text body
        sort_tsv.scope_label("071.labels", "d356-375", "orig071 note: 069s0 is the digital sync")
        self.assertEqual(sort_tsv.keyword_errors, [])


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
        # the re-homed labels keep their `file_<stem>:` prefix on the way out -- see
        # test_the_starter_link_survives_a_sort
        self.assertIn("file_d356-375: file end: d356-375.wav COMPLETE", texts)
        self.assertIn("file_d356-375: file start sync: d356-375.wav 1203.135 verified by 067",
                      texts)

    def test_the_starter_link_survives_a_sort(self):
        # `streamalign starter` reads `file_<other>:` rows back out of the committed
        # .labels.tsv (emit_labels._LINK_RE) to seed the neighbour's starter file. sort_tsv
        # used to PEEL that prefix on write, so sorting a file that carried a neighbour's
        # label track destroyed the link -- and dumped the neighbour's labels into this
        # file's .tsv as anonymous rows at the neighbour's timestamps. Sorting must be
        # idempotent for these rows.
        link = "file_d356-375: file start sync: d356-375.wav 1203.135 verified by 067"
        first, _sf, _m = run([line(0.000, "LABELTRACK d336-355"),
                              line(0.000, "file start sync: d336-355.wav 0.0 verified"),
                              line(1203.135, link)], PRIMARY)
        self.assertIn(link, first)
        # and again over its own output -- still there, unchanged
        second, _sf, _m = run([line(0.000, "file start sync: d336-355.wav 0.0 verified"),
                               line(1203.135, link)], PRIMARY)
        self.assertIn(link, second)


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

    def test_wav_suffixed_file_prefix_does_not_double_up(self):
        # The crash: a label in LABELTRACK d376-395 that already carried its own
        # `file_d376-395.wav:` prefix was compared against the stem prefix (`file_d376-395:`),
        # didn't match, and got double-prefixed. process_entry() peeled the outer one and
        # bucketed an inner label that *still* began with `file_…:`; assemble_write_lines()
        # then re-split it while iterating secondfiles -> RuntimeError: dictionary changed
        # size during iteration.
        self.assertEqual(
            sort_tsv.scope_label("d376-395.labels", "d356-375",
                                 "file_d376-395.wav: file start sync: d376-395.wav 0.0 verified"),
            "file_d376-395: file start sync: d376-395.wav 0.0 verified")

    def test_secondary_reentry_does_not_crash_the_write_pass(self):
        crasher = [
            line(0.000, "LABELTRACK d356-375"),
            line(0.000, "file start sync: d356-375.wav 0.0 verified by 067"),
            line(0.000, "LABELTRACK d376-395"),
            line(1200.000, "file_d376-395.wav: file start sync: d376-395.wav 0.0 verified"),
        ]
        texts, secondfiles, missing = run(crasher, "d356-375")   # used to raise RuntimeError
        self.assertEqual(missing, 0)
        self.assertEqual(list(secondfiles), ["d376-395"])        # one bucket, not two
        self.assertIn("file_d376-395: file start sync: d376-395.wav 0.0 verified", texts)

    def test_adjust_rewrites_secondary_entries(self):
        # secondary entries were tuples; adjust_line() assigns into the entry in place, so
        # any file with a secondary raised TypeError under --adjust.
        sort_tsv.reset_state()
        sort_tsv.primary_stem = "d356-375"
        for text in [
            line(10.000, "LABELTRACK d356-375"),
            line(10.000, "file start sync: d356-375.wav 100.0 verified by 067"),
            line(0.000, "LABELTRACK d376-395"),
            line(1200.000, "file_d376-395: track sync: A"),
        ]:
            sort_tsv.process_line(text)
        write_lines = sort_tsv.assemble_write_lines(do_adjustment=True)
        starts = {entry[2]: entry[0] for entry in write_lines}
        # timestamps shift by the 10.0 file start; the row keeps its file_ prefix
        self.assertEqual(starts["file_d376-395: track sync: A"], 1190.0)

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
