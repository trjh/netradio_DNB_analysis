"""AP-08 keep-warm worker: protocol framing + pair-cache behaviour, no audio needed.

The DSP itself is inspect_slice's and tested there; these pin the worker's own
contract -- one JSON line out per line in (ids echoed, errors as data, blank lines
skipped, EOF ends the loop), and the single-slot pair cache that reloads only when
the (stem, orig, rate) trio changes. Loaders are monkeypatched; no ffmpeg runs.
"""

import io
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))

from streamalign import inspect_worker as iw       # noqa: E402


class _CountingLoader:
    """Stands in for LoadedPair: records constructions, returns a marker object."""

    def __init__(self):
        self.calls = []

    def __call__(self, stream_src, orig_src, rate):
        self.calls.append((stream_src, orig_src, rate))
        return ("pair", stream_src, orig_src, rate)


class TestPairCache(unittest.TestCase):

    def test_same_trio_loads_once(self):
        loader = _CountingLoader()
        cache = iw.PairCache(loader=loader)
        p1 = cache.get("d376-395", "/src/072.wv", 1.02046)
        p2 = cache.get("d376-395", "/src/072.wv", 1.02046)
        self.assertIs(p1, p2)
        self.assertEqual(len(loader.calls), 1)

    def test_pair_or_rate_change_reloads(self):
        loader = _CountingLoader()
        cache = iw.PairCache(loader=loader)
        cache.get("d376-395", "/src/072.wv", 1.02046)
        cache.get("d376-395", "/src/072.wv", 1.0)          # rate change
        cache.get("d356-375", "/src/072.wv", 1.0)          # stem change
        cache.get("d356-375", "/src/071.wv", 1.0)          # orig change
        self.assertEqual(len(loader.calls), 4)

    def test_returning_to_a_previous_pair_reloads(self):
        # single-slot by design: the cache holds the CURRENT pair only
        loader = _CountingLoader()
        cache = iw.PairCache(loader=loader)
        cache.get("a", "/o1", 1.0)
        cache.get("b", "/o2", 1.0)
        cache.get("a", "/o1", 1.0)
        self.assertEqual(len(loader.calls), 3)


class TestHandle(unittest.TestCase):

    def test_ping(self):
        self.assertEqual(iw.handle({"op": "ping"}, iw.PairCache(), "src"), {"ok": True})

    def test_unknown_op_and_non_dict_are_errors_not_raises(self):
        self.assertIn("error", iw.handle({"op": "nope"}, iw.PairCache(), "src"))
        self.assertIn("error", iw.handle(["op"], iw.PairCache(), "src"))

    def test_unresolvable_stem_is_an_error_line(self):
        with mock.patch.object(iw._audio, "find_audio_file", return_value=None):
            out = iw.handle({"op": "slice", "stem": "dxxx", "orig": 72,
                             "stream_t": 1, "orig_t": 1}, iw.PairCache(), "src")
        self.assertIn("no audio for capture", out["error"])

    def _patched(self, cache):
        """Route resolution + DSP through fakes; returns the recorded calls."""
        calls = {}

        def _rec(name, ret):
            def fn(**kw):
                calls[name] = kw
                return ret
            return fn

        patches = [
            mock.patch.object(iw._audio, "find_audio_file", return_value="/a.mp3"),
            mock.patch("streamalign.track_mix.find_original", return_value="/o.wv"),
            mock.patch.object(iw._isl, "build_slices", side_effect=_rec("slice", {"sr": 16000})),
            mock.patch.object(iw._isl, "refine_seat", side_effect=_rec("refine", {"new_orig_t": 1.0})),
            mock.patch.object(iw._isl, "build_context", side_effect=_rec("context", {"n_cols": 10})),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return calls

    def test_ops_dispatch_with_pair_and_defaults(self):
        cache = iw.PairCache(loader=_CountingLoader())
        calls = self._patched(cache)
        req = {"stem": "d376-395", "orig": 72, "stream_t": 196.0, "orig_t": 228.425,
               "rate": 1.02046, "invert": True}
        self.assertEqual(iw.handle(dict(req, op="slice"), cache, "src"), {"sr": 16000})
        self.assertEqual(iw.handle(dict(req, op="refine", radius=0.1, engine="match"),
                                   cache, "src"), {"new_orig_t": 1.0})
        self.assertEqual(iw.handle(dict(req, op="context", context=30.0), cache, "src"),
                         {"n_cols": 10})
        self.assertEqual(calls["slice"]["pair"][0], "pair")   # the cached pair is passed
        self.assertEqual(calls["refine"]["engine"], "match")
        self.assertEqual(calls["refine"]["radius_s"], 0.1)
        self.assertEqual(calls["context"]["context_s"], 30.0)
        # one load served all three ops (same trio)
        self.assertEqual(len(cache._loader.calls), 1)

    def test_dsp_exception_becomes_an_error_dict(self):
        cache = iw.PairCache(loader=_CountingLoader())
        self._patched(cache)
        with mock.patch.object(iw._isl, "build_slices",
                               side_effect=ValueError("win out of range")):
            out = iw.handle({"op": "slice", "stem": "d", "orig": 1,
                             "stream_t": 1, "orig_t": 1}, cache, "src")
        self.assertIn("win out of range", out["error"])


class TestServeFraming(unittest.TestCase):

    def _serve(self, lines):
        out = io.StringIO()
        iw.serve("src", stdin=io.StringIO(lines), stdout=out, cache=iw.PairCache())
        return [json.loads(l) for l in out.getvalue().splitlines()]

    def test_one_response_line_per_request_ids_echoed(self):
        got = self._serve('{"op": "ping", "id": 7}\n\n{"op": "ping"}\n')
        self.assertEqual(got, [{"ok": True, "id": 7}, {"ok": True}])

    def test_bad_json_line_answers_error_and_the_loop_survives(self):
        got = self._serve('not json\n{"op": "ping", "id": "after"}\n')
        self.assertEqual(got[0], {"error": "bad JSON line"})
        self.assertEqual(got[1], {"ok": True, "id": "after"})

    def test_non_object_json_answers_error(self):
        got = self._serve('[1, 2]\n')
        self.assertIn("error", got[0])

    def test_eof_ends_cleanly(self):
        self.assertEqual(self._serve(""), [])


if __name__ == "__main__":
    unittest.main()
