"""Tests for the streaming-link matcher (offline — no network)."""
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import find_streaming_links as f  # noqa: E402


class ArtistMatchTests(unittest.TestCase):
    def test_the_prefix_and_ampersand(self):
        self.assertTrue(f.artist_ok("The Sonar Circle", "Sonar Circle"))
        self.assertTrue(f.artist_ok("Ed Rush & Fierce", "Ed Rush & Fierce"))
        self.assertTrue(f.artist_ok("Codename John feat. Grooverider", "Codename John"))

    def test_different_artist_rejected(self):
        self.assertFalse(f.artist_ok("Matrix", "Squarepusher"))


class TitleMatchTests(unittest.TestCase):
    def test_exact_and_feat_suffix(self):
        self.assertTrue(f.title_ok("Everything is Gonna Be Alright",
                                   "Everything Is Gonna Be Alright (feat. Carol Tripp)"))

    def test_short_title_not_loose_substring(self):
        # Regression: "Mute" must NOT match "You Don't Have To Wait (feat. En Mute)".
        self.assertFalse(f.title_ok("Mute", "You Don't Have To Wait (feat. En Mute)"))
        self.assertTrue(f.title_ok("Mute", "Mute"))

    def test_remix_qualifier_required_for_remix_track(self):
        self.assertTrue(f.title_ok("Free La Funk (PFM Remix)", "Free la Funk (PFM Remix)"))
        # a remix track should not match a differently-qualified result
        self.assertFalse(f.title_ok("Waves (Kid Loops Remix)", "Waves (Original Mix)"))


if __name__ == "__main__":
    unittest.main()
