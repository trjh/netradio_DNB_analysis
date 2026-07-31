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
