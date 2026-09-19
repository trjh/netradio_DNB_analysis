"""The facts /harvest renders must actually be published — and .wv clips must be seen.

The 2026-07-29 harvest-page diagnosis found four lies on the dashboard, three of which
start here (the player renders what this repo writes):

  * Mystery Track 4's clip was wavpack-compacted and became INVISIBLE — `.wv` was missing
    from the clip whitelist even though ffmpeg decodes it natively, so the harvester ran
    with an empty query set while the page said "working".
  * the signature count read the local working cache (~1 file) when the pool lives in the
    bucket (~4,244) — `stamp_pool` publishes the bucket's count.
  * "compared: N of pool" needs the CURRENT clip's query key per mystery — a re-cut clip
    changes the key, so only the harvester can say which one is now (`state["query_keys"]`).
  * an empty query set made the process exit and respawn forever while /harvest showed the
    stale last phase — now a first-class `state["no_queries"]` + phase, self-clearing.
"""

import json
import os
import shutil
import sys
import tempfile
import types
import unittest
import unittest.mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))

from streamalign import mystery  # noqa: E402  (light: os/re/json only)

try:
    import numpy as np              # noqa: F401
    import harvest
except Exception:                   # audio deps absent -> the harvest-side tests skip
    harvest = None

# queries() itself does a lazy `import librosa` before any of our seams run, so the
# query-key test needs the real thing even with the chroma computation patched out.
try:
    import librosa                  # noqa: F401
    HAVE_LIBROSA = True
except ImportError:
    HAVE_LIBROSA = False


class WvClipsAreSeen(unittest.TestCase):
    """`.wv` is a first-class clip format, and lossless always beats lossy."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mystery_")
        self.meta = os.path.join(self.tmp, "track-metadata.json")
        with open(self.meta, "w") as fh:
            json.dump({"tracks": {"68": {"title": "Mystery Track 4"}}}, fh)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _clip_for(self, *names):
        for n in names:
            open(os.path.join(self.tmp, n), "wb").close()
        out = mystery.current(sources_dir=self.tmp, metadata_path=self.meta)
        self.assertEqual(len(out), 1)
        return os.path.basename(out[0]["clip"]) if out[0]["clip"] else None

    def test_a_wv_clip_enters_the_query_set(self):
        # THE bug: this exact file was invisible and MT4 silently left the search.
        self.assertEqual(self._clip_for("Mystery Track 4.wv"), "Mystery Track 4.wv")

    def test_lossless_wv_beats_a_lossy_reencode(self):
        self.assertEqual(self._clip_for("Mystery Track 4.mp3", "Mystery Track 4.wv"),
                         "Mystery Track 4.wv")

    def test_wav_still_wins_over_wv(self):
        self.assertEqual(self._clip_for("Mystery Track 4.wv", "Mystery Track 4.wav"),
                         "Mystery Track 4.wav")


@unittest.skipUnless(harvest, "harvest deps unavailable")
class PoolStamp(unittest.TestCase):
    def setUp(self):
        self._remote = harvest._remote_keys

    def tearDown(self):
        harvest._remote_keys = self._remote

    def test_stamps_the_bucket_count_and_reports_change(self):
        harvest._remote_keys = lambda max_age_s=900: {"a.npy", "b.npy", "c.npy"}
        state = {}
        self.assertTrue(harvest.stamp_pool(state))          # 3 is new
        self.assertEqual(state["pool"]["count"], 3)
        self.assertIn("at", state["pool"])
        self.assertFalse(harvest.stamp_pool(state))         # unchanged -> not worth a save

    def test_a_dark_or_unlistable_bucket_keeps_the_last_stamp(self):
        state = {"pool": {"count": 4244, "at": "2026-07-30T00:00:00+00:00"}}
        harvest._remote_keys = lambda max_age_s=900: None
        self.assertFalse(harvest.stamp_pool(state))
        self.assertEqual(state["pool"]["count"], 4244)      # the honest last stamp stands

    def test_the_stamp_is_the_count_and_the_canary_only(self):
        # The active/retired breakdown left with the queue-flag read it came from: every such
        # figure is the queue owner's own join of the ledger with its queue and the rulings
        # file now, so this stamp publishes the count and the canary and nothing derived. The
        # page renders the breakdown only while all three of its fields arrive as numbers,
        # so their absence reads as the plain "in the bucket" line -- never a crash.
        urls = ("https://y/active1", "https://y/active2", "https://y/ruled", "https://y/canary")
        harvest._remote_keys = lambda max_age_s=900: {harvest._sig_key(u) for u in urls}
        self.addCleanup(os.environ.pop, "NETRADIO_CANARY_URL", None)
        os.environ["NETRADIO_CANARY_URL"] = "https://y/canary"
        state = {}
        self.assertTrue(harvest.stamp_pool(state))
        p = state["pool"]
        self.assertEqual((p["count"], p["canary"]), (4, 1))
        self.assertEqual(sorted(p), ["at", "canary", "count"], "the stamp's shape")

    def test_a_canary_change_alone_is_worth_a_save(self):
        # Same COUNT, the canary's key arriving in the bucket: the stamp changed and must
        # persist even though the count never moved.
        urls = ("https://y/a", "https://y/b")
        harvest._remote_keys = lambda max_age_s=900: {harvest._sig_key(u) for u in urls}
        state = {}
        harvest.stamp_pool(state)
        canary_key = harvest._sig_key("https://y/canary")
        harvest._remote_keys = lambda max_age_s=900: {harvest._sig_key("https://y/a"),
                                                    harvest._sig_key("https://y/b"), canary_key}
        self.addCleanup(os.environ.pop, "NETRADIO_CANARY_URL", None)
        os.environ["NETRADIO_CANARY_URL"] = "https://y/canary"
        self.assertTrue(harvest.stamp_pool(state))
        self.assertEqual(state["pool"]["canary"], 1)
        self.assertFalse(harvest.stamp_pool(state))         # and settles once recorded

    def test_no_canary_configured_is_a_zero_not_an_omission(self):
        harvest._remote_keys = lambda max_age_s=900: {"a.npy"}
        self.addCleanup(os.environ.pop, "NETRADIO_CANARY_URL", None)
        os.environ.pop("NETRADIO_CANARY_URL", None)
        state = {}
        self.assertTrue(harvest.stamp_pool(state))
        self.assertEqual(state["pool"]["canary"], 0)


@unittest.skipUnless(harvest and HAVE_LIBROSA,
                     "librosa unavailable -- see requirements-streamalign.txt")
class QueryKeysArePublished(unittest.TestCase):
    def test_queries_publishes_the_current_key_per_mystery(self):
        state = {}
        clip = os.path.join(tempfile.mkdtemp(prefix="qk_"), "Mystery Track 4.wav")
        open(clip, "wb").close()
        fake_audio = types.SimpleNamespace(SR=harvest._audio.SR,
                                           duration=lambda p: 120.0,
                                           load_audio=lambda p: np.zeros(8, dtype="float32"))
        with unittest.mock.patch.object(harvest, "_mystery",
                                        types.SimpleNamespace(searchable=lambda: [
                                            {"number": 4, "clip": clip}])), \
             unittest.mock.patch.object(harvest, "_audio", fake_audio), \
             unittest.mock.patch.object(harvest.chroma_recipe, "compute_chroma",
                                        lambda y, **kw: np.zeros((12, 4), dtype="float32")), \
             unittest.mock.patch.object(harvest, "clip_fingerprint", lambda p: "f00"):
            qs = harvest.queries(state)
        self.assertEqual([n for n, _, _ in qs], [4])
        self.assertEqual(state["searching"], [4])
        self.assertEqual(state["query_keys"], {"4": "4:f00"})


@unittest.skipUnless(harvest, "harvest deps unavailable")
class NothingToSearchForIsAState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="noq_")
        self._paths = harvest.STATE, harvest.QUEUE, harvest.WRITER_LOCK, harvest.RULINGS
        harvest.STATE = os.path.join(self.tmp, "state.json")
        harvest.QUEUE = os.path.join(self.tmp, "queue.json")
        harvest.WRITER_LOCK = os.path.join(self.tmp, "collector.lock")
        # run() refuses to start while the rulings file is absent, and these tests need to get
        # PAST that refusal (they stop the run on the query set / the excerpt sweep).
        harvest.RULINGS = os.path.join(self.tmp, "rulings.json")
        harvest._save(harvest.RULINGS, {})
        # run() refuses to start while its caches are dark, and the test below needs to get
        # PAST that refusal (it stops the run on the excerpt sweep). A throwaway root, with
        # the harvester's registrations re-read onto it, put back afterwards.
        import cache_budget
        self._saved_env = {k: os.environ.get(k) for k in list(os.environ)
                           if k.startswith("NETRADIO_")}
        for k in self._saved_env:
            os.environ.pop(k, None)
        os.environ["NETRADIO_CACHE_ROOT"] = os.path.join(self.tmp, "root")
        self._registry = dict(cache_budget._REGISTRY), dict(cache_budget._STATS)
        harvest.register_caches()

    def tearDown(self):
        (harvest.STATE, harvest.QUEUE, harvest.WRITER_LOCK,
         harvest.RULINGS) = self._paths
        import cache_budget
        cache_budget._REGISTRY.clear()
        cache_budget._REGISTRY.update(self._registry[0])
        cache_budget._STATS.clear()
        cache_budget._STATS.update(self._registry[1])
        for k in [k for k in list(os.environ) if k.startswith("NETRADIO_")]:
            os.environ.pop(k, None)
        os.environ.update(self._saved_env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_an_empty_query_set_is_stamped_before_the_exit(self):
        with unittest.mock.patch.object(harvest, "queries", lambda state=None: []):
            harvest.run(None)                               # exits straight away
        state = harvest._load(harvest.STATE, {})
        self.assertIn("no_queries", state)
        self.assertIn("nothing to search for", state["no_queries"]["why"])
        self.assertEqual(state["session"]["phase"], "nothing to search for")

    def test_the_state_stands_down_when_searching_resumes(self):
        harvest._save(harvest.STATE, dict(harvest.blank_state(),
                                          no_queries={"at": "x", "why": "y"}))
        # a non-empty query set entering run() must clear the flag on disk immediately;
        # stop the run right after by making the halt-clear path blow up on our sentinel
        qs = [(4, None, "4:f00")]

        class _Stop(Exception):
            pass

        def boom(*a, **kw):
            raise _Stop()
        with unittest.mock.patch.object(harvest, "queries", lambda state=None: qs), \
             unittest.mock.patch.object(harvest, "sweep_excerpts", boom):
            with self.assertRaises(_Stop):
                harvest.run(None)
        self.assertNotIn("no_queries", harvest._load(harvest.STATE, {}))


if __name__ == "__main__":
    unittest.main()
