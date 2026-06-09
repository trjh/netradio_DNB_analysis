"""Tests for the label-driven track-metadata generator."""
import os
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import build_track_metadata as b  # noqa: E402


class LabelIdTextTests(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(b.parse_label_track_id_text("start003: ID: Jamie Myserson - Sky Blue"),
                         (3, "Jamie Myserson", "Sky Blue"))

    def test_hyphenated_names_stay_intact(self):
        self.assertEqual(b.parse_label_track_id_text("start052: ID: B-Boy 3000 - Diet for Murder"),
                         (52, "B-Boy 3000", "Diet for Murder"))
        self.assertEqual(b.parse_label_track_id_text("start016: ID: Skyjuice - The Rope-A-Dope"),
                         (16, "Skyjuice", "The Rope-A-Dope"))

    def test_ampersand_artist_and_remix_title(self):
        self.assertEqual(
            b.parse_label_track_id_text("start020: ID: Angelo Badalamenti & David Lynch - Twin Peaks (Alëem Remix)"),
            (20, "Angelo Badalamenti & David Lynch", "Twin Peaks (Alëem Remix)"))

    def test_strips_stray_audio_suffix(self):
        self.assertEqual(b.parse_label_track_id_text("start049: ID: G-Money - Falling.mp3"),
                         (49, "G-Money", "Falling"))

    def test_non_id_rows_return_none(self):
        for text in ("file start sync: d019-040.wav 1102.848", "start003: Sky Blue",
                     "start005: ID: NoSeparator", "track sync: A/B", ""):
            self.assertIsNone(b.parse_label_track_id_text(text), msg=text)

    def test_owning_file(self):
        self.assertEqual(b.owning_file_for_label_path("/x/d019-040.labels.tsv"), "d019-040.mp3")
        self.assertEqual(b.owning_file_for_label_path("d-14Nov10-e.labels.tsv"), "d-14Nov10-e.mp3")


@unittest.skipUnless((REPO / "labels").is_dir(), "labels/ unavailable")
class LiveTests(unittest.TestCase):
    def test_parses_many_tracks_and_resolves_known_position(self):
        ids, _conflicts = b.parse_label_track_ids()
        self.assertGreaterEqual(len(ids), 40)
        self.assertEqual(ids[3]["track_name"], "Sky Blue")
        self.assertAlmostEqual(ids[3]["master_seconds"], 59.910324, places=2)

    def test_all_tracks_resolve_a_master_position(self):
        # Regression: prefixed 14Nov label files (d180_d-14Nov10-a.labels.tsv whose
        # file is d-14Nov10-a.au) must resolve via the sync row, not the filename.
        ids, _conflicts = b.parse_label_track_ids()
        missing = [n for n, r in ids.items() if r["master_seconds"] is None]
        self.assertEqual(missing, [], "tracks with no master_seconds: %s" % missing)
        # track 38 starts 324.585867 into d-14Nov10-a.au (master_start 10853.051068).
        self.assertAlmostEqual(ids[38]["master_seconds"], 10853.051068 + 324.585867, places=3)

    def test_source_files_use_original_names_not_prefixed_label_stems(self):
        ids, _conflicts = b.parse_label_track_ids()
        self.assertIn("d-14Nov10-a.au", ids[38]["source_files"])
        for name in ids[38]["source_files"]:
            self.assertFalse(name.startswith("d180_"), name)

    def test_source_files_include_continuation_captures(self):
        # Regression: a track continues into later captures (marked by bare `IDNNN:`
        # rows), not just the capture containing its start.
        ids, _conflicts = b.parse_label_track_ids()
        self.assertIn("d-14Nov10-b.au", ids[41]["source_files"])  # The Sonar Circle - Strength
        self.assertIn("d-14Nov10-c.au", ids[44]["source_files"])  # E-Z Rollers - Retro


if __name__ == "__main__":
    unittest.main()
