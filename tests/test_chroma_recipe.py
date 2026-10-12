"""chroma_recipe, the single recipe source.

Offline: the recipe runs for real on synthetic audio.
"""
import os
import sys
import unittest

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)

import numpy as np                      # noqa: E402
import chroma_recipe                    # noqa: E402

# chroma_recipe imports librosa LAZILY (inside the compute paths), so the module
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


if __name__ == "__main__":
    unittest.main()
