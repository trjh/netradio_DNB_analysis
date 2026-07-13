"""A rebuild must never silently destroy an identification.

An identification is the most expensive thing in this project — months of listening, or a stranger
on a forum who happened to be there in 1998 — and the easiest to lose, because losing it looks
exactly like a successful rebuild. Mystery Track 5 is the live example: it reaches the output only
through `--seed`, and a rebuild without one folds its artist into its title without a word.

The guard is deliberately one-directional. Going backwards is refused; going forwards — adding a
title, filling in a missing artist — is waved straight through, because a build that cannot improve
anything is a build nobody will run.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))

import build_track_metadata as btm  # noqa: E402


class IdentificationGuard(unittest.TestCase):
    def _existing(self, tracks):
        fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump({"schema": "v2", "tracks": tracks}, fh)
        fh.close()
        self.addCleanup(os.unlink, fh.name)
        return fh.name

    def test_a_solved_record_may_not_quietly_become_a_mystery_again(self):
        """Re-running remainderlist.pl would do exactly this to track 74: the source line in
        tracklist-2017.txt still reads "Mystery Track 5"."""
        out = self._existing({"74": {"title": "Solar Feelings (J Majik Remix)",
                                     "artist": "Jacob's Optical Stairway"}})
        losses = btm.identification_losses(out, {"74": {"title": "Mystery Track 5", "artist": ""}})
        fields = {f for _n, f, _h, _w in losses}
        self.assertIn("title", fields)
        self.assertIn("artist", fields)

    def test_an_artist_may_not_be_folded_into_the_title(self):
        """THE LIVE BUG. A seedless rebuild returns track 74 as
        title="Jacob's Optical Stairway - Solar Feelings (J Majik Remix)", artist=None — the whole
        identification is still *there*, but the artist field is gone, and nothing says so."""
        out = self._existing({"74": {"title": "Solar Feelings (J Majik Remix)",
                                     "artist": "Jacob's Optical Stairway"}})
        losses = btm.identification_losses(out, {"74": {
            "title": "Jacob's Optical Stairway - Solar Feelings (J Majik Remix)", "artist": None}})
        self.assertEqual([(n, f) for n, f, _h, _w in losses], [("74", "artist")])

    def test_adding_an_identification_is_not_a_loss(self):
        """A guard that blocks improvements is a guard nobody keeps."""
        out = self._existing({"7": {"title": "Mystery Track 7", "artist": ""}})
        self.assertEqual(
            btm.identification_losses(out, {"7": {"title": "Dead Calm - Urban Style",
                                                  "artist": "Dead Calm"}}),
            [])

    def test_filling_in_a_missing_artist_is_not_a_loss(self):
        out = self._existing({"9": {"title": "Some Record", "artist": ""}})
        self.assertEqual(btm.identification_losses(out, {"9": {"title": "Some Record",
                                                               "artist": "Someone"}}), [])

    def test_an_unchanged_rebuild_loses_nothing(self):
        t = {"74": {"title": "Solar Feelings (J Majik Remix)",
                    "artist": "Jacob's Optical Stairway"}}
        self.assertEqual(btm.identification_losses(self._existing(t), dict(t)), [])

    def test_a_first_build_has_nothing_to_lose(self):
        self.assertEqual(btm.identification_losses("/nonexistent/none.json",
                                                   {"1": {"title": "x", "artist": "y"}}), [])

    def test_a_track_that_disappears_entirely_is_not_reported_as_a_field_loss(self):
        """Out of scope for this guard, and reporting it would be noise -- it is a different
        failure (a label vanished), and it is already loud elsewhere."""
        out = self._existing({"74": {"title": "Solar Feelings", "artist": "JOS"}})
        self.assertEqual(btm.identification_losses(out, {}), [])


if __name__ == "__main__":
    unittest.main()
