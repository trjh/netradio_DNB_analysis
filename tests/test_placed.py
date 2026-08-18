"""placed: the hand-placed pair listing (AP-26 review-tool data source).

Hermetic: fabricated labels dirs only -- the op is pure label bookkeeping, so no
audio, no ffmpeg, no venv extras are involved anywhere in these tests. Covers the
seat reconstruction (orig_ts - start_t on the original's native clock), the
derivable flag + reasons, the rate convention (original seconds per stream second =
1 / the sheet speed), grade attachment from a saved sync-audit report, the point
cap, the CLI, and the inspect-worker op (which must never touch audio).
"""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))

from streamalign import __main__ as cli            # noqa: E402
from streamalign import inspect_worker as iw       # noqa: E402
from streamalign import placed                     # noqa: E402


def _write_labels(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for t, text in rows:
            f.write("%.6f\t%.6f\t%s\n" % (t, t, text))


class PlacedFixture(unittest.TestCase):
    """Three captures:
    dA-000 -- orig 042 fully bookkept (A verified + B), sheet rate 1.0;
    dB-001 -- orig 042 with NO start row (underivable) + orig 066 with a sync
              BEFORE its clip head (underivable, different reason);
    dC-002 -- orig 072 at sheet rate 0.9787 (the reciprocal-rate check).
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="placed-")
        cls.labels = os.path.join(cls.tmp, "labels")
        os.makedirs(cls.labels)
        _write_labels(os.path.join(cls.labels, "dA-000.labels.tsv"), [
            (20.0, "orig042 start: A"),
            (30.0, "orig042 sync: A verified confidence 5.9/10"),
            (30.0, "track sync: A verified confidence 5.9/10"),
            (20.03, "orig042 start: B"),
            (55.0, "orig042 sync: B"),
            (55.0, "track sync: B"),
        ])
        _write_labels(os.path.join(cls.labels, "dB-001.labels.tsv"), [
            (30.0, "orig042 sync: D"),
            (30.0, "track sync: D"),
            (50.0, "orig066 start: E"),
            (40.0, "orig066 sync: E"),
            (40.0, "track sync: E"),
        ])
        _write_labels(os.path.join(cls.labels, "dC-002.labels.tsv"), [
            (10.0, "orig072 start: A"),
            (20.0, "orig072 sync: A"),
            (20.0, "track sync: A"),
            (120.0, "orig072 sync: B"),
            (117.87, "track sync: B"),
        ])
        cls.audit = os.path.join(cls.tmp, "audit.json")
        with open(cls.audit, "w", encoding="utf-8") as f:
            json.dump({"points": [
                {"track": 42, "label": "A", "stem": "dA-000",
                 "verdict": "STRONG", "seat_conf": 0.83},
                {"track": 42, "label": "B", "stem": "dA-000",
                 "verdict": "NOT-FOUND", "seat_conf": 0.05},
            ], "background": []}, f)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _pairs(self, **kw):
        out = placed.list_placed(labels_dir=self.labels, **kw)
        self.assertNotIn("error", out)
        return {(p["stem"], p["orig"]): p for p in out["pairs"]}


class TestListing(PlacedFixture):

    def test_every_pair_listed_in_stem_orig_order(self):
        out = placed.list_placed(labels_dir=self.labels)
        keys = [(p["stem"], p["orig"]) for p in out["pairs"]]
        self.assertEqual(keys, [("dA-000", 42), ("dB-001", 42), ("dB-001", 66),
                                ("dC-002", 72)])

    def test_seat_is_orig_native_via_the_start_bookkeeping(self):
        pts = {p["k"]: p for p in self._pairs()[("dA-000", 42)]["points"]}
        # A: sync 30.0 - start 20.0 = native 10.0; B: 55.0 - 20.03 = 34.97
        self.assertAlmostEqual(pts["A"]["orig_s"], 10.0, places=6)
        self.assertAlmostEqual(pts["B"]["orig_s"], 34.97, places=6)
        for p in pts.values():
            self.assertTrue(p["derivable"])
            self.assertIsNone(p["why"])

    def test_points_ride_in_stream_order(self):
        pts = self._pairs()[("dA-000", 42)]["points"]
        self.assertEqual([p["stream_s"] for p in pts], sorted(p["stream_s"] for p in pts))

    def test_missing_start_row_is_flagged_not_dropped(self):
        pts = self._pairs()[("dB-001", 42)]["points"]
        self.assertEqual(len(pts), 1)
        self.assertFalse(pts[0]["derivable"])
        self.assertIsNone(pts[0]["orig_s"])
        self.assertEqual(pts[0]["why"], "no origNNN start: row")

    def test_sync_before_clip_head_is_flagged_with_its_own_reason(self):
        pts = self._pairs()[("dB-001", 66)]["points"]
        self.assertFalse(pts[0]["derivable"])
        self.assertEqual(pts[0]["why"], "sync before clip head")

    def test_rate_is_original_seconds_per_stream_second(self):
        # dA: sheet speed (55-30)/(55-30) = 1.0 -> rate 1.0; dC: sheet 0.9787 ->
        # the reciprocal (the match-hints / inspect-slice --rate convention)
        pairs = self._pairs()
        self.assertAlmostEqual(pairs[("dA-000", 42)]["rate"], 1.0, places=9)
        self.assertEqual(pairs[("dA-000", 42)]["rate_method"], "AB")
        sheet = (117.87 - 20.0) / (120.0 - 20.0)
        self.assertAlmostEqual(pairs[("dC-002", 72)]["rate"], 1.0 / sheet, places=9)

    def test_single_pair_track_has_no_rate(self):
        p = self._pairs()[("dB-001", 66)]
        self.assertIsNone(p["rate"])
        self.assertIsNone(p["rate_method"])

    def test_verified_token_carried_per_point(self):
        pts = {p["k"]: p for p in self._pairs()[("dA-000", 42)]["points"]}
        self.assertTrue(pts["A"]["verified"])
        self.assertFalse(pts["B"]["verified"])

    def test_stem_filter_limits_to_that_capture(self):
        out = placed.list_placed("dA-000", labels_dir=self.labels)
        self.assertEqual([p["stem"] for p in out["pairs"]], ["dA-000"])

    def test_unknown_stem_is_an_error(self):
        out = placed.list_placed("dZ-999", labels_dir=self.labels)
        self.assertIn("error", out)

    def test_path_shaped_stem_is_an_error_not_a_lookup(self):
        out = placed.list_placed("../labels/dA-000", labels_dir=self.labels)
        self.assertIn("error", out)


class TestGrades(PlacedFixture):

    def test_grades_attach_where_the_report_knows_the_point(self):
        pts = {p["k"]: p
               for p in self._pairs(audit_json=self.audit)[("dA-000", 42)]["points"]}
        self.assertEqual(pts["A"]["grade"], "STRONG")
        self.assertAlmostEqual(pts["A"]["seat_conf"], 0.83)
        self.assertEqual(pts["B"]["grade"], "NOT-FOUND")

    def test_ungraded_points_report_null_not_absent(self):
        pts = self._pairs(audit_json=self.audit)[("dB-001", 42)]["points"]
        self.assertIsNone(pts[0]["grade"])
        self.assertIsNone(pts[0]["seat_conf"])

    def test_no_report_means_all_grades_null(self):
        pts = {p["k"]: p for p in self._pairs()[("dA-000", 42)]["points"]}
        self.assertIsNone(pts["A"]["grade"])

    def test_broken_report_never_breaks_the_listing(self):
        broken = os.path.join(self.tmp, "broken.json")
        with open(broken, "w", encoding="utf-8") as f:
            f.write("{nope")
        pairs = self._pairs(audit_json=broken)
        self.assertIn(("dA-000", 42), pairs)
        self.assertEqual(placed.load_audit_grades(broken), {})
        self.assertEqual(placed.load_audit_grades(os.path.join(self.tmp, "absent")), {})


class TestPointCap(unittest.TestCase):

    def test_oversized_pairs_truncate_in_stream_order(self):
        tmp = tempfile.mkdtemp(prefix="placed-cap-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        rows = [(5.0, "orig042 start: p0")]
        for i in range(placed.MAX_POINTS_PER_PAIR + 6):
            t = 10.0 + i
            rows += [(t, "orig042 sync: p%d" % i), (t, "track sync: p%d" % i)]
        _write_labels(os.path.join(tmp, "dCAP-000.labels.tsv"), rows)
        out = placed.list_placed(labels_dir=tmp)
        pair = out["pairs"][0]
        self.assertEqual(len(pair["points"]), placed.MAX_POINTS_PER_PAIR)
        self.assertTrue(pair["truncated"])
        self.assertEqual(pair["points"][0]["k"], "p0")


class TestOwnerRateRules(unittest.TestCase):
    """Owner rules 2026-08-18: multi-capture A/B pairs use the FIRST rate with a
    note (never a silent median); duplicate designated letters in one capture
    are surfaced in the note."""

    def _tmp(self):
        tmp = tempfile.mkdtemp(prefix="placed-rules-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        return tmp

    def test_multi_capture_pairs_use_first_rate_with_note(self):
        tmp = self._tmp()
        # capture 1: A/B span 10 orig-s over 10 track-s -> sheet rate 1.0
        _write_labels(os.path.join(tmp, "dX-000.labels.tsv"), [
            (5.0, "orig050 start: A"),
            (10.0, "orig050 sync: A"), (10.0, "track sync: A"),
            (20.0, "orig050 sync: B"), (20.0, "track sync: B"),
        ])
        # capture 2: A/B span 10 orig-s over 20 track-s -> sheet rate 2.0
        _write_labels(os.path.join(tmp, "dY-001.labels.tsv"), [
            (5.0, "orig050 start: A"),
            (30.0, "orig050 sync: A"), (30.0, "track sync: A"),
            (40.0, "orig050 sync: B"), (60.0, "track sync: B"),
        ])
        out = placed.list_placed(labels_dir=tmp)
        by = {(p["stem"], p["orig"]): p for p in out["pairs"]}
        for key, pair in by.items():
            # FIRST pair in original-time order is dX's (orig 10-20 before 30-40):
            # sheet rate 1.0 -> emitted rate 1/1.0; NEVER the median of 1.0 and 2.0
            self.assertEqual(pair["rate_method"], "AB-first", key)
            self.assertAlmostEqual(pair["rate"], 1.0, places=9)
            self.assertIn("A/B pairs in 2 captures", pair["rate_note"])
            self.assertIn("using the first (dX-000)", pair["rate_note"])

    def test_duplicate_designated_letter_is_surfaced(self):
        tmp = self._tmp()
        _write_labels(os.path.join(tmp, "dZ-000.labels.tsv"), [
            (5.0, "orig051 start: A"),
            (10.0, "orig051 sync: A"), (10.0, "track sync: A"),
            (12.0, "orig051 sync: A"), (12.0, "track sync: A"),   # duplicate A
            (20.0, "orig051 sync: B"), (20.0, "track sync: B"),
        ])
        out = placed.list_placed(labels_dir=tmp)
        pair = out["pairs"][0]
        self.assertIn("2x A rows in dZ-000", pair["rate_note"])
        self.assertIn("last by original time wins", pair["rate_note"])

    def test_single_pair_has_no_note(self):
        tmp = self._tmp()
        _write_labels(os.path.join(tmp, "dW-000.labels.tsv"), [
            (5.0, "orig052 start: A"),
            (10.0, "orig052 sync: A"), (10.0, "track sync: A"),
            (20.0, "orig052 sync: B"), (20.0, "track sync: B"),
        ])
        out = placed.list_placed(labels_dir=tmp)
        pair = out["pairs"][0]
        self.assertEqual(pair["rate_method"], "AB")
        self.assertNotIn("rate_note", pair)


class TestCLI(PlacedFixture):

    def _run(self, argv):
        buf = io.StringIO()
        code = 0
        try:
            with redirect_stdout(buf):
                cli.main(argv)
        except SystemExit as exc:
            code = exc.code or 0
        return code, buf.getvalue()

    def test_placed_prints_json_for_one_stem(self):
        code, out = self._run(["--labels", self.labels, "placed", "dA-000",
                               "--audit-json", self.audit])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual([p["stem"] for p in payload["pairs"]], ["dA-000"])
        pts = {p["k"]: p for p in payload["pairs"][0]["points"]}
        self.assertEqual(pts["A"]["grade"], "STRONG")

    def test_placed_lists_everything_without_a_stem(self):
        code, out = self._run(["--labels", self.labels, "placed"])
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(out)["pairs"]), 4)

    def test_unknown_stem_exits_nonzero_with_json_error(self):
        code, out = self._run(["--labels", self.labels, "placed", "dZ-999"])
        self.assertEqual(code, 1)
        self.assertIn("error", json.loads(out))


class TestWorkerOp(PlacedFixture):

    def _handle(self, req):
        from streamalign import groundtruth as gt
        old = gt.LABELS_DIR
        gt.LABELS_DIR = self.labels
        try:
            class _NoAudio:                     # the op must never load a pair
                def get(self, *a, **k):
                    raise AssertionError("placed must not touch audio")
            return iw.handle(req, _NoAudio(), sources_dir=self.tmp)
        finally:
            gt.LABELS_DIR = old

    def test_placed_op_answers_without_touching_audio(self):
        out = self._handle({"op": "placed", "stem": "dA-000"})
        self.assertNotIn("error", out)
        self.assertEqual([p["stem"] for p in out["pairs"]], ["dA-000"])

    def test_placed_op_lists_everything_when_stem_omitted(self):
        out = self._handle({"op": "placed"})
        self.assertEqual(len(out["pairs"]), 4)

    def test_placed_op_echoes_the_request_id_through_serve(self):
        from streamalign import groundtruth as gt
        old = gt.LABELS_DIR
        gt.LABELS_DIR = self.labels
        try:
            stdin = io.StringIO(json.dumps({"op": "placed", "stem": "dA-000",
                                            "id": 7}) + "\n")
            stdout = io.StringIO()
            iw.serve(self.tmp, stdin=stdin, stdout=stdout)
        finally:
            gt.LABELS_DIR = old
        lines = [json.loads(x) for x in stdout.getvalue().splitlines()]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["id"], 7)
        self.assertIn("pairs", lines[0])

    def test_unknown_stem_is_a_request_error_never_a_dead_worker(self):
        out = self._handle({"op": "placed", "stem": "dZ-999"})
        self.assertIn("error", out)


if __name__ == "__main__":
    unittest.main()
