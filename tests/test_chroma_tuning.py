"""The blockwise tuning estimate must produce the SAME signature, byte for byte.

The pool holds thousands of chroma signatures computed under `RECIPE_VERSION` 1. Re-fetching them
would take weeks of deliberately slow, polite requests, so a change to `chroma_recipe` that moves
a single float is not a refactor -- it is a migration nobody asked for. `compute_chroma` now
estimates the tuning 300 seconds at a time instead of over the whole file, purely to stop one
librosa call from allocating ten gigabytes, and this file is what proves that the number, and
therefore the signature, did not move.

The reference in every pin is **librosa's own whole-file path**, called directly:

    ref_t = librosa.core.pitch.estimate_tuning(y=y32, sr=SR, bins_per_octave=36)
    ref_c = librosa.feature.chroma_cqt(y=y32, sr=SR, hop_length=HOP)      # tuning=None

not a saved copy of the old `compute_chroma`. That matters: a copy of the old code stops meaning
anything the moment the old code is gone, while librosa's whole-file path is the thing the pool
was actually built with. The comparisons are exact -- `==` on the floats, `np.array_equal` on the
matrices -- never a tolerance. `chroma_recipe.TOLERANCE` exists for arm64-vs-x86_64 agreement and
has no business here: within one architecture the two paths either agree bit for bit or the change
is wrong.

The routine set (everything below except the two-hour and real-audio cases) computes its own
whole-file references on signals of at most 12.7 minutes, so it runs in the ordinary suite. The
two-hour case is opt-in (`NETRADIO_LONG_TESTS=1`) and compares against a recorded fixture, because
its whole-file reference is the ~10 GB run this change exists to remove: it is paid once, when the
fixture is recorded, not on every suite.
"""

import hashlib
import json
import os
import platform
import subprocess
import sys
import unittest

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
sys.path.insert(0, SCRIPTS)

import numpy as np                      # noqa: E402
import chroma_recipe                    # noqa: E402

# chroma_recipe imports librosa LAZILY, so this module imports fine on a bare clone and the
# classes that actually compute chroma skip instead of erroring at call time.
try:
    import librosa                      # noqa: E402
    from librosa.core.pitch import estimate_tuning, piptrack      # noqa: E402
    HAVE_LIBROSA = True
except ImportError:                     # only the third-party dep -- anything else raises
    HAVE_LIBROSA = False

SR = chroma_recipe.SR
HOP = chroma_recipe.HOP
FIXTURE_2H = os.path.join(FIXTURES, "blockwise_2h.json")
TWO_HOURS_S = 2 * 3600


# --- the signal ---------------------------------------------------------------------------------
#
# Deterministic without an RNG. numpy's generator stream is a promise about a sequence, not about
# the bytes of a float array, and the two-hour fixture has to be reproducible years from now on a
# different numpy. So the noise comes from an integer hash of the sample index instead.

def _noise(offset, count):
    """Pseudo-noise in [-1, 1) for samples [offset, offset + count), from a 64-bit integer hash."""
    i = np.arange(offset, offset + count, dtype=np.uint64)
    h = i * np.uint64(6364136223846793005) + np.uint64(1442695040888963407)
    h ^= h >> np.uint64(33)
    h = h * np.uint64(0xff51afd7ed558ccd)
    h ^= h >> np.uint64(29)
    return (h >> np.uint64(11)).astype(np.float64) / float(1 << 53) * 2.0 - 1.0


def synthetic_signal(seconds, extra=123, detune_cents=-14.0):
    """A chord detuned by `detune_cents`, plus noise at about -34 dB.

    Detuned on purpose: a signal at concert pitch estimates to 0.0, and 0.0 is the answer a broken
    blockwise loop would also give. `extra` sets N modulo 512 -- the hop the tuning estimate uses,
    and therefore where the block edges fall relative to the end of the file.

    Built a megabyte at a time so two hours of audio costs 460 MB of float32, not several
    gigabytes of float64 intermediates.
    """
    n = int(seconds * SR) + extra
    y = np.empty(n, dtype="float32")
    ratio = 2.0 ** (detune_cents / 1200.0)
    voices = [(440.0 * 2.0 ** ((midi - 69) / 12.0) * ratio, 0.7 * k)
              for k, midi in enumerate((45, 52, 57, 64, 69, 76))]
    step = 1 << 20
    for lo in range(0, n, step):
        hi = min(n, lo + step)
        t = np.arange(lo, hi, dtype=np.float64) / SR
        block = 0.02 * _noise(lo, hi - lo)
        for freq, phase in voices:
            block += 0.15 * np.sin(2.0 * np.pi * freq * t + phase)
        y[lo:hi] = block
    return y


def silence(seconds):
    return np.zeros(int(seconds * SR), dtype="float32")


def _whole_file_chroma(y32):
    """What the recipe used to do, and what every signature in the pool was computed with."""
    c = librosa.feature.chroma_cqt(y=y32, sr=SR, hop_length=HOP) + chroma_recipe.EPSILON
    return librosa.util.normalize(c, norm=2, axis=0)


@unittest.skipUnless(HAVE_LIBROSA, "librosa unavailable -- see requirements-streamalign.txt")
class BlockwiseTuningIsExact(unittest.TestCase):
    """The routine pins: same tuning float, same chroma, on signals with awkward block edges."""

    def _assert_identical(self, y32, label):
        ref_t = estimate_tuning(y=y32, sr=SR, bins_per_octave=chroma_recipe.TUNING_BPO)
        got_t = chroma_recipe.estimate_tuning_blockwise(y32, SR)
        self.assertEqual(got_t, ref_t, "%s: tuning moved (%r vs %r)" % (label, got_t, ref_t))

        ref_c = _whole_file_chroma(y32)
        got_c = chroma_recipe.compute_chroma(y32)
        self.assertTrue(np.array_equal(got_c, ref_c), "%s: float32 chroma differs" % label)
        self.assertTrue(np.array_equal(got_c.astype(chroma_recipe.STORE_DTYPE),
                                       ref_c.astype(chroma_recipe.STORE_DTYPE)),
                        "%s: stored float16 signature differs" % label)

    def test_voiced_bins_identical_across_block_edges(self):
        """Nine block edges in 140 seconds, and every voiced bin still matches.

        The block length drops to 500 frames (16 s) for this one test, so the loop runs nine times
        on a signal short enough to hold a whole-file reference cheaply. Comparing the tuning float
        alone would be weak -- it is a histogram bin edge at 0.01 resolution, so it can survive a
        loop that quietly loses or duplicates columns. This compares the multiset of voiced
        (pitch, magnitude) pairs the aggregation actually sees, which cannot.
        """
        y32 = synthetic_signal(140, extra=7)
        self.assertEqual(len(y32) % chroma_recipe.TUNING_HOP, 7)

        ref_p, ref_m = piptrack(y=y32, sr=SR, n_fft=chroma_recipe.TUNING_N_FFT)
        self.assertEqual(ref_p.shape[1], 1 + len(y32) // chroma_recipe.TUNING_HOP)
        voiced = ref_p > 0

        original = chroma_recipe.TUNING_BLOCK_FRAMES
        chroma_recipe.TUNING_BLOCK_FRAMES = 500
        try:
            got_p, got_m = self._blockwise_voiced(y32)
            got_t = chroma_recipe.estimate_tuning_blockwise(y32, SR)
        finally:
            chroma_recipe.TUNING_BLOCK_FRAMES = original

        self.assertEqual(got_p.size, int(voiced.sum()))
        self.assertTrue(np.array_equal(np.sort(got_p), np.sort(ref_p[voiced])))
        self.assertTrue(np.array_equal(np.sort(got_m), np.sort(ref_m[voiced])))
        self.assertEqual(got_t, estimate_tuning(y=y32, sr=SR,
                                                bins_per_octave=chroma_recipe.TUNING_BPO))

    @staticmethod
    def _blockwise_voiced(y32):
        """The block loop's kept columns, as the aggregation sees them.

        A transcription of `estimate_tuning_blockwise`'s loop, stopping one step earlier so the
        test can compare the voiced bins rather than only the float they collapse to. It has to
        stay in step with the real loop; the tuning assertion in the same test is what catches it
        if it does not.
        """
        n = len(y32)
        n_frames = 1 + n // chroma_recipe.TUNING_HOP
        ctx = chroma_recipe.TUNING_N_FFT // 2
        pitches, mags = [], []
        t0 = 0
        while t0 < n_frames:
            t1 = min(t0 + chroma_recipe.TUNING_BLOCK_FRAMES, n_frames)
            s0, drop = (0, 0) if t0 == 0 else (t0 * chroma_recipe.TUNING_HOP - ctx, 2)
            e0 = min(n, t1 * chroma_recipe.TUNING_HOP + ctx)
            p, m = piptrack(y=y32[s0:e0], sr=SR, n_fft=chroma_recipe.TUNING_N_FFT)
            keep = slice(drop, drop + (t1 - t0))
            p, m = p[:, keep], m[:, keep]
            voiced = p > 0
            pitches.append(p[voiced])
            mags.append(m[voiced])
            t0 = t1
        return np.concatenate(pitches), np.concatenate(mags)

    def test_tuning_and_chroma_identical_multiblock(self):
        """12.7 minutes: three real 300-second blocks, at the shipped block length."""
        y32 = synthetic_signal(12.7 * 60)
        self.assertEqual(len(y32) % chroma_recipe.TUNING_HOP, 379)
        n_frames = 1 + len(y32) // chroma_recipe.TUNING_HOP
        self.assertEqual(-(-n_frames // chroma_recipe.TUNING_BLOCK_FRAMES), 3)   # ceil == 3 blocks
        self._assert_identical(y32, "12.7 min")

    def test_edge_lengths(self):
        """The lengths where the loop's arithmetic could go wrong, one subtest each."""
        cases = [
            ("exactly one block", synthetic_signal(300, extra=0)),
            ("one block plus a second", synthetic_signal(301, extra=0)),
            ("shorter than one block", synthetic_signal(75)),
            ("a whole number of hops", synthetic_signal(96, extra=0)),
        ]
        for label, y32 in cases:
            with self.subTest(case=label):
                self._assert_identical(y32, label)

    def test_silence_has_no_voiced_bins_and_still_agrees(self):
        """No voiced bin anywhere: librosa falls back to a 0.0 threshold and an empty histogram,
        and so must the block loop. This is the case a `np.median([])` would turn into a NaN."""
        y32 = silence(70)
        with self.assertWarns(Warning):          # pitch_tuning warns on an empty pitch set
            ref_t = estimate_tuning(y=y32, sr=SR, bins_per_octave=chroma_recipe.TUNING_BPO)
        with self.assertWarns(Warning):
            got_t = chroma_recipe.estimate_tuning_blockwise(y32, SR)
        self.assertEqual(got_t, ref_t)
        self.assertEqual(got_t, 0.0)

    def test_the_block_loop_does_not_touch_chroma_cqt(self):
        """The loop is a pitch-tracker loop. If `chroma_cqt` ever appears inside it, the estimate
        has stopped being an estimate and started being a second signature."""
        import inspect
        self.assertNotIn("chroma_cqt",
                         inspect.getsource(chroma_recipe.estimate_tuning_blockwise))


@unittest.skipUnless(HAVE_LIBROSA, "librosa unavailable -- see requirements-streamalign.txt")
class RecipeContractUnchanged(unittest.TestCase):
    """Blockwise tuning is an implementation detail of recipe 1, not a new recipe."""

    def test_recipe_version_is_still_one(self):
        self.assertEqual(chroma_recipe.RECIPE_VERSION, 1)

    def test_published_recipe_gains_no_tuning_field(self):
        """Edge workers assert-match `chroma/_recipe.json` against this dict. A new key there is
        a new contract, so the tuning method deliberately does not appear in it."""
        d = chroma_recipe.recipe_dict(with_toolchain=False)
        self.assertEqual(set(d), {"version", "pipeline", "sr", "hop", "feature", "epsilon",
                                  "norm", "dtype", "min_seconds", "tolerance", "comparison",
                                  "key"})
        self.assertNotIn("tuning", json.dumps(d))

    def test_the_tuning_constants_are_librosas_defaults(self):
        """Written down rather than read from librosa at runtime, so a librosa release that
        changes a default fails here instead of silently changing every signature."""
        self.assertEqual(chroma_recipe.TUNING_N_FFT, 2048)
        self.assertEqual(chroma_recipe.TUNING_HOP, chroma_recipe.TUNING_N_FFT // 4)
        self.assertEqual(chroma_recipe.TUNING_BPO, 36)
        self.assertEqual(chroma_recipe.TUNING_RESOLUTION, 0.01)
        # 300 seconds at 16 kHz on the 512-sample hop, exactly.
        self.assertEqual(chroma_recipe.TUNING_BLOCK_FRAMES, 300 * SR // chroma_recipe.TUNING_HOP)
        self.assertEqual(300 * SR % chroma_recipe.TUNING_HOP, 0)


def record_2h_fixture(path=FIXTURE_2H):
    """Compute the two-hour whole-file reference ONCE and write it down.

    This is the ~10 GB run the change exists to remove, so it is not part of any suite. Run it on
    an arm64 Mac with at least 12 GB free:

        PYTHONPATH=scripts .venv/bin/python tests/test_chroma_tuning.py --record
    """
    y32 = synthetic_signal(TWO_HOURS_S)
    ref_t = estimate_tuning(y=y32, sr=SR, bins_per_octave=chroma_recipe.TUNING_BPO)
    ref_c = _whole_file_chroma(y32).astype(chroma_recipe.STORE_DTYPE)
    fixture = {
        "seconds": TWO_HOURS_S,
        "n_samples": int(len(y32)),
        "tuning": float(ref_t),
        "sha256_float16_npy": hashlib.sha256(ref_c.tobytes()).hexdigest(),
        "shape": list(ref_c.shape),
        "platform.machine": platform.machine(),
        "librosa": librosa.__version__,
        "numpy": np.__version__,
        "python": platform.python_version(),
    }
    try:
        import scipy
        fixture["scipy"] = scipy.__version__
    except ImportError:
        fixture["scipy"] = "absent"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(fixture, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return fixture


@unittest.skipUnless(HAVE_LIBROSA, "librosa unavailable -- see requirements-streamalign.txt")
@unittest.skipUnless(os.environ.get("NETRADIO_LONG_TESTS") == "1",
                     "long test -- set NETRADIO_LONG_TESTS=1 (about a minute, ~1 GB)")
class TwoHourSynthetic(unittest.TestCase):
    """Two hours is the length that made the harvester allocate ten gigabytes, so it is the length
    worth pinning -- but only the blockwise side is recomputed here (about 1 GB). The whole-file
    reference lives in the fixture."""

    def test_two_hour_synthetic_matches_recorded_reference(self):
        if not os.path.exists(FIXTURE_2H):
            self.skipTest("no fixture yet -- record it with `python tests/test_chroma_tuning.py "
                          "--record` on a machine with >=12 GB free (see record_2h_fixture)")
        with open(FIXTURE_2H, "r", encoding="utf-8") as fh:
            fixture = json.load(fh)

        y32 = synthetic_signal(fixture["seconds"])
        self.assertEqual(len(y32), fixture["n_samples"])
        got_t = chroma_recipe.estimate_tuning_blockwise(y32, SR)
        self.assertEqual(got_t, fixture["tuning"])

        got = chroma_recipe.compute_chroma(y32).astype(chroma_recipe.STORE_DTYPE)
        self.assertEqual(list(got.shape), fixture["shape"])
        same_arch = (platform.machine() == fixture["platform.machine"]
                     and librosa.__version__ == fixture["librosa"]
                     and np.__version__ == fixture["numpy"])
        digest = hashlib.sha256(got.tobytes()).hexdigest()
        if same_arch:
            self.assertEqual(digest, fixture["sha256_float16_npy"])
        else:
            # Byte-identity is a same-architecture property (chroma_recipe.TOLERANCE). Off the
            # recording machine the fixture can only pin the tuning float, which it did above.
            self.skipTest("fixture recorded on %s/librosa %s; this is %s/librosa %s"
                          % (fixture["platform.machine"], fixture["librosa"],
                             platform.machine(), librosa.__version__))


@unittest.skipUnless(HAVE_LIBROSA, "librosa unavailable -- see requirements-streamalign.txt")
class AgainstRealAudio(unittest.TestCase):
    """Synthetic signals have a clean spectrum; real music does not, and the voiced-bin set is
    where a wrong block edge would show. These run on audio the developer machine already has and
    skip everywhere else -- no network, ever."""

    @staticmethod
    def _decode(path):
        """The recipe's own front end: ffmpeg -> mono float32 @ 16 kHz."""
        out = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-ac", "1",
                              "-ar", str(SR), "-f", "f32le", "pipe:1"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if out.returncode != 0:
            raise RuntimeError(out.stderr.decode("utf-8", "replace")[-200:])
        return np.frombuffer(out.stdout, dtype="float32")

    def test_canary_set_reproduces(self):
        """The bucket's canary excerpts, if a local copy is configured.

        Each canary is 75 seconds, so this exercises the single-block path only; the multi-block
        path is pinned by the synthetic tests above and by the capture files below.
        """
        d = os.environ.get("NETRADIO_CANARY_DIR", "")
        manifest_path = os.path.join(d, "manifest.json") if d else ""
        if not manifest_path or not os.path.exists(manifest_path):
            self.skipTest("set NETRADIO_CANARY_DIR to a local copy of chroma/_canary/")
        with open(manifest_path, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
        items = manifest.get("items") or manifest.get("canaries") or []
        self.assertTrue(items, "canary manifest lists nothing")
        for item in items:
            name = item.get("audio") or item.get("file")
            with self.subTest(canary=name):
                y32 = self._decode(os.path.join(d, name))
                got = chroma_recipe.compute_chroma(y32)
                expected = item.get("sha256_expected") or item.get("sha256")
                if expected:
                    self.assertEqual(
                        hashlib.sha256(got.astype(chroma_recipe.STORE_DTYPE).tobytes()).hexdigest(),
                        expected)
                self.assertTrue(np.array_equal(got, _whole_file_chroma(y32)))

    def test_real_audio_multiblock(self):
        """Two capture files: 20-25 minutes each is four or five real blocks of real music."""
        d = (os.environ.get("NETRADIO_CANARY_SOURCE_DIR")
             or os.environ.get("NETRADIO_MP3_DIR") or "")
        if not d or not os.path.isdir(d):
            self.skipTest("set NETRADIO_CANARY_SOURCE_DIR or NETRADIO_MP3_DIR to real audio")
        names = sorted(n for n in os.listdir(d)
                       if n.lower().endswith((".mp3", ".wav", ".flac", ".wv", ".m4a")))[:2]
        if not names:
            self.skipTest("no decodable audio in %s" % d)
        for name in names:
            with self.subTest(file=name):
                y32 = self._decode(os.path.join(d, name))
                self.assertEqual(chroma_recipe.estimate_tuning_blockwise(y32, SR),
                                 estimate_tuning(y=y32, sr=SR,
                                                 bins_per_octave=chroma_recipe.TUNING_BPO))
                self.assertTrue(np.array_equal(chroma_recipe.compute_chroma(y32),
                                               _whole_file_chroma(y32)))


if __name__ == "__main__":
    if "--record" in sys.argv:
        print(json.dumps(record_2h_fixture(), indent=2, sort_keys=True))
    else:
        unittest.main()
