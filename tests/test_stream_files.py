"""Tests for the label-timeline parser and the stream-files.json generator."""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import stream_files as sf  # noqa: E402
from streamalign import groundtruth  # noqa: E402


class LabelDirCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="stream-files-")
        self.labels = os.path.join(self.tmp, "labels")
        os.mkdir(self.labels)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_labels(self, stem, rows, suffix=".labels.tsv"):
        with open(os.path.join(self.labels, stem + suffix), "w", encoding="utf-8") as fh:
            for seconds, text in rows:
                fh.write("%s\t%s\t%s\n" % (seconds, seconds, text))

    def timeline(self):
        return sf.parse_file_timeline(sf.read_label_rows(self.labels))[0]


class TimelineTests(LabelDirCase):
    def test_seed_at_local_zero(self):
        self.write_labels("a", [(0.0, "file start sync: a.wav 100")])
        self.assertEqual(self.timeline()["a.mp3"]["master_start_seconds"], 100.0)

    def test_trailing_anchor_is_not_subtracted(self):
        # The regression this grammar once had: the trailing number on a cross-file row is the
        # anchor it was verified against, not an offset. b starts at a's start + 50 s.
        self.write_labels("a", [(0.0, "file start sync: a.wav 100"),
                                (50.0, "file start sync: b.wav 1102.848 verified a")])
        self.assertEqual(self.timeline()["b.mp3"]["master_start_seconds"], 150.0)

    def test_mark_placeholder_still_places_the_file(self):
        self.write_labels("a", [(0.0, "file start sync: a.wav 100"),
                                (20.0, "file start sync: b.wav MARK")])
        self.assertEqual(self.timeline()["b.mp3"]["master_start_seconds"], 120.0)

    def test_file_start_seeds_only_a_file_with_no_sync_row(self):
        self.write_labels("a", [(0.0, "file start sync: a.wav 100"),
                                (-5.0, "note: file start c.wav"),
                                (30.0, "note: file start b.wav")])
        self.write_labels("b", [(0.0, "file sync: b.wav 400")])
        tl = self.timeline()
        self.assertEqual(tl["c.mp3"]["master_start_seconds"], 95.0)
        self.assertEqual(tl["b.mp3"]["master_start_seconds"], 400.0)

    def test_file_end_latest_wins(self):
        self.write_labels("a", [(0.0, "file start sync: a.wav 100"),
                                (60.0, "file end: b.wav"),
                                (70.0, "file end b.wav"),
                                (90.0, "file end: a")])
        tl = self.timeline()
        self.assertEqual(tl["a.mp3"]["master_end_seconds"], 190.0)
        # b has no start, so it is not in the timeline; its end is still resolved.
        _starts, ends, _owners = sf.resolve_timeline(sf.read_label_rows(self.labels))
        self.assertEqual(ends["b.mp3"], 170.0)

    def test_chain_resolves_across_files(self):
        self.write_labels("a", [(0.0, "file start sync: a.wav 0"),
                                (10.0, "file start sync: b.wav 0")])
        self.write_labels("b", [(0.0, "file start sync: b.wav 10"),
                                (25.0, "file start sync: c.wav 0")])
        self.assertEqual(self.timeline()["c.mp3"]["master_start_seconds"], 35.0)

    def test_cross_file_row_overrides_a_seed(self):
        # Precedence pinned: once its owner is placed, a cross-file sync row wins over the
        # named file's own seed.
        self.write_labels("a", [(0.0, "file start sync: a.wav 0"),
                                (10.0, "file start sync: b.wav 0")])
        self.write_labels("b", [(0.0, "file start sync: b.wav 500")])
        self.assertEqual(self.timeline()["b.mp3"]["master_start_seconds"], 10.0)

    def test_chain_needs_a_second_round(self):
        # "a" sorts first but is owned by c, whose start is known only after the row in "b"
        # places it; so c's row in "a" can place d only in the second round of pass 3.
        self.write_labels("a", [(0.0, "file start sync: c.wav 0"),
                                (5.0, "file start sync: d.wav 0")])
        self.write_labels("b", [(0.0, "file start sync: b.wav 100"),
                                (20.0, "file start sync: c.wav 0")])
        rows = sf.read_label_rows(self.labels)
        starts, _ends, _owners = sf.resolve_timeline(rows)
        self.assertEqual(starts["c.mp3"], 120.0)
        self.assertEqual(starts["d.mp3"], 125.0)

    def test_self_row_places_nothing(self):
        self.write_labels("a", [(0.0, "file start sync: a.wav 100"),
                                (30.0, "file start sync: a.wav 7")])
        self.assertEqual(self.timeline()["a.mp3"]["master_start_seconds"], 100.0)

    def test_starter_files_are_never_read(self):
        self.write_labels("a", [(0.0, "file start sync: a.wav 100")])
        self.write_labels("a", [(0.0, "file start sync: a.wav 999")], suffix=".starter.labels.tsv")
        self.assertEqual(self.timeline()["a.mp3"]["master_start_seconds"], 100.0)

    def test_groundtruth_keys_by_stem_with_the_same_values(self):
        self.write_labels("a", [(0.0, "file start sync: a.wav 100"),
                                (50.0, "file start sync: b.wav 1102.848 verified a")])
        self.assertEqual(groundtruth.resolve_starts(self.labels), {"a": 100.0, "b": 150.0})

    def test_a_chain_longer_than_six_links_resolves(self):
        # Each file is read after the one whose capture it places (they sort in reverse chain
        # order), so each round settles one link: nine links need nine rounds.
        for i in range(9):
            seed = 0 if i == 0 else 1000
            self.write_labels("l%d" % (9 - i), [(0.0, "file start sync: x%d.wav %d" % (i, seed)),
                                                (10.0, "file start sync: x%d.wav 0" % (i + 1))])
        tl = self.timeline()
        self.assertEqual(tl["x9.mp3"]["master_start_seconds"], 90.0)

    def test_rows_that_never_settle_stop_the_build_naming_the_captures(self):
        # a places b at a + 10 and b places a at b + 10: each round moves both again.
        self.write_labels("a", [(0.0, "file start sync: a.wav 0"),
                                (10.0, "file start sync: b.wav 0")])
        self.write_labels("b", [(0.0, "file start sync: b.wav 0"),
                                (10.0, "file start sync: a.wav 0")])
        with self.assertRaises(sf.BuildRefused) as ctx:
            sf.resolve_timeline(sf.read_label_rows(self.labels))
        self.assertIn("did not settle", str(ctx.exception))
        self.assertIn("a.mp3", str(ctx.exception))
        self.assertIn("b.mp3", str(ctx.exception))

    def test_an_unreadable_label_file_stops_the_read_naming_it(self):
        self.write_labels("a", [(0.0, "file start sync: a.wav 100")])
        os.mkdir(os.path.join(self.labels, "b.labels.tsv"))     # open() fails on a directory
        with self.assertRaises(sf.BuildRefused) as ctx:
            sf.read_label_rows(self.labels)
        self.assertIn("b.labels.tsv", str(ctx.exception))


class PrimarySpecTests(LabelDirCase):
    def test_timing_section(self):
        path = os.path.join(self.tmp, "tracklist.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("Timing\n"
                     "x   d019-040.wav\tPRIMARY FILE\n"
                     "x   d088-107.wav\tPRIMARY FILE (actual 087-108)\n"
                     "x   d-14Nov10-b\t\t200-220 PRIMARY\n"
                     "    d019-040.wav\tPRIMARY\n"
                     "    d472-491.wav\tPRIMARY?\n"
                     "    d500-510.wav\tnot this one\n"
                     "definitive track listing\n"
                     "    d999-999.wav\tPRIMARY\n")
        specs = sf.read_primary_specs(path)
        self.assertEqual([s["filename"] for s in specs],
                         ["d019-040.mp3", "d088-107.mp3", "d-14Nov10-b.mp3", "d472-491.mp3"])
        ranges = {s["filename"]: (s["range_start_seconds"], s["range_end_seconds"]) for s in specs}
        self.assertEqual(ranges["d088-107.mp3"], (87 * 60.0, 108 * 60.0))
        self.assertEqual(ranges["d-14Nov10-b.mp3"], (200 * 60.0, 220 * 60.0))


class BuildTests(LabelDirCase):
    def test_build(self):
        self.write_labels("da", [(0.0, "file start sync: da.wav 100"),
                                (50.0, "file start sync: db.wav 1 verified da"),
                                (80.0, "file end: da")])
        tracklist = os.path.join(self.tmp, "tracklist.txt")
        with open(tracklist, "w", encoding="utf-8") as fh:
            fh.write("x   da.wav\tPRIMARY 1-3\n")
        metadata = os.path.join(self.tmp, "track-metadata.json")
        with open(metadata, "w", encoding="utf-8") as fh:
            json.dump({"tracks": {
                "1": {"master_begin_seconds": 90.0, "master_end_seconds": 120.0},
                "2": {"master_begin_seconds": 120.0, "master_end_seconds": 200.0},
                "3": {"master_begin_seconds": 500.0, "source_files": ["dc.wav"]},
            }}, fh)
        previous = {"da.mp3": {"filename": "da.mp3", "duration": 80.0, "size": 10},
                    "db.mp3": {"filename": "db.mp3", "duration_seconds": 61.5, "size": 20},
                    "dc.mp3": {"filename": "dc.mp3"}}
        data = sf.build(labels_dir=self.labels, tracklist_path=tracklist, metadata_path=metadata,
                        previous=previous)
        self.assertEqual(data["primary_files"], ["da.mp3"])
        rows = {r["filename"]: r for r in data["files"]}
        a, b, c = rows["da.mp3"], rows["db.mp3"], rows["dc.mp3"]
        self.assertEqual((a["master_start_seconds"], a["master_end_seconds"]), (100.0, 180.0))
        self.assertEqual((a["timing_range_start_seconds"], a["timing_range_end_seconds"]),
                         (60.0, 180.0))
        self.assertEqual((a["duration_seconds"], a["duration"], a["duration_label"], a["size"]),
                         (80.0, 80.0, "1:20", 10))
        self.assertEqual(a["track_count"], 2)
        self.assertEqual(a["original_filename"], "da.wav")
        self.assertEqual(b["master_start_seconds"], 150.0)
        self.assertEqual(b["duration_seconds"], 61.5)
        self.assertFalse(b["is_primary"])
        self.assertIsNone(c["master_start_seconds"])
        self.assertEqual(c["track_count"], 1)          # counted by its first source file
        self.assertIsNone(c["duration_seconds"])
        self.assertEqual([r["filename"] for r in data["files"]], ["da.mp3", "db.mp3", "dc.mp3"])
        self.assertEqual([r["listing_order"] for r in data["files"]], [1, 2, 3])

    def _inputs(self):
        self.write_labels("da", [(0.0, "file start sync: da.wav 100")])
        tracklist = os.path.join(self.tmp, "tracklist.txt")
        with open(tracklist, "w", encoding="utf-8") as fh:
            fh.write("x   da.wav\tPRIMARY 1-3\n")
        metadata = os.path.join(self.tmp, "track-metadata.json")
        with open(metadata, "w", encoding="utf-8") as fh:
            json.dump({"tracks": {}}, fh)
        return dict(labels_dir=self.labels, tracklist_path=tracklist, metadata_path=metadata)

    def _stub(self, body):
        path = os.path.join(self.tmp, "ffprobe")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\n" + body + "\n")
        os.chmod(path, 0o755)
        return path

    def _audio(self, *names):
        audio = os.path.join(self.tmp, "mp3")
        os.makedirs(audio, exist_ok=True)
        for name in names:
            with open(os.path.join(audio, name), "wb") as fh:
                fh.write(b"x" * 7)
        return audio

    def _originals(self, *names):
        captures = os.path.join(self.tmp, "captures")
        os.makedirs(captures, exist_ok=True)
        for name in names:
            with open(os.path.join(captures, name), "wb") as fh:
                fh.write(b"w" * 3)
        return captures

    def test_recorded_duration_is_reused_not_remeasured(self):
        # Measured once and committed: a recorded duration wins over the audio, so ffprobe's
        # last-digit drift never reaches the file. The size still comes from the audio.
        data = sf.build(previous={"da.mp3": {"duration_seconds": 1.0, "size": 1}},
                        audio_dir=self._audio("da.mp3"), ffprobe=self._stub("echo 12.5"),
                        **self._inputs())
        row = data["files"][0]
        self.assertEqual((row["duration_seconds"], row["size"]), (1.0, 7))

    def test_capture_with_no_duration_is_measured_from_its_original(self):
        # The stub prints the path it was given, so the test sees which file was measured.
        data = sf.build(previous={"da.mp3": {"size": 1}},
                        audio_dir=self._audio("da.mp3"), captures_dir=self._originals("da.wav"),
                        ffprobe=self._stub('case "$*" in *da.wav*) echo 12.5;; *) echo 99;; esac'),
                        **self._inputs())
        row = data["files"][0]
        self.assertEqual((row["duration_seconds"], row["duration"], row["size"]), (12.5, 12.5, 7))

    def test_the_mp3_is_never_measured(self):
        # 9.1A2: with the MP3 here and no original, nothing is measured; the capture is named.
        report = {}
        data = sf.build(previous={"da.mp3": {"duration_seconds": 1.0}},
                        audio_dir=self._audio("da.mp3"), captures_dir=self._originals(),
                        ffprobe=self._stub("echo 12.5"), remeasure=True, report=report,
                        **self._inputs())
        self.assertEqual(data["files"][0]["duration_seconds"], 1.0)
        self.assertEqual(report["no_original"], ["da.mp3"])

    def test_remeasure_measures_a_recorded_capture_again_from_its_original(self):
        data = sf.build(previous={"da.mp3": {"duration_seconds": 1.0}},
                        audio_dir=self._audio("da.mp3"), captures_dir=self._originals("da.wav"),
                        ffprobe=self._stub("echo 12.5"), remeasure=True, **self._inputs())
        self.assertEqual(data["files"][0]["duration_seconds"], 12.5)

    def test_a_recorded_capture_with_no_original_is_not_named_without_remeasure(self):
        report = {}
        sf.build(previous={"da.mp3": {"duration_seconds": 1.0}}, captures_dir=self._originals(),
                 ffprobe=self._stub("echo 12.5"), report=report, **self._inputs())
        self.assertEqual(report, {})

    def test_failing_probe_falls_back_to_the_recorded_duration(self):
        data = sf.build(previous={"da.mp3": {"duration": 1.0, "size": 1}},
                        audio_dir=self._audio("da.mp3"), captures_dir=self._originals("da.wav"),
                        ffprobe=self._stub("exit 1"), remeasure=True, **self._inputs())
        row = data["files"][0]
        self.assertEqual((row["duration_seconds"], row["size"]), (1.0, 7))

    def test_a_failed_probe_is_reported(self):
        report = {}
        sf.build(previous={"da.mp3": {}}, captures_dir=self._originals("da.wav"),
                 ffprobe=self._stub("exit 1"), report=report, **self._inputs())
        self.assertEqual(report["probe_failed"], ["da.mp3"])

    def _main(self, *extra, ffprobe):
        out = os.path.join(self.tmp, "stream-files.json")
        with open(out, "w", encoding="utf-8") as fh:
            json.dump({"files": [{"filename": "da.mp3", "duration_seconds": 1.0}]}, fh)
        with open(out, encoding="utf-8") as fh:
            before = fh.read()
        inputs = self._inputs()
        stdout, err = io.StringIO(), io.StringIO()
        env = {"NETRADIO_MP3_DIR": os.path.join(self.tmp, "none"),
               "NETRADIO_CAPTURES_DIR": self._originals("da.wav"), "NETRADIO_FFPROBE": ffprobe}
        with unittest.mock.patch.dict(os.environ, env), \
                unittest.mock.patch("sys.stdout", stdout), unittest.mock.patch("sys.stderr", err):
            rc = sf.main(["--out", out, "--labels-dir", inputs["labels_dir"],
                          "--tracklist", inputs["tracklist_path"],
                          "--metadata", inputs["metadata_path"], *extra])
        with open(out, encoding="utf-8") as fh:
            after = fh.read()
        return rc, before, after, stdout.getvalue(), err.getvalue()

    def test_remeasure_refuses_without_a_working_ffprobe(self):
        rc, before, after, out, _err = self._main(
            "--remeasure", ffprobe=os.path.join(self.tmp, "no-such-ffprobe"))
        self.assertEqual(rc, 1)
        self.assertEqual(before, after)
        self.assertIn("REFUSING TO WRITE", out)

    def test_main_names_a_capture_its_probe_could_not_measure(self):
        # `-version` answers, so the run goes ahead; the measurement itself fails.
        stub = self._stub('[ "$1" = "-version" ] && exit 0; exit 1')
        rc, _before, after, _out, err = self._main("--remeasure", ffprobe=stub)
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(after)["files"][0]["duration_seconds"], 1.0)
        self.assertIn("ffprobe could not measure", err)
        self.assertIn("da.mp3", err)

    def test_main_names_a_missing_ffprobe_rather_than_a_failed_probe(self):
        out = os.path.join(self.tmp, "stream-files.json")
        with open(out, "w", encoding="utf-8") as fh:
            json.dump({"files": [{"filename": "da.mp3"}]}, fh)     # no duration recorded
        inputs = self._inputs()
        err = io.StringIO()
        env = {"NETRADIO_MP3_DIR": os.path.join(self.tmp, "none"),
               "NETRADIO_CAPTURES_DIR": self._originals("da.wav"),
               "NETRADIO_FFPROBE": os.path.join(self.tmp, "no-such-ffprobe")}
        with unittest.mock.patch.dict(os.environ, env), \
                unittest.mock.patch("sys.stdout", io.StringIO()), \
                unittest.mock.patch("sys.stderr", err):
            rc = sf.main(["--out", out, "--labels-dir", inputs["labels_dir"],
                          "--tracklist", inputs["tracklist_path"],
                          "--metadata", inputs["metadata_path"], "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertIn("no working ffprobe", err.getvalue())
        self.assertNotIn("ffprobe could not measure", err.getvalue())

    def test_main_exits_non_zero_naming_an_unreadable_label_file(self):
        inputs = self._inputs()
        os.mkdir(os.path.join(self.labels, "db.labels.tsv"))
        out = os.path.join(self.tmp, "stream-files.json")
        before = json.dumps({"files": [{"filename": "da.mp3", "duration_seconds": 1.0}]})
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(before)
        stdout = io.StringIO()
        with unittest.mock.patch.dict(os.environ, {"NETRADIO_MP3_DIR": os.path.join(self.tmp, "none"),
                                                     "NETRADIO_CAPTURES_DIR": os.path.join(self.tmp, "none")}), \
                unittest.mock.patch("sys.stdout", stdout):
            rc = sf.main(["--out", out, "--labels-dir", inputs["labels_dir"],
                          "--tracklist", inputs["tracklist_path"],
                          "--metadata", inputs["metadata_path"]])
        self.assertEqual(rc, 1)
        self.assertIn("REFUSING TO WRITE", stdout.getvalue())
        self.assertIn("db.labels.tsv", stdout.getvalue())
        with open(out, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), before)

    def test_an_overridden_own_start_label_is_reported(self):
        self.write_labels("da", [(0.0, "file start sync: da.wav 1000"),
                                (237.408, "note db: file start sync: db.wav 963.763 verified da")])
        self.write_labels("db", [(0.0, "file start sync: db.wav 963.763 verified da")])
        tracklist = os.path.join(self.tmp, "tracklist.txt")
        with open(tracklist, "w", encoding="utf-8") as fh:
            fh.write("x   da.wav\tPRIMARY 1-3\n")
        metadata = os.path.join(self.tmp, "track-metadata.json")
        with open(metadata, "w", encoding="utf-8") as fh:
            json.dump({"tracks": {}}, fh)
        report = {}
        sf.build(labels_dir=self.labels, tracklist_path=tracklist, metadata_path=metadata,
                 previous={"da.mp3": {}, "db.mp3": {}}, report=report)
        self.assertEqual(list(report["overridden"]), ["db.mp3"])
        seed, placed = report["overridden"]["db.mp3"]
        self.assertAlmostEqual(seed, 963.763)
        self.assertAlmostEqual(placed, 1237.408)

    def test_original_path_spellings(self):
        captures = self._originals("d-14Nov10-b.au", "dnb-14Nov02-a.au", "d019-040.wav")
        self.assertEqual(sf.original_path("d201-221.14Nov10-b.mp3", captures),
                         os.path.join(captures, "d-14Nov10-b.au"))
        self.assertEqual(sf.original_path("d198-218.14Nov02-a.mp3", captures),
                         os.path.join(captures, "dnb-14Nov02-a.au"))
        self.assertEqual(sf.original_path("d019-040.mp3", captures),
                         os.path.join(captures, "d019-040.wav"))
        self.assertIsNone(sf.original_path("d507-000b.mp3", captures))
        self.assertIsNone(sf.original_path("d019-040.mp3", None))

    def test_labels_place_a_primary_capture_never_the_timing_range(self):
        # 9.1A3: the Timing range (minutes 50-70) disagrees with the labels (100 s to 180 s);
        # the labels place the capture, and the range is carried only in its own fields.
        self.write_labels("da", [(0.0, "file start sync: da.wav 100"),
                                (80.0, "file end: da")])
        tracklist = os.path.join(self.tmp, "tracklist.txt")
        with open(tracklist, "w", encoding="utf-8") as fh:
            fh.write("x   da.wav\tPRIMARY 50-70\n")
        metadata = os.path.join(self.tmp, "track-metadata.json")
        with open(metadata, "w", encoding="utf-8") as fh:
            json.dump({"tracks": {}}, fh)
        row = sf.build(labels_dir=self.labels, tracklist_path=tracklist, metadata_path=metadata,
                       previous={"da.mp3": {"duration_seconds": 80.0}})["files"][0]
        self.assertTrue(row["is_primary"])
        self.assertEqual((row["master_start_seconds"], row["master_end_seconds"]), (100.0, 180.0))
        self.assertEqual((row["timing_range_start_seconds"], row["timing_range_end_seconds"]),
                         (3000.0, 4200.0))

    def test_cross_file_placement_beats_the_captures_own_start_label(self):
        # 9.1A4, in the shape the real disagreements take: the capture's own label file seeds
        # it at local 0.0, and a `note` row in another capture's file places it differently.
        self.write_labels("da", [(0.0, "file start sync: da.wav 1000"),
                                (237.408, "note db: file start sync: db.wav 963.763 verified da")])
        self.write_labels("db", [(0.0, "file start sync: db.wav 963.763 verified da")])
        tracklist = os.path.join(self.tmp, "tracklist.txt")
        with open(tracklist, "w", encoding="utf-8") as fh:
            fh.write("x   da.wav\tPRIMARY 1-3\n")
        metadata = os.path.join(self.tmp, "track-metadata.json")
        with open(metadata, "w", encoding="utf-8") as fh:
            json.dump({"tracks": {}}, fh)
        rows = {r["filename"]: r for r in sf.build(
            labels_dir=self.labels, tracklist_path=tracklist, metadata_path=metadata,
            previous={"da.mp3": {}, "db.mp3": {}})["files"]}
        self.assertAlmostEqual(rows["db.mp3"]["master_start_seconds"], 1237.408)

    def test_no_duration_and_no_audio_writes_null_and_names_it_on_stderr(self):
        out = os.path.join(self.tmp, "stream-files.json")
        with open(out, "w", encoding="utf-8") as fh:
            json.dump({"files": [{"filename": "da.mp3", "size": 1}]}, fh)
        inputs = self._inputs()
        err = io.StringIO()
        with unittest.mock.patch.dict(os.environ, {"NETRADIO_MP3_DIR": os.path.join(self.tmp, "none"),
                                                     "NETRADIO_CAPTURES_DIR": os.path.join(self.tmp, "none")}), \
                unittest.mock.patch("sys.stdout"), unittest.mock.patch("sys.stderr", err):
            rc = sf.main(["--out", out, "--labels-dir", inputs["labels_dir"],
                          "--tracklist", inputs["tracklist_path"],
                          "--metadata", inputs["metadata_path"]])
        self.assertEqual(rc, 0)
        with open(out, encoding="utf-8") as fh:
            row = json.load(fh)["files"][0]
        self.assertIsNone(row["duration_seconds"])
        self.assertIn("da.mp3", err.getvalue())

    def test_unreadable_previous_file_is_refused(self):
        path = os.path.join(self.tmp, "stream-files.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("<<<<<<< HEAD\n{}\n")
        with self.assertRaises(sf.BuildRefused):
            sf.load_previous(path)
        self.assertEqual(sf.load_previous(os.path.join(self.tmp, "absent.json")), {})

    def test_main_exits_non_zero_and_writes_nothing_on_an_unreadable_file(self):
        path = os.path.join(self.tmp, "stream-files.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("<<<<<<< HEAD\n")
        inputs = self._inputs()
        with unittest.mock.patch.dict(os.environ, {"NETRADIO_MP3_DIR": os.path.join(self.tmp, "none"),
                                                     "NETRADIO_CAPTURES_DIR": os.path.join(self.tmp, "none")}), \
                unittest.mock.patch("sys.stdout"):
            rc = sf.main(["--out", path, "--labels-dir", inputs["labels_dir"],
                          "--tracklist", inputs["tracklist_path"],
                          "--metadata", inputs["metadata_path"]])
        self.assertEqual(rc, 1)
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "<<<<<<< HEAD\n")

    def test_empty_capture_list_is_refused(self):
        with self.assertRaises(sf.BuildRefused):
            sf.build(previous={}, **self._inputs())

    def test_shrink_needs_prune(self):
        previous = {"da.mp3": {"duration": 1.0}, "dz.mp3": {"duration": 2.0}}
        audio = self._audio("da.mp3")
        stub = self._stub("echo 3")
        # Default: keep every recorded capture, audio or not.
        kept = sf.build(previous=previous, audio_dir=audio, ffprobe=stub, **self._inputs())
        self.assertEqual([r["filename"] for r in kept["files"]], ["da.mp3", "dz.mp3"])
        pruned = sf.build(previous=previous, audio_dir=audio, ffprobe=stub, prune=True,
                          **self._inputs())
        self.assertEqual([r["filename"] for r in pruned["files"]], ["da.mp3"])
        with self.assertRaises(sf.BuildRefused):     # prune with no audio directory
            sf.build(previous=previous, prune=True, **self._inputs())

    def test_14nov_original_names(self):
        self.assertEqual(sf.original_filename("d201-221.14Nov10-b.mp3"), "d-14Nov10-b.au")
        self.assertEqual(sf.original_filename("dnb-14Nov02-a.mp3"), "dnb-14Nov02-a.au")
        self.assertEqual(sf.original_filename("d019-040.mp3"), "d019-040.wav")


class CommittedFileTests(unittest.TestCase):
    def test_rebuild_without_audio_reproduces_the_committed_file(self):
        # Also catches a label edit committed without `make stream-data`.
        path = os.path.join(REPO, "stream-files.json")
        with open(path, encoding="utf-8") as fh:
            committed = json.load(fh)
        rebuilt = sf.build(previous=sf.load_previous(path), audio_dir=None, ffprobe=None)
        self.assertEqual(rebuilt, committed)

    def test_committed_file_shape(self):
        with open(os.path.join(REPO, "stream-files.json"), encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["schema"], sf.SCHEMA)
        names = {r["filename"] for r in data["files"]}
        self.assertTrue(set(data["primary_files"]) <= names)
        for row in data["files"]:
            for key in ("master_start_seconds", "master_end_seconds", "duration_seconds",
                        "timing_range_start_seconds", "timing_range_end_seconds", "is_primary"):
                self.assertIn(key, row)


if __name__ == "__main__":
    unittest.main()
