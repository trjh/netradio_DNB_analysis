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


class SpotifyImportTests(unittest.TestCase):
    def _tracks(self):
        return {
            "10": {"artist": "Odyssey", "title": "Artificial Life", "fields": {}},
            "22": {"artist": "Castillo", "title": "Junkle I", "fields": {}},
            "99": {"artist": "Done", "title": "Already", "fields": {"spotify": "x"}},
        }

    def test_split_matched(self):
        self.assertEqual(f.split_matched("Odyssey - Artificial Life"),
                         ("Odyssey", "Artificial Life"))
        self.assertEqual(f.split_matched("NoSeparator"), ("NoSeparator", ""))

    def test_applies_high_confidence_agreeing_match(self):
        tracks = self._tracks()
        items = [{"track_number": 10, "spotify": "https://open.spotify.com/track/abc",
                  "matched": "Odyssey - Artificial Life", "confidence": "high"}]
        applied, review = f.import_spotify_json(tracks, self._write(items))
        self.assertEqual(applied, 1)
        self.assertEqual(tracks["10"]["fields"]["spotify"],
                         "https://open.spotify.com/track/abc")

    def test_holds_artist_mismatch_and_low_confidence_and_bad_url(self):
        tracks = self._tracks()
        items = [
            {"track_number": 22, "spotify": "https://open.spotify.com/track/x",
             "matched": "Callisto - Junkle I", "confidence": "high"},        # artist mismatch
            {"track_number": 10, "spotify": "https://open.spotify.com/track/y",
             "matched": "Odyssey - Artificial Life", "confidence": "medium"},  # low confidence
            {"track_number": 10, "spotify": "http://example.com/x",
             "matched": "Odyssey - Artificial Life", "confidence": "high"},  # not a spotify url
            {"track_number": 99, "spotify": "https://open.spotify.com/track/z",
             "matched": "Done - Already", "confidence": "high"},             # already set
        ]
        applied, review = f.import_spotify_json(tracks, self._write(items))
        self.assertEqual(applied, 0)
        self.assertEqual(len(review), 4)
        self.assertNotIn("spotify", tracks["22"]["fields"])

    def _write(self, items):
        import json
        import tempfile
        fd = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(items, fd)
        fd.close()
        return fd.name


if __name__ == "__main__":
    unittest.main()
