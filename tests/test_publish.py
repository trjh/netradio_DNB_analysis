"""Tests for the A4 publish gate (labels/publish.py).

The hard gate is the safety-critical part: valid hand labels pass; bad-syntax,
unverified, not-COMPLETE, missing-anchor, and seed/engine files are refused with a
clear per-file message and a non-zero (zero-push) exit. The git/push/refresh side of
publish() is exercised only in --dry-run (no network, no real push).
"""

import os
import sys
import tempfile
import unittest

LABELS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "labels")
sys.path.insert(0, LABELS_DIR)

import publish  # noqa: E402


COMPLETE_FILE = [
    "0.000000\t0.000000\tfile start sync: d999-000.wav 100.000000 verified d998-999",
    "10.000000\t10.000000\torig069 sync: 0",
    "20.000000\t25.000000\tfile end: d999-000.wav COMPLETE",
]


def write(dir_, stem, lines):
    path = os.path.join(dir_, stem + ".labels.tsv")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    return path


class GateTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_complete_verified_file_passes(self):
        path = write(self.dir, "d999-000", COMPLETE_FILE)
        self.assertEqual(publish.gate_errors(path), [])

    def test_unverified_sync_is_blocked(self):
        lines = list(COMPLETE_FILE)
        lines[0] = "0.000000\t0.000000\tfile start sync: d999-000.wav 100.000000"  # no `verified`
        path = write(self.dir, "d999-000", lines)
        errs = publish.gate_errors(path)
        self.assertTrue(any("verified" in e for e in errs), errs)

    def test_missing_complete_is_blocked(self):
        lines = list(COMPLETE_FILE)
        lines[2] = "20.000000\t25.000000\tfile end: d999-000.wav"  # no COMPLETE
        path = write(self.dir, "d999-000", lines)
        errs = publish.gate_errors(path)
        self.assertTrue(errs)
        self.assertTrue(any("COMPLETE" in e for e in errs), errs)

    def test_no_file_end_anchor_is_blocked(self):
        path = write(self.dir, "d999-000", COMPLETE_FILE[:2])  # drop the file end row
        errs = publish.gate_errors(path)
        self.assertTrue(any("not finished" in e for e in errs), errs)

    def test_bad_syntax_row_is_blocked(self):
        lines = list(COMPLETE_FILE) + ["30.000000\t30.000000\tthis is not valid grammar"]
        path = write(self.dir, "d999-000", lines)
        errs = publish.gate_errors(path)
        self.assertTrue(any("WARNING" in e or "Unrecognized" in e for e in errs), errs)

    def test_starter_and_auto_files_refused(self):
        sp = os.path.join(self.dir, "d999-000.starter.labels.tsv")
        ap = os.path.join(self.dir, "d999-000.auto.labels.tsv")
        for p, lines in ((sp, COMPLETE_FILE), (ap, COMPLETE_FILE)):
            with open(p, "w", encoding="utf-8") as h:
                h.write("\n".join(lines) + "\n")
        self.assertTrue(any("seed-only" in e for e in publish.gate_errors(sp)))
        self.assertTrue(any("never hand-published" in e for e in publish.gate_errors(ap)))


class AllOrNothingTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_one_bad_file_blocks_the_whole_publish(self):
        good = write(self.dir, "d999-000", COMPLETE_FILE)
        bad = write(self.dir, "d999-001", COMPLETE_FILE[:2])  # missing file end
        rc = publish.publish([good, bad], "msg", dry_run=True)
        self.assertEqual(rc, 1)  # zero-push exit because one target failed

    def test_all_valid_dry_run_succeeds_without_pushing(self):
        good = write(self.dir, "d999-000", COMPLETE_FILE)
        rc = publish.publish([good], "msg", dry_run=True, refresh=False)
        self.assertEqual(rc, 0)


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_stem_maps_to_labels_tsv_when_no_txt(self):
        p = publish.resolve_target("d336-355", "/x/labels")
        self.assertEqual(p, os.path.join("/x/labels", "d336-355.labels.tsv"))

    def test_stem_prefers_fresh_txt_export(self):
        # the normal post-Audacity-export case: only <stem>.labels.txt exists
        write(self.dir, "d336-355", COMPLETE_FILE)
        txt = os.path.join(self.dir, "d336-355.labels.tsv")
        os.rename(txt, txt[:-3] + "txt")  # -> d336-355.labels.txt
        self.assertEqual(publish.resolve_target("d336-355", self.dir),
                         os.path.join(self.dir, "d336-355.labels.txt"))

    def test_published_path_maps_txt_to_tsv(self):
        self.assertEqual(publish.published_path("/a/d999-000.labels.txt"),
                         "/a/d999-000.labels.tsv")
        self.assertEqual(publish.published_path("/a/d999-000.labels.tsv"),
                         "/a/d999-000.labels.tsv")

    def test_explicit_txt_dry_run_stages_the_tsv(self):
        import io
        from contextlib import redirect_stdout
        path = write(self.dir, "d999-000", COMPLETE_FILE)
        txt = path[:-3] + "txt"
        os.rename(path, txt)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = publish.publish([txt], "msg", dry_run=True, refresh=False)
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("git add", out)
        self.assertIn("d999-000.labels.tsv", out)        # stages the converted name
        self.assertNotIn("git add %s" % txt, out)        # not the vanished .txt


if __name__ == "__main__":
    unittest.main()
