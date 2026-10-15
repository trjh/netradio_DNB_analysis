"""The /align inspector's server side: the security contract, the seam, the hints read.

All offline. The load-bearing tests are the §3.3/Phase-2 security-contract ones:
identifier-based requests (no client path ever reaches the filesystem), hard caps on
the numeric knobs, the fixed-argv subprocess shape, the one-at-a-time lock, and the
loopback-only-without-auth route gate.
"""

import json
import os
import re
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import align_inspect
import align_server
import cache_budget


def _fake_checkout(tmp):
    """A directory shaped like this checkout, enough for enabled() to say yes."""
    os.makedirs(os.path.join(tmp, "checkout", ".venv", "bin"))
    open(os.path.join(tmp, "checkout", ".venv", "bin", "python"), "w").close()
    os.makedirs(os.path.join(tmp, "checkout", "scripts", "streamalign"))
    os.makedirs(os.path.join(tmp, "checkout", "sources_local"))
    open(os.path.join(tmp, "checkout", "sources_local",
                      "072-J Majik - Your Sound.wv"), "w").close()
    return os.path.join(tmp, "checkout")


def _root(path):
    """Point the align modules at `path` as their checkout ("" = none: everything dark)."""
    return mock.patch.object(align_inspect, "ROOT_DIR", path)


def _fake_mp3_dir(tmp):
    d = os.path.join(tmp, "mp3")
    os.makedirs(d)
    open(os.path.join(d, "d376-395.mp3"), "w").close()
    return d


class _Env(unittest.TestCase):
    """Base: a configured-and-enabled align environment in a tempdir."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="align-test-")
        self.addCleanup(self.tmp.cleanup)
        self.repo = _fake_checkout(self.tmp.name)
        self.mp3 = _fake_mp3_dir(self.tmp.name)
        root = _root(self.repo)
        root.start()
        self.addCleanup(root.stop)
        patcher = mock.patch.dict(os.environ, {
            "NETRADIO_SOURCES_DIR": "",
            # the one-shot path is the contract under test here; the keep-warm
            # worker has its own test classes below
            "NETRADIO_ALIGN_WORKER": "0",
        })
        patcher.start()
        self.addCleanup(patcher.stop)

    def _params(self, **kw):
        p = dict(stem="d376-395", orig=72, stream_t=194.315, orig_t=226.699,
                 rate=1.0218, invert=True, win=6.0)
        p.update(kw)
        return p


class TestEnabledGate(_Env):

    def test_enabled_with_full_fixture(self):
        self.assertTrue(align_inspect.enabled())

    def test_dark_without_a_checkout(self):
        with _root(""):
            self.assertFalse(align_inspect.enabled())
            payload, code = align_inspect.run_request(self._params(), self.mp3)
            self.assertEqual(code, 404)
            self.assertIn("not configured", payload["error"])

    def test_dark_without_venv(self):
        os.remove(os.path.join(self.repo, ".venv", "bin", "python"))
        self.assertFalse(align_inspect.enabled())


class TestSecurityContract(_Env):
    """Identifier-based, capped, contained: the §3.3 contract, line by line."""

    def _expect_400(self, msg_fragment, **kw):
        payload, code = align_inspect.run_request(self._params(**kw), self.mp3)
        self.assertEqual(code, 400, payload)
        self.assertIn(msg_fragment, payload["error"])

    def test_non_object_json_bodies_get_400_not_a_crash(self):
        # the route hands whatever JSON decoded to run_request; a list/string/number/
        # null body must come back as the documented 400, never an AttributeError
        for body in ([], "x", 1, None, [{"stem": "d376-395"}]):
            payload, code = align_inspect.run_request(body, self.mp3)
            self.assertEqual(code, 400, repr(body))
            self.assertIn("JSON object", payload["error"])

    def test_traversal_stem_refused(self):
        self._expect_400("bad stem", stem="../../etc/passwd")

    def test_absolute_path_stem_refused(self):
        self._expect_400("bad stem", stem="/etc/passwd")

    def test_unknown_stem_refused(self):
        self._expect_400("unknown stem", stem="d000-000")

    def test_unknown_orig_refused(self):
        self._expect_400("unknown orig", orig=999)

    def test_non_numeric_orig_refused(self):
        self._expect_400("bad orig", orig="; rm -rf /")

    def test_window_cap(self):
        self._expect_400("win out of range", win=31.0)

    def test_radius_cap(self):
        payload, code = align_inspect.run_request(
            self._params(radius=5.0), self.mp3, op="refine")
        self.assertEqual(code, 400)
        self.assertIn("radius out of range", payload["error"])

    def test_context_cap(self):
        payload, code = align_inspect.run_request(
            self._params(context=61.0), self.mp3, op="context")
        self.assertEqual(code, 400)
        self.assertIn("context out of range", payload["error"])

    def test_engine_validated(self):
        payload, code = align_inspect.run_request(
            self._params(engine="dtw"), self.mp3, op="refine")
        self.assertEqual(code, 400)
        self.assertIn("bad engine", payload["error"])

    def test_unknown_op_refused(self):
        payload, code = align_inspect.run_request(self._params(), self.mp3, op="warp")
        self.assertEqual(code, 400)
        self.assertIn("unknown op", payload["error"])

    def test_overview_points_required_and_shaped(self):
        # DP-10: the overview op demands a bounded list of [a, b] number pairs
        for bad in (None, [], {}, "x", [[1.0]], [[1.0, 2.0, 3.0]], ["ab"],
                    [[1.0, "; rm -rf /"]],
                    [[10 ** 400, 2.0]],     # valid JSON, but OverflowErrors float()
                    [[1.0, 2.0]] * (align_inspect.MAX_OVERVIEW_POINTS + 1)):
            payload, code = align_inspect.run_request(
                self._params(points=bad), self.mp3, op="overview")
            self.assertEqual(code, 400, repr(bad))
            self.assertIn("bad points", payload["error"])

    def test_overview_points_range(self):
        for bad in ([[-1.0, 2.0]], [[1.0, 1e9]], [[float("nan"), 2.0]]):
            payload, code = align_inspect.run_request(
                self._params(points=bad), self.mp3, op="overview")
            self.assertEqual(code, 400, repr(bad))
            self.assertIn("points out of range", payload["error"])

    def test_overview_points_ignored_on_other_ops(self):
        # a stale points field on a slice request is not an error (per-op fields)
        clean, err = align_inspect._validated(
            self._params(points="garbage"), self.mp3, op="slice")
        self.assertIsNone(err)
        self.assertNotIn("points", clean)

    def test_rate_range(self):
        self._expect_400("rate out of range", rate=10.0)

    def test_time_caps(self):
        self._expect_400("stream_t out of range", stream_t=1e9)
        self._expect_400("orig_t out of range", orig_t=-5)

    def test_stream_listing_excludes_symlink_escapes(self):
        outside = os.path.join(self.tmp.name, "outside.mp3")
        open(outside, "w").close()
        os.symlink(outside, os.path.join(self.mp3, "escape.mp3"))
        self.assertNotIn("escape", align_inspect.list_streams(self.mp3))
        self.assertIn("d376-395", align_inspect.list_streams(self.mp3))


class TestSubprocessSeam(_Env):

    def test_argv_is_fixed_shape_with_discrete_values(self):
        clean, err = align_inspect._validated(self._params(), self.mp3)
        self.assertIsNone(err)
        argv = align_inspect.build_argv(clean)
        self.assertEqual(argv[0], os.path.join(self.repo, ".venv", "bin", "python"))
        self.assertEqual(argv[1:4], ["-m", "streamalign", "inspect-slice"])
        self.assertIn("d376-395", argv)
        self.assertIn("--invert", argv)
        self.assertNotIn("--refine", argv)
        self.assertNotIn("--context", argv)
        # every element is a single argv entry; nothing shell-quoted or joined
        self.assertTrue(all(isinstance(a, str) and "\n" not in a for a in argv))

    def test_refine_argv_adds_flag_radius_and_engine(self):
        clean, _ = align_inspect._validated(self._params(radius=0.1, engine="match"),
                                            self.mp3)
        argv = align_inspect.build_argv(clean, op="refine")
        self.assertIn("--refine", argv)
        self.assertIn("0.100", argv[argv.index("--radius") + 1])
        self.assertEqual("match", argv[argv.index("--engine") + 1])

    def test_context_argv_adds_the_context_flag(self):
        clean, _ = align_inspect._validated(self._params(context=45.0), self.mp3)
        argv = align_inspect.build_argv(clean, op="context")
        self.assertIn("--context", argv)
        self.assertIn("45.000", argv[argv.index("--context") + 1])
        self.assertNotIn("--refine", argv)

    def test_overview_argv_adds_flag_and_discrete_points(self):
        clean, err = align_inspect._validated(
            self._params(points=[[196.0, 228.425], [226.0, 259.05]]),
            self.mp3, op="overview")
        self.assertIsNone(err)
        argv = align_inspect.build_argv(clean, op="overview")
        self.assertIn("--overview", argv)
        self.assertEqual([argv[i + 1] for i, a in enumerate(argv) if a == "--point"],
                         ["196.000000:228.425000", "226.000000:259.050000"])
        self.assertNotIn("--refine", argv)
        self.assertNotIn("--context", argv)
        self.assertTrue(all(isinstance(a, str) and "\n" not in a for a in argv))

    def test_runner_seam_relays_json(self):
        def fake_run(argv, **kw):
            self.assertFalse(kw.get("shell", False))
            return mock.Mock(stdout=json.dumps({"sr": 16000}).encode(), stderr=b"")
        payload, code = align_inspect.run_request(self._params(), self.mp3,
                                                  runner=fake_run)
        self.assertEqual((payload, code), ({"sr": 16000}, 200))

    def test_garbage_stdout_is_a_502_not_a_crash(self):
        fake_run = lambda argv, **kw: mock.Mock(stdout=b"Traceback ...", stderr=b"boom")
        payload, code = align_inspect.run_request(self._params(), self.mp3,
                                                  runner=fake_run)
        self.assertEqual(code, 502)
        self.assertIn("no JSON", payload["error"])

    def test_non_object_stdout_json_is_also_a_502(self):
        # valid JSON that isn't an object (a schema mismatch) must take the same
        # controlled 502 path, not crash on payload.get
        for out in (b"[]", b'"ok"', b"3", b"null"):
            fake_run = lambda argv, **kw: mock.Mock(stdout=out, stderr=b"tail")
            payload, code = align_inspect.run_request(self._params(), self.mp3,
                                                      runner=fake_run)
            self.assertEqual(code, 502, out)
            self.assertIn("no JSON object", payload["error"])

    def test_launch_failure_is_a_502_not_a_crash(self):
        # a configured-but-unexecutable venv python (or vanished cwd) must come back
        # as the controlled 502, never propagate out of run_request
        def fake_run(argv, **kw):
            raise PermissionError("not executable")
        payload, code = align_inspect.run_request(self._params(), self.mp3,
                                                  runner=fake_run)
        self.assertEqual(code, 502)
        self.assertIn("could not be launched", payload["error"])

    def test_concurrent_requests_get_busy(self):
        acquired = align_inspect._run_lock.acquire(blocking=False)
        self.assertTrue(acquired)
        try:
            payload, code = align_inspect.run_request(self._params(), self.mp3)
            self.assertEqual(code, 429)
        finally:
            align_inspect._run_lock.release()


class TestHintsParsing(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="align-hints-")
        self.addCleanup(self.tmp.cleanup)
        self.labels = self.tmp.name

    def _write(self, name, rows):
        with open(os.path.join(self.labels, name), "w") as f:
            for a, b, t in rows:
                f.write("%.6f\t%.6f\t%s\n" % (a, b, t))

    def test_automated_subdir_is_scanned_and_wins(self):
        # DP-24: the converter emits into labels/automated/; a pre-migration
        # checkout still has loose root pairs. automated/ wins per (stem, orig).
        os.makedirs(os.path.join(self.labels, "automated"))
        def _write_in(sub, name, rows):
            with open(os.path.join(self.labels, sub, name), "w") as f:
                for a, b, t in rows:
                    f.write("%.6f\t%.6f\t%s\n" % (a, b, t))
        summary = ("note HINT: orig072 via MATCH+PHAT: rate 1.02046 (original runs "
                   "+2.05% vs stream), polarity not inverted, 1 anchors HINT")
        _write_in("automated", "d376-395.orig072.match.hints.tsv",
                  [(0.0, 0.0, summary),
                   (76.0, 76.0, "track sync: 1 confidence 5.9/10 HINT")])
        _write_in("automated", "orig072.d376-395.match.hints.tsv",
                  [(105.992, 105.992, "orig072 sync: 1 confidence 5.9/10 HINT")])
        # a STALE root copy of the same pair, different instant — must lose
        self._write("d376-395.orig072.match.hints.tsv",
                    [(0.0, 0.0, summary),
                     (99.0, 99.0, "track sync: 1 confidence 1.0/10 HINT")])
        self._write("orig072.d376-395.match.hints.tsv",
                    [(1.0, 1.0, "orig072 sync: 1 confidence 1.0/10 HINT")])
        sets = align_inspect.parse_match_hints(self.labels)
        self.assertEqual(len(sets), 1)
        self.assertAlmostEqual(sets[0]["points"][0]["stream_s"], 76.0)

    def test_dirs_union_and_empty_automated_pair_is_claimed(self):
        # a pair present only at the root still serves (pre-migration files),
        # ALONGSIDE a different pair in automated/; and an automated pair that
        # parses to zero points CLAIMS its identity — a stale root copy must
        # not shadow fresh-but-empty output
        os.makedirs(os.path.join(self.labels, "automated"))
        def _write_in(sub, name, rows):
            with open(os.path.join(self.labels, sub, name), "w") as f:
                for a, b, t in rows:
                    f.write("%.6f\t%.6f\t%s\n" % (a, b, t))
        summary = ("note HINT: orig073 via MATCH+PHAT: rate 1.0 (original runs "
                   "+0.00% vs stream), polarity not inverted, 0 anchors HINT")
        # automated: orig073 pair with NO sync rows (zero points)
        _write_in("automated", "d376-395.orig073.match.hints.tsv", [(0.0, 0.0, summary)])
        _write_in("automated", "orig073.d376-395.match.hints.tsv", [(0.0, 0.0, summary)])
        # root: a stale orig073 pair WITH points (must be shadowed) and a
        # root-only orig074 pair (must serve)
        self._write("d376-395.orig073.match.hints.tsv",
                    [(50.0, 50.0, "track sync: 1 confidence 2.0/10 HINT")])
        self._write("orig073.d376-395.match.hints.tsv",
                    [(9.0, 9.0, "orig073 sync: 1 confidence 2.0/10 HINT")])
        self._write("d376-395.orig074.match.hints.tsv",
                    [(60.0, 60.0, "track sync: 1 confidence 3.0/10 HINT")])
        self._write("orig074.d376-395.match.hints.tsv",
                    [(8.0, 8.0, "orig074 sync: 1 confidence 3.0/10 HINT")])
        sets = align_inspect.parse_match_hints(self.labels)
        self.assertEqual([(x["orig"], x["points"][0]["stream_s"]) for x in sets],
                         [(74, 60.0)])

    def test_pairs_and_summary_parse(self):
        self._write("d376-395.orig072.match.hints.tsv", [
            (0.0, 0.0, "note HINT: orig072 via MATCH+PHAT: rate 1.02046 (original runs "
                       "+2.05% vs stream), polarity INVERTED (flip the original before a "
                       "null test), 8 anchors HINT"),
            (76.0, 76.0, "track sync: 1 confidence 5.9/10 HINT"),
            (106.0, 106.0, "track sync: 2 confidence 6.8/10 HINT"),
        ])
        self._write("orig072.d376-395.match.hints.tsv", [
            (105.992, 105.992, "orig072 sync: 1 confidence 5.9/10 HINT"),
            (136.601, 136.601, "orig072 sync: 2 confidence 6.8/10 HINT"),
        ])
        sets = align_inspect.parse_match_hints(self.labels)
        self.assertEqual(len(sets), 1)
        h = sets[0]
        self.assertEqual((h["stem"], h["orig"]), ("d376-395", 72))
        self.assertAlmostEqual(h["rate"], 1.02046)
        self.assertTrue(h["inverted"])
        self.assertEqual(len(h["points"]), 2)
        self.assertEqual(h["points"][0]["k"], "1")
        self.assertAlmostEqual(h["points"][0]["stream_s"], 76.0)
        self.assertAlmostEqual(h["points"][0]["orig_s"], 105.992)
        self.assertAlmostEqual(h["points"][1]["confidence"], 6.8)

    def test_verified_rows_parse_too(self):
        # Pass-1 (matchconv DP-01) emits `track sync: 1 verified confidence ...`
        # (with per-anchor MATCH deltas after the confidence), and this page's own
        # DP-16 exports carry the same token -- a parser without it silently drops
        # every such point, which is exactly what happened before this test
        self._write("d376-395.orig072.match.hints.tsv", [
            (0.0, 0.0, "note HINT: orig072 via MATCH+PHAT: rate 1.02046 (original runs "
                       "+2.05% vs stream), polarity INVERTED (flip the original before a "
                       "null test), 8 anchors HINT"),
            (76.0, 76.0, "track sync: 1 verified confidence 5.9/10 MATCH +0.003s HINT"),
        ])
        self._write("orig072.d376-395.match.hints.tsv", [
            (105.992, 105.992,
             "orig072 sync: 1 verified confidence 5.9/10 MATCH +0.003s HINT"),
        ])
        sets = align_inspect.parse_match_hints(self.labels)
        self.assertEqual(len(sets), 1)
        self.assertEqual(len(sets[0]["points"]), 1)
        p = sets[0]["points"][0]
        self.assertEqual(p["k"], "1")
        self.assertAlmostEqual(p["orig_s"], 105.992)
        self.assertAlmostEqual(p["confidence"], 5.9)

    def test_anchor_rows_without_trailing_hint_parse_identically(self):
        # DP-26: the converter dropped the trailing ` HINT` token from emitted
        # ANCHOR rows (sync/start/end); `note HINT:` rows keep theirs. The parser
        # accepts BOTH forms, so files written before and after the change both read.
        self._write("d376-395.orig072.match.hints.tsv", [
            (0.0, 0.0, "note HINT: orig072 via MATCH+PHAT: rate 1.02046 (original "
                       "runs +2.05% vs stream), polarity INVERTED (flip the original "
                       "before a null test), 8 anchors HINT"),
            (76.0, 76.0, "track sync: 1 confidence 5.9/10"),
            (106.0, 106.0, "track sync: 2 verified confidence 6.8/10 MATCH +0.003s"),
        ])
        self._write("orig072.d376-395.match.hints.tsv", [
            (105.992, 105.992, "orig072 sync: 1 confidence 5.9/10"),
            (136.601, 136.601,
             "orig072 sync: 2 verified confidence 6.8/10 MATCH +0.003s"),
        ])
        sets = align_inspect.parse_match_hints(self.labels)
        self.assertEqual(len(sets), 1)
        h = sets[0]
        self.assertAlmostEqual(h["rate"], 1.02046)
        self.assertTrue(h["inverted"])
        self.assertEqual([p["k"] for p in h["points"]], ["1", "2"])
        self.assertAlmostEqual(h["points"][0]["orig_s"], 105.992)
        self.assertAlmostEqual(h["points"][1]["confidence"], 6.8)

    def test_mixed_hint_and_bare_anchor_rows_parse_together(self):
        # transition state: a file touched mid-migration can carry both forms
        self._write("d376-395.orig072.match.hints.tsv", [
            (0.0, 0.0, "note HINT: orig072 via MATCH+PHAT: rate 1.02046 (original "
                       "runs +2.05% vs stream), polarity not inverted, 2 anchors HINT"),
            (76.0, 76.0, "track sync: 1 confidence 5.9/10 HINT"),
            (106.0, 106.0, "track sync: 2 confidence 6.8/10"),
        ])
        self._write("orig072.d376-395.match.hints.tsv", [
            (105.992, 105.992, "orig072 sync: 1 confidence 5.9/10"),
            (136.601, 136.601, "orig072 sync: 2 confidence 6.8/10 HINT"),
        ])
        sets = align_inspect.parse_match_hints(self.labels)
        self.assertEqual(len(sets), 1)
        self.assertEqual([p["k"] for p in sets[0]["points"]], ["1", "2"])

    def test_match_deltas_ride_on_the_points(self):
        # DP-33: the per-anchor `MATCH %+.3fs` token (matchconv DP-03 --
        # MATCH-implied minus PHAT anchor position, original-native seconds) is
        # CAPTURED as match_delta_s, not just tolerated; rows without it (older
        # files, this page's own exports) read None. The page rebuilds the MATCH
        # path for its jitter window from exactly these numbers.
        self._write("d376-395.orig072.match.hints.tsv", [
            (76.0, 76.0, "track sync: 1 verified confidence 5.9/10 MATCH -35.372s HINT"),
            (106.0, 106.0, "track sync: 2 verified confidence 6.8/10 MATCH +0.042s"),
            (116.0, 116.0, "track sync: 3 confidence 6.6/10"),
        ])
        self._write("orig072.d376-395.match.hints.tsv", [
            (105.992, 105.992,
             "orig072 sync: 1 verified confidence 5.9/10 MATCH -35.372s HINT"),
            (136.601, 136.601,
             "orig072 sync: 2 verified confidence 6.8/10 MATCH +0.042s"),
            (146.803, 146.803, "orig072 sync: 3 confidence 6.6/10"),
        ])
        sets = align_inspect.parse_match_hints(self.labels)
        self.assertEqual(len(sets), 1)
        pts = sets[0]["points"]
        self.assertEqual([p["k"] for p in pts], ["1", "2", "3"])
        self.assertAlmostEqual(pts[0]["match_delta_s"], -35.372)
        self.assertAlmostEqual(pts[1]["match_delta_s"], 0.042)
        self.assertIsNone(pts[2]["match_delta_s"])
        # the delta never leaks into the confidence
        self.assertAlmostEqual(pts[0]["confidence"], 5.9)

    def test_stream_side_without_orig_side_is_skipped(self):
        self._write("d376-395.orig072.match.hints.tsv",
                    [(76.0, 76.0, "track sync: 1 confidence 5.9/10 HINT")])
        self.assertEqual(align_inspect.parse_match_hints(self.labels), [])

    def test_missing_dir_is_empty_not_fatal(self):
        self.assertEqual(align_inspect.parse_match_hints("/nonexistent"), [])
        self.assertEqual(align_inspect.parse_match_hints(None), [])


# NOTE: there is no per-route loopback gate: the server is its own process
# (align_server.py), and exposure is controlled by its loopback bind plus whatever
# proxy is deliberately put in front of it. Basic Auth, when configured, gates every
# route (tests/test_align_server.py).


class TestContextStripNudge(unittest.TestCase):
    """Review iter 1 P2: the strip's ORIGINAL band must follow the hand nudge.

    The strip is drawn client-side, so the guard is the repo's frontend idiom
    (cf. test_frontend_active_track): (a) string guards that align.html draws the
    original band through the shift term and redraws the strip on nudge; (b) a
    Python mirror of the column-lookup math proving a positive shift moves the
    original's content RIGHT (later), the same direction the inspector's
    visible() moves it.
    """

    HTML = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "public", "align.html")

    def setUp(self):
        with open(self.HTML, "r", encoding="utf-8") as handle:
            self.src = handle.read()

    def test_orig_band_is_drawn_through_the_shift_term(self):
        # the orig band's pixel->column lookup must be offset by the nudge
        self.assertIn('const dx = k === 1 ? ((shift / SR) / (2 * ctx.context_s)) * cw : 0;',
                      self.src)
        self.assertIn("Math.floor((x - dx) * n / cw)", self.src)

    def test_strip_redraws_on_nudge(self):
        # drawAll (the nudge/drag redraw path) must redraw the strip too -- the
        # DP-16 session-commit lines (plus the DP-29/DP-30 title/persistence
        # refreshes) may sit between the readout and the redraw
        self.assertRegex(self.src,
                         r'\("shiftTxt"\)\.textContent[\s\S]{0,800}?drawCtx\(\);')

    def test_click_play_and_strip_share_the_nudged_seat(self):
        # click playback already tracks baseOrigT - (shift/SR)*rate; keep it that way
        self.assertIn("const seatOrigT = cur.baseOrigT - (shift / SR) * rate;", self.src)

    def test_mirror_positive_shift_moves_orig_content_later_both_views(self):
        # strip mirror: content column c is drawn at pixels x where
        # floor((x - dx) * n / cw) == c, i.e. around x = c*cw/n + dx
        def strip_x_of_col(c, shift, sr=16000, context_s=45.0, n=4500, cw=900):
            dx = (shift / sr) / (2 * context_s) * cw
            return c * cw / n + dx
        # inspector mirror: displayed index of sample s is s - marginN + shift
        def insp_i_of_sample(s, shift, margin_n=8000):
            return s - margin_n + shift
        for shift in (160, 8000):
            self.assertGreater(strip_x_of_col(1000, shift), strip_x_of_col(1000, 0))
            self.assertGreater(insp_i_of_sample(20000, shift), insp_i_of_sample(20000, 0))
        self.assertEqual(strip_x_of_col(1000, 0), 1000 * 900 / 4500)


def _export_texts(stem, orig, points, rate, inverted):
    """A Python MIRROR of align.html's buildExport() (DP-16) -- keep in lockstep.

    `points` are the already-filtered included points, each
    {k, stream_s, orig_s (the session seat), confidence}. Returns
    (stream_name, stream_text, orig_name, orig_text).
    """
    tag = "orig%03d" % int(orig)
    pol = ("INVERTED (flip the original before a null test)" if inverted
           else "not inverted")
    pct = (rate - 1.0) * 100.0
    summary = ("%s via /align inspector: rate %.5f (original runs %s%.2f%% vs stream), "
               "polarity %s, %d anchors"
               % (tag, rate, "+" if pct >= 0 else "", pct, pol, len(points)))
    srows = [(p["stream_s"], "track sync: %s verified confidence %.1f/10"
              % (p["k"], p["confidence"])) for p in points]
    orows = [(p["orig_s"], "%s sync: %s verified confidence %.1f/10"
              % (tag, p["k"], p["confidence"])) for p in points]
    srows.append((0.0, "note HINT: " + summary + " HINT"))
    orows.append((0.0, "note HINT: " + summary
                  + "; times are original-local seconds HINT"))

    def text(rows):
        return "".join("%.6f\t%.6f\t%s\n" % (t, t, txt)
                       for t, txt in sorted(rows, key=lambda r: r[0]))

    return ("%s.%s.match.hints.tsv" % (stem, tag), text(srows),
            "%s.%s.match.hints.tsv" % (tag, stem), text(orows))


class TestExportLoop(unittest.TestCase):
    """DP-16: the inspector -> labels export loop.

    The export is built CLIENT-side (a browser download; no server route may ever
    write labels), so the guards follow the repo's frontend idiom: (a) a Python
    mirror of the row/naming format proving the exported pair ROUND-TRIPS through
    parse_match_hints with the adjusted seats and the ` verified` token intact;
    (b) string guards over align.html pinning the load-bearing pieces.
    """

    HTML = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "public", "align.html")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="align-export-")
        self.addCleanup(self.tmp.cleanup)
        with open(self.HTML, "r", encoding="utf-8") as handle:
            self.src = handle.read()

    def test_export_round_trips_through_the_hints_parser(self):
        # letters as point markers, an adjusted seat, a slowed rate (negative %)
        points = [{"k": "A", "stream_s": 196.0, "orig_s": 228.425125, "confidence": 5.9},
                  {"k": "B", "stream_s": 226.0, "orig_s": 259.05, "confidence": 6.8}]
        sname, stext, oname, otext = _export_texts(
            "d376-395", 72, points, rate=0.97954, inverted=True)
        self.assertEqual(sname, "d376-395.orig072.match.hints.tsv")
        self.assertEqual(oname, "orig072.d376-395.match.hints.tsv")
        for name, text in ((sname, stext), (oname, otext)):
            with open(os.path.join(self.tmp.name, name), "w") as f:
                f.write(text)
        sets = align_inspect.parse_match_hints(self.tmp.name)
        self.assertEqual(len(sets), 1)
        h = sets[0]
        self.assertEqual((h["stem"], h["orig"]), ("d376-395", 72))
        self.assertAlmostEqual(h["rate"], 0.97954)
        self.assertTrue(h["inverted"])
        self.assertEqual([p["k"] for p in h["points"]], ["A", "B"])
        # the ADJUSTED seat survives at 6-decimal precision
        self.assertAlmostEqual(h["points"][0]["orig_s"], 228.425125, places=6)
        self.assertAlmostEqual(h["points"][1]["confidence"], 6.8)

    def test_anchor_rows_bare_note_rows_hint_marked_and_6dp(self):
        # DP-26's export half: anchor rows are BARE (` verified` carries the
        # provenance), only the summary `note HINT:` rows keep the trailing suffix
        points = [{"k": "1", "stream_s": 10.0, "orig_s": 20.123456789,
                   "confidence": 9.9}]
        _, stext, _, otext = _export_texts("d1-2", 3, points, 1.0, False)
        for text in (stext, otext):
            for line in text.strip().split("\n"):
                a, b, body = line.split("\t")
                if body.startswith("note HINT:"):
                    self.assertTrue(body.endswith(" HINT"), line)
                else:
                    self.assertFalse(body.endswith(" HINT"), line)
                    self.assertIn(" verified confidence ", body)
                self.assertRegex(a, r"^\d+\.\d{6}$")
                self.assertEqual(a, b)
        self.assertIn("20.123457\t", otext)   # rounded at 6 dp, not truncated

    def test_js_mirror_templates_match(self):
        # the JS template strings the Python mirror above mirrors
        for needle in (
                "verified confidence ${p.confidence.toFixed(1)}/10",
                "${r[0].toFixed(6)}\\t${r[0].toFixed(6)}\\t${r[1]}\\n",
                "note HINT: ${summary} HINT",
                "via /align inspector: rate ${rate.toFixed(5)}",
                "; times are original-local seconds",
                ".match.hints.tsv`",
        ):
            self.assertIn(needle, self.src)

    def test_export_is_a_browser_download_never_a_server_write(self):
        # the DP-16 block must build a Blob download and never call the network
        block = self.src.split("DP-16: the inspector -> labels export loop", 1)[1]
        block = block.split("// ---------- derived signals", 1)[0]
        self.assertIn("URL.createObjectURL", block)
        self.assertIn("new Blob(", block)
        self.assertIn("a.download = name", block)
        self.assertNotIn("fetch(", block)
        # and no export route exists server-side
        import align_server
        self.assertNotIn("/api/align/export", align_server.AlignHandler.POST_OPS)

    def test_pending_nudge_folds_into_the_seat_before_slice_reload(self):
        # review iteration 1 P2: window/invert/rate changes reload through
        # fetchSlice, which resets shift -- the pending hand nudge must be folded
        # into baseOrigT AND the session seat BEFORE that reset, or the reload
        # lands at the pre-nudge base and drawAll then overwrites the session
        # seat with it, silently losing the operator's adjustment from the export
        fold = self.src.index(
            "if (shift) { cur.baseOrigT = seatNow(); "
            "stateFor(cur).seats[cur.point.k] = cur.baseOrigT; }")
        reset = self.src.index("slice = null;\n  shift = 0;")
        self.assertLess(fold, reset)
        # mirror of the fold math: the folded base IS the displayed seat
        sr, rate, base, shift = 16000, 1.02046, 228.425, 160
        seat = base - (shift / sr) * rate
        self.assertAlmostEqual(seat, base - 0.01 * rate, places=9)
        # and the slice request body uses the (now folded) base as orig_t
        self.assertIn("stream_t: cur.point.stream_s, orig_t: cur.baseOrigT", self.src)

    def test_include_defaults_off_and_session_seat_wins(self):
        # DP-31: points default to NOT included; only ticked points export
        self.assertIn(".filter(p => st.include[p.k] === true)", self.src)
        self.assertNotIn("st.include[p.k] !== false", self.src)        # old default-on
        self.assertIn("st.seats[p.k] !== undefined ? st.seats[p.k] : p.orig_s",
                      self.src)                                        # seat override
        self.assertIn("<label>include in results</label>", self.src)   # the toggle

    def test_summary_line_shows_points_adjusted_rate(self):
        self.assertIn("adjusted, rate ${ex.rate.toFixed(5)}", self.src)


class TestOverviewFrontend(unittest.TestCase):
    """DP-10 frontend guards: collapsed by default, flags gray-until-measured
    through the DP-11 gradient, click jumps to the nearest sync point."""

    HTML = TestExportLoop.HTML

    def setUp(self):
        with open(self.HTML, "r", encoding="utf-8") as handle:
            self.src = handle.read()

    def test_collapsed_by_default(self):
        self.assertIn('<details id="ovBox">', self.src)
        self.assertNotIn('<details id="ovBox" open', self.src)

    def test_flags_use_residual_gradient_gray_until_examined(self):
        # DP-31: gray until EXAMINED; once examined, the DP-11 residual gradient
        # (accent-blue only for restored points whose residual was never stored)
        self.assertIn("col: exam ? (r !== undefined ? qcol(r) : [94, 194, 255]) "
                      ": [138, 147, 160],", self.src)

    def test_click_jumps_to_nearest_point(self):
        # nearest-by-stream-time then the normal pickPoint path (context re-centres)
        self.assertIn("Math.abs(p.stream_s - t)", self.src)
        self.assertIn("pickPoint(pairIdx, best)", self.src)

    def test_overview_requests_the_hint_points(self):
        self.assertIn('fetch("/api/align/overview"', self.src)
        self.assertIn("points: h.points.map(p => [p.stream_s, p.orig_s])", self.src)


class TestRcWaveFrontend(unittest.TestCase):
    """DP-27..DP-32 (a review of the live tool): string guards + math mirrors
    over align.html, in the repo's frontend idiom (cf. TestExportLoop)."""

    HTML = TestExportLoop.HTML

    def setUp(self):
        with open(self.HTML, "r", encoding="utf-8") as handle:
            self.src = handle.read()

    # ---- DP-27: overview flags moved to the bottom edge ----
    def test_overview_flags_sit_at_the_bottom_edge(self):
        # the top row hid the numbers behind the "stream ↑ · original ↓" legend
        self.assertIn("g.fillRect(x + 1.5, chh - 15, w, 14)", self.src)
        self.assertIn("chh - 8.5", self.src)                   # the number text too
        self.assertNotIn("g.fillRect(x + 1.5, 1, 18, 14)", self.src)
        # the full-height jump line (the click target's visual) stays
        self.assertIn("g.moveTo(x + 0.5, 0); g.lineTo(x + 0.5, chh)", self.src)

    # ---- DP-28: context strip and controls swapped; controls text +2 ----
    def test_context_section_precedes_controls(self):
        self.assertLess(self.src.index("<h2>Context</h2>"),
                        self.src.index("<h2>Controls</h2>"))
        self.assertLess(self.src.index("<h2>Controls</h2>"),
                        self.src.index('<h2 id="inspTitle">'))

    def test_controls_font_grew_two_points(self):
        self.assertRegex(self.src, r"\.controls \{[^}]*font-size:15px")
        self.assertRegex(self.src, r"\.seg \.seglabel \{[^}]*font-size:14px")
        self.assertRegex(self.src, r"\.seg button \{[^}]*font-size:14px")

    # ---- DP-29: the original-native clock in the inspector title ----
    def test_title_carries_the_original_native_clock(self):
        self.assertIn("orig ${fmtT(seatNow())} native", self.src)
        # and it live-updates through the drawAll redraw path (nudge/drag/snap)
        self.assertRegex(self.src, r"updateInspTitle\(\);\s*\n\s*refreshPointLabels\(\);")

    def test_native_offset_math_mirror(self):
        # seatNow() = baseOrigT - (shift/SR)*rate. shift counts DISPLAYED samples
        # (the rate-corrected/stream clock), so × rate divides the correction back
        # out into the original's OWN unaltered clock -- the hints' orig rows and
        # every slice request's orig_t are in exactly those original-native
        # seconds. A positive shift (orig content drawn later/right) seats EARLIER
        # original content under the fixed stream anchor: the readout must fall.
        sr = 16000

        def native(base, shift, rate):
            return base - (shift / sr) * rate

        self.assertAlmostEqual(native(228.425, 0, 1.02046), 228.425)
        self.assertLess(native(228.425, 160, 1.02046), 228.425)
        # a 160-sample nudge is 10 ms of stream clock = 10 ms × rate native clock
        self.assertAlmostEqual(228.425 - native(228.425, 160, 1.02046),
                               0.010 * 1.02046, places=9)
        self.assertIn("return cur.baseOrigT - (shift / SR) * rate;", self.src)

    # ---- DP-30: per-pair adjustment persistence ----
    def test_adjustments_persist_under_a_per_pair_key(self):
        self.assertIn('const ADJUST_PREFIX = "netradio.align.adjust.";', self.src)
        self.assertIn('ADJUST_PREFIX + h.stem + "|" + String(h.orig).padStart(3, "0")',
                      self.src)
        self.assertIn("JSON.stringify({ v: 1, points })", self.src)
        # the schema fields: seat + refined + ncc, plus inc/resid so the tick and
        # the flag color survive a reload too
        for needle in ("refined: st.refined[p.k] === true,",
                       'ncc: typeof st.ncc[p.k] === "number" ? st.ncc[p.k] : null,',
                       "inc: st.include[p.k] === true,",
                       'resid: typeof st.resid[p.k] === "number" ? st.resid[p.k] : null,'):
            self.assertIn(needle, self.src)

    def test_restore_note_and_discard_control(self):
        self.assertIn("restored ${restored} adjustment", self.src)
        self.assertIn('id="btnDiscard"', self.src)
        self.assertIn("localStorage.removeItem(adjustKey(h))", self.src)
        # discard reverts the SESSION too, then reopens from the hint seats
        self.assertIn('delete ptState[h.stem + "|" + h.orig];', self.src)

    def test_restore_only_on_first_visit_per_session(self):
        # a pair already touched this session keeps its live state (the reqToken/
        # staleness architecture is untouched: restore is synchronous, before the
        # pickPoint -> fetchSlice chain that owns the tokens)
        self.assertIn("const untouched = !Object.keys(st.seats).length "
                      "&& !Object.keys(st.exam).length;", self.src)
        self.assertIn("untouched ? restoreAdjustments(h) : 0", self.src)

    def test_only_worked_points_are_stored(self):
        # merely OPENING a point colors it for the session but must not write
        # storage -- otherwise every page load would store the auto-opened first
        # point as a phantom "adjustment"
        self.assertRegex(self.src,
                         r"const worked = Math\.abs\(seat - p\.orig_s\) > 1e-6\s*\n\s*"
                         r"\|\| st\.refined\[p\.k\] === true "
                         r"\|\| st\.include\[p\.k\] === true;")

    # ---- DP-31: examined marker, three states, ticked-only export ----
    def test_points_default_not_included(self):
        self.assertIn('$("incChk").checked = st.include[p.k] === true;', self.src)
        self.assertNotIn('id="incChk" checked', self.src)

    def test_three_state_visual_vocabulary(self):
        self.assertIn("function ptVisual(st, p)", self.src)
        # selector marks: ✓ included, ○ examined-not-accepted, · untouched
        self.assertIn('const mark = v.inc ? "✓ " : (v.exam ? "○ " : "· ");', self.src)
        # overview flags append the tick (and widen for it)
        self.assertIn('String(p.k).slice(0, 2) + (v.inc ? "✓" : "")', self.src)
        self.assertIn("const w = v.inc ? 26 : 18;", self.src)

    def test_examined_marked_on_open_refine_and_tick(self):
        self.assertIn("st.exam[p.k] = true;", self.src)             # pickPoint
        self.assertIn("stSnap.exam[cur.point.k] = true;", self.src)  # snap
        self.assertIn("st.exam[cur.point.k] = true;", self.src)      # include toggle

    # ---- DP-32: refined confidence in the export ----
    def test_refined_confidence_exports_ncc_times_ten(self):
        self.assertIn("confidence: refined ? st.ncc[p.k] * 10 : p.confidence,",
                      self.src)
        self.assertIn('const refined = st.refined[p.k] === true '
                      '&& typeof st.ncc[p.k] === "number";', self.src)
        # snap records the ncc the export will carry; DP-30 persists it
        self.assertIn("stSnap.ncc[cur.point.k] = j.confidence;", self.src)

    def test_refined_confidence_round_trips_in_the_hint_format(self):
        # ncc 0.72 -> `confidence 7.2/10`, same n/10 format, one decimal -- and it
        # survives the parser like any hint confidence
        tmp = tempfile.TemporaryDirectory(prefix="align-rc11-")
        self.addCleanup(tmp.cleanup)
        points = [{"k": "1", "stream_s": 10.0, "orig_s": 20.0,
                   "confidence": 0.72 * 10}]
        sname, stext, oname, otext = _export_texts("d1-2", 3, points, 1.0, False)
        self.assertIn("confidence 7.2/10", stext)
        self.assertIn("confidence 7.2/10", otext)
        for name, text in ((sname, stext), (oname, otext)):
            with open(os.path.join(tmp.name, name), "w") as f:
                f.write(text)
        sets = align_inspect.parse_match_hints(tmp.name)
        self.assertEqual(len(sets), 1)
        self.assertAlmostEqual(sets[0]["points"][0]["confidence"], 7.2)


class TestAllPairsOverview(unittest.TestCase):
    """DP-18: EVERY hints set's flags for the current capture on the one overview
    lane -- the current pair bright exactly as before, the others muted (dimmed/
    desaturated, still numbered) in stacked rows above, rendered from the sources
    data alone (each point's stream_s is known; NO extra DSP fetch). Clicking a
    foreign flag switches the pair selector to that pair at the clicked point.
    String guards in the repo's frontend idiom (cf. TestOverviewFrontend)."""

    HTML = TestExportLoop.HTML

    def setUp(self):
        with open(self.HTML, "r", encoding="utf-8") as handle:
            self.src = handle.read()

    def _foreign_block(self):
        block = self.src.split("DP-18: EVERY other hints set on this capture", 1)[1]
        return block.split("// numbered sync flags at the BOTTOM edge", 1)[0]

    def test_foreign_pairs_filtered_to_the_current_stream_stem(self):
        # hints sets are keyed stem+orig; only sets on the CURRENT capture render,
        # and the current pair itself is skipped (it keeps the bright bottom row)
        self.assertIn("if (i2 === pairIdx || h2.stem !== cur.stem) return;",
                      self._foreign_block())

    def test_foreign_flags_are_muted_still_numbered(self):
        block = self._foreign_block()
        # each foreign pair's own session state colors through ptVisual, then
        # dimmed/desaturated toward the panel gray; the number (and ✓) stays
        self.assertIn("const col = mixCol(v.col, [58, 63, 72], 0.6);", block)
        self.assertIn('String(p.k).slice(0, 2) + (v.inc ? "✓" : "")', block)
        # one stacked row per foreign pair, above the current pair's bottom row,
        # with the pair's orig tag at the row's left edge
        self.assertIn("y = chh - 15 - 16 * row;", block)
        self.assertIn('"orig" + String(h2.orig).padStart(3, "0")', block)

    def test_foreign_flags_render_from_sources_alone(self):
        block = self._foreign_block()
        # no DSP fetch for foreign pairs -- their stream_s positions are already
        # in the sources payload -- and the piecewise warped band (envBand over
        # ov.segments) stays CURRENT-pair-only
        self.assertNotIn("fetch(", block)
        self.assertNotIn("envBand", block)

    def test_click_switches_pair_at_the_clicked_point(self):
        # the hit boxes recorded at draw time route the click; the switch rides
        # the NORMAL pickPair -> pickPoint -> fetchSlice chain, which owns the
        # reqToken staleness architecture (a stale slice/context/overview response
        # from the old pair is discarded exactly as on a selector change)
        self.assertIn("const f = ovForeign.find(b =>", self.src)
        self.assertIn('if (f) { $("pairSel").value = String(f.pairIdx); '
                      "pickPair(f.pairIdx, f.ptIdx); return; }", self.src)
        self.assertIn("function pickPair(i, ptIdx = 0)", self.src)
        self.assertIn('$("pointSel").value = String(ptIdx);\n  pickPoint(i, ptIdx);',
                      self.src)

    def test_hit_boxes_never_outlive_a_redraw(self):
        # ovForeign resets BEFORE the no-payload early return, so a cleared/stale
        # overview can't keep routing clicks to boxes that are no longer drawn
        self.assertRegex(self.src,
                         r"ovForeign = \[\];[^\n]*\n  if \(!ov \|\| !cur\) return;")

    def test_eight_foreign_pairs_all_rows_stay_on_canvas(self):
        # review iteration 1 P2: at a fixed 120 px the seventh foreign row began
        # at y = -7 and the eighth at y = -23 -- drawn (and clickable) nowhere.
        # The lane now grows with the pair count; mirror the geometry and prove
        # every row's 14 px flag box sits fully on-canvas, above the current
        # pair's bottom row, with envelope still visible at the top.
        self.assertIn("const cssH = Math.max(120, 55 + 16 * (nF + 1));", self.src)
        self.assertIn('c.style.height = cssH + "px";', self.src)
        self.assertIn("const nF = (cur && sources) ? sources.hints.filter(",
                      self.src)

        def css_h(n_foreign):
            return max(120, 55 + 16 * (n_foreign + 1))

        for n_f in (0, 1, 2, 3, 8, 12, 30):
            chh = css_h(n_f)
            for row in range(1, n_f + 1):
                y = chh - 15 - 16 * row              # the JS row placement
                self.assertGreaterEqual(y, 40, (n_f, row))       # fully visible
                self.assertLessEqual(y + 14, chh - 15, (n_f, row))  # above bottom row
        self.assertEqual(css_h(2), 120)              # the default height still fits few

    def test_one_overview_fetch_per_pair_cached(self):
        # at most ONE overview DSP fetch per key (pair/rate/invert scope): the
        # cache short-circuit sits before the network call, the payload is cached
        # on arrival, and the cache is bounded
        self.assertIn("const ovCache = new Map();", self.src)
        self.assertIn("const cached = ovCache.get(key);", self.src)
        self.assertIn("ovCache.set(key, j);", self.src)
        self.assertLess(self.src.index("const cached = ovCache.get(key);"),
                        self.src.index('fetch("/api/align/overview"'))
        self.assertIn("if (ovCache.size > OV_CACHE_MAX) "
                      "ovCache.delete(ovCache.keys().next().value);", self.src)


class TestJitterWindow(unittest.TestCase):
    """DP-33/DP-34: the MATCH−PHAT jitter window, drawn rather than smoothed.

    The MATCH path is rebuilt page-side from the Pass-1 hints' per-anchor MATCH
    deltas (matchconv DP-03), linearly interpolated between anchors; the gap
    |b_match(t) − b_line(t)| against the seat-implied line
    b_line(t) = seatNow() + (t − stream_t)·rate renders as a LIGHT bottom-edge
    whole-lane shading on the inspector's stream + original lanes (DP-34
    moved it there, DP-35 widened the ribbon to the lane body:
    on the overview/context it reduced the data shown). Guards in the repo's
    frontend idiom: a Python MIRROR of the JS math (keep in lockstep) + string
    pins."""

    HTML = TestExportLoop.HTML
    GREEN, YELLOW, RED = (12, 163, 12), (237, 161, 0), (208, 59, 59)

    def setUp(self):
        with open(self.HTML, "r", encoding="utf-8") as handle:
            self.src = handle.read()

    # ---- Python mirrors of matchSamples / bMatchAt / jitterGapMs / jcol ----
    @staticmethod
    def _samples(points):
        return sorted(((p["stream_s"], p["orig_s"] + p["match_delta_s"])
                       for p in points if p.get("match_delta_s") is not None),
                      key=lambda s: s[0])

    @classmethod
    def _b_match(cls, samples, t):
        n = len(samples)
        if n < 2 or t < samples[0][0] or t > samples[-1][0]:
            return None
        i = 1
        while i < n - 1 and samples[i][0] < t:
            i += 1
        (a0, m0), (a1, m1) = samples[i - 1], samples[i]
        f = (t - a0) / (a1 - a0) if a1 > a0 else 0.0
        return m0 + f * (m1 - m0)

    @classmethod
    def _gap_ms(cls, samples, t, seat, anchor_stream_s, rate):
        bm = cls._b_match(samples, t)
        if bm is None:
            return None
        return abs(bm - (seat + (t - anchor_stream_s) * rate)) * 1000.0

    @classmethod
    def _jcol(cls, ms):
        def mix(a, b, t):
            return tuple(round(v + (b[i] - v) * t) for i, v in enumerate(a))
        ms = min(max(ms, 0), 150)
        if ms <= 20:
            return cls.GREEN
        if ms <= 60:
            return mix(cls.GREEN, cls.YELLOW, (ms - 20) / 40)
        if ms <= 100:
            return mix(cls.YELLOW, cls.RED, (ms - 60) / 40)
        return cls.RED

    POINTS = [
        {"k": "1", "stream_s": 100.0, "orig_s": 200.0, "match_delta_s": 0.010},
        {"k": "2", "stream_s": 200.0, "orig_s": 302.0, "match_delta_s": 0.050},
        {"k": "3", "stream_s": 300.0, "orig_s": 404.0, "match_delta_s": None},
    ]

    def test_gap_at_the_anchor_is_the_delta_when_unmoved(self):
        # seat at the current anchor's hint value: the line passes through
        # (100, 200), so the gap AT that anchor is exactly the anchor's own
        # MATCH delta -- whatever the rate (the (t − stream_t) term is zero)
        samples = self._samples(self.POINTS)
        for rate in (1.0, 1.02046):
            self.assertAlmostEqual(
                self._gap_ms(samples, 100.0, 200.0, 100.0, rate), 10.0)

    def test_line_moves_with_the_seat_the_path_does_not(self):
        # a MATCH-engine snap seats AT the path: gap 0. A 10 ms hand nudge the
        # other way doubles the anchor's 10 ms delta to 20 ms. Snap/nudge move
        # ONLY the b_line term -- the samples never change.
        samples = self._samples(self.POINTS)
        self.assertAlmostEqual(
            self._gap_ms(samples, 100.0, 200.010, 100.0, 1.0), 0.0)
        self.assertAlmostEqual(
            self._gap_ms(samples, 100.0, 199.990, 100.0, 1.0), 20.0)

    def test_interpolates_linearly_between_anchors(self):
        # halfway between (100→200.010) and (200→302.050) the path reads
        # 251.030; a rate-1.02 line seated at the first anchor reads 251.000
        samples = self._samples(self.POINTS)
        self.assertAlmostEqual(self._b_match(samples, 150.0), 251.030)
        self.assertAlmostEqual(
            self._gap_ms(samples, 150.0, 200.0, 100.0, 1.02), 30.0, places=6)

    def test_no_extrapolation_beyond_the_anchor_span(self):
        samples = self._samples(self.POINTS)
        self.assertIsNone(self._b_match(samples, 99.9))
        self.assertIsNone(self._b_match(samples, 200.1))   # "3" has no delta
        # fewer than two delta-carrying anchors: no path at all
        self.assertIsNone(self._b_match(self._samples(self.POINTS[:1]), 100.0))

    def test_delta_less_points_are_not_path_knots(self):
        self.assertEqual(len(self._samples(self.POINTS)), 2)

    def test_jcol_scale_one_match_frame_green_red_by_100(self):
        # green through one ~20 ms MATCH frame, yellow at 60, red from 100,
        # capped at 150 (the band height cap; a globally-off path pegs red)
        self.assertEqual(self._jcol(0), self.GREEN)
        self.assertEqual(self._jcol(20), self.GREEN)
        self.assertEqual(self._jcol(60), self.YELLOW)
        self.assertEqual(self._jcol(100), self.RED)
        self.assertEqual(self._jcol(40000), self.RED)
        # the JS constants the mirror mirrors
        self.assertIn("const JIT_CAP_MS = 150;", self.src)
        self.assertIn("if (ms <= 20) return GRAD_GREEN;", self.src)
        self.assertIn("mixCol(GRAD_GREEN, GRAD_YELLOW, (ms - 20) / 40)", self.src)
        self.assertIn("mixCol(GRAD_YELLOW, GRAD_RED, (ms - 60) / 40)", self.src)

    def test_band_math_pins(self):
        # the JS the mirrors above mirror -- keep in lockstep
        self.assertIn("p.orig_s + p.match_delta_s", self.src)
        self.assertIn("seatNow() + (t - cur.point.stream_s) * rate", self.src)
        self.assertIn("Math.abs(bm - line) * 1000", self.src)
        self.assertIn('.filter(p => typeof p.match_delta_s === "number")', self.src)

    # ---- DP-34 (a review of the live DP-33 band): the visualization moved
    # off the overview/context lanes ("aren't helpful there and reduce the
    # amount of data shown") onto the INSPECTOR's stream + original lanes as a
    # light overlay ----

    def _fn_body(self, name):
        block = self.src.split("function %s(" % name, 1)[1]
        return block.split("\nfunction ", 1)[0]

    def test_band_absent_from_context_strip_and_overview(self):
        # DP-34: those lanes are back to pre-DP-33 full data density -- no
        # jitter drawing of any kind inside drawCtx or drawOv, and the old
        # midline band drawer is gone entirely
        self.assertNotIn("drawJitter(g", self.src)
        for fn in ("drawCtx", "drawOv"):
            body = self._fn_body(fn)
            for needle in ("Jitter", "jitterGapMs", "jcol(", "JIT_"):
                self.assertNotIn(needle, body, (fn, needle))

    def test_overlay_rides_the_inspector_stream_and_orig_lanes(self):
        # the shading draws on BOTH main waveform panels, after drawLane painted
        # them (drawAll order), mapping x through the inspector window
        body = self._fn_body("drawJitterOverlay")
        self.assertIn('for (const id of ["inStream", "inOrig"])', body)
        self.assertIn("const win = slice.winN / SR, "
                      "t0 = cur.point.stream_s - win / 2;", body)
        self.assertIn("jitterGapMs(samples, t0 + (x / cw) * win)", body)
        self.assertLess(
            self.src.index('drawLane("inOrig"'),
            self.src.index("drawJitterOverlay();   // DP-34"))
        # never extrapolated (null = no cover), and an agreed instant stays clean
        self.assertIn("if (gap === null || gap <= JIT_TINT_MIN_MS) continue;", body)

    def test_overlay_is_light_whole_lane_tint(self):
        # DP-35 (the 4 px ribbon was too subtle): the overlay is now a
        # per-x tint of the WHOLE lane body -- so the alpha must be far lighter
        # than the ribbon's 0.45 ("light" is the review's own word; the
        # waveform must stay fully readable through it). Mirror the constants
        # and bound them.
        self.assertIn("const JIT_ALPHA = 0.145;", self.src)
        self.assertIn("const JIT_TINT_MIN_MS = 20;", self.src)
        alpha = float(re.search(r"const JIT_ALPHA = ([0-9.]+);", self.src).group(1))
        tint_min = float(re.search(r"const JIT_TINT_MIN_MS = ([0-9.]+);",
                                   self.src).group(1))
        self.assertLessEqual(alpha, 0.16)            # whole-lane fill: err light
        self.assertGreaterEqual(alpha, 0.05)         # but still visible
        self.assertAlmostEqual(tint_min, 20.0)       # ~one MATCH frame = agreement
        body = self._fn_body("drawJitterOverlay")
        self.assertIn("g.globalAlpha = JIT_ALPHA;", body)
        # full-lane-height per-x tint, not an edge sliver; the old ribbon is gone
        self.assertIn("g.fillRect(x, 0, 1, ch);", body)
        self.assertNotIn("JIT_RIBBON_PX", self.src)
        self.assertIn("g.globalAlpha = 1;", body)    # never leaks onto later draws

    def test_agreed_regions_stay_clean(self):
        # a tool where most pixels are tinted green teaches nothing: at or
        # below JIT_TINT_MIN_MS (jcol's pure-green span -- one MATCH frame)
        # nothing is drawn, so the shading READS as "flagged sections"
        self.assertIn("gap <= JIT_TINT_MIN_MS) continue;", self.src)
        # the threshold sits exactly at jcol's pure-green boundary
        self.assertEqual(self._jcol(20), self.GREEN)
        self.assertNotEqual(self._jcol(20.5), self.GREEN)

    def test_lane_titles_explain_the_shading(self):
        # DP-35 (the review's exact ask): the DP-14 outside-panel lane titles say in
        # words what the shading means -- plain language ("the MATCH
        # cross-check", "the current seat"; no bare engine jargon), appended to
        # BOTH lane titles, and only when the loaded pair has a MATCH path
        clause = (" — shaded sections: the MATCH cross-check disagrees with the"
                  " current seat (green → red = gap in ms)")
        # the JS builds the clause across two source lines; pin both halves
        self.assertIn('" — shaded sections: the MATCH cross-check disagrees'
                      ' with the current"', self.src)
        self.assertIn('+ " seat (green → red = gap in ms)"', self.src)
        self.assertEqual(clause,
                         " — shaded sections: the MATCH cross-check disagrees"
                         " with the current" + " seat (green → red = gap in ms)")
        # conditional: 2+ delta anchors, else the empty string (no orphaned note)
        self.assertRegex(self.src, r"const shadeNote =\s*\n\s*"
                         r"matchSamples\(sources\.hints\[\+\$\(\"pairSel\"\)\.value\]\)"
                         r"\.length >= 2")
        self.assertRegex(self.src, r'shadeNote[\s\S]{0,220}?:\s*"";')
        # appended to BOTH titles
        self.assertIn("`Stream (fixed): ${cur.stem}` + shadeNote", self.src)
        self.assertRegex(self.src, r"and RMS-matched`\s*\n\s*\+ shadeNote;")

    def test_band_tracks_snap_and_nudge_through_the_seat(self):
        # the line term reads seatNow() at DRAW time, and drawAll (the nudge/
        # drag/snap redraw path) is exactly where the overlay is drawn -- so the
        # shading moves with the seat while the MATCH-path samples stay fixed
        self.assertIn("const line = seatNow() + (t - cur.point.stream_s) * rate;",
                      self.src)
        self.assertIn("drawJitterOverlay();   // DP-34", self.src)

    def test_legend_and_seat_readout_on_the_metrics_line(self):
        # DP-34: the legend + the numeric gap at the current SEAT position moved
        # to the inspector's metrics line (they lived on the overview/context
        # canvases before)
        self.assertIn('" · MATCH−PHAT gap, ms (lane shading) @seat "', self.src)
        self.assertIn('${sg.toFixed(1)}', self.src)
        self.assertIn("jitterMetricsBit();   // DP-34", self.src)
        # no path knowledge (fewer than 2 delta anchors): no shading, no legend
        self.assertIn("if (samples.length < 2) return;", self.src)
        self.assertIn('if (samples.length < 2) return "";', self.src)
        # the seat outside the anchor span still shows the legend, with n/a
        self.assertIn('(sg === null ? "n/a"', self.src)


class _FakeStdout:
    """Scripted stdout lines, then BLOCK (a live-but-silent worker), never EOF."""

    def __init__(self, lines):
        self.lines = list(lines)
        self.ev = threading.Event()

    def __iter__(self):
        for line in self.lines:
            yield line
        self.ev.wait()          # daemon pump thread parks here; test process exits fine


class _FakeProc:
    """A Popen stand-in whose stdout is either a list (EOF after) or _FakeStdout."""

    def __init__(self, lines, block_after=True):
        self.stdout = _FakeStdout(lines) if block_after else iter(lines)
        self.written = []
        self.dead = False
        proc = self

        class _Stdin:
            def write(self, s):
                proc.written.append(s)
            def flush(self):
                pass
        self.stdin = _Stdin()

    def poll(self):
        return 1 if self.dead else None

    def kill(self):
        self.dead = True


class _FakePopen:
    """Hands out scripted _FakeProcs in order and records every argv."""

    def __init__(self, procs):
        self.procs = list(procs)
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        if not self.procs:
            raise OSError("no more procs scripted")
        return self.procs.pop(0)


PING_OK = '{"ok": true}\n'


class TestWorkerClient(_Env):
    """DP-07: the keep-warm client — spawn shape, protocol, timeout, restart."""

    def test_spawn_argv_and_env_are_fixed(self):
        proc = _FakeProc([PING_OK, '{"sr": 16000}\n'])
        popen = _FakePopen([proc])
        w = align_inspect.WorkerClient(popen=popen)
        out = w.request({"op": "slice"}, self.mp3, timeout=1)
        self.assertEqual(out, {"sr": 16000})
        argv, kw = popen.calls[0]
        self.assertEqual(argv[:4], [os.path.join(self.repo, ".venv", "bin", "python"),
                                    "-m", "streamalign", "inspect-worker"])
        self.assertIn("--sources", argv)
        self.assertFalse(kw.get("shell", False))
        self.assertEqual(kw["env"]["NETRADIO_AUDIO_DIR"], self.mp3)
        self.assertEqual(kw["env"]["PYTHONPATH"], os.path.join(self.repo, "scripts"))
        # the ping and the request each went out as one JSON line
        self.assertEqual([json.loads(s)["op"] for s in proc.written],
                         ["ping", "slice"])

    def test_worker_reused_across_requests(self):
        proc = _FakeProc([PING_OK, '{"a": 1}\n', '{"b": 2}\n'])
        popen = _FakePopen([proc])
        w = align_inspect.WorkerClient(popen=popen)
        self.assertEqual(w.request({"op": "slice"}, self.mp3, timeout=1), {"a": 1})
        self.assertEqual(w.request({"op": "refine"}, self.mp3, timeout=1), {"b": 2})
        self.assertEqual(len(popen.calls), 1)      # one spawn served both

    def test_timeout_kills_and_raises(self):
        proc = _FakeProc([PING_OK])                # ping answered, then silence
        w = align_inspect.WorkerClient(popen=_FakePopen([proc]))
        with self.assertRaises(align_inspect.WorkerError):
            w.request({"op": "slice"}, self.mp3, timeout=0.05)
        self.assertTrue(proc.dead)

    def test_death_raises_and_next_request_respawns(self):
        dying = _FakeProc([PING_OK], block_after=False)     # EOF right after ping
        fresh = _FakeProc([PING_OK, '{"ok2": true}\n'])
        popen = _FakePopen([dying, fresh])
        w = align_inspect.WorkerClient(popen=popen)
        with self.assertRaises(align_inspect.WorkerError):
            w.request({"op": "slice"}, self.mp3, timeout=1)
        self.assertEqual(w.request({"op": "slice"}, self.mp3, timeout=1),
                         {"ok2": True})
        self.assertEqual(len(popen.calls), 2)

    def test_garbage_line_raises(self):
        proc = _FakeProc([PING_OK, "Traceback (most recent call last)\n"])
        w = align_inspect.WorkerClient(popen=_FakePopen([proc]))
        with self.assertRaises(align_inspect.WorkerError):
            w.request({"op": "slice"}, self.mp3, timeout=1)

    def test_spawn_failure_raises_workererror(self):
        w = align_inspect.WorkerClient(popen=_FakePopen([]))   # popen raises OSError
        with self.assertRaises(align_inspect.WorkerError):
            w.request({"op": "slice"}, self.mp3, timeout=1)

    def test_mp3_dir_change_respawns(self):
        p1 = _FakeProc([PING_OK, '{"a": 1}\n'])
        p2 = _FakeProc([PING_OK, '{"b": 2}\n'])
        popen = _FakePopen([p1, p2])
        w = align_inspect.WorkerClient(popen=popen)
        self.assertEqual(w.request({"op": "slice"}, self.mp3, timeout=1), {"a": 1})
        self.assertEqual(w.request({"op": "slice"}, self.mp3 + "2", timeout=1), {"b": 2})
        self.assertEqual(len(popen.calls), 2)
        self.assertTrue(p1.dead)


class TestRunRequestWorkerPath(_Env):
    """run_request in front of the worker: success, error relay, fallback."""

    class _StubWorker:
        def __init__(self, result=None, exc=None):
            self.result, self.exc, self.calls = result, exc, []
        def request(self, req, mp3_dir, timeout=None):
            self.calls.append(req)
            if self.exc:
                raise self.exc
            return self.result

    def test_worker_answer_short_circuits_the_subprocess(self):
        worker = self._StubWorker(result={"sr": 16000})
        runner = mock.Mock(side_effect=AssertionError("one-shot path must not run"))
        payload, code = align_inspect.run_request(
            self._params(), self.mp3, op="slice", runner=runner, worker=worker)
        self.assertEqual((payload, code), ({"sr": 16000}, 200))
        self.assertEqual(worker.calls[0]["op"], "slice")
        self.assertEqual(worker.calls[0]["stem"], "d376-395")

    def test_worker_error_payload_is_a_400(self):
        worker = self._StubWorker(result={"error": "win out of range"})
        payload, code = align_inspect.run_request(
            self._params(), self.mp3, op="slice", worker=worker)
        self.assertEqual(code, 400)

    def test_worker_failure_falls_back_to_one_shot(self):
        worker = self._StubWorker(exc=align_inspect.WorkerError("dead"))
        runner = mock.Mock(return_value=mock.Mock(
            stdout=json.dumps({"sr": 16000}).encode(), stderr=b""))
        payload, code = align_inspect.run_request(
            self._params(), self.mp3, op="slice", runner=runner, worker=worker)
        self.assertEqual((payload, code), ({"sr": 16000}, 200))
        self.assertEqual(runner.call_count, 1)

    def test_refine_request_carries_engine_and_radius(self):
        worker = self._StubWorker(result={"new_orig_t": 1.0})
        align_inspect.run_request(self._params(engine="match", radius=0.1),
                                  self.mp3, op="refine", worker=worker)
        req = worker.calls[0]
        self.assertEqual((req["op"], req["engine"], req["radius"]),
                         ("refine", "match", 0.1))

    def test_context_request_carries_context_default(self):
        worker = self._StubWorker(result={"n_cols": 10})
        align_inspect.run_request(self._params(), self.mp3, op="context",
                                  worker=worker)
        req = worker.calls[0]
        self.assertEqual(req["op"], "context")
        self.assertEqual(req["context"], align_inspect.DEFAULT_CONTEXT_S)

    def test_overview_request_carries_the_points(self):
        worker = self._StubWorker(result={"segments": []})
        align_inspect.run_request(self._params(points=[[196.0, 228.425]]),
                                  self.mp3, op="overview", worker=worker)
        req = worker.calls[0]
        self.assertEqual(req["op"], "overview")
        self.assertEqual(req["points"], [[196.0, 228.425]])

    def test_worker_disabled_env(self):
        with mock.patch.dict(os.environ, {"NETRADIO_ALIGN_WORKER": "0"}):
            self.assertFalse(align_inspect.worker_enabled())
        with mock.patch.dict(os.environ, {"NETRADIO_ALIGN_WORKER": ""}):
            self.assertTrue(align_inspect.worker_enabled())


class TestPlacedProvider(_Env):
    """DP-22 §6.5 source (b): list_placed() through the same seam + caps discipline."""

    class _StubWorker:
        def __init__(self, result=None, exc=None):
            self.result, self.exc, self.calls = result, exc, []

        def request(self, req, mp3_dir, timeout=None):
            self.calls.append(req)
            if self.exc:
                raise self.exc
            return self.result

    def _op_payload(self, **kw):
        pair = {"stem": "d376-395", "orig": 72, "rate": 1.0218, "rate_method": "AB",
                "points": [{"k": "A", "stream_s": 30.0, "orig_s": 10.0,
                            "derivable": True, "why": None, "verified": True,
                            "grade": "STRONG", "seat_conf": 0.83}]}
        pair.update(kw)
        return {"pairs": [pair]}

    def _runner(self, payload):
        return mock.Mock(return_value=mock.Mock(
            stdout=json.dumps(payload).encode(), stderr=b""))

    def test_dark_without_the_venv_is_empty(self):
        with _root(""):
            self.assertEqual(align_inspect.list_placed(self.mp3), [])

    def test_argv_is_fixed_with_nothing_client_supplied(self):
        runner = self._runner(self._op_payload())
        align_inspect.list_placed(self.mp3, runner=runner)
        argv = runner.call_args[0][0]
        self.assertEqual(argv[1:], ["-m", "streamalign", "placed"])
        self.assertEqual(argv[0], os.path.join(self.repo, ".venv", "bin", "python"))

    def test_sets_are_source_tagged_and_shaped(self):
        sets = align_inspect.list_placed(self.mp3,
                                         runner=self._runner(self._op_payload()))
        self.assertEqual(len(sets), 1)
        s = sets[0]
        self.assertEqual((s["source"], s["stem"], s["orig"], s["rate"]),
                         ("placed", "d376-395", 72, 1.0218))
        p = s["points"][0]
        self.assertEqual((p["k"], p["stream_s"], p["orig_s"], p["derivable"],
                          p["verified"], p["grade"], p["seat_conf"]),
                         ("A", 30.0, 10.0, True, True, "STRONG", 0.83))

    def test_unknown_stems_are_filtered_to_the_servers_namespace(self):
        payload = {"pairs": [self._op_payload()["pairs"][0],
                             self._op_payload(stem="not-in-mp3-dir")["pairs"][0]]}
        sets = align_inspect.list_placed(self.mp3, runner=self._runner(payload))
        self.assertEqual([s["stem"] for s in sets], ["d376-395"])

    def test_rate_note_is_forwarded_sanitized_and_capped(self):
        # the rate rules of #130 (AB-first + duplicate letters) reach
        # the picker via rate_note — control chars stripped, length capped, and
        # the key absent entirely when the op sent none (or junk)
        note = "A/B pairs in 2 captures (d376-395, d420-440);\x07\n using the first"
        sets = align_inspect.list_placed(
            self.mp3, runner=self._runner(self._op_payload(rate_note=note)))
        self.assertEqual(sets[0]["rate_note"],
                         "A/B pairs in 2 captures (d376-395, d420-440); using the first")
        long = align_inspect.list_placed(
            self.mp3, runner=self._runner(self._op_payload(rate_note="x" * 999)))
        self.assertEqual(long[0]["rate_note"], "x" * 200)
        absent = align_inspect.list_placed(
            self.mp3, runner=self._runner(self._op_payload()))
        self.assertNotIn("rate_note", absent[0])
        blank = align_inspect.list_placed(
            self.mp3, runner=self._runner(self._op_payload(rate_note="\x01\x02 ")))
        self.assertNotIn("rate_note", blank[0])

    def test_out_of_range_rate_is_nulled_not_served(self):
        sets = align_inspect.list_placed(
            self.mp3, runner=self._runner(self._op_payload(rate=7.5)))
        self.assertIsNone(sets[0]["rate"])

    def test_underivable_points_survive_with_reason_and_null_seat(self):
        payload = self._op_payload(points=[
            {"k": "D", "stream_s": 30.0, "orig_s": None, "derivable": False,
             "why": "no origNNN start: row", "verified": False,
             "grade": None, "seat_conf": None}])
        p = align_inspect.list_placed(self.mp3,
                                      runner=self._runner(payload))[0]["points"][0]
        self.assertFalse(p["derivable"])
        self.assertIsNone(p["orig_s"])
        self.assertEqual(p["why"], "no origNNN start: row")

    def test_points_are_capped_and_malformed_points_dropped(self):
        points = [{"k": "p%d" % i, "stream_s": float(i), "orig_s": float(i),
                   "derivable": True, "why": None, "verified": False,
                   "grade": None, "seat_conf": None}
                  for i in range(align_inspect.MAX_OVERVIEW_POINTS + 9)]
        points.insert(0, {"k": "bad", "stream_s": "nope"})
        sets = align_inspect.list_placed(
            self.mp3, runner=self._runner(self._op_payload(points=points)))
        self.assertEqual(len(sets[0]["points"]), align_inspect.MAX_OVERVIEW_POINTS)

    def test_garbage_stdout_is_empty_not_fatal(self):
        runner = mock.Mock(return_value=mock.Mock(stdout=b"traceback...", stderr=b""))
        self.assertEqual(align_inspect.list_placed(self.mp3, runner=runner), [])

    def test_non_finite_numbers_never_reach_the_wire(self):
        # a bare NaN/Infinity would serialize as invalid JSON on the sources
        # route (json.dumps emits NaN) and can make a browser reject the whole
        # response — every optional number must come back None instead
        payload = self._op_payload(
            rate=float("nan"),
            points=[{"k": "A", "stream_s": 30.0, "orig_s": float("inf"),
                     "derivable": True, "why": None, "verified": False,
                     "grade": None, "seat_conf": float("nan")},
                    {"k": "B", "stream_s": float("nan"), "orig_s": 1.0,
                     "derivable": True, "why": None, "verified": False,
                     "grade": None, "seat_conf": None}])
        sets = align_inspect.list_placed(self.mp3, runner=self._runner(payload))
        s = sets[0]
        self.assertIsNone(s["rate"])
        self.assertEqual(len(s["points"]), 1)      # the NaN-stream_s point dropped
        self.assertIsNone(s["points"][0]["orig_s"])
        self.assertIsNone(s["points"][0]["seat_conf"])
        self.assertNotIn("NaN", json.dumps(sets))
        self.assertNotIn("Infinity", json.dumps(sets))

    def test_oversized_json_integers_drop_the_value_not_the_route(self):
        # a 400-digit JSON int is valid JSON but OverflowErrors float(); the
        # boundary must drop the malformed value/point, never abort the sources
        # response
        huge = 10 ** 400
        payload = self._op_payload(
            rate=huge,
            points=[{"k": "A", "stream_s": 30.0, "orig_s": huge,
                     "derivable": True, "why": None, "verified": False,
                     "grade": None, "seat_conf": None},
                    {"k": "B", "stream_s": huge, "orig_s": 1.0,
                     "derivable": True, "why": None, "verified": False,
                     "grade": None, "seat_conf": None}])
        sets = align_inspect.list_placed(self.mp3, runner=self._runner(payload))
        s = sets[0]
        self.assertIsNone(s["rate"])
        self.assertEqual(len(s["points"]), 1)      # oversized stream_s point dropped
        self.assertIsNone(s["points"][0]["orig_s"])

    def test_op_error_answer_is_empty(self):
        runner = self._runner({"error": "no labels"})
        self.assertEqual(align_inspect.list_placed(self.mp3, runner=runner), [])

    def test_lock_contention_is_empty_not_blocking(self):
        acquired = align_inspect._run_lock.acquire()
        try:
            runner = mock.Mock(side_effect=AssertionError("must not run while busy"))
            self.assertEqual(align_inspect.list_placed(self.mp3, runner=runner), [])
        finally:
            if acquired:
                align_inspect._run_lock.release()

    def test_worker_answer_short_circuits_the_subprocess(self):
        worker = self._StubWorker(result=self._op_payload())
        runner = mock.Mock(side_effect=AssertionError("one-shot path must not run"))
        sets = align_inspect.list_placed(self.mp3, runner=runner, worker=worker)
        self.assertEqual(worker.calls, [{"op": "placed"}])
        self.assertEqual(sets[0]["stem"], "d376-395")

    def test_worker_failure_falls_back_to_one_shot(self):
        worker = self._StubWorker(exc=align_inspect.WorkerError("dead"))
        sets = align_inspect.list_placed(self.mp3,
                                         runner=self._runner(self._op_payload()),
                                         worker=worker)
        self.assertEqual(sets[0]["source"], "placed")

    def test_worker_unknown_op_falls_back_to_one_shot(self):
        # an older checkout's DSP answers {"error": "unknown op"}: fall through
        worker = self._StubWorker(result={"error": "unknown op 'placed'"})
        sets = align_inspect.list_placed(self.mp3,
                                         runner=self._runner(self._op_payload()),
                                         worker=worker)
        self.assertEqual(len(sets), 1)

    # The op's contract, pinned OFFLINE: this is the VERBATIM stdout of
    # `python -m streamalign placed` (#130's op) over a labels fixture for this
    # stem — captured 2026-08-06, not hand-written. If the producer's shape ever
    # drifts, re-capture it here deliberately; tests/test_placed.py pins the same
    # shape from the producer's end.
    CONTRACT_JSON = (
        '{"pairs": [{"stem": "d376-395", "orig": 72, "rate": 1.0464740418233394, '
        '"rate_method": "AB", "points": [{"k": "A", "stream_s": 99.134, "orig_s": '
        '29.662000000000006, "derivable": true, "why": null, "verified": true, '
        '"grade": null, "seat_conf": null}, {"k": "B", "stream_s": 190.088, '
        '"orig_s": 124.843, "derivable": true, "why": null, "verified": false, '
        '"grade": null, "seat_conf": null}]}]}')

    def test_the_placed_ops_verbatim_answer_parses_as_the_contract(self):
        runner = mock.Mock(return_value=mock.Mock(
            stdout=self.CONTRACT_JSON.encode(), stderr=b""))
        sets = align_inspect.list_placed(self.mp3, runner=runner)
        self.assertEqual(len(sets), 1)
        s = sets[0]
        self.assertEqual((s["source"], s["stem"], s["orig"]),
                         ("placed", "d376-395", 72))
        self.assertAlmostEqual(s["rate"], 1.0464740418233394)
        self.assertEqual([p["k"] for p in s["points"]], ["A", "B"])
        self.assertAlmostEqual(s["points"][0]["orig_s"], 29.662, places=3)
        self.assertTrue(s["points"][0]["verified"])
        self.assertIsNone(s["points"][1]["grade"])


class TestCachePolicy(_Env):
    """The decoded-audio cache on the one cache policy: the registration
    `align_inspect` makes from the same variable family the venv child reads, and the
    align server's one start-time eviction run — both skipped under READ_ONLY."""

    def setUp(self):
        super().setUp()
        self.root = tempfile.TemporaryDirectory(prefix="align-policy-")
        self.addCleanup(self.root.cleanup)
        # READ_ONLY is cleared here even under the suite's read-only posture: the
        # non-skip tests exercise the ordinary start; the skip test patches it back on
        patcher = mock.patch.dict(os.environ, {
            "NETRADIO_CACHE_ROOT": self.root.name,
            "NETRADIO_READ_ONLY": "",
            "NETRADIO_STREAMALIGN_CACHE_DIR": "",
            "NETRADIO_STREAMALIGN_CACHE_GB": "",
            "NETRADIO_STREAMALIGN_CACHE_MAX_AGE_DAYS": "",
            "NETRADIO_DOWNLOAD_ROOT": "",
            # the floor is pinned out of the way. Left unset it defaults to 82%, and
            # the eviction run measures the volume this suite happens to run on: on a
            # fuller disk the run's floor pass evicts from this cache as well, so the
            # age test below would be reading the machine instead of the age rule it
            # names. Every test here that needs a cache root pins both variables.
            "NETRADIO_DISK_MAX_PCT": "100",
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        snapshot = (dict(cache_budget._REGISTRY), dict(cache_budget._STATS))
        cache_budget._REGISTRY.clear()
        cache_budget._STATS.clear()
        self.addCleanup(self._restore_registry, snapshot)

    @staticmethod
    def _restore_registry(snapshot):
        cache_budget._REGISTRY.clear()
        cache_budget._REGISTRY.update(snapshot[0])
        cache_budget._STATS.clear()
        cache_budget._STATS.update(snapshot[1])

    def test_register_cache_registers_streamalign_with_the_policy_defaults(self):
        rec = align_inspect.register_cache()
        self.assertTrue(cache_budget.registered("streamalign"))
        self.assertEqual(rec["dir"], os.path.join(self.root.name, "streamalign"))
        self.assertEqual(rec["cap"], cache_budget.DEFAULT_CAP)
        self.assertEqual(rec["max_age"], 14)             # the code default, no .env line
        self.assertEqual((rec["order"], rec["refill"], rec["rank"]),
                         ("oldest-added", "re-decode", 3))

    def test_register_cache_reads_the_same_family_the_child_reads(self):
        with mock.patch.dict(os.environ, {
            "NETRADIO_STREAMALIGN_CACHE_DIR": os.path.join(self.root.name, "elsewhere"),
            "NETRADIO_STREAMALIGN_CACHE_GB": "2",
            "NETRADIO_STREAMALIGN_CACHE_MAX_AGE_DAYS": "3",
        }):
            rec = align_inspect.register_cache()
        self.assertEqual(rec["dir"], os.path.join(self.root.name, "elsewhere"))
        self.assertEqual(rec["cap"], 2 * cache_budget.GB)
        self.assertEqual(rec["max_age"], 3)              # the variable overrides the default

    def test_the_age_default_can_be_turned_off(self):
        with mock.patch.dict(os.environ, {"NETRADIO_STREAMALIGN_CACHE_MAX_AGE_DAYS": "none"}):
            rec = align_inspect.register_cache()
        self.assertIsNone(rec["max_age"])

    def test_register_cache_is_dark_without_the_root(self):
        with mock.patch.dict(os.environ, {"NETRADIO_CACHE_ROOT": ""}):
            self.assertIsNone(align_inspect.register_cache())
            self.assertFalse(cache_budget.registered("streamalign"))

    def test_start_cache_policy_registers_then_runs_the_eviction_once(self):
        run = mock.Mock(return_value={"ran": True, "evicted": {}})
        with mock.patch.object(cache_budget, "run_eviction", run):
            summary = align_server.start_cache_policy()
        self.assertEqual(summary, {"ran": True, "evicted": {}})
        run.assert_called_once_with()
        self.assertTrue(cache_budget.registered("streamalign"))

    def test_start_cache_policy_enforces_the_age_for_real(self):
        # the run is the point: a start of this server applies the cache's age limit
        # whatever else is running, so an entry past the limit is gone after it
        with mock.patch.dict(os.environ,
                             {"NETRADIO_STREAMALIGN_CACHE_MAX_AGE_DAYS": "0"}):
            rec = align_inspect.register_cache()
            os.makedirs(rec["dir"])
            stale = os.path.join(rec["dir"], "dead.npy")
            with open(stale, "wb") as fh:
                fh.write(b"x" * 100)
            os.utime(stale, (1000000, 1000000))          # decades past any age limit
            summary = align_server.start_cache_policy()
        self.assertTrue(summary["ran"])
        self.assertEqual(summary["evicted"]["streamalign"], 1)
        self.assertFalse(os.path.exists(stale))

    def test_start_cache_policy_skips_registration_and_run_under_read_only(self):
        register = mock.Mock(side_effect=AssertionError("READ_ONLY must skip the registration"))
        run = mock.Mock(side_effect=AssertionError("READ_ONLY must skip the run"))
        with mock.patch.dict(os.environ, {"NETRADIO_READ_ONLY": "1"}), \
                mock.patch.object(align_inspect, "register_cache", register), \
                mock.patch.object(cache_budget, "run_eviction", run):
            self.assertIsNone(align_server.start_cache_policy())
        register.assert_not_called()
        run.assert_not_called()

    def test_main_calls_the_policy_after_load_env(self):
        # string guard on the call order: the registration reads the environment at
        # call time, so it must sit after load_env's setdefault seeding of it
        with open(align_server.__file__, encoding="utf-8") as fh:
            body = fh.read().split("def main():", 1)[1]
        self.assertLess(body.index("load_env()"), body.index("start_cache_policy()"))


if __name__ == "__main__":
    unittest.main()
