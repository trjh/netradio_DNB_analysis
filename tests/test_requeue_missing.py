"""Lost signatures regenerate — and mass loss reports instead of refetching.

`done` URLs are never re-fetched, so a signature missing from BOTH the working cache and the
bucket left its candidate permanently dark to every future mystery. `requeue_missing_sigs`
closes that hole; these tests pin its policy (Tim, 2026-07-30):

  * a small loss requeues automatically (and is idempotent),
  * a loss past the cap (10%, env-overridable) REPORTS — a standing `sig_alert` plus ONE
    issues row — and touches nothing,
  * an unlistable bucket means "cannot tell lost from evicted": do nothing at all,
  * ruled-on (retired) URLs are never requeued,
  * the alert stands down by itself when the condition stops holding.
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))

try:
    import harvest
    import numpy as np                  # harvest imports it anyway; used to write sig files
except Exception:                       # librosa/numpy absent -> not this test's job
    harvest = None


URLS = ["https://youtu.be/vid%02d" % i for i in range(10)]


@unittest.skipUnless(harvest, "harvest deps unavailable")
class RequeueBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="requeue_")
        self._cache = harvest.CACHE
        harvest.CACHE = os.path.join(self.tmp, "cache")
        os.makedirs(harvest.CACHE)
        self._enabled = harvest.sigstore.enabled
        self._remote = harvest._remote_keys
        harvest.sigstore.enabled = lambda: False       # cache-only unless a test says otherwise
        harvest._remote_keys = lambda max_age_s=900: None
        self._env = os.environ.pop("NETRADIO_REQUEUE_MISSING_CAP", None)

    def tearDown(self):
        harvest.CACHE = self._cache
        harvest.sigstore.enabled = self._enabled
        harvest._remote_keys = self._remote
        if self._env is not None:
            os.environ["NETRADIO_REQUEUE_MISSING_CAP"] = self._env
        else:
            os.environ.pop("NETRADIO_REQUEUE_MISSING_CAP", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def hold(self, url):
        """Write a real (tiny) signature file for `url` into the cache."""
        np.save(harvest.sig_path(url), np.zeros((12, 4), dtype="float16"))

    def q(self, done=URLS, pending=()):
        return {"pending": list(pending), "done": list(done)}


class SmallLossRequeues(RequeueBase):
    def test_lost_sig_is_requeued_and_the_call_is_idempotent(self):
        for u in URLS[1:]:
            self.hold(u)                               # 1 of 10 lost = 10%, NOT past the cap
        q, state = self.q(), {}
        res = harvest.requeue_missing_sigs(state, q, retired=set())
        self.assertEqual(res["requeued"], 1)
        self.assertNotIn(URLS[0], q["done"])
        self.assertIn(URLS[0], q["pending"])
        # again: the URL is now pending, done is clean — nothing further happens
        res2 = harvest.requeue_missing_sigs(state, q, retired=set())
        self.assertEqual(res2["requeued"], 0)
        self.assertEqual(q["pending"].count(URLS[0]), 1)

    def test_a_requeued_url_is_never_duplicated_in_pending(self):
        for u in URLS[1:]:
            self.hold(u)
        q = self.q(pending=[URLS[0]])                  # already pending (e.g. a prior run)
        harvest.requeue_missing_sigs({}, q, retired=set())
        self.assertEqual(q["pending"].count(URLS[0]), 1)
        self.assertNotIn(URLS[0], q["done"])

    def test_retired_urls_are_not_requeued(self):
        for u in URLS[1:]:
            self.hold(u)                               # URLS[0] lost — but ruled on
        q = self.q()
        res = harvest.requeue_missing_sigs({}, q, retired={URLS[0]})
        self.assertEqual(res["requeued"], 0)
        self.assertIn(URLS[0], q["done"])              # left exactly where it was


class MassLossReports(RequeueBase):
    def test_mass_loss_reports_once_and_touches_nothing(self):
        for u in URLS[:2]:
            self.hold(u)                               # 8 of 10 lost
        q, state = self.q(), {}
        res = harvest.requeue_missing_sigs(state, q, retired=set())
        self.assertTrue(res["reported"])
        self.assertEqual(res["requeued"], 0)
        self.assertEqual(q["done"], URLS)              # untouched
        self.assertEqual(q["pending"], [])
        self.assertIn("sig_alert", state)
        self.assertEqual(state["sig_alert"]["missing"], 8)
        rows = [i for i in state["issues"] if i["issue"].startswith("missing-sigs:")]
        self.assertEqual(len(rows), 1)
        # a supervisor respawn re-checks: the alert stands, the issues row is NOT repeated
        harvest.requeue_missing_sigs(state, q, retired=set())
        rows = [i for i in state["issues"] if i["issue"].startswith("missing-sigs:")]
        self.assertEqual(len(rows), 1)

    def test_the_alert_stands_down_when_the_loss_is_dealt_with(self):
        q, state = self.q(), {}
        harvest.requeue_missing_sigs(state, q, retired=set())      # all 10 lost -> alert
        self.assertIn("sig_alert", state)
        for u in URLS:
            self.hold(u)                               # the human restored the store
        res = harvest.requeue_missing_sigs(state, q, retired=set())
        self.assertTrue(res["cleared"])
        self.assertNotIn("sig_alert", state)

    def test_the_cap_is_env_overridable_for_a_deliberate_mass_regen(self):
        os.environ["NETRADIO_REQUEUE_MISSING_CAP"] = "1"
        q, state = self.q(), {}
        res = harvest.requeue_missing_sigs(state, q, retired=set())
        self.assertEqual(res["requeued"], len(URLS))
        self.assertEqual(q["done"], [])
        self.assertEqual(q["pending"], URLS)


class WriterLockAndStartup(RequeueBase):
    """The ONE-writer lock is shared by every queue/state writer, and every writer runs the
    startup recovery — including the split runtime's collector."""

    def setUp(self):
        super().setUp()
        self._paths = harvest.WRITER_LOCK, harvest.STATE, harvest.QUEUE, harvest.listen_queue_split
        harvest.WRITER_LOCK = os.path.join(self.tmp, "collector.lock")
        harvest.STATE = os.path.join(self.tmp, "state.json")
        harvest.QUEUE = os.path.join(self.tmp, "queue.json")
        harvest.listen_queue_split = lambda: ([], set())

    def tearDown(self):
        (harvest.WRITER_LOCK, harvest.STATE, harvest.QUEUE,
         harvest.listen_queue_split) = self._paths
        super().tearDown()

    def test_the_writer_lock_is_exclusive_until_released(self):
        first = harvest.acquire_writer_lock()
        self.assertIsNotNone(first)
        self.assertIsNone(harvest.acquire_writer_lock())   # held -> a second writer refuses
        first.close()                                      # the lock dies with the file
        second = harvest.acquire_writer_lock()
        self.assertIsNotNone(second)
        second.close()

    def test_startup_recovery_requeues_through_the_real_files(self):
        for u in URLS[1:]:
            self.hold(u)
        harvest._save(harvest.QUEUE, self.q())
        res = harvest.recover_missing_sigs_at_start()
        self.assertEqual(res["requeued"], 1)
        on_disk = harvest._load(harvest.QUEUE, {})
        self.assertIn(URLS[0], on_disk["pending"])
        self.assertNotIn(URLS[0], on_disk["done"])

    def test_startup_recovery_persists_the_alert_on_mass_loss(self):
        harvest._save(harvest.QUEUE, self.q())             # every sig lost
        res = harvest.recover_missing_sigs_at_start()
        self.assertTrue(res["reported"])
        self.assertIn("sig_alert", harvest._load(harvest.STATE, {}))
        self.assertEqual(harvest._load(harvest.QUEUE, {})["done"], URLS)

    def test_every_writer_takes_the_lock_and_runs_the_recovery(self):
        # A source-level pin: Mode A, the split collector, and the CLI must all go through
        # acquire_writer_lock() and recover_missing_sigs_at_start(). If one of them stops,
        # the split runtime silently loses the recovery (the original review finding).
        import inspect
        try:
            import collector
        except Exception:
            self.skipTest("collector deps unavailable")
        # same lock FILE, not a twin: collector aliases harvest's, whatever its value was
        # at import time (tests re-point harvest.WRITER_LOCK, so compare source not value)
        self.assertIn("LOCK = harvest.WRITER_LOCK", inspect.getsource(collector))
        run_a = inspect.getsource(harvest.run)
        run_split = inspect.getsource(collector.run)
        cli = inspect.getsource(harvest.main)
        for src in (run_a, run_split, cli):
            self.assertIn("acquire_writer_lock()", src)
            self.assertIn("recover_missing_sigs_at_start(", src)


class BucketSemantics(RequeueBase):
    def test_a_bucket_held_sig_is_not_lost(self):
        harvest.sigstore.enabled = lambda: True
        held = {harvest._sig_key(u) for u in URLS}     # everything evicted to the bucket
        harvest._remote_keys = lambda max_age_s=900: held
        q = self.q()
        res = harvest.requeue_missing_sigs({}, q, retired=set())
        self.assertEqual(res["requeued"], 0)
        self.assertEqual(q["done"], URLS)

    def test_an_unlistable_bucket_means_do_nothing_at_all(self):
        harvest.sigstore.enabled = lambda: True
        harvest._remote_keys = lambda max_age_s=900: None
        q, state = self.q(), {"sig_alert": {"at": "x"}}
        res = harvest.requeue_missing_sigs(state, q, retired=set())
        self.assertEqual(res["requeued"], 0)
        self.assertFalse(res["reported"])
        self.assertEqual(q["done"], URLS)              # not requeued
        self.assertIn("sig_alert", state)              # and not cleared — we cannot tell
        self.assertIn("cannot tell lost from evicted", res["why"])


if __name__ == "__main__":
    unittest.main()
