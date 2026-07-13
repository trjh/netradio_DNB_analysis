"""Canary self-test tests — including the bug the self-test itself had.

The point of a canary is that it goes red when something is broken. So the tests that matter here
are the SABOTAGE ones: break the matcher, and check the canary notices.

The most important test in this file is `test_a_tie_is_not_a_win`. The first version of the
offline check required only "cost in range" and "rank 1". A degenerate matcher — one that returns
the SAME cost for everything — produces a table of ties; ties sort by track number; the subject is
the lowest-numbered case; so it lands at rank 1 and sails through. I found this by sabotaging the
matcher to prove the check would catch it, and watching it not. That is the Mystery Track 7 lesson
from the other side: those five false positives were false because they were all within 0.0007 of
each other. Winning by nothing is not winning.

No network and no audio: `fetch` is injected, and the local-file paths are stubbed.
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))

try:
    import selftest
except Exception:                       # librosa/numba absent -> not this test's job
    selftest = None


@unittest.skipIf(selftest is None, "selftest.py needs the librosa venv (.venv) — skipping")
class OfflineCanary(unittest.TestCase):
    """The subject is track 1; the pool is tracks 1-3. `costs` decides what the matcher 'sees'."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        selftest.RESULT = os.path.join(self.tmp, "selftest.json")
        selftest.CANARY = os.path.join(self.tmp, "canary.json")
        self.cases = [{"num": n, "orig": "/x/%d.wav" % n, "name": "T%d" % n,
                       "extract": None, "cap": None, "cstart": 0, "mb": 0, "me": 300}
                      for n in (1, 2, 3)]

    def _run(self, costs):
        """costs: {track_num: cost} as the matcher would report them."""
        seq = []

        def fake_match(q, cand):
            return (costs[self.cases[len(seq) % len(self.cases)]["num"]], 0, 0.0)

        # match() is called once per pool case, in order — so hand back that case's cost
        it = iter([costs[c["num"]] for c in self.cases])
        with mock.patch.object(selftest, "cases", return_value=self.cases), \
             mock.patch.object(selftest._cal, "mix_query", return_value=[0.0] * 99999), \
             mock.patch.object(selftest._cal, "chroma", return_value="CHROMA"), \
             mock.patch.object(selftest._audio, "load_audio", return_value=[0.0]), \
             mock.patch.object(selftest._cm, "match",
                               side_effect=lambda q, c: (next(it), 0, 0.0)):
            return selftest.offline()

    def test_a_healthy_matcher_passes(self):
        r = self._run({1: 0.004, 2: 0.095, 3: 0.101})
        self.assertTrue(r["ok"])
        self.assertEqual(r["rank"], 1)

    def test_a_tie_is_not_a_win(self):
        """THE BUG. Every cost identical: ties sort by track number, the subject is lowest, so it
        'wins' at rank 1 with a cost inside the true-match range. A rank-only check passes this.
        A degenerate matcher must NOT be able to pass the canary."""
        r = self._run({1: 0.001, 2: 0.001, 3: 0.001})
        self.assertFalse(r["ok"])                      # was True before MIN_MARGIN
        self.assertEqual(r["rank"], 1)                 # it really does rank first...
        self.assertIn("tie", r["why"])                 # ...and is still correctly rejected

    def test_a_bare_win_by_a_hair_is_not_a_win(self):
        """The MT7 shape: right answer first, but by 0.0007. Not good enough."""
        r = self._run({1: 0.0400, 2: 0.0407, 3: 0.0500})
        self.assertFalse(r["ok"])
        self.assertEqual(r["rank"], 1)

    def test_a_false_negative_is_caught(self):
        """The MT5 shape: the record IS there, and the matcher says no."""
        r = self._run({1: 0.069, 2: 0.010, 3: 0.020})
        self.assertFalse(r["ok"])
        self.assertGreater(r["rank"], 1)
        self.assertIn("did not win", r["why"])

    def test_it_wins_but_outside_the_true_match_range(self):
        r = self._run({1: 0.070, 2: 0.200, 3: 0.300})
        self.assertFalse(r["ok"])
        self.assertIn("true-match range", r["why"])

    def test_no_calibration_data_is_skipped_not_failed(self):
        """An unset NETRADIO_SOURCES_DIR must not look like a broken matcher."""
        with mock.patch.object(selftest, "cases", return_value=[]):
            r = selftest.offline()
        self.assertIsNone(r["ok"])                     # None = skipped, not False = broken


@unittest.skipIf(selftest is None, "selftest.py needs the librosa venv (.venv) — skipping")
class LiveCanary(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        selftest.RESULT = os.path.join(self.tmp, "selftest.json")
        selftest.CANARY = os.path.join(self.tmp, "canary.json")
        self.case = {"num": 1, "orig": "/x/1.wav", "name": "Dead Calm - Urban Style",
                     "extract": None, "cap": None, "cstart": 0, "mb": 0, "me": 300}

    def test_refuses_to_enshrine_a_stream_that_is_not_the_record(self):
        """THE WOLF-CRY GUARD. If the upload we found is the wrong record, the canary would fail
        forever and we would stop believing it. So a candidate canary is validated against the
        original we already hold, and rejected if it does not match."""
        with mock.patch.object(selftest, "cases", return_value=[self.case]), \
             mock.patch.object(selftest, "_search", return_value="https://y/wrong"), \
             mock.patch.object(selftest._cal, "chroma", return_value="C"), \
             mock.patch.object(selftest._audio, "load_audio", return_value=[0.0]), \
             mock.patch.object(selftest._cm, "match", return_value=(0.42, 0, 0.0)):  # nothing like it
            est = selftest.establish_canary(lambda url: ("C", None, None))
        self.assertFalse(est["ok"])
        self.assertIn("not the record", est["why"])
        self.assertFalse(os.path.exists(selftest.CANARY))   # and it is NOT saved

    def test_establishes_a_canary_that_does_match_our_own_copy(self):
        with mock.patch.object(selftest, "cases", return_value=[self.case]), \
             mock.patch.object(selftest, "_search", return_value="https://y/right"), \
             mock.patch.object(selftest._cal, "chroma", return_value="C"), \
             mock.patch.object(selftest._audio, "load_audio", return_value=[0.0]), \
             mock.patch.object(selftest._cm, "match", return_value=(0.006, 0, 0.0)):
            est = selftest.establish_canary(lambda url: ("C", None, None))
        self.assertTrue(est["ok"])
        self.assertEqual(est["url"], "https://y/right")
        self.assertTrue(os.path.exists(selftest.CANARY))

    def test_a_known_record_that_stops_matching_is_a_failure(self):
        """The whole point: a record we KNOW is the answer, fetched live, must come back a match.
        If it doesn't, the streaming path or the matcher is broken — and this is the only check
        that can tell us so."""
        selftest._save(selftest.CANARY, {"url": "https://y/known", "track": 1, "name": "known"})
        with mock.patch.object(selftest, "cases", return_value=[self.case]), \
             mock.patch.object(selftest._cal, "mix_query", return_value=[0.0] * 99999), \
             mock.patch.object(selftest._cal, "chroma", return_value="C"), \
             mock.patch.object(selftest._cm, "match", return_value=(0.31, 0, 0.0)):
            r = selftest.live(lambda url: ("C", None, None), mystery_queries=[])
        self.assertFalse(r["ok"])
        self.assertIn("broken", r["why"])

    def test_a_fetch_failure_on_a_known_good_url_is_a_failure_not_a_skip(self):
        """yt-dlp breaking is EXACTLY what this canary exists to catch. It must not be excused."""
        selftest._save(selftest.CANARY, {"url": "https://y/known", "track": 1, "name": "known"})
        with mock.patch.object(selftest, "cases", return_value=[self.case]):
            r = selftest.live(lambda url: (None, None, "yt-dlp: HTTP 403"), mystery_queries=[])
        self.assertFalse(r["ok"])
        self.assertIn("403", r["why"])

    def test_the_live_canary_passes_when_everything_works(self):
        selftest._save(selftest.CANARY, {"url": "https://y/known", "track": 1, "name": "known"})
        with mock.patch.object(selftest, "cases", return_value=[self.case]), \
             mock.patch.object(selftest._cal, "mix_query", return_value=[0.0] * 99999), \
             mock.patch.object(selftest._cal, "chroma", return_value="C"), \
             mock.patch.object(selftest._cm, "match", return_value=(0.009, 2, 41.0)):
            r = selftest.live(lambda url: ("C", None, None), mystery_queries=[])
        self.assertTrue(r["ok"])
        self.assertEqual(r["cost"], 0.009)


if __name__ == "__main__":
    unittest.main()
