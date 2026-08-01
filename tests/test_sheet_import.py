"""The sheet import filter, mirrored and pinned.

`sheetscript/Code.js` decides which files in `labels/` reach the Google Sheet. It is Apps
Script with no test harness, so the rule is easy to drift. This file encodes the SAME rule in
Python and pins three things:

  1. it matches `groundtruth.is_pipeline_label_file` (so the sheet and the engine can never
     again disagree about what is real) -- with `remainder.tsv` the one hand-kept exception;
  2. against the real committed `labels/` listing, the scratch files (`*.hints.tsv`, a bare
     mis-named `<stem>.tsv`, `*.starter.labels.tsv`) are excluded and the hand/engine files
     are included;
  3. the Code.js source still contains the rule this mirror claims -- a guard against the JS
     changing while this test keeps passing.
"""

import os
import re
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))
sys.path.insert(0, os.path.join(REPO, "labels"))

import sort_tsv  # noqa: E402
from streamalign import groundtruth as gt  # noqa: E402


def sheet_imports(name):
    """Python mirror of Code.js's file-level import filter.

    Note `.starter.labels.tsv` ALSO ends `.labels.tsv`, so it must be excluded explicitly --
    a seed file is not a hand file.
    """
    return ((name.endswith(".labels.tsv") and not name.endswith(".starter.labels.tsv"))
            or name == "remainder.tsv")


class TheRuleMatchesThePipeline(unittest.TestCase):
    def test_sheet_and_engine_agree_except_for_remainder(self):
        for name in ("d356-375.labels.tsv", "d356-375.auto.labels.tsv",
                     "d356-375.starter.labels.tsv", "d356-375.hints.tsv",
                     "d356-375.tsv", "remainder.tsv", "notes.txt"):
            expected = gt.is_pipeline_label_file(name) or name == "remainder.tsv"
            self.assertEqual(sheet_imports(name), expected, name)


class ScratchNeverReachesTheSheet(unittest.TestCase):
    def test_the_leakers_are_excluded(self):
        # these three are committed/on-disk today and were all being swept into the sheet
        for name in ("d356-375.hints.tsv", "d356-375.tsv", "d376-395.starter.labels.tsv"):
            self.assertFalse(sheet_imports(name), name)

    def test_hand_and_auto_and_remainder_are_included(self):
        for name in ("d336-355.labels.tsv", "d900-901.auto.labels.tsv", "remainder.tsv"):
            self.assertTrue(sheet_imports(name), name)

    def test_against_the_real_labels_listing(self):
        labels = os.path.join(REPO, "labels")
        for name in os.listdir(labels):
            if not name.endswith(".tsv"):
                continue
            # every .tsv the sheet takes is a pipeline file or the remainder; nothing else
            if sheet_imports(name):
                self.assertTrue(gt.is_pipeline_label_file(name) or name == "remainder.tsv", name)


class CodeJsStillHasTheRule(unittest.TestCase):
    def test_source_matches_this_mirror(self):
        src = open(os.path.join(REPO, "sheetscript", "Code.js"), encoding="utf-8").read()
        # the exact predicate, whitespace-insensitive
        self.assertTrue(
            re.search(r"endsWith\('\.labels\.tsv'\)\s*&&\s*!file\.name\.endsWith\('\.starter"
                      r"\.labels\.tsv'\)\)\s*\|\|\s*file\.name\s*===\s*'remainder\.tsv'", src),
            "Code.js import filter no longer matches tests/test_sheet_import.sheet_imports")


# AP-04: the sheet's Verified column keys on the same rule as labels/sort_tsv.sync_verified
# -- `verified` immediately after a track/orig sync marker, NEVER a file-sync row's
# `verified <neighbour>` keyword. Code.js is Apps Script with no test harness, so (same
# pattern as above) the Python side is the mirror and the JS source is pinned against it.
_JS_VERIFIED_RE = r"/\^\(orig\|track\)\\d\*\\s\+sync:\\s\*\\S\+\\s\+verified\\b/i"


class VerifiedColumnMatchesTheLabelRule(unittest.TestCase):
    def test_code_js_still_has_the_verified_regex(self):
        src = open(os.path.join(REPO, "sheetscript", "Code.js"), encoding="utf-8").read()
        self.assertTrue(
            re.search(_JS_VERIFIED_RE, src),
            "Code.js Verified-column regex no longer mirrors labels/sort_tsv.sync_verified")
        # and the flag rides every emitted row (12th column)
        self.assertIn("syncVerified,", src)

    def test_code_js_names_the_new_column(self):
        # the import must not fill a blank-headed column: updatesheet() self-migrates the
        # row-1/column-12 header to 'Verified' (review finding, iteration 1)
        src = open(os.path.join(REPO, "sheetscript", "Code.js"), encoding="utf-8").read()
        self.assertTrue(
            re.search(r"getRange\(1,\s*12\)[\s\S]{0,300}setValue\('Verified'\)", src),
            "Code.js no longer writes the Verified header for its new column")

    def test_python_mirror_agrees_with_sort_tsv(self):
        # the JS regex, transliterated; parity with the canonical Python rule
        js_mirror = re.compile(r"^(orig|track)\d*\s+sync:\s*\S+\s+verified\b", re.I)
        for label, expect in (
                ("track sync: 1 verified confidence 5.9/10", True),
                ("orig072 sync: 3 verified confidence 5.9/10", True),
                ("track sync: A first four-note", False),
                ("track sync: A first four-note verified", False),
                ("file start sync: d336-355.wav 19637.763 verified d328-342", False),
                ("file sync: d356-375.wav 1203.135 verified by 067", False)):
            self.assertEqual(bool(js_mirror.match(label)), expect, label)
            self.assertEqual(sort_tsv.sync_verified(label), expect, label)


if __name__ == "__main__":
    unittest.main()
