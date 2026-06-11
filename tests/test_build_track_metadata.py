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
        self.assertAlmostEqual(ids[3]["master_begin_seconds"], 59.910324, places=2)

    def test_all_tracks_resolve_a_master_position(self):
        # Regression: prefixed 14Nov label files (d180_d-14Nov10-a.labels.tsv whose
        # file is d-14Nov10-a.au) must resolve via the sync row, not the filename.
        ids, _conflicts = b.parse_label_track_ids()
        missing = [n for n, r in ids.items() if r["master_begin_seconds"] is None]
        self.assertEqual(missing, [], "tracks with no master_seconds: %s" % missing)
        # track 38 starts 324.585867 into d-14Nov10-a.au (master_start 10853.051068).
        self.assertAlmostEqual(ids[38]["master_begin_seconds"], 10853.051068 + 324.585867, places=3)

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


class ComputeTrackEndsTests(unittest.TestCase):
    """master_end_seconds is the LATEST master timestamp of a track's end markers
    (`origNNN end:`, `mix end: NNN`, or a `startNNN:` region-end), with forward-
    reference notes excluded."""

    PATH = "/x/fileA.labels.tsv"
    TIMELINE = {"fileA.mp3": {"master_start_seconds": 1000.0, "master_end_seconds": 2000.0}}

    def _ends(self, rows):
        rows = [{"path": self.PATH, "seconds": s, "end": e, "text": t} for s, e, t in rows]
        return b.compute_track_ends(rows, self.TIMELINE, {self.PATH: "fileA.mp3"})

    def test_orig_and_mix_end_take_the_latest(self):
        ends = self._ends([(50.0, 50.0, "orig005 end: A"),
                           (60.0, 60.0, "mix end: 005")])
        self.assertAlmostEqual(ends[5], 1060.0)  # latest marker wins

    def test_region_end_uses_the_end_column(self):
        ends = self._ends([(10.0, 70.0, "start005: ID: Artist - Title")])
        self.assertAlmostEqual(ends[5], 1070.0)  # 1000 + col1 (70), not col0 (10)

    def test_point_start_row_is_not_a_region_end(self):
        # A start row whose end column equals its start (a point label) contributes
        # no end (only its start position matters, handled elsewhere).
        self.assertEqual(self._ends([(10.0, 10.0, "start005: ID: Artist - Title")]), {})

    def test_forward_reference_note_is_excluded(self):
        # `note d122-144: mix end: 027` describes a DIFFERENT file; it must not set
        # track 27's end. Only an anchored `mix end:`/`origNNN end:` row counts.
        ends = self._ends([(60.0, 60.0, "mix end: 027"),
                           (99999.0, 99999.0, "note d122-144: mix end: 027 (but no sigh)")])
        self.assertAlmostEqual(ends[27], 1060.0)

    def test_zero_padded_numbers(self):
        ends = self._ends([(40.0, 40.0, "orig008 end: A")])
        self.assertIn(8, ends)


@unittest.skipUnless((REPO / "labels").is_dir(), "labels/ unavailable")
class LiveEndTests(unittest.TestCase):
    def test_every_track_except_the_last_has_a_segment_end(self):
        # Definitive segments for the player: every track (incl. the 30 s promos,
        # which have no label end-marker) gets a master_end_seconds. Only the very
        # last track by master order, which has no next track, may lack one.
        ids, _ = b.parse_label_track_ids()
        ordered = sorted((r for r in ids.values() if r.get("master_begin_seconds") is not None),
                         key=lambda r: r["master_begin_seconds"])
        without = [r["track_number"] for r in ordered[:-1] if r.get("master_end_seconds") is None]
        self.assertEqual(without, [], "non-last tracks missing a segment end: %s" % without)

    def test_promo_without_a_label_end_runs_to_the_next_begin(self):
        # Track 1 (Promo1) has no orig/mix end-marker; its segment end defaults to
        # the next track's begin (contiguous), not None.
        ids, _ = b.parse_label_track_ids()
        self.assertAlmostEqual(ids[1]["master_end_seconds"], ids[2]["master_begin_seconds"], places=3)

    def test_track_ends_after_it_starts(self):
        ids, _ = b.parse_label_track_ids()
        for n, r in ids.items():
            end, start = r.get("master_end_seconds"), r.get("master_begin_seconds")
            if end is not None and start is not None:
                self.assertGreater(end, start, "track %s end before start" % n)

    def test_segments_do_not_overlap_the_next_track(self):
        # Definitive non-overlapping segments: every end is clamped to the next
        # track's begin, so master_end_seconds[n] <= master_seconds[n+1] always.
        ids, _ = b.parse_label_track_ids()
        ordered = sorted((r for r in ids.values() if r.get("master_begin_seconds") is not None),
                         key=lambda r: r["master_begin_seconds"])
        for i, r in enumerate(ordered[:-1]):
            end = r.get("master_end_seconds")
            if end is None:
                continue
            self.assertLessEqual(end, ordered[i + 1]["master_begin_seconds"] + 1e-6,
                                 "track %s end overlaps the next track" % r["track_number"])

    def test_overlapping_end_is_clamped_to_next_begin(self):
        # Track 3's raw label end (~311.6) runs past track 4's begin (~303.9); the
        # written end must be clamped to track 4's begin, not the raw musical end.
        ids, _ = b.parse_label_track_ids()
        self.assertAlmostEqual(ids[3]["master_end_seconds"], ids[4]["master_begin_seconds"], places=2)

    def test_output_uses_master_begin_seconds_not_the_old_name(self):
        # The written JSON carries master_begin_seconds; the old ambiguous
        # master_seconds field is gone entirely (no back-compat alias).
        import json
        import sys
        import tempfile
        out = tempfile.mktemp(suffix=".json")
        argv = sys.argv
        sys.argv = ["build_track_metadata.py", "--out", out]
        try:
            b.main()
            data = json.load(open(out, encoding="utf-8"))
        finally:
            sys.argv = argv
            if os.path.exists(out):
                os.remove(out)
        t3 = data["tracks"]["3"]
        self.assertIn("master_begin_seconds", t3)
        self.assertNotIn("master_seconds", t3)
        self.assertNotIn("master_seconds", json.dumps(data))


class RemainderParseTests(unittest.TestCase):
    """First-pass remainder.tsv: `Title / Artist` (artist last, may be empty/absent)."""

    def test_slash_splits_title_and_artist(self):
        self.assertEqual(b.parse_remainder_id_text("start069: ID: Hypnotising / PFM"),
                         (69, "PFM", "Hypnotising"))

    def test_no_slash_means_no_artist(self):
        self.assertEqual(b.parse_remainder_id_text("start091: ID: Mystery Track 11"),
                         (91, None, "Mystery Track 11"))

    def test_trailing_slash_is_empty_artist(self):
        self.assertEqual(
            b.parse_remainder_id_text("start090: ID: Home (Jedi Knights Remix (Drowning In Time)) /"),
            (90, None, "Home (Jedi Knights Remix (Drowning In Time))"))

    def test_parentheses_on_both_sides(self):
        self.assertEqual(
            b.parse_remainder_id_text("start088: ID: Omen (Director's Cut) / Makai (vs. Nico)"),
            (88, "Makai (vs. Nico)", "Omen (Director's Cut)"))

    def test_non_id_row_is_none(self):
        self.assertIsNone(b.parse_remainder_id_text("file end: d.wav"))

    def test_mystery_detection(self):
        self.assertTrue(b.MYSTERY_RE.match("Mystery Track 3 ?? Coo-ooo"))
        self.assertFalse(b.MYSTERY_RE.match("Sea of Tears"))

    def test_drop_labelled_keeps_only_past_the_frontier(self):
        recs = [{"master_begin_seconds": 100.0}, {"master_begin_seconds": 200.0},
                {"master_begin_seconds": 300.0}]
        kept = [r["master_begin_seconds"] for r in b.drop_labelled(recs, 200.0)]
        self.assertEqual(kept, [300.0])


class AnchorIdTests(unittest.TestCase):
    """Stable id = first title word + first artist word, each capitalised; collisions
    extend with more words, then a numeric suffix as a last resort."""

    def test_first_words_of_title_and_artist(self):
        self.assertEqual(b.anchor_id("Promo1 Club Groove", "Net Radio", set()), "Promo1Net")
        self.assertEqual(b.anchor_id("You're My Life", "Jamie Myerson", set()), "YoureJamie")

    def test_collision_extends_with_more_words(self):
        # A different second word is available, so it appends rather than numbering.
        self.assertEqual(b.anchor_id("You're My Life", "Jamie Myerson", {"YoureJamie"}), "YoureJamieMy")

    def test_collision_falls_back_to_numeric_when_words_exhausted(self):
        used = set()
        self.assertEqual(b.anchor_id("A", "B", used), "AB")        # single words, no extras
        self.assertEqual(b.anchor_id("A", "B", used), "AB2")       # identical -> numeric suffix

    def test_missing_artist_uses_title_only(self):
        self.assertEqual(b.anchor_id("Mystery Track 5", None, set()), "Mystery")

    def test_strips_punctuation_and_capitalises(self):
        self.assertEqual(b.anchor_id("don't stop", "a.b. crew", set()), "DontAb")

    def test_empty_falls_back_to_track(self):
        self.assertEqual(b.anchor_id("", None, set()), "Track")


class TailPrimaryTests(unittest.TestCase):
    def test_maps_master_time_to_its_primary_capture_file(self):
        self.assertEqual(b.tail_primary_for(21008), "d336-355.wav")
        self.assertEqual(b.tail_primary_for(21074.552), "d356-375.wav")  # span_end -> next file
        self.assertEqual(b.tail_primary_for(27521), "d456-470.wav")

    def test_out_of_range_is_none(self):
        self.assertIsNone(b.tail_primary_for(99999))
        self.assertIsNone(b.tail_primary_for(None))


@unittest.skipUnless((REPO / "labels" / "remainder.tsv").is_file(), "remainder.tsv unavailable")
class LiveTailTests(unittest.TestCase):
    """The full build folds the first-pass tail (tracks ~67-91 + Mystery Tracks)
    onto the second-pass labels, tagged source=rough and kind=mystery."""

    @classmethod
    def setUpClass(cls):
        import json
        import sys
        import tempfile
        out = tempfile.mktemp(suffix=".json")
        argv = sys.argv
        sys.argv = ["build_track_metadata.py", "--out", out]
        try:
            b.main()
            cls.tr = json.load(open(out, encoding="utf-8"))["tracks"]
        finally:
            sys.argv = argv
            if os.path.exists(out):
                os.remove(out)

    def test_tail_present_and_tagged(self):
        self.assertGreaterEqual(len(self.tr), 91)
        self.assertEqual(self.tr["66"]["source"], "precise")
        self.assertEqual(self.tr["69"]["source"], "rough")
        self.assertEqual(self.tr["69"]["title"], "Hypnotising")
        self.assertEqual(self.tr["69"]["artist"], "PFM")
        self.assertEqual(self.tr["67"]["kind"], "mystery")
        self.assertNotIn("kind", self.tr["69"])  # an identified rough track isn't a mystery

    def test_boundary_stitched_and_last_track_open(self):
        # The last precise track now ends where the first rough track begins, and
        # only the global-last track lacks an end.
        self.assertAlmostEqual(self.tr["66"]["master_end_seconds"],
                               self.tr["67"]["master_begin_seconds"], places=2)
        self.assertIsNone(self.tr["91"].get("master_end_seconds"))

    def test_no_overlaps_across_the_combined_timeline(self):
        ts = sorted((t for t in self.tr.values() if t.get("master_begin_seconds") is not None),
                    key=lambda t: t["master_begin_seconds"])
        for i, t in enumerate(ts[:-1]):
            end = t.get("master_end_seconds")
            if end is not None:
                self.assertLessEqual(end, ts[i + 1]["master_begin_seconds"] + 1e-6)

    def test_rough_tracks_have_no_master_seconds_alias(self):
        self.assertNotIn("master_seconds", self.tr["69"])

    def test_every_track_has_a_unique_anchor(self):
        anchors = [t.get("anchor") for t in self.tr.values()]
        self.assertTrue(all(anchors), "some track has no anchor")
        self.assertEqual(len(anchors), len(set(anchors)), "anchors are not unique")
        self.assertEqual(self.tr["1"]["anchor"], "Promo1Net")
        self.assertEqual(self.tr["69"]["anchor"], "HypnotisingPFM")

    def test_rough_tracks_map_to_their_primary_capture_file(self):
        self.assertEqual(self.tr["67"]["source_files"], ["d336-355.wav"])
        self.assertEqual(self.tr["91"]["source_files"], ["d456-470.wav"])

    def test_curated_metadata_transfers_by_anchor_across_renumber(self):
        # A curated "Hypnotising / PFM" seeded at a DIFFERENT number (with artwork)
        # must follow its anchor onto generated track 69, and not linger at 500.
        import json
        import sys
        import tempfile
        seed = {"schema": b.SCHEMA, "tracks": {"500": {
            "title": "Hypnotising", "artist": "PFM", "artwork": "art/track-500.jpg",
            "fields": {"year": "1998"}, "master_begin_seconds": 1.0}}}
        seed_path, out_path = tempfile.mktemp(suffix=".json"), tempfile.mktemp(suffix=".json")
        with open(seed_path, "w", encoding="utf-8") as handle:
            json.dump(seed, handle)
        argv = sys.argv
        sys.argv = ["build_track_metadata.py", "--seed", seed_path, "--out", out_path]
        try:
            b.main()
            data = json.load(open(out_path, encoding="utf-8"))
        finally:
            sys.argv = argv
            for path in (seed_path, out_path):
                if os.path.exists(path):
                    os.remove(path)
        self.assertEqual(data["tracks"]["69"]["artwork"], "art/track-500.jpg")
        self.assertEqual(data["tracks"]["69"]["fields"]["year"], "1998")
        self.assertNotIn("500", data["tracks"])  # the stale number didn't linger


class SavePreservesAlbumsTests(unittest.TestCase):
    """save() must round-trip the schema-v2 `albums` map (and any other top-level
    keys), so a --seed regenerate never drops the player's album curation."""

    def test_albums_and_extra_top_level_keys_survive(self):
        import json
        import tempfile
        data = {
            "schema": "netradio.track-metadata.v2",
            "albums": {
                "earth-volume-two": {
                    "title": "Earth Volume Two", "artist": "LTJ Bukem",
                    "fields": {"year": "1997", "discogs": "https://x"},
                    "verified": {"discogs": "2026-06-10"},
                },
            },
            "tracks": {
                "10": {"title": "Artificial Life", "artist": "Odyssey",
                       "album": "earth-volume-two", "master_begin_seconds": 1.0,
                       "source_files": ["d.wav"], "fields": {}},
            },
            "note": "an unrelated top-level key",
        }
        path = tempfile.mktemp(suffix=".json")
        try:
            b.save(data, path)
            out = json.load(open(path, encoding="utf-8"))
        finally:
            os.remove(path)
        self.assertEqual(out["schema"], "netradio.track-metadata.v2")
        self.assertIn("earth-volume-two", out["albums"])
        self.assertEqual(out["albums"]["earth-volume-two"]["verified"], {"discogs": "2026-06-10"})
        self.assertEqual(out["tracks"]["10"]["album"], "earth-volume-two")
        self.assertEqual(out.get("note"), "an unrelated top-level key")


if __name__ == "__main__":
    unittest.main()
