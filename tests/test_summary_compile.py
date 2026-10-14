"""DP-24 §3: the summary compile — labels/automated + labels/review → labels/summary.

Hermetic: a scratch checkout with the three directories. Pins the §3 contract:
drops nothing, verbatim rows (both `verified` families untouched, markers identical across
the two files), the seat row arithmetic a − b_native, the original-clock conversion of
review rows off their own text, sort_tsv's ordering, idempotence, whole-file writes, stale
derived output removed, containment, and the route gates (dark 404, body shape, nudge).
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))

import align_inspect  # noqa: E402
import summary_compile as sc  # noqa: E402

STREAM_PAIR = """\
0.000000	0.000000	note HINT: orig072 via MATCH+PHAT: rate 1.02046 (original runs +2.05% vs stream), polarity INVERTED (flip the original before a null test), 2 anchors HINT
0.000000	0.000000	note QUESTION: orig072 starts 29.992 s BEFORE this capture's first sample HINT
106.000000	106.000000	track sync: 2 verified confidence 6.8/10 MATCH -37.061s
76.000000	76.000000	track sync: 1 verified confidence 5.9/10 MATCH -35.372s
"""
ORIG_PAIR = """\
0.000000	0.000000	note HINT: orig072 via MATCH+PHAT: rate 1.02046 (original runs +2.05% vs stream), polarity INVERTED (flip the original before a null test), 2 anchors; times are original-local seconds HINT
105.992413	105.992413	orig072 sync: 1 verified confidence 5.9/10 MATCH -35.372s
136.600683	136.600683	orig072 sync: 2 verified confidence 6.8/10 MATCH -37.061s
"""
STEM_HINTS = """\
0.000000	0.000000	file start sync?: d376-395.wav 22278.306068  (already anchored in the hand labels) HINT
19.214000	19.214000	note HINT: the 1998/2017 notes put a track start here: Your Sound / J Majik HINT
"""
REVIEW = """\
0.000000	0.000000	note REVIEW session s1: re-anchor rate 1.020399 from 2 pairs 2026-08-06
76.000000	76.000000	note REVIEW anchor s1: 1 orig072 105.992413
106.000000	106.000000	note REVIEW anchor s1: 2 orig072 136.600683
65.500000	65.500000	note REVIEW s1: orig072 95.714806 | Your Sound
"""


class CompileCase(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix="sc-repo-")
        self.addCleanup(shutil.rmtree, self.repo, True)
        self.labels = os.path.join(self.repo, "labels")
        for d in ("automated", "review"):
            os.makedirs(os.path.join(self.labels, d))
        root = unittest.mock.patch.object(align_inspect, "ROOT_DIR", self.repo)
        root.start()
        self.addCleanup(root.stop)

    def _write(self, sub, name, text):
        path = os.path.join(self.labels, sub, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def _fixture(self):
        self._write("automated", "d376-395.orig072.match.hints.tsv", STREAM_PAIR)
        self._write("automated", "orig072.d376-395.match.hints.tsv", ORIG_PAIR)
        self._write("automated", "d376-395.hints.tsv", STEM_HINTS)
        self._write("review", "d376-395.orig072.review.tsv", REVIEW)

    def _read(self, name):
        with open(os.path.join(self.labels, "summary", name), encoding="utf-8") as fh:
            return fh.read()

    def _rows(self, name):
        return [l.split("\t") for l in self._read(name).splitlines()]


class TheStemSummary(CompileCase):
    def test_every_input_row_rides_through_verbatim(self):
        """Drops nothing (decision made 2026-08-11): every automated row — QUESTION rows, the
        summary notes, the file-start row — and every review row, text untouched."""
        self._fixture()
        sc.compile_summaries()
        texts = [r[2] for r in self._rows("d376-395.summary.tsv")]
        for src in (STREAM_PAIR, STEM_HINTS, REVIEW):
            for line in src.splitlines():
                self.assertIn(line.split("\t")[2], texts)
        # inputs: 4 + 2 + 4 rows, + the gap-filling seat row and its note
        self.assertEqual(len(texts), 4 + 2 + 4 + 2)

    def test_the_verified_token_is_never_rewritten(self):
        """Both families untouched: the bare machine token on the anchor rows stays,
        and the prose REVIEW rows come out byte-identical (they carry neither)."""
        self._fixture()
        sc.compile_summaries()
        out = self._read("d376-395.summary.tsv")
        self.assertIn("\ttrack sync: 1 verified confidence 5.9/10 MATCH -35.372s\n", out)
        for line in REVIEW.splitlines():
            self.assertIn(line + "\n", out)
        self.assertEqual(out.count(" verified"), 2)         # the two anchor rows, no more

    def test_the_gap_seat_is_a_minus_b_native_in_the_converters_grammar(self):
        """The converter omits a seat before the capture's first sample (TIMELINE_GUIDE),
        so this pair has no `orig072 start:` row: the compile fills the gap with ONE row
        in the converter's own grammar — seat = a − b_native (the correction of
        2026-08-17: the clip seat, NOT the rate-corrected zero) — plus a note row."""
        self._fixture()
        sc.compile_summaries()
        rows = self._rows("d376-395.summary.tsv")
        seat = [r for r in rows if r[2].startswith("orig072 start:")]
        self.assertEqual(len(seat), 1)
        self.assertEqual(seat[0][0], "%.6f" % (106.0 - 136.600683))     # negative is fine
        self.assertEqual(seat[0][1], seat[0][0])
        self.assertEqual(seat[0][2], "orig072 start: 2 confidence 6.8/10")
        note = [r for r in rows if r[2].startswith("orig072 note:")]
        self.assertEqual(len(note), 1)
        self.assertEqual(note[0][0], seat[0][0])
        self.assertIn("summary compile", note[0][2])
        self.assertIn("converter emitted no start row", note[0][2])

    def test_the_gap_seat_takes_the_best_confidence_anchor_not_the_earliest(self):
        """Anchor 2 (6.8/10) beats anchor 1 (5.9/10) although anchor 1 is earlier."""
        self._fixture()
        sc.compile_summaries()
        seat = [r for r in self._rows("d376-395.summary.tsv") if r[2].startswith("orig072 start:")][0]
        self.assertEqual(seat[2], "orig072 start: 2 confidence 6.8/10")
        self.assertEqual(seat[0], "%.6f" % (106.0 - 136.600683))

    def test_a_tie_on_confidence_goes_to_the_earliest_anchor(self):
        self._write("automated", "x.orig005.match.hints.tsv",
                    "90.000000\t90.000000\ttrack sync: 2 verified confidence 5.0/10\n"
                    "50.000000\t50.000000\ttrack sync: 1 verified confidence 5.0/10\n")
        self._write("automated", "orig005.x.match.hints.tsv",
                    "20.000000\t20.000000\torig005 sync: 1 verified confidence 5.0/10\n"
                    "61.000000\t61.000000\torig005 sync: 2 verified confidence 5.0/10\n")
        sc.compile_summaries()
        seat = [r for r in self._rows("x.summary.tsv") if r[2].startswith("orig005 start:")]
        self.assertEqual([s[2] for s in seat], ["orig005 start: 1 confidence 5.0/10"])
        self.assertEqual(seat[0][0], "30.000000")

    def test_no_seat_is_added_when_the_converter_already_emitted_one(self):
        """The real shape for a positive seat: the pair file carries the converter's
        per-anchor `origNNN start: k confidence n/10` rows — they ride verbatim and the
        compile adds nothing (no duplicate, no third grammar)."""
        self._write("automated", "x.orig005.match.hints.tsv",
                    "50.000000\t50.000000\ttrack sync: 1 verified confidence 5.0/10\n"
                    "30.000000\t30.000000\torig005 start: 1 confidence 5.0/10\n"
                    "230.000000\t230.000000\torig005 end: 1 confidence 5.0/10\n")
        self._write("automated", "orig005.x.match.hints.tsv",
                    "20.000000\t20.000000\torig005 sync: 1 verified confidence 5.0/10\n")
        sc.compile_summaries()
        rows = self._rows("x.summary.tsv")
        self.assertEqual([r for r in rows if r[2].startswith("orig005 start:")],
                         [["30.000000", "30.000000", "orig005 start: 1 confidence 5.0/10"]])
        self.assertFalse([r for r in rows if r[2].startswith("orig005 note:")])

    def test_no_seat_row_without_a_marker_on_both_sides(self):
        self._write("automated", "x.orig005.match.hints.tsv",
                    "50.000000\t50.000000\ttrack sync: 1 verified confidence 5.0/10\n")
        self._write("automated", "orig005.x.match.hints.tsv",
                    "20.000000\t20.000000\torig005 sync: 9 verified confidence 5.0/10\n")
        sc.compile_summaries()
        self.assertFalse([r for r in self._rows("x.summary.tsv") if "start:" in r[2]])

    def test_sort_is_file_start_first_then_time_major(self):
        """sort_tsv.py's discipline: `file start` rows lead even below a negative seat;
        then time, so a summary diffs cleanly and imports predictably."""
        self._fixture()
        sc.compile_summaries()
        rows = self._rows("d376-395.summary.tsv")
        self.assertTrue(rows[0][2].startswith("file start sync?:"))
        self.assertTrue(rows[1][2].startswith("orig072 "))                # the -30.6 s seat + note
        times = [float(r[0]) for r in rows[1:]]
        self.assertEqual(times, sorted(times))

    def test_markers_are_identical_in_both_files(self):
        """`track sync: k` in the stem summary ↔ `origNNN sync: k` in the original's:
        the operator's cross-track eye check depends on it."""
        self._fixture()
        sc.compile_summaries()
        sk = sorted(r[2].split()[2] for r in self._rows("d376-395.summary.tsv")
                    if r[2].startswith("track sync:"))
        ok = sorted(r[2].split()[2] for r in self._rows("orig072.summary.tsv")
                    if r[2].startswith("orig072 sync:"))
        self.assertEqual(sk, ok)
        self.assertEqual(sk, ["1", "2"])

    def test_a_multi_pair_capture_merges_every_pair_and_seats_each_original(self):
        self._fixture()
        self._write("automated", "d376-395.orig073.match.hints.tsv",
                    "300.000000\t300.000000\ttrack sync: 1 verified confidence 4.0/10\n")
        self._write("automated", "orig073.d376-395.match.hints.tsv",
                    "10.000000\t10.000000\torig073 sync: 1 verified confidence 4.0/10\n")
        sc.compile_summaries()
        texts = [r[2] for r in self._rows("d376-395.summary.tsv")]
        self.assertEqual(sum(t.startswith("orig072 start:") for t in texts), 1)
        self.assertEqual(sum(t.startswith("orig073 start:") for t in texts), 1)
        self.assertEqual(sum(t.startswith("track sync:") for t in texts), 3)

    def test_a_torn_last_line_is_dropped_and_reported(self):
        """A review append in flight (no trailing newline yet) must never be copied into
        a summary — the committer could ship it before the next compile heals it."""
        self._fixture()
        self._write("review", "d376-395.orig072.review.tsv",
                    REVIEW + "80.000000\t80.000000\tnote REVIEW s1: orig072 110.5")
        rep = sc.compile_summaries()
        self.assertEqual(rep["torn"], ["d376-395.orig072.review.tsv"])
        self.assertNotIn("110.5", self._read("d376-395.summary.tsv"))
        self.assertNotIn("110.5", self._read("orig072.summary.tsv"))

    def test_the_review_directory_is_read_under_the_append_lock(self):
        import review_notes
        self._fixture()
        seen = []
        real = sc._read_rows

        def spy(path):
            seen.append((os.path.basename(path), review_notes._append_lock.locked()))
            return real(path)
        with unittest.mock.patch.object(sc, "_read_rows", spy):
            sc.compile_summaries()
        self.assertIn(("d376-395.orig072.review.tsv", True), seen)
        self.assertIn(("d376-395.hints.tsv", False), seen)


class TheOriginalSummary(CompileCase):
    def test_review_rows_land_on_the_records_clock_from_their_own_text(self):
        """§6.6 rows carry `origNNN <orig_s>`; the conversion is a read, not arithmetic."""
        self._fixture()
        sc.compile_summaries()
        rows = self._rows("orig072.summary.tsv")
        by_text = {r[2]: r[0] for r in rows}
        self.assertEqual(by_text["note REVIEW s1: orig072 95.714806 | Your Sound"], "95.714806")
        self.assertEqual(by_text["note REVIEW anchor s1: 1 orig072 105.992413"], "105.992413")
        self.assertEqual(by_text["note REVIEW session s1: re-anchor rate 1.020399 from 2 pairs 2026-08-06"],
                         "0.000000")

    def test_the_orig_pair_rows_ride_verbatim_and_sorted(self):
        self._fixture()
        sc.compile_summaries()
        rows = self._rows("orig072.summary.tsv")
        self.assertIn(["105.992413", "105.992413",
                       "orig072 sync: 1 verified confidence 5.9/10 MATCH -35.372s"], rows)
        times = [float(r[0]) for r in rows]
        self.assertEqual(times, sorted(times))
        self.assertEqual(len(rows), 3 + 4)                # pair rows + converted review rows

    def test_an_original_across_two_captures_merges_into_one_file(self):
        """The point of the per-original file (and the retirement of matchconv's
        clobbered-by-name hazard): every capture's rows, one clock."""
        self._fixture()
        self._write("automated", "d396-410.orig072.match.hints.tsv",
                    "5.000000\t5.000000\ttrack sync: 1 verified confidence 7.0/10\n")
        self._write("automated", "orig072.d396-410.match.hints.tsv",
                    "400.000000\t400.000000\torig072 sync: 1 verified confidence 7.0/10\n")
        sc.compile_summaries()
        rows = self._rows("orig072.summary.tsv")
        self.assertEqual(rows[-1][0], "400.000000")
        self.assertEqual(len(rows), 3 + 4 + 1)
        self.assertTrue(os.path.isfile(os.path.join(self.labels, "summary", "d396-410.summary.tsv")))

    def test_a_review_row_about_another_original_stays_stream_side_only(self):
        """A row the grammar cannot place on THIS record's clock is never invented
        there — it stays in the stem summary (nothing dropped) and is counted."""
        self._fixture()
        self._write("review", "d376-395.orig072.review.tsv",
                    REVIEW + "80.000000\t80.000000\tnote REVIEW s1: orig099 1.000000 | stray\n"
                    + "81.000000\t81.000000\tnote something else entirely\n")
        rep = sc.compile_summaries()
        self.assertEqual(rep["unconverted"], 2)
        otexts = [r[2] for r in self._rows("orig072.summary.tsv")]
        self.assertFalse([t for t in otexts if "stray" in t or "something else" in t])
        stexts = [r[2] for r in self._rows("d376-395.summary.tsv")]
        self.assertTrue([t for t in stexts if "stray" in t])
        self.assertTrue([t for t in stexts if "something else" in t])


class TheWrite(CompileCase):
    def test_idempotent_second_run_changes_nothing(self):
        self._fixture()
        first = sc.compile_summaries()
        self.assertTrue(all(f["changed"] for f in first["files"]))
        before = {f["file"]: self._read(f["file"]) for f in first["files"]}
        second = sc.compile_summaries()
        self.assertFalse(any(f["changed"] for f in second["files"]))
        self.assertEqual({f["file"]: self._read(f["file"]) for f in second["files"]}, before)

    def test_stale_summary_files_are_removed_but_nothing_else_is(self):
        """Derived-only output: a summary the compile did not produce is stale. Other
        files in the directory (the README) are not the compile's to touch."""
        self._fixture()
        self._write("summary", "gone.summary.tsv", "0.000000\t0.000000\tstale\n")
        self._write("summary", "orig999.summary.tsv", "0.000000\t0.000000\tstale\n")
        self._write("summary", "README.md", "# keep me\n")
        rep = sc.compile_summaries()
        self.assertEqual(sorted(rep["removed"]), ["gone.summary.tsv", "orig999.summary.tsv"])
        listing = sorted(os.listdir(os.path.join(self.labels, "summary")))
        self.assertEqual(listing, ["README.md", "d376-395.summary.tsv", "orig072.summary.tsv"])

    def test_dry_run_writes_nothing_and_removes_nothing(self):
        self._fixture()
        self._write("summary", "gone.summary.tsv", "0.000000\t0.000000\tstale\n")
        rep = sc.compile_summaries(dry_run=True)
        self.assertTrue(rep["dry_run"])
        self.assertEqual(rep["removed"], ["gone.summary.tsv"])
        self.assertEqual(sorted(os.listdir(os.path.join(self.labels, "summary"))),
                         ["gone.summary.tsv"])
        self.assertTrue(all(f["changed"] is None for f in rep["files"]))

    def test_the_write_is_whole_no_temp_file_survives(self):
        self._fixture()
        sc.compile_summaries()
        self.assertFalse([f for f in os.listdir(os.path.join(self.labels, "summary"))
                          if f.startswith(".summary-")])

    def test_a_failed_write_leaves_the_old_bytes(self):
        self._fixture()
        sc.compile_summaries()
        old = self._read("d376-395.summary.tsv")
        self._write("automated", "d376-395.hints.tsv", STEM_HINTS + "1.000000\t1.000000\tnote HINT: more HINT\n")
        with unittest.mock.patch.object(sc.os, "replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                sc.compile_summaries()
        self.assertEqual(self._read("d376-395.summary.tsv"), old)
        self.assertFalse([f for f in os.listdir(os.path.join(self.labels, "summary"))
                          if f.startswith(".summary-")])

    def test_only_the_two_input_directories_are_read(self):
        """The hand labels at the labels root are neither read nor written: a root
        `.labels.tsv` never appears in a summary, and the root is byte-identical after."""
        self._fixture()
        root = self._write("", "d376-395.labels.tsv", "0.000000\t0.000000\tfile start d376-395.wav\n")
        sc.compile_summaries()
        with open(root, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "0.000000\t0.000000\tfile start d376-395.wav\n")
        self.assertNotIn("file start d376-395.wav", self._read("d376-395.summary.tsv"))
        self.assertEqual(sorted(os.listdir(self.labels)),
                         ["automated", "d376-395.labels.tsv", "review", "summary"])

    def test_a_symlink_escaping_labels_is_ignored(self):
        outside = tempfile.mkdtemp(prefix="sc-outside-")
        self.addCleanup(shutil.rmtree, outside, True)
        with open(os.path.join(outside, "secret.tsv"), "w") as fh:
            fh.write("0.000000\t0.000000\ttrack sync: 1 verified confidence 1.0/10\n")
        os.symlink(os.path.join(outside, "secret.tsv"),
                   os.path.join(self.labels, "automated", "evil.orig001.match.hints.tsv"))
        self._fixture()
        rep = sc.compile_summaries()
        self.assertFalse([f for f in rep["files"] if f["id"] == "evil"])

    def test_a_symlink_to_the_hand_labels_one_level_up_is_ignored_too(self):
        """Containment is per subdirectory: automated/ cannot reach labels/*.labels.tsv."""
        self._fixture()
        root = self._write("", "d376-395.labels.tsv", "0.000000\t0.000000\tfile start d376-395.wav\n")
        os.symlink(root, os.path.join(self.labels, "automated", "sneaky.hints.tsv"))
        sc.compile_summaries()
        self.assertNotIn("file start d376-395.wav", self._read("d376-395.summary.tsv"))
        self.assertFalse(os.path.isfile(os.path.join(self.labels, "summary", "sneaky.summary.tsv")))

    def test_a_stem_shaped_like_an_original_tag_is_refused(self):
        """`orig072.hints.tsv` would compile to orig072.summary.tsv — the original's file."""
        self._fixture()
        self._write("automated", "orig072.hints.tsv", "1.000000\t1.000000\tnote HINT: bogus HINT\n")
        sc.compile_summaries()
        self.assertNotIn("bogus", self._read("orig072.summary.tsv"))

    def test_a_non_utf8_byte_is_a_500_naming_no_absolute_path(self):
        self._fixture()
        with open(os.path.join(self.labels, "review", "d376-395.orig072.review.tsv"), "ab") as fh:
            fh.write(b"1.000000\t1.000000\tnote REVIEW s1: orig072 2.000000 | caf\xe9\n")
        payload, status = sc.handle_compile({})
        self.assertEqual(status, 500)
        self.assertIn("compile failed", payload["error"])
        self.assertNotIn(self.repo, payload["error"])

    def test_file_name_shapes_are_disambiguated(self):
        """`x.orig072.match.hints.tsv` is a pair file, never read as the stem hints of a
        stem called `x.orig072.match`; a stray name in automated/ is ignored."""
        self._fixture()
        self._write("automated", "stray.txt", "0.000000\t0.000000\tnope\n")
        rep = sc.compile_summaries()
        self.assertEqual(sorted(f["id"] for f in rep["files"]), ["d376-395", "orig072"])

    def test_empty_inputs_compile_to_nothing(self):
        rep = sc.compile_summaries()
        self.assertEqual(rep["files"], [])
        self.assertTrue(os.path.isdir(os.path.join(self.labels, "summary")))


class TheGuards(CompileCase):
    """The 2026-08-30 fresh-review round: missing inputs refuse, sign-aware note prose,
    a corrupt existing summary is overwritten not fatal."""

    def test_missing_input_dirs_refuse_instead_of_deleting_every_summary(self):
        # A checkout mid-sync has labels/summary/ but neither input dir. "No inputs" must be
        # a configuration error: the old behaviour unlinked every summary, exited 0, and
        # review_sync committed the deletions.
        self._write("summary", "d376-395.summary.tsv", "0.000000\t0.000000\tx\n")
        for d in ("automated", "review"):
            shutil.rmtree(os.path.join(self.labels, d))
        with self.assertRaises(OSError):
            sc.compile_summaries()
        self.assertTrue(os.path.isfile(
            os.path.join(self.labels, "summary", "d376-395.summary.tsv")),
            "the existing summary must survive the refusal")

    def test_compiled_note_prose_matches_the_seat_sign(self):
        # A positive seat (an old converter that emitted no start rows at all) must not
        # claim "every anchor seats it before the capture's first sample".
        self._write("automated", "d376-395.orig080.match.hints.tsv",
                    "50.000000\t50.000000\ttrack sync: 1 verified confidence 5.0/10\n")
        self._write("automated", "orig080.d376-395.match.hints.tsv",
                    "20.000000\t20.000000\torig080 sync: 1 verified confidence 5.0/10\n")
        report = sc.compile_summaries()
        rows = open(os.path.join(self.labels, "summary", "d376-395.summary.tsv"),
                    encoding="utf-8").read()
        self.assertIn("orig080 start: 1 confidence 5.0/10", rows)
        self.assertIn("emitted no start row for this pair", rows)
        self.assertNotIn("before the capture's first sample", rows)
        # and the negative-seat case keeps the causal clause (the STREAM_PAIR fixture's
        # orig072 anchors all seat before the head)
        self._write("automated", "d376-395.orig072.match.hints.tsv", STREAM_PAIR)
        self._write("automated", "orig072.d376-395.match.hints.tsv", ORIG_PAIR)
        sc.compile_summaries()
        rows = open(os.path.join(self.labels, "summary", "d376-395.summary.tsv"),
                    encoding="utf-8").read()
        self.assertIn("before the capture's first sample", rows)

    def test_a_corrupt_existing_summary_is_overwritten_not_fatal(self):
        self._write("automated", "d376-395.orig072.match.hints.tsv", STREAM_PAIR)
        self._write("automated", "orig072.d376-395.match.hints.tsv", ORIG_PAIR)
        os.makedirs(os.path.join(self.labels, "summary"), exist_ok=True)
        with open(os.path.join(self.labels, "summary", "d376-395.summary.tsv"), "wb") as fh:
            fh.write(b"\xe9 not utf-8")
        report = sc.compile_summaries()          # must not raise
        self.assertTrue(report["ok"])
        rows = open(os.path.join(self.labels, "summary", "d376-395.summary.tsv"),
                    encoding="utf-8").read()                  # decodes now
        self.assertIn("track sync: 1", rows)

    def test_summaries_are_world_readable(self):
        self._write("automated", "d376-395.orig072.match.hints.tsv", STREAM_PAIR)
        self._write("automated", "orig072.d376-395.match.hints.tsv", ORIG_PAIR)
        old = os.umask(0o022)
        try:
            sc.compile_summaries()
        finally:
            os.umask(old)
        mode = os.stat(os.path.join(self.labels, "summary",
                                    "d376-395.summary.tsv")).st_mode & 0o777
        self.assertEqual(mode, 0o644)


class TheRoute(CompileCase):
    def test_dark_without_a_checkout(self):
        with unittest.mock.patch.object(align_inspect, "ROOT_DIR", ""):
            payload, status = sc.handle_compile({})
        self.assertEqual(status, 404)
        self.assertIn("not configured", payload["error"])

    def test_dark_when_the_checkout_has_no_labels_dir(self):
        shutil.rmtree(self.labels)
        payload, status = sc.handle_compile({})
        self.assertEqual(status, 404)

    def test_a_non_object_body_is_refused(self):
        for body in ([], "x", 5):
            with self.subTest(body=body):
                payload, status = sc.handle_compile(body)
                self.assertEqual(status, 400)

    def test_null_and_empty_bodies_compile(self):
        self._fixture()
        for body in (None, {}):
            payload, status = sc.handle_compile(body)
            self.assertEqual(status, 200, payload)
            self.assertTrue(payload["ok"])
            self.assertEqual(sorted(f["file"] for f in payload["files"]),
                             ["d376-395.summary.tsv", "orig072.summary.tsv"])

    def test_dry_run_flag_rides_the_body(self):
        self._fixture()
        payload, status = sc.handle_compile({"dry_run": True})
        self.assertEqual(status, 200)
        self.assertTrue(payload["dry_run"])
        self.assertFalse(os.path.isfile(os.path.join(self.labels, "summary", "d376-395.summary.tsv")))

    def test_the_report_is_json_serialisable(self):
        self._fixture()
        payload, _ = sc.handle_compile({})
        json.dumps(payload)

    def test_the_server_nudges_review_sync_on_a_real_compile_only(self):
        """summary/ is in review_sync's PATHSPEC: a real compile nudges the committer;
        a dry run has nothing to commit."""
        import io
        import align_server
        import review_sync
        self._fixture()

        def post(body):
            h = align_server.AlignHandler.__new__(align_server.AlignHandler)
            h.path = align_server.AlignHandler.COMPILE_PATH
            h.headers = {"Content-Length": str(len(body))}
            h.rfile = io.BytesIO(body.encode())
            h.wfile = io.BytesIO()
            h.status = None
            h.send_response = lambda c: setattr(h, "status", c)
            h.send_header = lambda k, v: None
            h.end_headers = lambda: None
            h._gate = lambda: True
            align_server.AlignHandler.do_POST(h)
            return h.status, json.loads(h.wfile.getvalue().decode() or "{}")

        with unittest.mock.patch.object(review_sync, "nudge") as nudge:
            status, out = post('{"dry_run": true}')
            self.assertEqual(status, 200)
            nudge.assert_not_called()
            status, out = post("{}")
            self.assertEqual(status, 200, out)
            nudge.assert_called_once()
        self.assertEqual(len(out["files"]), 2)


class ThePageAndTheLayout(unittest.TestCase):
    """Static pins on the surfaces: the button, its route, and that the page still never
    fetches anywhere foreign."""

    @classmethod
    def setUpClass(cls):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "public", "align.html"), encoding="utf-8") as fh:
            cls.html = fh.read()
        with open(os.path.join(here, "Makefile"), encoding="utf-8") as fh:
            cls.makefile = fh.read()

    def test_the_page_posts_to_the_compile_route_with_an_empty_body(self):
        self.assertIn('id="btnCompile"', self.html)
        self.assertIn('fetch("/api/align/compile-summary"', self.html)
        self.assertIn('body: "{}"', self.html)

    def test_the_download_stays_available(self):
        self.assertIn('id="btnExport"', self.html)

    def test_make_target_exists(self):
        self.assertIn("compile-summary:", self.makefile)
        self.assertIn("scripts/summary_compile.py $(ARGS)", self.makefile)


if __name__ == "__main__":
    unittest.main()
