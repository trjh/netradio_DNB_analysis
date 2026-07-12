"""The 1998/2017 notes, read as data.

These notes are the oldest evidence in the project and, on the stretches where captures do not
overlap, very nearly the only evidence. The parser has to be trusted, so it is pinned here
against the real file rather than a fixture.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))

from streamalign import tracklist2017 as tl   # noqa: E402


class TestParse(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.data = tl.parse()

    def test_it_finds_the_capture_blocks(self):
        self.assertGreater(len(self.data), 20)
        self.assertIn("d336-355", self.data)

    def test_the_dnb_prefix_variant_normalises_to_the_real_stem(self):
        # The notes head one block `dnb356-375`; the capture on disk is `d356-375.wav`. The
        # engine must see the same stem as everything else, or the hints go to a file nobody
        # has. Resolved against the audio that exists, not by a guessed rule.
        self.assertIn("d356-375", self.data)
        self.assertNotIn("dnb356-375", self.data)

    def test_master_start_is_derived_not_assumed(self):
        # `351:14.552  00:00.000 -- START` -> 351*60 + 14.552
        self.assertAlmostEqual(self.data["d356-375"]["master_start_s"], 21074.552, places=2)

    def test_it_reads_the_exact_join_and_the_carried_track(self):
        entry = self.data["d356-375"]
        self.assertEqual(entry["transition_from"], "d336-355")
        self.assertIn("Mystery Track 3", entry["continuation"])

    def test_track_starts_are_in_the_captures_local_time(self):
        tracks = self.data["d356-375"]["tracks"]
        self.assertGreaterEqual(len(tracks), 4)
        local = {round(t["local_s"], 1): t["name"] for t in tracks}
        self.assertIn(117.8, local)                       # 01:57.839
        self.assertIn(338.6, local)                       # 05:38.556
        self.assertIn("Hypnotising", local[338.6])
        for t in tracks:                                  # a 20-minute capture
            self.assertLessEqual(t["local_s"], 1250)

    def test_it_keeps_the_hand_marked_sync_cue_inside_an_original(self):
        # The notes mark one cue in Hypnotising as `[sync point]` -- the single most valuable
        # line in the block, because an A/B anchor is made of exactly that.
        hyp = [t for t in self.data["d356-375"]["tracks"] if "Hypnotising" in t["name"]][0]
        syncs = [c for c in hyp["cues"] if "sync point" in c["text"].lower()]
        self.assertTrue(syncs, "the [sync point] cue was dropped")
        self.assertAlmostEqual(syncs[0]["at_s"], 93.0, places=0)   # 1:33
        self.assertNotIn("\t", syncs[0]["text"])          # hand-typed tab runs collapsed


class TestAgreementWithTheHandLabels(unittest.TestCase):
    """The notes are approximate, but they are not random: where a capture is placed by hand,
    the two agree closely. That is what makes a LARGE disagreement meaningful."""

    def test_the_notes_track_the_hand_labels_within_a_couple_of_seconds(self):
        from streamalign import groundtruth as gt
        data, starts = tl.parse(), gt.resolve_starts()
        deltas = [abs(d["master_start_s"] - starts[s])
                  for s, d in data.items()
                  if d.get("master_start_s") is not None and s in starts
                  and s != "d356-375"]          # the known outlier -- see below
        self.assertGreaterEqual(len(deltas), 5)
        self.assertLess(max(deltas), 2.5,
                        "the 2017 notes should agree with the hand labels to a couple of "
                        "seconds; a bigger gap means the parser or the labels have drifted")

    def test_d356_375_is_the_outlier_that_prompted_the_question(self):
        from streamalign import groundtruth as gt
        data, starts = tl.parse(), gt.resolve_starts()
        delta = starts["d356-375"] - data["d356-375"]["master_start_s"]
        # Three independent sources (audio exact-join, the 2017 notes) say the hand anchor is
        # ~3s late. If this ever drops below the others' spread, the anomaly was fixed and the
        # hint should stop asking about it.
        self.assertGreater(delta, 2.5)


if __name__ == "__main__":
    unittest.main()
