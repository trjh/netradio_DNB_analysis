"""Hint labels: the invariants that keep suggestions from becoming facts.

The load-bearing one is the first class: a hint must never be able to overwrite, or be
mistaken for, a hand label. Everything else is presentation.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))

from streamalign import groundtruth as gt          # noqa: E402
from streamalign import hints                      # noqa: E402


class TestHintsNeverOverrideLabels(unittest.TestCase):
    """Hints only ever ADD. They cannot overwrite hand work, and cannot leak into the solve."""

    def test_write_hints_refuses_a_hand_label_file(self):
        # The whole safety story rests on this: even asked explicitly, it will not write a
        # hand label file.
        with self.assertRaises(ValueError) as ctx:
            hints.write_hints([(0.0, 0.0, "x")], "/tmp/d356-375.labels.tsv")
        self.assertIn("labels.tsv", str(ctx.exception))

    def test_write_hints_refuses_an_auto_label_file(self):
        # *.auto.labels.tsv is the solve's output and also ends .labels.tsv.
        with self.assertRaises(ValueError):
            hints.write_hints([(0.0, 0.0, "x")], "/tmp/d356-375.auto.labels.tsv")

    def test_hints_file_is_invisible_to_the_pipeline(self):
        # The naming trap: is_pipeline_label_file() accepts ANYTHING ending .labels.tsv
        # except .starter. -- so a `<stem>.hint.labels.tsv` would have been swallowed into
        # the solve and into the player's track metadata. `.hints.tsv` cannot be.
        self.assertFalse(gt.is_pipeline_label_file("d356-375.hints.tsv"))
        # guard the trap itself, so nobody "helpfully" renames the output later:
        self.assertTrue(gt.is_pipeline_label_file("d356-375.hint.labels.tsv"))

    def test_written_hints_land_in_a_hints_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "d356-375.hints.tsv")
            hints.write_hints([(1.0, 2.0, "note HINT: x HINT")], path)
            self.assertTrue(os.path.exists(path))
            with open(path) as handle:
                self.assertEqual(handle.read(), "1.000000\t2.000000\tnote HINT: x HINT\n")


class TestEveryRowIsMarkedAndScored(unittest.TestCase):

    def test_every_row_ends_in_the_hint_marker(self):
        for row in (hints._row(0, 0, "a"), hints._hint(0, 0, "b"), hints._question(0, 0, "c")):
            self.assertTrue(row[2].endswith(hints.SUFFIX), row[2])

    def test_questions_and_hints_use_the_existing_note_grammar(self):
        # `note <TAG>: ...` already parses in labels/sort_tsv.py -- no grammar change needed.
        self.assertTrue(hints._question(0, 0, "why?")[2].startswith("note QUESTION: "))
        self.assertTrue(hints._hint(0, 0, "maybe")[2].startswith("note HINT: "))

    def test_confidence_is_spelled_out_on_a_ten_scale(self):
        self.assertEqual(hints._conf(0.98), "confidence 9.8/10")
        self.assertEqual(hints._conf(1.0), "confidence 10.0/10")
        self.assertEqual(hints._conf(0.0), "confidence 0.0/10")

    def test_confidence_clamps_rather_than_lying(self):
        self.assertEqual(hints._conf(1.7), "confidence 10.0/10")
        self.assertEqual(hints._conf(-0.5), "confidence 0.0/10")


class TestOverlapDetection(unittest.TestCase):
    """Exactly-joined captures share no audio. Correlating them returns noise, so they must
    not be offered as neighbours -- this is exactly d336-355 -> d356-375."""

    def test_exactly_joined_neighbour_is_not_an_overlap(self):
        starts = {"a": 0.0, "b": 1200.0}     # b begins exactly where a ends

        class _FakeAudio:
            @staticmethod
            def find_audio_file(stem, audio_dir=None):
                return "/fake/%s.wav" % stem

            @staticmethod
            def duration_seconds(stem, audio_dir=None):
                return 1200.0

            @staticmethod
            def stem_of(name):
                return name

        real = hints._audio
        hints._audio = _FakeAudio
        try:
            self.assertEqual(hints.overlapping_neighbours("a", starts), [])
        finally:
            hints._audio = real

    def test_a_real_overlap_is_found_with_the_engine_offset_convention(self):
        starts = {"a": 0.0, "b": 600.0}      # b starts halfway through a -> 600s of overlap

        class _FakeAudio:
            @staticmethod
            def find_audio_file(stem, audio_dir=None):
                return "/fake/%s.wav" % stem

            @staticmethod
            def duration_seconds(stem, audio_dir=None):
                return 1200.0

            @staticmethod
            def stem_of(name):
                return name

        real = hints._audio
        hints._audio = _FakeAudio
        try:
            got = hints.overlapping_neighbours("a", starts)
        finally:
            hints._audio = real
        self.assertEqual(len(got), 1)
        other, overlap, seed = got[0]
        self.assertEqual(other, "b")
        self.assertAlmostEqual(overlap, 600.0)
        # engine convention: offset = master_start(b) - master_start(a)
        self.assertAlmostEqual(seed, 600.0)


class TestCarryForwardMatchesGrammarNotSubstrings(unittest.TestCase):
    """A `note: ...starting here?` is prose. Treating it as a track start yields a
    confidently wrong hint -- which is worse than no hint at all."""

    def test_prose_containing_the_word_starting_is_not_a_track_start(self):
        self.assertIsNone(hints._TRACK_START_RE.match(
            "note: another siren-y sound starting here? or just compression artifact?"))
        self.assertIsNone(hints._ORIG_START_RE.match("note: starting to fade"))

    def test_real_track_and_orig_starts_match(self):
        self.assertTrue(hints._TRACK_START_RE.match("start067: ID: Aquarius - Wave Forms"))
        self.assertTrue(hints._ORIG_START_RE.match("orig067 start: 0"))


if __name__ == "__main__":
    unittest.main()


class TestTrackNumberResolution(unittest.TestCase):
    """The 1998/2017 notes name tracks; the anchor search needs NNN. Both keys must agree."""

    META = {
        "69": {"artist": "PFM", "title": "Hypnotising", "master_begin_seconds": 21454.2},
        "70": {"artist": "Dead Calm", "title": "Urban Style (Original Mix)",
               "master_begin_seconds": 21703.0},
        "71": {"artist": "Fokus", "title": "On Line (Original Mix)",
               "master_begin_seconds": 22022.0},
    }

    def test_it_handles_both_hand_typed_name_orders(self):
        # The notes are inconsistent: "Hypnotising / PFM" is Title/Artist, while
        # "Fokus / On Line (Original Mix)" is Artist/Title. Comparing word SETS sidesteps a
        # rule the data does not actually follow.
        self.assertEqual(hints.resolve_track_number("Hypnotising / PFM", 21413.0, self.META), 69)
        self.assertEqual(
            hints.resolve_track_number("Fokus / On Line (Original Mix)", 22022.0, self.META), 71)
        self.assertEqual(
            hints.resolve_track_number("Urban Style (Original Mix) / Dead Calm", 21703.0,
                                       self.META), 70)

    def test_time_alone_does_not_decide(self):
        # A track sitting at the right master time but with an unrelated name must NOT match:
        # proximity alone would happily pick the neighbour of a track that is merely absent,
        # and a wrong number points the anchor search at the wrong record.
        self.assertIsNone(
            hints.resolve_track_number("Something Else Entirely", 21703.0, self.META))

    def test_name_alone_does_not_decide(self):
        # Right name, but hours away from where the metadata puts it -> not this track.
        self.assertIsNone(
            hints.resolve_track_number("Hypnotising / PFM", 900.0, self.META))


class TestAnchorPairOrdering(unittest.TestCase):
    """A is the EARLY anchor, B the late one. Not cosmetic: the sheet computes
    (trackB - trackA) / (origB - origA), so swapping them inverts the rate."""

    def test_implied_rate_is_orientation_stable(self):
        from streamalign import track_mix as tm
        early = {"mix_s": 100.0, "orig_s": 10.0}
        late = {"mix_s": 200.0, "orig_s": 110.0}
        self.assertAlmostEqual(tm.implied_rate([early, late]), 1.0, places=6)
        self.assertAlmostEqual(tm.implied_rate([late, early]), 1.0, places=6)

    def test_anchors_too_close_together_give_no_rate(self):
        from streamalign import track_mix as tm
        self.assertIsNone(tm.implied_rate([{"mix_s": 10.0, "orig_s": 1.0},
                                           {"mix_s": 12.0, "orig_s": 3.0}]))

    def test_a_dj_does_not_pitch_a_record_by_seventy_percent(self):
        from streamalign import track_mix as tm
        lo, hi = tm.RATE_PLAUSIBLE
        self.assertTrue(lo <= 0.99 <= hi)      # a real pitch
        self.assertFalse(lo <= 0.30 <= hi)     # matched noise -- both real failures looked
        self.assertFalse(lo <= 0.10 <= hi)     # exactly like this
