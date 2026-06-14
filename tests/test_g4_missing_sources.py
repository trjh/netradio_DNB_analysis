"""Tests for the G4 missing-originals inventory (scripts/g4_missing_sources.py).

Pure filesystem + metadata logic — no audio, no librosa — so these run anywhere.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import g4_missing_sources as g4  # noqa: E402


class G4MissingSourcesTests(unittest.TestCase):
    def _build(self):
        d = tempfile.mkdtemp()
        src = os.path.join(d, "sources")
        os.mkdir(src)

        def touch(name, size):
            with open(os.path.join(src, name), "wb") as fh:
                fh.write(b"\0" * size)

        # track 3: a real, name-matching original -> have
        touch("003-Jamie Myerson - Sky Blue.mp3", 8 << 20)
        # track 14: an unrelated large m4a that merely shares the 014- prefix (a DJ
        # mix, NOT the original) + a .null stub for the real, missing original. Must
        # classify as PLACEHOLDER, not a false "have" from the big m4a.
        touch("014-LeRadioClub - Dj Mix Nagra.m4a", 30 << 20)
        touch("014-Me'Shell NdegeOcello - Stay (The Midnight Rockers Remix).null", 0)
        # track 50: identified, no file -> missing + sourceable
        # track 67: unidentified "Mystery Track" -> missing + needs G3
        meta = {"tracks": {
            "3": {"artist": "Jamie Myerson", "title": "Sky Blue",
                  "fields": {"discogs": "https://discogs/3"}},
            "14": {"artist": "Me'Shell NdegeOcello",
                   "title": "Stay (The Midnight Rockers Remix)",
                   "fields": {"discogs": "https://discogs/14"}},
            "50": {"artist": "DJ 3D", "title": "Cairo", "fields": {}},
            "67": {"artist": "", "title": "Mystery Track 3", "fields": {}},
        }}
        meta_path = os.path.join(d, "track-metadata.json")
        with open(meta_path, "w") as fh:
            json.dump(meta, fh)
        return meta_path, src

    def test_unrelated_prefix_file_is_not_a_false_have(self):
        meta_path, src = self._build()
        inv = g4.inventory(meta_path, src)
        by = {r["track"]: r for r in inv["tracks"]}
        self.assertEqual(by[3]["status"], "have")
        self.assertEqual(by[3]["ext"], "mp3")
        # the DJ-mix m4a must NOT count as track 14's original
        self.assertEqual(by[14]["status"], "placeholder")
        self.assertTrue(by[14]["file"].endswith(".null"))
        self.assertEqual(by[50]["status"], "missing")
        self.assertEqual(by[67]["status"], "missing")
        self.assertEqual(inv["summary"],
                         {"have": 1, "placeholder": 1, "missing": 2})

    def test_identified_vs_needs_g3_split(self):
        meta_path, src = self._build()
        inv = g4.inventory(meta_path, src)
        self.assertIn(14, inv["sourceable"])   # identified gap (placeholder)
        self.assertIn(50, inv["sourceable"])   # identified gap (missing)
        self.assertIn(67, inv["needs_g3"])     # Mystery Track -> G3, not sourceable
        self.assertNotIn(67, inv["sourceable"])
        by = {r["track"]: r for r in inv["tracks"]}
        self.assertEqual(by[14]["leads"].get("discogs"), "https://discogs/14")

    def test_single_audio_file_trusted_without_name_match(self):
        # A lone, cryptically-named audio file with the right prefix and no competing
        # placeholder is trusted (no ambiguity to resolve).
        d = tempfile.mkdtemp()
        src = os.path.join(d, "sources")
        os.mkdir(src)
        with open(os.path.join(src, "009-xyz.mp3"), "wb") as fh:
            fh.write(b"\0" * (1 << 20))
        meta_path = os.path.join(d, "m.json")
        with open(meta_path, "w") as fh:
            json.dump({"tracks": {"9": {"artist": "Net Radio", "title": "Promo3"}}}, fh)
        inv = g4.inventory(meta_path, src)
        self.assertEqual(inv["tracks"][0]["status"], "have")

    def test_name_matching_helpers(self):
        self.assertTrue(g4._name_matches(
            "014-Me'Shell - Stay (Midnight Rockers).null",
            "Me'Shell NdegeOcello", "Stay (The Midnight Rockers Remix)"))
        self.assertFalse(g4._name_matches(
            "014-LeRadioClub - Dj Mix Nagra.m4a",
            "Me'Shell NdegeOcello", "Stay (The Midnight Rockers Remix)"))
        self.assertFalse(g4._is_identified("", "Mystery Track 5"))
        self.assertTrue(g4._is_identified("Goldie", "Sea of Tears"))


if __name__ == "__main__":
    unittest.main()
