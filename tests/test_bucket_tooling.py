"""chroma_recipe (the single recipe source) + the two bucket-bootstrap generators.

Offline: the recipe runs for real on synthetic audio; ffmpeg is stubbed for the canary builder.
"""
import io
import json
import os
import sys
import tempfile
import unittest
import unittest.mock

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)

import numpy as np                      # noqa: E402
import chroma_recipe                    # noqa: E402
import make_recipe                      # noqa: E402
import make_canary                      # noqa: E402

# chroma_recipe/make_canary import librosa LAZILY (inside the compute paths), so the module
# imports above succeed on a bare clone — and the tests then error at call time. Probe once;
# the classes that actually compute chroma skip cleanly instead.
try:
    import librosa                      # noqa: E402,F401
    HAVE_LIBROSA = True
except ImportError:                     # only the third-party dep -- anything else raises
    HAVE_LIBROSA = False


def _signal(seconds=70, seed=7):
    return (np.random.default_rng(seed).standard_normal(int(seconds * chroma_recipe.SR)) * 0.1
            ).astype("float32")


@unittest.skipUnless(HAVE_LIBROSA, "librosa unavailable -- see requirements-streamalign.txt")
class TestRecipeIsOneSource(unittest.TestCase):
    def test_compute_chroma_shape_dtype_and_determinism(self):
        y = _signal()
        a = chroma_recipe.compute_chroma(y)
        b = chroma_recipe.compute_chroma(y)
        self.assertEqual(a.shape[0], 12)
        self.assertTrue(np.array_equal(a, b))                 # deterministic
        self.assertEqual(a.dtype, np.dtype("float32"))        # stored form is the float16 cast

    def test_no_signature_producer_has_an_inline_recipe(self):
        """The real anti-drift guard: EVERY live script that produces a comparable chroma
        signature must call chroma_recipe, so `librosa.feature.chroma_cqt` may appear ONLY in
        chroma_recipe.py (the source) and in the documented exceptions. A new inline copy
        anywhere else fails this test. (An earlier version guarded only harvest.py, and three
        other producers drifted unnoticed — review 2026-07-18.)"""
        # Documented exceptions, each with a WHY:
        #   chroma_recipe.py      — the single source itself.
        #   streamalign/track_mix.py — alignment DTW rate/offset chroma: configurable sr/hop,
        #                              sometimes un-normalised; NOT a pool signature (see its note).
        allowed = {"chroma_recipe.py", os.path.join("streamalign", "track_mix.py")}
        offenders = []
        for root in (SCRIPTS, os.path.join(SCRIPTS, "streamalign")):
            for name in sorted(os.listdir(root)):
                if not name.endswith(".py"):
                    continue
                rel = os.path.relpath(os.path.join(root, name), SCRIPTS)
                if rel in allowed:
                    continue
                if "librosa.feature.chroma_cqt" in open(os.path.join(root, name)).read():
                    offenders.append(rel)
        self.assertEqual(offenders, [], "inline chroma recipe (drift risk) in: %r — route "
                         "through chroma_recipe.compute_chroma or add a documented exception"
                         % offenders)
        import harvest
        self.assertIs(harvest.chroma_recipe, chroma_recipe)

    def test_sr_override_is_preserved_for_the_variable_api(self):
        y = _signal()
        a = chroma_recipe.compute_chroma(y)                   # default SR
        b = chroma_recipe.compute_chroma(y, sr=chroma_recipe.SR)
        self.assertTrue(np.array_equal(a, b))                 # explicit default == implicit
        import identify_by_chroma
        self.assertTrue(np.array_equal(identify_by_chroma.chroma(y), a))

    def test_recipe_dict_has_the_contract_fields(self):
        d = chroma_recipe.recipe_dict()
        for k in ("version", "sr", "hop", "feature", "epsilon", "norm", "dtype", "min_seconds",
                  "tolerance", "key"):
            self.assertIn(k, d)
        self.assertEqual(d["sr"], chroma_recipe.SR)
        self.assertEqual(d["hop"], chroma_recipe.HOP)
        self.assertEqual(d["tolerance"], chroma_recipe.TOLERANCE)


class TestMakeRecipe(unittest.TestCase):
    def test_writes_valid_json_matching_recipe_dict(self):
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, "_recipe.json")
            with unittest.mock.patch.object(sys, "argv", ["make_recipe.py", "-o", out]):
                make_recipe.main()
            d = json.load(open(out))
            self.assertEqual(d["sr"], chroma_recipe.SR)
            self.assertEqual(d["tolerance"], chroma_recipe.TOLERANCE)

    def test_no_toolchain_flag_omits_versions(self):
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, "r.json")
            with unittest.mock.patch.object(sys, "argv",
                                            ["make_recipe.py", "--no-toolchain", "-o", out]):
                make_recipe.main()
            self.assertNotIn("toolchain", json.load(open(out)))


@unittest.skipUnless(HAVE_LIBROSA, "librosa unavailable -- see requirements-streamalign.txt")
class TestMakeCanary(unittest.TestCase):
    def test_sign_is_stored_float16_and_reproducible(self):
        y = _signal()
        npy1, sha1, shape = make_canary.sign(y)
        npy2, sha2, _ = make_canary.sign(y)
        self.assertEqual(sha1, sha2)                          # a rebuild reproduces the bytes
        arr = np.load(io.BytesIO(npy1))
        self.assertEqual(arr.dtype, np.dtype("float16"))
        self.assertEqual(shape, list(arr.shape))

    def test_build_produces_consistent_manifest(self):
        """End-to-end with ffmpeg stubbed: decode -> sign -> flac -> manifest, and the manifest's
        recorded sha256 matches recomputing from the (stubbed) decoded audio."""
        y = _signal(seconds=75)

        def fake_run(cmd, **kw):
            class P:
                returncode = 0
                stderr = b""
            p = P()
            if "pipe:1" in cmd:                                # the DECODE call -> yields PCM
                p.stdout = y.tobytes()
            else:                                              # the FLAC ENCODE call -> writes file
                open(cmd[-1], "wb").close()
                p.stdout = b""
            return p

        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as out:
            open(os.path.join(src, "a.wav"), "wb").close()     # one "source" file
            with unittest.mock.patch.object(make_canary.subprocess, "run", side_effect=fake_run):
                m = make_canary.build(src, out, "ffmpeg", count=1, seconds=75, offset=0)
            self.assertEqual(len(m["items"]), 1)
            item = m["items"][0]
            self.assertEqual(m["tolerance"], chroma_recipe.TOLERANCE)
            # the manifest sha must match the committed expected .npy AND a fresh signing
            expected = open(os.path.join(out, item["expected"]), "rb").read()
            import hashlib
            self.assertEqual(item["sha256_expected"], hashlib.sha256(expected).hexdigest())
            self.assertEqual(item["sha256_expected"], make_canary.sign(y)[1])
            self.assertTrue(os.path.exists(os.path.join(out, item["audio"])))
            self.assertTrue(os.path.exists(os.path.join(out, "manifest.json")))


if __name__ == "__main__":
    unittest.main()
