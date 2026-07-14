"""`graph.next_stem` -- the successor guess that drives sort_tsv's next-file prep.

Three sources, most-authoritative first: the hand `file_<other>:` link, the 1998/2017 notes,
and (weakest) the filename range. Each is pinned here, plus the "nothing resolves" case, so a
wrong guess is a test failure rather than a surprise in the loop.
"""

import os
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

from streamalign import graph  # noqa: E402


def _write(path, rows):
    with open(path, "w", encoding="utf-8") as handle:
        for a, b, text in rows:
            handle.write("%.6f\t%.6f\t%s\n" % (a, b, text))


class HandLinkWins(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_the_file_link_is_the_successor(self):
        _write(os.path.join(self.dir, "d356-375.labels.tsv"), [
            (0.0, 0.0, "file start sync: d356-375.wav 0 verified by 067"),
            (1200.0, 1200.0, "file_d376-395: file start sync: d376-395.wav 0 verified"),
        ])
        nxt, why = graph.next_stem("d356-375", labels_dir=self.dir)
        self.assertEqual(nxt, "d376-395")
        self.assertIn("hand link", why)

    def test_the_latest_beginning_neighbour_is_next(self):
        # two neighbours homed; the one that begins LATEST is the successor, the earlier one is
        # a mid-file overlap
        _write(os.path.join(self.dir, "d100-120.labels.tsv"), [
            (0.0, 0.0, "file start sync: d100-120.wav 0 verified"),
            (300.0, 300.0, "file_d090-110: note: overlap tail"),          # begins earlier
            (1100.0, 1100.0, "file_d120-140: file start sync: d120-140.wav 0 verified"),
        ])
        nxt, _why = graph.next_stem("d100-120", labels_dir=self.dir)
        self.assertEqual(nxt, "d120-140")

    def test_a_wav_suffixed_link_still_resolves(self):
        _write(os.path.join(self.dir, "d356-375.labels.tsv"), [
            (0.0, 0.0, "file start sync: d356-375.wav 0 verified"),
            (1200.0, 1200.0, "file_d376-395.wav: file start sync: d376-395.wav 0 verified"),
        ])
        nxt, _why = graph.next_stem("d356-375", labels_dir=self.dir)
        self.assertEqual(nxt, "d376-395")


class Fallbacks(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_the_notes_place_the_successor_when_there_is_no_link(self):
        # no file_ link -> the 1998/2017 notes. The real notes put d376-395 after dnb356-375,
        # which _loose() coerces to d356-375.
        _write(os.path.join(self.dir, "d336-355.labels.tsv"),
               [(0.0, 0.0, "file start sync: d336-355.wav 0 verified")])
        nxt, why = graph.next_stem("d336-355", labels_dir=self.dir)
        self.assertEqual(nxt, "d356-375")
        self.assertIn("notes", why)

    def test_filename_range_is_the_last_resort(self):
        # stems the notes don't mention and with no file_ links: only the filename ranges can
        # order them. d540-560 starts after d500-520, so it is the successor.
        for name in ("d500-520", "d540-560"):
            _write(os.path.join(self.dir, name + ".labels.tsv"),
                   [(0.0, 0.0, "file start sync: %s.wav 0 verified" % name)])
        nxt, why = graph.next_stem("d500-520", labels_dir=self.dir)
        self.assertEqual(nxt, "d540-560")
        self.assertIn("filename", why)

    def test_no_successor_is_reported_not_guessed(self):
        _write(os.path.join(self.dir, "d998-999.labels.tsv"),
               [(0.0, 0.0, "file start sync: d998-999.wav 0 verified")])
        nxt, why = graph.next_stem("d998-999", labels_dir=self.dir)
        self.assertIsNone(nxt)
        self.assertIn("no successor", why)


class ImportSurface(unittest.TestCase):
    def test_the_light_helpers_do_not_touch_numpy_at_call_time(self):
        # next_stem must be callable in the sort path without the alignment maths running
        self.assertEqual(graph.filename_range("d356-375"), (356, 375))
        self.assertIsNone(graph.filename_range("remainder"))


if __name__ == "__main__":
    unittest.main()
