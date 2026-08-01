"""align-tool Pass 2 slice provider: geometry, gain, polarity, and the refine seam.

Hermetic: synthetic stereo WAVs in a tempdir, decoded through the real ffmpeg path
(skipped if ffmpeg is absent), so the numbers the player's inspector will draw are
exercised end-to-end -- slicing geometry, zero-padding at file edges, rate
correction, RMS matching, polarity, and snap-to-best.
"""

import base64
import json
import os
import shutil
import sys
import tempfile
import unittest
import wave

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))

from streamalign import inspect_slice as isl       # noqa: E402

SR = isl.SR
HAVE_FFMPEG = shutil.which("ffmpeg") is not None


def _write_stereo_wav(path, left, right):
    pcm = np.stack([np.clip(left, -1, 1), np.clip(right, -1, 1)], axis=1)
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((pcm * 32767.0).astype("<i2").tobytes())
    return path


def _decode(b64):
    return np.frombuffer(base64.b64decode(b64), dtype="<i2").astype(np.float32) / 32767.0


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not on PATH")
class TestBuildSlices(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="inspect-slice-")
        rng = np.random.default_rng(42)
        k = np.hanning(33).astype(np.float32)
        k /= k.sum()
        raw = rng.standard_normal(SR * 30).astype(np.float32)
        # band-limited + a white floor: pure smoothed noise has exact spectral comb
        # nulls, and PHAT's whitening amplifies shared-null bins into peak-burying
        # noise -- an artefact of synthetic material real audio doesn't have
        base = np.convolve(raw, k, "same") + 0.1 * raw
        cls.orig_sig = 0.5 * base
        # stream = the original, polarity-inverted, quarter amplitude, 4 s late
        stream = np.zeros(SR * 40, dtype=np.float32)
        stream[4 * SR:4 * SR + len(base)] = -0.125 * base
        cls.stream_path = _write_stereo_wav(os.path.join(cls.tmp, "stream.wav"),
                                            stream, stream)
        cls.orig_path = _write_stereo_wav(os.path.join(cls.tmp, "orig.wav"),
                                          cls.orig_sig, cls.orig_sig)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _build(self, **kw):
        args = dict(stream_src=self.stream_path, orig_src=self.orig_path,
                    stream_t=14.0, orig_t=10.0, rate=1.0, invert=True, win_s=6.0)
        args.update(kw)
        return isl.build_slices(**args)

    def test_geometry_and_length(self):
        out = self._build()
        n_expect = int(round((6.0 + 2 * isl.MARGIN_S) * SR))
        self.assertEqual(out["n"], n_expect)
        for side in ("stream", "orig"):
            for ch in ("L", "R"):
                self.assertEqual(len(_decode(out[side][ch])), n_expect)

    def test_aligned_slices_null_after_gain_and_polarity(self):
        # stream 14.0 s <-> orig 10.0 s is the true seat; with invert + RMS match the
        # difference over the window must be tiny compared to the signal itself
        out = self._build()
        s = _decode(out["stream"]["L"])
        o = _decode(out["orig"]["L"])
        core = slice(int(isl.MARGIN_S * SR), int((isl.MARGIN_S + 6.0) * SR))
        resid = np.sqrt(np.mean((s[core] - o[core]) ** 2))
        ref = np.sqrt(np.mean(s[core] ** 2))
        self.assertLess(resid, ref * 0.05)
        self.assertGreater(out["rms_gain"], 0.0)

    def test_without_invert_the_signals_sum_not_null(self):
        out = self._build(invert=False)
        s = _decode(out["stream"]["L"])
        o = _decode(out["orig"]["L"])
        core = slice(int(isl.MARGIN_S * SR), int((isl.MARGIN_S + 6.0) * SR))
        resid = np.sqrt(np.mean((s[core] - o[core]) ** 2))
        ref = np.sqrt(np.mean(s[core] ** 2))
        self.assertGreater(resid, ref * 1.5)

    def test_edge_slice_is_zero_padded_not_shifted(self):
        # stream_t=1.0 with a 6 s window + margin => slice spans file time [-2.5, 4.5):
        # 2.5 s of pre-file zero padding, then file zeros, then real content only from
        # file time 4.0 s (where the test signal begins) at slice index 6.5 s
        out = self._build(stream_t=1.0)
        s = _decode(out["stream"]["L"])
        content = int(6.5 * SR)
        self.assertEqual(float(np.abs(s[:content - 100]).max()), 0.0)
        self.assertGreater(float(np.abs(s[content + 100:]).max()), 0.0)

    def test_win_cap_enforced(self):
        out = self._build(win_s=9999.0)
        self.assertEqual(out["win_s"], isl.MAX_WIN_S)

    def test_refine_finds_a_deliberate_20ms_error(self):
        got = isl.refine_seat(self.stream_path, self.orig_path,
                              stream_t=14.0, orig_t=10.0 - 0.020,   # seat claimed 20 ms early
                              rate=1.0, invert=True, win_s=6.0, radius_s=0.05)
        # orig content now sits 20 ms LATE relative to the claim; refine must point
        # back to the true instant
        self.assertAlmostEqual(got["new_orig_t"], 10.0, delta=0.002)
        self.assertGreater(got["confidence"], 0.5)
        self.assertAlmostEqual(got["offset_ms"], -20.0, delta=2.0)

    def test_refine_radius_cap(self):
        got = isl.refine_seat(self.stream_path, self.orig_path,
                              stream_t=14.0, orig_t=10.0, rate=1.0, invert=True,
                              win_s=6.0, radius_s=99.0)
        # a capped radius still refines sanely at the true seat
        self.assertLess(abs(got["offset_ms"]), 2.0)


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not on PATH")
class TestBuildContext(TestBuildSlices):
    """AP-05: the zoomed-out context strip -- decimated min/max columns, not audio."""

    def _ctx(self, **kw):
        args = dict(stream_src=self.stream_path, orig_src=self.orig_path,
                    stream_t=14.0, orig_t=10.0, rate=1.0, invert=True,
                    context_s=10.0, win_s=6.0)
        args.update(kw)
        return isl.build_context(**args)

    def test_geometry_and_payload_shape(self):
        out = self._ctx()
        cols = int(2 * 10.0 * isl.CONTEXT_COLS_PER_S)
        self.assertEqual(out["n_cols"], cols)
        for side in ("stream", "orig"):
            self.assertEqual(len(out[side]["min"]), cols)
            self.assertEqual(len(out[side]["max"]), cols)
        self.assertEqual(out["cols_per_s"], isl.CONTEXT_COLS_PER_S)
        self.assertAlmostEqual(out["start_s"], 4.0)
        self.assertAlmostEqual(out["stream_len_s"], 40.0, delta=0.1)
        # min <= max everywhere, and values are the decimation of real audio
        for side in ("stream", "orig"):
            for lo, hi in zip(out[side]["min"], out[side]["max"]):
                self.assertLessEqual(lo, hi)

    def test_columns_track_the_signal_envelope(self):
        # the stream is silent before file-time 4 s: a strip over -2..18 s must show
        # zero columns in the silent head and live columns once the signal starts
        out = self._ctx(stream_t=8.0, orig_t=4.0)   # strip covers file time -2..18 s
        head = out["stream"]["max"][:int(5.5 * isl.CONTEXT_COLS_PER_S)]
        body = out["stream"]["max"][int(6.5 * isl.CONTEXT_COLS_PER_S):]
        self.assertEqual(max(abs(v) for v in head), 0.0)
        self.assertGreater(max(body), 0.0)

    def test_context_cap_and_floor(self):
        out = self._ctx(context_s=999.0)
        self.assertEqual(out["context_s"], isl.MAX_CONTEXT_S)
        for bad in (0.5, float("nan"), -3.0):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self._ctx(context_s=bad)

    def test_cli_context_end_to_end(self):
        # regression: the CLI passes win POSITIONALLY, so build_context's signature
        # must keep win_s in the shared positional slot -- an argument-order slip
        # here produced "multiple values for argument 'context_s'" on the real CLI
        # while every keyword-calling test stayed green
        import io
        import shutil as _sh
        from contextlib import redirect_stdout
        from unittest import mock
        from streamalign import audio as _audio
        from streamalign.__main__ import main
        _sh.copy(self.orig_path, os.path.join(self.tmp, "072-synth.wav"))
        buf = io.StringIO()
        with mock.patch.object(_audio, "AUDIO_DIR", self.tmp):
            with redirect_stdout(buf):
                main(["inspect-slice", "stream", "72",
                      "--stream-t", "14", "--orig-t", "10", "--rate", "1.0",
                      "--invert", "--win", "6", "--context", "10",
                      "--sources", self.tmp])
        out = json.loads(buf.getvalue())
        self.assertNotIn("error", out)
        self.assertEqual(out["context_s"], 10.0)
        self.assertEqual(out["win_s"], 6.0)
        self.assertEqual(out["n_cols"], int(2 * 10.0 * isl.CONTEXT_COLS_PER_S))

    def test_cli_refine_engine_end_to_end(self):
        # same boundary for --refine: --engine must reach refine_seat (phat path;
        # the match engine needs sonic-annotator and is pinned separately)
        import io
        import shutil as _sh
        from contextlib import redirect_stdout
        from unittest import mock
        from streamalign import audio as _audio
        from streamalign.__main__ import main
        _sh.copy(self.orig_path, os.path.join(self.tmp, "072-synth.wav"))
        buf = io.StringIO()
        with mock.patch.object(_audio, "AUDIO_DIR", self.tmp):
            with redirect_stdout(buf):
                main(["inspect-slice", "stream", "72",
                      "--stream-t", "14", "--orig-t", "10", "--rate", "1.0",
                      "--invert", "--win", "6", "--refine", "--engine", "phat",
                      "--sources", self.tmp])
        out = json.loads(buf.getvalue())
        self.assertNotIn("error", out)
        self.assertEqual(out["engine"], "phat")
        self.assertLess(abs(out["offset_ms"]), 2.0)

    def test_aligned_context_columns_agree_after_gain_and_polarity(self):
        # at the true seat the two envelopes must roughly coincide over the overlap
        out = self._ctx()
        n0 = int(2 * isl.CONTEXT_COLS_PER_S)     # skip the stream's silent head
        s = np.array(out["stream"]["max"][n0:])
        o = np.array(out["orig"]["max"][n0:])
        keep = s > 0.01
        self.assertGreater(keep.sum(), 100)
        rel = np.abs(s[keep] - o[keep]) / s[keep]
        self.assertLess(float(np.median(rel)), 0.25)


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not on PATH")
class TestLoadedPairSeam(TestBuildSlices):
    """AP-08: the pair seam gives identical answers to the load-per-call path."""

    def test_pair_reuse_matches_fresh_loads(self):
        pair = isl.LoadedPair(self.stream_path, self.orig_path, 1.0)
        a = isl.build_slices(self.stream_path, self.orig_path, 14.0, 10.0, 1.0,
                             True, 6.0)
        b = isl.build_slices(self.stream_path, self.orig_path, 14.0, 10.0, 1.0,
                             True, 6.0, pair=pair)
        self.assertEqual(a, b)
        r1 = isl.refine_seat(self.stream_path, self.orig_path, 14.0, 10.0 - 0.02,
                             1.0, True, 6.0, radius_s=0.05)
        r2 = isl.refine_seat(self.stream_path, self.orig_path, 14.0, 10.0 - 0.02,
                             1.0, True, 6.0, radius_s=0.05, pair=pair)
        self.assertEqual(r1, r2)


class TestMatchEngine(unittest.TestCase):
    """AP-14: the MATCH-path snap engine's target math + gross-median gate.

    Hermetic: the MATCH path itself is injected by monkeypatching the pair's
    `match_pairs` (running sonic-annotator is Pass-1-tested); these pin the target
    interpolation, the delta report, and the mis-seat error."""

    class _FakePair:
        def __init__(self, pairs, n_s=60.0):
            sr = isl.SR
            self.stream = np.zeros((int(n_s * sr), 2), dtype=np.float32)
            self.orig = np.zeros((int(n_s * sr), 2), dtype=np.float32)
            self.orig2 = self.orig
            self._pairs = pairs
        def match_pairs(self, around_s):
            return self._pairs

    @staticmethod
    def _line_pairs(off_s, n=200, step=0.25):
        # a well-behaved path: orig = stream + off_s, rate 1, plenty of rows
        return [(a * step + 10.0, a * step + 10.0 + off_s) for a in range(n)]

    def test_match_target_is_the_path_implied_instant(self):
        pair = self._FakePair(self._line_pairs(off_s=2.0))
        out = isl.refine_seat("s", "o", stream_t=30.0, orig_t=31.5, rate=1.0,
                              invert=False, win_s=6.0, engine="match", pair=pair)
        self.assertEqual(out["engine"], "match")
        self.assertAlmostEqual(out["new_orig_t"], 32.0, places=6)
        # convention parity with PHAT: positive offset = orig content sits early
        self.assertAlmostEqual(out["offset_ms"], (31.5 - 32.0) * 1000.0, places=3)
        self.assertIn("match_vs_phat_ms", out)
        self.assertIn("phat_orig_t", out)

    def test_grossly_misseated_path_is_an_error_not_a_snap(self):
        pair = self._FakePair(self._line_pairs(off_s=42.0))   # 42 s off the seat line
        with self.assertRaises(ValueError) as ctx:
            isl.refine_seat("s", "o", stream_t=30.0, orig_t=30.0, rate=1.0,
                            invert=False, win_s=6.0, engine="match", pair=pair)
        self.assertIn("globally mis-seated", str(ctx.exception))

    def test_short_path_is_an_error(self):
        pair = self._FakePair(self._line_pairs(off_s=0.0, n=5))
        with self.assertRaises(ValueError) as ctx:
            isl.refine_seat("s", "o", stream_t=30.0, orig_t=30.0, rate=1.0,
                            invert=False, win_s=6.0, engine="match", pair=pair)
        self.assertIn("too short", str(ctx.exception))

    def test_out_of_path_instant_is_an_error(self):
        pair = self._FakePair(self._line_pairs(off_s=0.0))
        with self.assertRaises(ValueError) as ctx:
            isl.refine_seat("s", "o", stream_t=2.0, orig_t=2.0, rate=1.0,
                            invert=False, win_s=6.0, engine="match", pair=pair)
        self.assertIn("does not cover", str(ctx.exception))

    def test_unknown_engine_rejected(self):
        with self.assertRaises(ValueError):
            isl.refine_seat("s", "o", stream_t=1.0, orig_t=1.0, rate=1.0,
                            invert=False, win_s=6.0, engine="dtw")


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not on PATH")
class TestParamGuards(TestBuildSlices):
    """Nonsense parameters raise ValueError (the CLI turns it into JSON), never a
    numpy traceback -- the subprocess's stdout must stay parseable by the player."""

    def test_bad_params_raise_valueerror_not_numpy_errors(self):
        for kw in (dict(rate=0.0), dict(win_s=-2.0), dict(win_s=float("nan")),
                   dict(rate=float("inf")), dict(stream_t=-1.0)):
            args = dict(stream_src=self.stream_path, orig_src=self.orig_path,
                        stream_t=14.0, orig_t=10.0, rate=1.0, invert=True, win_s=6.0)
            args.update(kw)
            with self.assertRaises(ValueError, msg=repr(kw)):
                isl.build_slices(**args)

    def test_refine_radius_guard(self):
        with self.assertRaises(ValueError):
            isl.refine_seat(self.stream_path, self.orig_path, stream_t=14.0,
                            orig_t=10.0, rate=1.0, invert=True, win_s=6.0,
                            radius_s=float("nan"))

    def test_cli_boundary_emits_error_json(self):
        import io
        from contextlib import redirect_stdout
        from streamalign.__main__ import main
        buf = io.StringIO()
        with redirect_stdout(buf):
            with self.assertRaises(SystemExit):
                main(["inspect-slice", "no-such-stem", "72",
                      "--stream-t", "1", "--orig-t", "1"])
        out = json.loads(buf.getvalue())
        self.assertIn("error", out)
