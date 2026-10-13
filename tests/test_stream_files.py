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

    def test_recorded_duration_is_reused_not_remeasured(self):
        # Measured once and committed: a recorded duration wins over the audio, so ffprobe's
        # last-digit drift never reaches the file. The size still comes from the audio.
        data = sf.build(previous={"da.mp3": {"duration_seconds": 1.0, "size": 1}},
                        audio_dir=self._audio("da.mp3"), ffprobe=self._stub("echo 12.5"),
                        **self._inputs())
        row = data["files"][0]
        self.assertEqual((row["duration_seconds"], row["size"]), (1.0, 7))

    def test_capture_with_no_duration_is_measured(self):
        data = sf.build(previous={"da.mp3": {"size": 1}},
                        audio_dir=self._audio("da.mp3"), ffprobe=self._stub("echo 12.5"),
                        **self._inputs())
        row = data["files"][0]
        self.assertEqual((row["duration_seconds"], row["duration"], row["size"]), (12.5, 12.5, 7))

    def test_remeasure_measures_a_recorded_capture_again(self):
        data = sf.build(previous={"da.mp3": {"duration_seconds": 1.0}},
                        audio_dir=self._audio("da.mp3"), ffprobe=self._stub("echo 12.5"),
                        remeasure=True, **self._inputs())
        self.assertEqual(data["files"][0]["duration_seconds"], 12.5)

    def test_failing_probe_falls_back_to_the_recorded_duration(self):
        data = sf.build(previous={"da.mp3": {"duration": 1.0, "size": 1}},
                        audio_dir=self._audio("da.mp3"), ffprobe=self._stub("exit 1"),
                        remeasure=True, **self._inputs())
        row = data["files"][0]
        self.assertEqual((row["duration_seconds"], row["size"]), (1.0, 7))

    def test_no_duration_and_no_audio_writes_null_and_names_it_on_stderr(self):
        out = os.path.join(self.tmp, "stream-files.json")
        with open(out, "w", encoding="utf-8") as fh:
            json.dump({"files": [{"filename": "da.mp3", "size": 1}]}, fh)
        inputs = self._inputs()
        err = io.StringIO()
        with unittest.mock.patch.dict(os.environ, {"NETRADIO_MP3_DIR": os.path.join(self.tmp, "none")}), \
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
        with unittest.mock.patch.dict(os.environ, {"NETRADIO_MP3_DIR": os.path.join(self.tmp, "none")}), \
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
