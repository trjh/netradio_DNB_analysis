"""The harvester/collector split: fetch-half and fold-half, each owning only its own files.

All offline: stream_chroma and the matcher are stubbed; soundfile writes real (tiny) FLACs so
the retained-audio → excerpt path is exercised for real.
"""

import json
import os
import sys
import tempfile
import unittest
import unittest.mock

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)

import numpy as np                     # noqa: E402
import soundfile as sf                 # noqa: E402

import harvest                         # noqa: E402
import harvester                       # noqa: E402
import collector                       # noqa: E402
import sigstore                        # noqa: E402

URL = "https://www.youtube.com/watch?v=testvideo001"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        t = self.tmp.name
        # Re-point every path constant the three modules hold, on ALL their bindings.
        self._patches = []
        for mod, attr, val in [
            (harvest, "STATE", os.path.join(t, "state.json")),
            (harvest, "QUEUE", os.path.join(t, "queue.json")),
            (harvest, "CACHE", os.path.join(t, "cache")),
            (harvest, "KEEP", os.path.join(t, "keep")),
            (harvester, "HSTATE", os.path.join(t, "harvester_state.json")),
            (harvester, "JOBS", os.path.join(t, "jobs")),
            (harvester, "RESULTS", os.path.join(t, "results")),
            (harvester, "QUEUE", os.path.join(t, "queue.json")),
            (collector, "JOBS", os.path.join(t, "jobs")),
            (collector, "RESULTS", os.path.join(t, "results")),
            (collector, "STATE", os.path.join(t, "state.json")),
            (collector, "QUEUE", os.path.join(t, "queue.json")),
            (collector, "KEEP", os.path.join(t, "keep")),
        ]:
            p = unittest.mock.patch.object(mod, attr, val)
            p.start()
            self._patches.append(p)
        self.addCleanup(lambda: [p.stop() for p in self._patches])
        os.makedirs(os.path.join(t, "cache"), exist_ok=True)
        os.makedirs(os.path.join(t, "keep"), exist_ok=True)
        # sigstore dark by default in tests.
        os.environ.pop("NETRADIO_SIG_BUCKET", None)

    def _samples(self, seconds=70):
        rng = np.random.default_rng(1)
        return (rng.standard_normal(seconds * 16000) * 0.1).astype("float32")

    def _chroma(self):
        rng = np.random.default_rng(2)
        return rng.random((12, 500)).astype("float32")

    def _fake_fetch(self, c=None, samples=None, err=None):
        c = self._chroma() if c is None and err is None else c
        samples = self._samples() if samples is None and err is None else samples

        def fetch(url):
            if err is None:
                np.save(harvest.sig_path(url), c.astype("float16"))
            return (c, samples, err)
        return unittest.mock.patch.object(harvest, "stream_chroma", side_effect=fetch)


class TestHarvester(Base):
    def test_fetch_writes_job_dir_result_and_nothing_shared(self):
        hstate = harvester.blank_hstate()
        q = {"pending": [URL], "done": []}
        with self._fake_fetch():
            self.assertEqual(harvester.work_once(hstate, q), "fetched")
        jd = harvester.job_dir(URL)
        for name in ("url.json", "audio.flac", "sig.npy", "done"):
            self.assertTrue(os.path.exists(os.path.join(jd, name)), name)
        self.assertFalse([n for n in os.listdir(jd) if n.endswith(".part")])
        recs = os.listdir(harvester.RESULTS)
        self.assertEqual(len(recs), 1)
        with open(os.path.join(harvester.RESULTS, recs[0])) as fh:
            rec = json.load(fh)
        self.assertTrue(rec["ok"])
        # The shared files were NEVER touched:
        self.assertFalse(os.path.exists(collector.STATE))
        self.assertEqual(q, {"pending": [URL], "done": []})   # queue is read-only here
        self.assertEqual(hstate["analyzed"], 1)

    def test_already_held_skips_without_fetching(self):
        np.save(harvest.sig_path(URL), self._chroma().astype("float16"))
        hstate = harvester.blank_hstate()
        q = {"pending": [URL], "done": []}
        with self._fake_fetch() as m:
            self.assertEqual(harvester.work_once(hstate, q), "skipped")
            m.assert_not_called()
        self.assertEqual(len(os.listdir(harvester.RESULTS)), 1)   # result still submitted

    def test_refusal_ladder_strikes_then_blocks(self):
        hstate = harvester.blank_hstate()
        q = {"pending": [URL], "done": []}
        for i in range(harvester.BLOCK_AFTER):
            host = harvest.host_of(URL)
            hstate["hosts"].setdefault(host, {})["next_ok"] = 0    # bypass the backoff wait
            with self._fake_fetch(err="HTTP Error 403: Forbidden"):
                self.assertEqual(harvester.work_once(hstate, q), "waiting")
        hinfo = hstate["hosts"][harvest.host_of(URL)]
        self.assertTrue(hinfo["blocked"])
        self.assertEqual(hinfo["strikes"], harvester.BLOCK_AFTER)
        self.assertEqual(os.listdir(harvester.RESULTS) if os.path.isdir(harvester.RESULTS)
                         else [], [])                              # host trouble is not a result

    def test_bot_wall_halts(self):
        hstate = harvester.blank_hstate()
        q = {"pending": [URL], "done": []}
        with self._fake_fetch(err="Sign in to confirm you're not a bot"):
            self.assertEqual(harvester.work_once(hstate, q), "halted")
        self.assertIn("halted", hstate)

    def test_permanent_url_failure_submits_error_result(self):
        hstate = harvester.blank_hstate()
        q = {"pending": [URL], "done": []}
        with self._fake_fetch(err="unsupported url"):
            self.assertEqual(harvester.work_once(hstate, q), "fetched")
        with open(os.path.join(harvester.RESULTS, os.listdir(harvester.RESULTS)[0])) as fh:
            rec = json.load(fh)
        self.assertFalse(rec["ok"])
        self.assertIn("unsupported", rec["error"])


class TestCollector(Base):
    def _harvested(self):
        """Run one real harvester pass to lay down job dir + result."""
        hstate = harvester.blank_hstate()
        q = {"pending": [URL], "done": []}
        with self._fake_fetch():
            harvester.work_once(hstate, q)
        return q

    def test_fold_scores_excerpts_advances_and_cleans(self):
        q = self._harvested()
        state = harvest.blank_state()
        qs = [(4, self._chroma(), "MT4:deadbeef")]
        with unittest.mock.patch.object(collector, "_cm") as cm:
            cm.match.return_value = (0.031, 2, 12.0)     # a MATCH at 12s
            n = collector.collect_once(state, q, qs)
        self.assertEqual(n, 1)
        key = harvest._sig_key(URL)
        self.assertIn(key, state["scored"]["MT4:deadbeef"])
        self.assertEqual(len(state["matches"]), 1)
        hit = state["matches"][0]
        self.assertEqual(hit["verdict"], "MATCH")
        self.assertTrue(os.path.exists(hit["audio"]))     # the excerpt, cut from job audio
        self.assertEqual(q["pending"], [])
        self.assertEqual(q["done"], [URL])
        self.assertEqual(os.listdir(collector.RESULTS), [])          # spool drained
        self.assertFalse(os.path.isdir(os.path.join(collector.JOBS, key[:-4])))  # job gone
        self.assertTrue(os.path.exists(collector.STATE))             # state persisted

    def test_error_result_advances_queue_and_records_issue(self):
        os.makedirs(harvester.RESULTS, exist_ok=True)
        harvester.submit_result(URL, ok=False, error="unsupported url")
        state = harvest.blank_state()
        q = {"pending": [URL], "done": []}
        n = collector.collect_once(state, q, [])
        self.assertEqual(n, 1)
        self.assertEqual(state["errors"], 1)
        self.assertEqual(q["done"], [URL])

    def test_ok_result_with_missing_sig_is_left_for_later(self):
        os.makedirs(harvester.RESULTS, exist_ok=True)
        harvester.submit_result(URL, ok=True)             # ...but no sig anywhere
        state = harvest.blank_state()
        q = {"pending": [URL], "done": []}
        n = collector.collect_once(state, q, [])
        self.assertEqual(n, 0)
        self.assertEqual(len(os.listdir(collector.RESULTS)), 1)     # kept on the spool
        self.assertEqual(q["pending"], [URL])                        # queue NOT advanced

    def test_fold_persists_before_cleanup_ordering(self):
        """The crash-window P1: state+queue reach disk BEFORE the spool record or job dir go."""
        q = self._harvested()
        state = harvest.blank_state()
        qs = [(4, self._chroma(), "MT4:deadbeef")]
        seq = []
        real_save, real_unlink = collector._save, os.unlink
        def rec_save(path, data):
            seq.append(("save", os.path.basename(path)))
            return real_save(path, data)
        def rec_unlink(path, *a, **kw):
            seq.append(("unlink", os.path.basename(str(path))))
            return real_unlink(path, *a, **kw)
        with unittest.mock.patch.object(collector, "_cm") as cm, \
             unittest.mock.patch.object(collector, "_save", side_effect=rec_save), \
             unittest.mock.patch("os.unlink", side_effect=rec_unlink):
            cm.match.return_value = (0.031, 2, 12.0)
            collector.collect_once(state, q, qs)
        kinds = [k for k, _ in seq]
        self.assertIn("unlink", kinds)
        first_unlink = kinds.index("unlink")
        self.assertGreaterEqual(kinds[:first_unlink].count("save"), 2,
                                "state+queue must persist before any cleanup: %r" % seq)

    def test_crash_between_persist_and_cleanup_replays_without_loss(self):
        """Fold persisted, cleanup crashed: the replay finishes cleanup and double-scores
        nothing — and no audio was needed, because the hit + excerpt already landed."""
        q = self._harvested()
        state = harvest.blank_state()
        qs = [(4, self._chroma(), "MT4:deadbeef")]
        with unittest.mock.patch.object(collector, "_cm") as cm, \
             unittest.mock.patch.object(collector, "_cleanup"):   # cleanup "crashed"
            cm.match.return_value = (0.031, 2, 12.0)
            self.assertEqual(collector.collect_once(state, q, qs), 1)
        # Recovery inputs still on disk, fold durable:
        self.assertEqual(len(os.listdir(collector.RESULTS)), 1)
        self.assertEqual(q["done"], [URL])
        self.assertEqual(len(state["matches"]), 1)
        # The replay pass: cleanup-only — no second match row, no re-score.
        with unittest.mock.patch.object(collector, "_cm") as cm:
            cm.match.return_value = (0.031, 2, 12.0)
            self.assertEqual(collector.collect_once(state, q, qs), 1)
        self.assertEqual(len(state["matches"]), 1)
        self.assertEqual(state["analyzed"], 1)
        self.assertEqual(os.listdir(collector.RESULTS), [])
        key = harvest._sig_key(URL)
        self.assertFalse(os.path.isdir(os.path.join(collector.JOBS, key[:-4])))

    def test_crash_between_state_and_queue_saves_scores_exactly_once(self):
        """The two-file boundary: state persisted, queue save crashed. The replay must
        reconcile the queue and clean up WITHOUT scoring again."""
        q = self._harvested()
        state = harvest.blank_state()
        qs = [(4, self._chroma(), "MT4:deadbeef")]
        real_save = collector._save
        calls = {"n": 0}
        def crashing_save(path, data):
            real_save(path, data)
            calls["n"] += 1
            if calls["n"] == 1:                      # state.json landed; queue.json never does
                raise RuntimeError("power cut")
        with unittest.mock.patch.object(collector, "_cm") as cm, \
             unittest.mock.patch.object(collector, "_save", side_effect=crashing_save):
            cm.match.return_value = (0.031, 2, 12.0)
            with self.assertRaises(RuntimeError):
                collector.collect_once(state, q, qs)
        # Restart: BOTH files reloaded from disk, exactly as a real restart would see them.
        state2 = harvest._load(collector.STATE, harvest.blank_state())
        q2 = harvest._load(collector.QUEUE, {"pending": [URL], "done": []})
        self.assertEqual(q2["pending"], [URL])                    # queue never persisted
        self.assertIn(harvest._sig_key(URL), state2.get("folded", {}))
        with unittest.mock.patch.object(collector, "_cm") as cm:
            cm.match.return_value = (0.031, 2, 12.0)
            self.assertEqual(collector.collect_once(state2, q2, qs), 1)
        self.assertEqual(len(state2["matches"]), 1)               # exactly one, not two
        self.assertEqual(state2["analyzed"], 1)
        self.assertEqual(q2["done"], [URL])
        self.assertEqual(os.listdir(collector.RESULTS), [])
        self.assertEqual(state2.get("folded", {}), {})            # marker pruned after cleanup
        q3 = harvest._load(collector.QUEUE, {})
        self.assertEqual(q3["done"], [URL])                       # queue reconciled DURABLY

    def test_no_retained_audio_scores_but_cannot_excerpt(self):
        q = self._harvested()
        key = harvest._sig_key(URL)
        os.remove(os.path.join(collector.JOBS, key[:-4], "f1", "audio.flac"))
        state = harvest.blank_state()
        qs = [(4, self._chroma(), "MT4:deadbeef")]
        with unittest.mock.patch.object(collector, "_cm") as cm:
            cm.match.return_value = (0.031, 2, 12.0)
            collector.collect_once(state, q, qs)
        self.assertIn(key, state["scored"]["MT4:deadbeef"])          # scored regardless
        self.assertEqual(state["matches"], [])                       # but no clip-less hit row
        self.assertEqual(state["kept"], 0)


if __name__ == "__main__":
    unittest.main()
