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


if __name__ == "__main__":
    unittest.main()
