"""The harvester asks with sliding windows, not with the first 120 s of a clip.

A mystery clip is a stretch of broadcast, and the broadcast plays a record's opening mixed under
the record before it. So the query is 120 s windows, a new one every 15 s, the last placed to end
at the clip's end; the lowest cost over the windows is the pair's score; a pair that clears
MATCH_COST meets a deciding pass (a band-limited path and the peak-to-sidelobe ratio); and every
verdict reached the old way is stale. These tests pin each of those, and that a key in the retired
set is still never asked again.

The last class is a real-audio check: original 071 against capture d356-375, where the record's
clear part starts about 105 s after its seat. It must be found with the new query and missed with
the old one. It skips when the audio is not on this machine.
"""

import glob
import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)

try:
    import numpy as np
    import harvest
    from streamalign import chroma_match as cm
except Exception:                       # numpy/librosa absent -> not this test's job
    harvest = None


# The real alignments need librosa; CI runs without it.
HAVE_LIBROSA = importlib.util.find_spec("librosa") is not None


def _frames(seconds):
    return int(round(seconds * cm.FPS))


@unittest.skipUnless(harvest, "needs the analysis venv")
class WindowPlacement(unittest.TestCase):
    """Windows start at 0 and every 15 s, and the last one ends where the clip ends."""

    def test_a_five_minute_clip(self):
        n = _frames(300)
        width = _frames(120)
        starts = cm.window_starts(n)
        self.assertEqual(starts[0], 0)
        # every 15 s up to the last full step, each start rounded to the nearest frame
        regular = starts[:-1]
        self.assertEqual(regular, [int(round(k * 15 * cm.FPS)) for k in range(len(regular))])
        self.assertEqual(len(regular), 12, "0, 15, ... 165 s: twelve regular windows")
        self.assertEqual(starts[-1] + width, n, "the last window ends at the clip's end")
        self.assertEqual(len(starts), 13)
        self.assertEqual(starts, sorted(set(starts)), "no window twice")

    def test_a_clip_whose_tail_falls_on_the_step(self):
        # 120 + 15k seconds: the last regular start would BE the aligned one, so it is not
        # added twice.
        n = int(round(120 * cm.FPS)) + int(round(30 * cm.FPS))
        starts = cm.window_starts(n)
        self.assertEqual(starts[-1] + _frames(120), n)
        self.assertEqual(starts, sorted(set(starts)))

    def test_a_clip_no_longer_than_a_window_is_one_window_the_whole_clip(self):
        for seconds in (60, 90, 120):
            q = np.ones((12, _frames(seconds)), dtype="float32")
            ws = cm.windows(q)
            self.assertEqual(len(ws), 1, seconds)
            self.assertEqual(ws[0][0], 0.0)
            self.assertEqual(ws[0][1].shape, q.shape, "the window is the whole clip")

    def test_a_clip_one_frame_longer_than_a_window_is_two(self):
        self.assertEqual(cm.window_starts(_frames(120) + 1), [0, 1])

    def test_the_harvester_refuses_a_clip_below_the_floor_as_before(self):
        self.assertEqual(harvest.MIN_QUERY_S, 60.0)
        self.assertEqual((harvest.QUERY_S, harvest.QUERY_STEP_S), (120.0, 15.0))

    def test_a_pass_spends_one_minute_re_asking(self):
        # A budget of time, not of pairs: a pair costs seconds against a track and minutes
        # against a long mix (decided 2026-10-11).
        self.assertEqual(harvest.RESCAN_BUDGET_S, 60.0)
        self.assertFalse(hasattr(harvest, "RESCAN_PER_PASS"))


@unittest.skipUnless(harvest, "needs the analysis venv")
class BestWindowWins(unittest.TestCase):
    """The lowest cost over the windows is the score, and its window's offset is recorded."""

    TARGET_S = 90.0

    def _query(self, seconds=300):
        # The first chroma bin carries each frame's own index, so a fake matcher can tell windows apart.
        q = np.ones((12, _frames(seconds)), dtype="float32")
        q[0, :] = np.arange(q.shape[1])
        return q

    def _fake_match(self, calls):
        target = _frames(self.TARGET_S)

        def fake(q, c):
            calls.append(int(q[0, 0]))
            return 0.004 + abs(int(q[0, 0]) - target) / 10000.0, 3, 41.0
        return fake

    def test_the_cheapest_window_wins_and_its_offset_comes_back(self):
        calls = []
        with unittest.mock.patch.object(cm, "match", self._fake_match(calls)):
            cost, shift, at, offset = cm.match_sliding(self._query(), None)
        self.assertEqual(len(calls), 13, "every window was scored")
        self.assertAlmostEqual(offset, self.TARGET_S, delta=0.2)
        self.assertAlmostEqual(cost, 0.004, places=3)
        self.assertEqual((shift, at), (3, 41.0), "the winning window's rotation and position")

    def test_the_row_records_the_offset_in_the_clip_and_the_position_in_the_candidate(self):
        calls = []
        with unittest.mock.patch.object(cm, "match", self._fake_match(calls)), \
                unittest.mock.patch.object(cm, "match_band",
                                           lambda q, c, tries=None: (0.003, 0, 41.0, 9.5)):
            res = harvest.score_pair(self._query(), None)
        self.assertEqual(res["verdict"], "MATCH")
        self.assertAlmostEqual(res["query_offset_s"], self.TARGET_S, delta=0.2)
        self.assertEqual(res["at_s"], 41.0, "at_s stays the position in the candidate")
        self.assertEqual(res["psr"], 9.5)

    def test_a_low_ratio_turns_a_match_into_a_near(self):
        with unittest.mock.patch.object(cm, "match", return_value=(0.02, 0, 5.0)), \
                unittest.mock.patch.object(cm, "match_band",
                                           lambda q, c, tries=None: (0.03, 0, 5.0, 1.2)):
            res = harvest.score_pair(self._query(), None)
        self.assertEqual(res["verdict"], "near", "a ratio under PSR_MATCH withholds MATCH")
        self.assertEqual(res["cost"], 0.02, "the lead keeps its screen cost")

    def test_no_ratio_leaves_the_cost_to_decide(self):
        with unittest.mock.patch.object(cm, "match", return_value=(0.02, 0, 5.0)), \
                unittest.mock.patch.object(cm, "match_band",
                                           lambda q, c, tries=None: (None, None, None, None)):
            res = harvest.score_pair(self._query(), None)
        self.assertEqual(res["verdict"], "MATCH")
        self.assertIsNone(res["psr"])
        self.assertEqual(res["psr_note"], harvest.PSR_NO_SIDELOBE,
                         "the pass ran; the candidate had no sidelobe")

    def test_a_pair_the_pass_never_ran_on_says_so(self):
        with unittest.mock.patch.object(cm, "match", return_value=(0.08, 0, 5.0)):
            res = harvest.score_pair(self._query(), None)
        self.assertIsNone(res["psr"])
        self.assertEqual(res["psr_note"], harvest.PSR_NOT_RUN)

    def test_a_ratio_carries_no_note(self):
        with unittest.mock.patch.object(cm, "match", return_value=(0.02, 0, 5.0)), \
                unittest.mock.patch.object(cm, "match_band",
                                           lambda q, c, tries=None: (0.01, 0, 5.0, 9.0)):
            res = harvest.score_pair(self._query(), None)
        self.assertEqual((res["psr"], res["psr_note"]), (9.0, None))

    def test_the_deciding_pass_runs_only_on_survivors(self):
        band = unittest.mock.Mock(return_value=(0.0, 0, 0.0, 9.0))
        with unittest.mock.patch.object(cm, "match", return_value=(0.08, 0, 5.0)), \
                unittest.mock.patch.object(cm, "match_band", band):
            res = harvest.score_pair(self._query(), None)
        self.assertEqual(res["verdict"], "near")
        band.assert_not_called()
        with unittest.mock.patch.object(cm, "match", return_value=(0.5, 0, 5.0)), \
                unittest.mock.patch.object(cm, "match_band", band):
            self.assertIsNone(harvest.score_pair(self._query(), None),
                              "above KEEP_CEILING there is no row at all")
        band.assert_not_called()


@unittest.skipUnless(harvest, "needs the analysis venv")
@unittest.skipUnless(HAVE_LIBROSA, "needs librosa (requirements-streamalign.txt)")
class TheDecidingPass(unittest.TestCase):
    """The band-limited path and the ratio, on synthetic chroma (real DTW, small sizes)."""

    def setUp(self):
        self.rng = np.random.default_rng(7)
        self.cand = self.rng.random((12, 400)).astype("float32") + 1e-3

    def test_a_true_copy_is_a_sharp_minimum(self):
        q = self.cand[:, 250:290] + 0.01 * self.rng.random((12, 40)).astype("float32")
        cost, shift, at, psr = cm.match_band(q, self.cand)
        self.assertEqual(shift, 0)
        self.assertAlmostEqual(at, 250 / cm.FPS, delta=2 / cm.FPS)
        self.assertGreater(psr, harvest.PSR_MATCH)

    def test_an_unrelated_query_has_a_ratio_near_one(self):
        q = self.rng.random((12, 40)).astype("float32") + 1e-3
        _cost, _shift, _at, psr = cm.match_band(q, self.cand)
        self.assertLess(psr, harvest.PSR_MATCH)

    def test_a_candidate_with_no_room_for_a_sidelobe_has_no_ratio(self):
        q = self.cand[:, 10:50]
        self.assertIsNone(cm.match_band(q, self.cand[:, :60])[3])

    def test_a_candidate_shorter_than_the_window_is_refused_not_raised(self):
        # librosa swaps the inputs when the candidate is the shorter one; the guard must
        # refuse first, at any length between 6/7 of the window and the window.
        rng = np.random.default_rng(3)
        q = rng.random((12, 938)).astype("float32") + 1e-3
        for frames in (810, 900, 937):
            c = rng.random((12, frames)).astype("float32") + 1e-3
            self.assertEqual(cm.match_band(q, c), (None, None, None, None), frames)

    def test_a_stand_in_query_is_not_scored(self):
        self.assertEqual(cm.match_band(None, self.cand), (None, None, None, None))


@unittest.skipUnless(harvest, "needs the analysis venv")
class EveryOldScoreIsStale(unittest.TestCase):
    """The query key carries the shape; the old leads and pairings go once."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sliding-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cache = os.path.join(self.tmp, "chroma")
        os.makedirs(self.cache)
        p = unittest.mock.patch.object(harvest, "_chroma_dir", lambda: self.cache)
        p.start()
        self.addCleanup(p.stop)

    def _key(self, ch):
        return "u" + ch * 20

    def _held(self, *keys):
        ledger = {}
        for k in keys:
            np.save(os.path.join(self.cache, k + ".npy"), np.ones((12, 8), dtype="float32"))
            ledger[k] = {"status": "signed"}
        return ledger

    def test_the_key_carries_the_shape(self):
        self.assertEqual(harvest.QUERY_SHAPE, "w120s15")
        self.assertEqual(harvest.query_key(4, "abc"), "4:abc:w120s15")

    def test_a_pair_scored_under_the_old_key_is_asked_again(self):
        a = self._key("a")
        ledger = self._held(a)
        state = {"scored": {"4:abc": [a + ".npy"]}}          # the old, head-only key
        qs = [(4, None, harvest.query_key(4, "abc"))]
        self.assertEqual([p[3] for p in harvest.unscored_pairs(state, ledger, set(), qs)], [a])

    def test_the_migration_drops_every_old_lead_and_pairing_once(self):
        a, b, r = self._key("a"), self._key("b"), self._key("c")
        state = {"kept": 2, "scored": {"4:abc": [a + ".npy"],
                                       harvest.query_key(4, "abc"): [b + ".npy"]},
                 "matches": [{"mystery": 4, "key": a, "cost": 0.04, "verdict": "MATCH",
                              "audio": os.path.join(self.tmp, "MT4-a.wav")},
                             {"mystery": 4, "key": r, "cost": 0.03, "verdict": "MATCH"}],
                 "current_query": {"mystery": 4, "query_key": "4:abc"}}
        removed = []
        with unittest.mock.patch.object(harvest.cache_budget, "remove",
                                        lambda cache, path, why: removed.append(why) or True):
            first = harvest.migrate_query_shape(state)
            snapshot = repr(state)
            second = harvest.migrate_query_shape(state)
        self.assertEqual(first, (2, 1))
        self.assertEqual(state["matches"], [],
                         "every old lead goes, the ruled-on one too: past findings are not kept")
        self.assertEqual(list(state["scored"]), [harvest.query_key(4, "abc")])
        self.assertNotIn("current_query", state)
        self.assertEqual(state["query_shape"], "w120s15")
        self.assertEqual(removed, ["query-shape-changed"], "the dropped lead's excerpt went")
        self.assertEqual(state["kept"], 0)
        self.assertIsNone(second, "the second call does nothing")
        self.assertEqual(repr(state), snapshot)

    def test_a_retired_key_is_not_asked_again_after_the_key_change(self):
        free, retired = self._key("d"), self._key("e")
        ledger = self._held(free, retired)
        state = {"matches": [], "kept": 0,
                 "scored": {"4:abc": [free + ".npy", retired + ".npy"]}}
        harvest.migrate_query_shape(state)
        qs = [(4, None, harvest.query_key(4, "abc"))]
        pairs = harvest.unscored_pairs(state, ledger, {retired}, qs)
        self.assertEqual([p[3] for p in pairs], [free])
        loaded = []
        real_load = harvest._load_sig
        with unittest.mock.patch.object(harvest, "_load_sig",
                                        lambda k: loaded.append(k) or real_load(k)), \
                unittest.mock.patch.object(cm, "match", return_value=(1.0, 0, 0.0)):
            harvest.rescan(state, ledger, {retired}, qs, verbose=False)
        self.assertEqual(loaded, [free], "the retired key's signature was never even loaded")
        self.assertEqual(state["scored"][qs[0][2]], [free + ".npy"])


@unittest.skipUnless(harvest, "needs the analysis venv")
class TheRescanMinute(unittest.TestCase):
    """The loop's re-ask runs on a clock: a minute a pass, at least one pair, and a pair
    that overruns finishes. A stop is noticed between pairs."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="minute-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cache = os.path.join(self.tmp, "chroma")
        os.makedirs(self.cache)
        self.keys = ["u%020d" % i for i in range(6)]
        self.ledger = {}
        for k in self.keys:
            np.save(os.path.join(self.cache, k + ".npy"), np.ones((12, 8), dtype="float32"))
            self.ledger[k] = {"status": "signed"}
        self.now = [0.0]
        for name, value in (("_chroma_dir", lambda: self.cache),
                            ("_clock", lambda: self.now[0])):
            p = unittest.mock.patch.object(harvest, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(harvest._STOP.update, {"signum": 0})

    def _pair_takes(self, seconds, stop_after=None):
        scored = []

        def fake(qc, c):
            scored.append(1)
            self.now[0] += seconds
            if stop_after and len(scored) >= stop_after:
                harvest._STOP["signum"] = 15
            return None
        p = unittest.mock.patch.object(harvest, "score_pair", fake)
        p.start()
        self.addCleanup(p.stop)
        return scored

    def _rescan(self, **kw):
        state = {"matches": [], "kept": 0, "scored": {}}
        n = harvest.rescan(state, self.ledger, set(), [(4, None, "4:fp:w120s15")],
                           verbose=False, **kw)
        return n, state

    def test_pairs_are_scored_until_the_minute_is_spent(self):
        scored = self._pair_takes(25.0)
        n, _state = self._rescan(budget_s=harvest.RESCAN_BUDGET_S)
        self.assertEqual((n, len(scored)), (3, 3), "0, 25 and 50 s start a pair; 75 s does not")

    def test_at_least_one_pair_and_an_overrun_finishes(self):
        scored = self._pair_takes(600.0)
        n, state = self._rescan(budget_s=harvest.RESCAN_BUDGET_S)
        self.assertEqual((n, len(scored)), (1, 1),
                         "one pair, though it ran ten times the budget")
        self.assertEqual(len(state["scored"]["4:fp:w120s15"]), 1, "and it was recorded")

    def test_even_a_spent_budget_scores_one_pair(self):
        self._pair_takes(1.0)
        n, _state = self._rescan(budget_s=0.0)
        self.assertEqual(n, 1, "always at least one pair")

    def test_without_a_budget_every_pair_is_scored(self):
        self._pair_takes(600.0)
        n, _state = self._rescan()
        self.assertEqual(n, len(self.keys))

    def test_a_stop_ends_the_rescan_between_pairs(self):
        scored = self._pair_takes(1.0, stop_after=2)
        n, state = self._rescan()
        self.assertEqual((n, len(scored)), (2, 2), "the pair in hand finished; no third began")
        self.assertEqual(len(state["scored"]["4:fp:w120s15"]), 2, "both are in the state")

    def test_the_checkpoint_runs_as_time_passes(self):
        self._pair_takes(harvest.RESCAN_SAVE_EVERY_S)
        saves = []
        self._rescan(checkpoint=lambda: saves.append(self.now[0]))
        self.assertEqual(len(saves), len(self.keys))

    def test_the_estimate_scales_with_the_candidate(self):
        q = np.ones((12, _frames(300)), dtype="float32")
        short, long_ = self.keys[0], self.keys[1]
        ledger = {short: {"duration_s": 360.0}, long_: {"duration_s": 3600.0}}
        with unittest.mock.patch.object(harvest, "_chroma_dir", lambda: None):
            a = harvest.rescan_estimate_s([(4, q, "k", short)], ledger)
            b = harvest.rescan_estimate_s([(4, q, "k", long_)], ledger)
            c = harvest.rescan_estimate_s([(4, q, "k", "u" + "f" * 20)], ledger)
        self.assertAlmostEqual(b / a, 10.0, places=6, msg="ten times the candidate, ten times the time")
        self.assertAlmostEqual(a, 13 * 6 * harvest.SCORE_S_PER_WINDOW_MINUTE, places=6)
        self.assertAlmostEqual(c, 13 * harvest.DEFAULT_CANDIDATE_MIN
                               * harvest.SCORE_S_PER_WINDOW_MINUTE, places=6)


def _real_audio():
    """(original 071, capture d356-375) when both are on this machine, else None."""
    sources = os.environ.get("NETRADIO_SOURCES_DIR") or ""
    captures = os.environ.get("NETRADIO_MP3_DIR") or ""
    orig = sorted(glob.glob(os.path.join(sources, "071-*.flac"))
                  + glob.glob(os.path.join(sources, "071-*.wav"))) if sources else []
    cap = sorted(glob.glob(os.path.join(captures, "d356-375.*"))) if captures else []
    return (orig[0], cap[0]) if orig and cap else None


@unittest.skipUnless(harvest, "needs the analysis venv")
@unittest.skipUnless(HAVE_LIBROSA, "needs librosa (requirements-streamalign.txt)")
class OriginalSeventyOneOnD356(unittest.TestCase):
    """071 (Fokus - On Line) is seated at 913.6 s on d356-375, and its clear part starts about
    105 s later: the longest mixed front the labels record. A clip cut from the seat must be
    a MATCH with the sliding query and must not be one with the first 120 s alone."""

    SEAT_S = 913.6
    CLIP_S = 300.0

    def test_found_with_the_new_query_and_missed_with_the_old(self):
        found = _real_audio()
        if not found:
            self.skipTest("set NETRADIO_SOURCES_DIR (original 071) and NETRADIO_MP3_DIR "
                          "(capture d356-375) to run the real-audio check")
        orig, cap = found
        import chroma_recipe
        from streamalign import audio
        y = audio.load_audio(cap)
        clip = y[int(self.SEAT_S * audio.SR):int((self.SEAT_S + self.CLIP_S) * audio.SR)]
        cand = chroma_recipe.compute_chroma(audio.load_audio(orig))

        old_cost = cm.match(chroma_recipe.compute_chroma(clip[:int(120 * audio.SR)]), cand)[0]
        self.assertGreater(old_cost, harvest.MATCH_COST, "the first 120 s alone misses it")

        res = harvest.score_pair(chroma_recipe.compute_chroma(clip), cand)
        self.assertEqual(res["verdict"], "MATCH", res)
        self.assertGreaterEqual(res["query_offset_s"], 90.0,
                                "the winning window lies past the mixed front")


if __name__ == "__main__":
    unittest.main()
