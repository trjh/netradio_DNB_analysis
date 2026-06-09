"""Tests for the track-sources sheet merge helpers."""
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import merge_track_sources as m  # noqa: E402


class HelperTests(unittest.TestCase):
    def test_is_url(self):
        self.assertTrue(m.is_url("https://music.apple.com/ie/album/x"))
        self.assertFalse(m.is_url("n/a"))
        self.assertFalse(m.is_url("Jamie Myerson - Sky Blue"))
        self.assertFalse(m.is_url(""))

    def test_clean_release_strips_suffixes_and_artifacts(self):
        self.assertEqual(m.clean_release("LTJ Bukem - Earth Volume Two | Releases | Discogs"),
                         "LTJ Bukem - Earth Volume Two")
        self.assertEqual(m.clean_release("Me'Shell NdegéOcello – Stay (1996, CD) - Discogs"),
                         "Me'Shell NdegéOcello – Stay (1996, CD)")
        # copy/paste artifact after "Discogs" is dropped
        self.assertEqual(m.clean_release("Eighty Mile Beach – Arboleda De Manzanitas (1997, Vinyl) - Discogsda-De-Manzanitas"),
                         "Eighty Mile Beach – Arboleda De Manzanitas (1997, Vinyl)")
        self.assertEqual(m.clean_release("n/a"), "")
        self.assertEqual(m.clean_release(""), "")

    def test_suspect_release_detects_glued_slug(self):
        # Track 23's value: title with a Discogs URL slug glued on, no separator.
        self.assertTrue(m.is_suspect_release(
            m.clean_release("The Amalgamation Of Soundzundz-Amalgamation-Of-Soundz")))
        # Genuinely clean release names are not flagged.
        self.assertFalse(m.is_suspect_release("LTJ Bukem - Earth Volume Two"))
        self.assertFalse(m.is_suspect_release("Various - Ultra Mix Drum & Bass"))
        self.assertFalse(m.is_suspect_release("Eighty Mile Beach – Arboleda De Manzanitas (1997, Vinyl)"))
        # A single short hyphenated token (e.g. an artist like "E-Z Rollers") is fine.
        self.assertFalse(m.is_suspect_release("E-Z Rollers - Retro"))


class MergeNoClobberTests(unittest.TestCase):
    def _row(self, n, **cols):
        base = {"Track Number": n, "Track Name": "", "Track Artist": "",
                "Discogs": "", "YouTube": "", "Apple": "", "Spotify": ""}
        base.update(cols)
        return base

    def test_fills_blank_but_keeps_curated_on_conflict(self):
        tracks = {
            "1": {"artist": "A", "title": "T", "fields": {}},                       # blank → fill
            "2": {"artist": "A", "title": "T",
                  "fields": {"apple": "https://music.apple.com/curated"}},          # differs → keep
        }
        rows = [
            self._row("1", Apple="https://music.apple.com/new1"),
            self._row("2", Apple="https://music.apple.com/sheet2"),
        ]
        report = m.merge_rows(tracks, rows)
        self.assertEqual(tracks["1"]["fields"]["apple"], "https://music.apple.com/new1")
        self.assertEqual(tracks["2"]["fields"]["apple"], "https://music.apple.com/curated")  # unchanged
        self.assertEqual(len(report["conflicts"]), 1)
        self.assertEqual(report["conflicts"][0][:2], ("2", "apple"))

    def test_overwrite_flag_replaces_on_conflict(self):
        tracks = {"2": {"fields": {"apple": "https://music.apple.com/curated"}}}
        rows = [self._row("2", Apple="https://music.apple.com/sheet2")]
        m.merge_rows(tracks, rows, overwrite=True)
        self.assertEqual(tracks["2"]["fields"]["apple"], "https://music.apple.com/sheet2")

    def test_idempotent_second_run_is_a_noop(self):
        tracks = {"1": {"fields": {}}}
        rows = [self._row("1", Apple="https://music.apple.com/x")]
        m.merge_rows(tracks, rows)
        report = m.merge_rows(tracks, rows)        # second run
        self.assertEqual(report["added"], {})
        self.assertEqual(report["conflicts"], [])

    def test_quarantines_slug_garbage_release(self):
        tracks = {"23": {"fields": {}}}
        rows = [self._row("23", Discogs="The Amalgamation Of Soundzundz-Amalgamation-Of-Soundz")]
        report = m.merge_rows(tracks, rows)
        self.assertNotIn("release", tracks["23"]["fields"])     # never written
        self.assertEqual(len(report["quarantined"]), 1)


if __name__ == "__main__":
    unittest.main()
