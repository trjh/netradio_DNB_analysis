"""The cache policy: the registry, reserve/commit/remove, the eviction run and the disk floor.

A copy of this suite travels with the module wherever it is registered (this repo's copy adjusts
only the import path). Hermetic: one temporary directory per cache under a temporary root, and the volume faked through
the `_disk_usage` seam so the floor can be put anywhere. The fake volume's "used" figure is a
fixed base plus every byte under the temporary tree, so an eviction moves it the way a real
deletion would.
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))

import cache_budget as cb  # noqa: E402

KB = 1000
ENV_NAMES = ("NETRADIO_CACHE_ROOT", "NETRADIO_DOWNLOAD_ROOT", "NETRADIO_DISK_MAX_PCT",
             "NETRADIO_CACHE_EVENTS_DAYS")


class CacheBudgetBase(unittest.TestCase):
    VOLUME = 100 * KB           # the fake volume's size, used + free

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        saved = {k: os.environ.get(k) for k in os.environ if k.startswith("NETRADIO_")
                 and ("CACHE" in k or k in ENV_NAMES)}
        for k in saved:
            os.environ.pop(k)
        self.addCleanup(self._restore_env, saved)
        self.root = os.path.join(self.tmp, "cache")
        os.environ["NETRADIO_CACHE_ROOT"] = self.root
        self.addCleanup(self._restore_registry, dict(cb._REGISTRY), dict(cb._STATS))
        cb._REGISTRY.clear()
        cb._STATS.clear()
        self.base = 0               # bytes "used" on the fake volume outside the temp tree
        self.addCleanup(setattr, cb, "_disk_usage", cb._disk_usage)
        cb._disk_usage = self._fake_disk_usage

    @staticmethod
    def _restore_env(saved):
        for k in [k for k in os.environ if k.startswith("NETRADIO_")
                  and ("CACHE" in k or k in ENV_NAMES)]:
            os.environ.pop(k)
        for k, v in saved.items():
            os.environ[k] = v

    @staticmethod
    def _restore_registry(reg, stats):
        cb._REGISTRY.clear()
        cb._REGISTRY.update(reg)
        cb._STATS.clear()
        cb._STATS.update(stats)

    def _tree_bytes(self):
        total = 0
        for dirpath, _dirs, files in os.walk(self.tmp):
            for f in files:
                if f.endswith(".jsonl") or f == cb.LOCK_NAME:
                    continue
                total += os.path.getsize(os.path.join(dirpath, f))
        return total

    def _fake_disk_usage(self, _path):
        used = self.base + self._tree_bytes()
        return (self.VOLUME, used, self.VOLUME - used)

    def cache(self, name, **kw):
        kw.setdefault("dir", os.path.join(self.tmp, name))
        os.makedirs(kw["dir"], exist_ok=True)
        return cb.register(name, **kw)

    def file(self, rec, name, size=KB, age_s=0):
        path = os.path.join(rec["dir"], name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(b"x" * size)
        t = time.time() - age_s
        os.utime(path, (t, t))
        return path

    def names(self, rec):
        return sorted(os.listdir(rec["dir"]))

    def events(self):
        path = cb.events_path()
        if not os.path.isfile(path):
            return []
        with open(path) as fh:
            return [json.loads(line) for line in fh]


class Dark(CacheBudgetBase):
    """No NETRADIO_CACHE_ROOT: the module records nothing and changes nothing."""

    def setUp(self):
        super().setUp()
        os.environ.pop("NETRADIO_CACHE_ROOT")

    def test_everything_is_a_no_op(self):
        d = os.path.join(self.tmp, "clips")
        os.makedirs(d)
        self.assertIsNone(cb.register("clips", dir=d, cap=1))
        self.assertFalse(cb.registered("clips"))
        self.assertIsNone(cb.dir_of("clips"))
        with open(os.path.join(d, "a"), "wb") as fh:
            fh.write(b"x" * 10)
        self.assertTrue(cb.reserve("clips", 10 ** 12))
        cb.commit("clips", os.path.join(d, "a"))
        self.assertFalse(cb.remove("clips", os.path.join(d, "a"), "test"))
        self.assertFalse(cb.run_eviction()["ran"])
        st = cb.status()
        self.assertFalse(st["enabled"])
        self.assertIn("NETRADIO_CACHE_ROOT", st["reason"])
        self.assertEqual(os.listdir(d), ["a"])
        self.assertFalse(os.path.exists(self.root))       # no lock file, no event log


class Environment(CacheBudgetBase):
    """The variables of the one contract, read by the names given there."""

    def test_defaults(self):
        rec = self.cache("thing")
        self.assertEqual(rec["cap"], 4 * cb.GB)
        self.assertEqual(rec["headroom"], 0)
        self.assertIsNone(rec["max_age"])
        self.assertEqual(rec["order"], "oldest-added")
        self.assertEqual(cb.disk_max_pct(), 82)
        self.assertEqual(cb.events_days(), 30)

    def test_the_default_dir_is_under_the_root(self):
        rec = cb.register("thing")
        self.assertEqual(rec["dir"], os.path.join(self.root, "thing"))

    def test_the_environment_overrides_the_registration(self):
        os.environ["NETRADIO_STREAM_MP3_CACHE_GB"] = "0.25"
        os.environ["NETRADIO_STREAM_MP3_CACHE_HEADROOM_MB"] = "500"
        os.environ["NETRADIO_STREAM_MP3_CACHE_MAX_AGE_DAYS"] = "14"
        os.environ["NETRADIO_STREAM_MP3_CACHE_DIR"] = os.path.join(self.tmp, "elsewhere")
        os.environ["NETRADIO_DISK_MAX_PCT"] = "75"
        os.environ["NETRADIO_CACHE_EVENTS_DAYS"] = "7"
        rec = cb.register("stream_mp3", dir=os.path.join(self.tmp, "x"), cap=cb.GB)
        self.assertEqual(rec["cap"], 250 * cb.MB)
        self.assertEqual(rec["headroom"], 500 * cb.MB)
        self.assertEqual(rec["max_age"], 14)
        self.assertEqual(rec["dir"], os.path.join(self.tmp, "elsewhere"))
        self.assertEqual(cb.disk_max_pct(), 75)
        self.assertEqual(cb.events_days(), 7)

    def test_none_means_no_cap_and_no_age(self):
        os.environ["NETRADIO_KEEP_CACHE_GB"] = "none"
        os.environ["NETRADIO_KEEP_CACHE_MAX_AGE_DAYS"] = "none"
        rec = cb.register("keep", max_age=5)
        self.assertIsNone(rec["cap"])
        self.assertIsNone(rec["max_age"])

    def test_a_bad_value_falls_back_to_the_default(self):
        os.environ["NETRADIO_CLIPS_CACHE_GB"] = "lots"
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cb.register("clips", cap=7)["cap"], 7)
        self.assertIn("NETRADIO_CLIPS_CACHE_GB", out.getvalue())

    def test_a_non_finite_or_negative_value_falls_back_to_the_default(self):
        for bad in ("inf", "nan", "-1"):
            os.environ["NETRADIO_CLIPS_CACHE_GB"] = bad
            os.environ["NETRADIO_CLIPS_CACHE_HEADROOM_MB"] = bad
            with contextlib.redirect_stdout(io.StringIO()):
                rec = cb.register("clips", cap=7, headroom=3)
            self.assertEqual((rec["cap"], rec["headroom"]), (7, 3), bad)

    def test_a_directory_that_holds_the_root_or_overlaps_another_cache_is_refused(self):
        self.cache("a")
        for name, d in (("b", os.path.join(self.tmp, "a")),            # shares a's
                        ("c", os.path.join(self.tmp, "a", "inner")),   # inside a's
                        ("d", self.tmp),                               # holds a's, and the root
                        ("e", self.root)):                             # the root itself
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertIsNone(cb.register(name, dir=d), name)
            self.assertIn("not registering", out.getvalue())
            self.assertFalse(cb.registered(name))
        self.assertIsNotNone(self.cache("a"))                         # re-registering is fine

    def test_the_event_log_sits_under_the_download_root_when_there_is_one(self):
        self.assertEqual(cb.events_path(), os.path.join(self.root, "events.jsonl"))
        os.environ["NETRADIO_DOWNLOAD_ROOT"] = os.path.join(self.tmp, "dl")
        self.assertEqual(cb.events_path(), os.path.join(self.tmp, "dl", "events.jsonl"))

    def test_an_unknown_order_or_a_score_order_without_a_score_is_refused(self):
        with self.assertRaises(ValueError):
            cb.register("x", order="random")
        with self.assertRaises(ValueError):
            cb.register("x", order="by-score")


class RegistryAndEvictionRun(CacheBudgetBase):

    def test_reserve_evicts_oldest_first_to_fit(self):
        rec = self.cache("c", cap=3 * KB)
        self.file(rec, "a", age_s=300)
        self.file(rec, "b", age_s=200)
        self.file(rec, "c", age_s=100)
        self.assertTrue(cb.reserve("c", KB))
        self.assertEqual(self.names(rec), ["b", "c"])

    def test_newest_added_evicts_the_newest(self):
        rec = self.cache("c", cap=3 * KB, order="newest-added")
        self.file(rec, "a", age_s=300)
        self.file(rec, "b", age_s=200)
        self.file(rec, "c", age_s=100)
        self.assertTrue(cb.reserve("c", KB))
        self.assertEqual(self.names(rec), ["a", "b"])

    def test_by_score_evicts_the_lowest_score(self):
        scores = {"a": 5, "b": 1, "c": 9}
        rec = self.cache("c", cap=3 * KB, order="by-score",
                         score=lambda p: scores[os.path.basename(p)])
        for n in "abc":
            self.file(rec, n)
        self.assertTrue(cb.reserve("c", KB))
        self.assertEqual(self.names(rec), ["a", "c"])

    def test_a_pinned_entry_is_skipped_and_counted(self):
        rec = self.cache("c", cap=3 * KB, pinned=lambda p: p.endswith("a"))
        self.file(rec, "a", age_s=300)
        self.file(rec, "b", age_s=200)
        self.file(rec, "c", age_s=100)
        self.assertTrue(cb.reserve("c", KB))
        self.assertEqual(self.names(rec), ["a", "c"])
        row = cb.status()["caches"][0]
        self.assertEqual(row["pinned"], 1)
        self.assertEqual(row["entries"], 2)

    def test_a_write_in_progress_is_counted_and_never_evicted(self):
        rec = self.cache("c", cap=2 * KB)
        self.file(rec, "old.part", age_s=900)
        self.file(rec, "b", age_s=100)
        self.file(rec, "c", age_s=50)
        cb.run_eviction()
        self.assertEqual(self.names(rec), ["c", "old.part"])

    def test_a_write_that_died_part_way_is_evicted_after_an_hour(self):
        rec = self.cache("c", cap=2 * KB)
        self.file(rec, "dead.tmp", age_s=2 * 3600)
        self.file(rec, "b", age_s=100)
        self.file(rec, "c", age_s=50)
        cb.run_eviction()
        self.assertEqual(self.names(rec), ["b", "c"])
        stale = self.file(rec, "dead2.part", age_s=2 * 3600)
        self.assertTrue(cb.remove("c", stale, "cleanup"))
        self.assertTrue(cb.remove("c", stale, "cleanup"))   # already gone: gone, not pinned

    def test_the_lock_file_and_the_event_log_are_never_entries(self):
        rec = self.cache("c", cap=KB)
        for fname in (cb.LOCK_NAME, cb.EVENTS_NAME):
            self.file(rec, fname, 5 * KB, age_s=900)
        self.file(rec, "a")
        cb.run_eviction()
        self.assertEqual(self.names(rec), sorted([cb.LOCK_NAME, cb.EVENTS_NAME, "a"]))
        self.assertEqual(cb.status()["caches"][0]["size"], KB)

    def test_evict_never_deletes_outside_the_directory(self):
        rec = self.cache("c")
        other = self.cache("d")
        outside = self.file(other, "x")
        self.assertFalse(cb._evict(rec, (outside, KB, 0), "cap"))
        self.assertTrue(os.path.exists(outside))

    def test_the_floor_pass_skips_a_cache_on_another_volume(self):
        here = self.cache("here", rank=2)
        away = self.cache("away", rank=1)
        self.file(here, "h", 4 * KB)
        self.file(away, "a", 4 * KB)
        self.addCleanup(setattr, cb, "_device", cb._device)
        cb._device = lambda p: 2 if os.path.realpath(p).startswith(
            os.path.realpath(away["dir"])) else 1
        self.base = 80 * KB
        cb.run_eviction()
        self.assertEqual(self.names(away), ["a"])     # freeing it would not move this volume
        self.assertEqual(self.names(here), [])

    def test_all_pinned_is_refused_and_over_cap_reported(self):
        rec = self.cache("c", cap=2 * KB, pinned=lambda p: True)
        self.file(rec, "a")
        self.file(rec, "b")
        self.file(rec, "c")
        self.assertFalse(cb.reserve("c", KB))
        cb.run_eviction()
        self.assertEqual(self.names(rec), ["a", "b", "c"])
        self.assertTrue(cb.status()["caches"][0]["over_cap"])

    def test_a_lowered_cap_evicts_on_the_next_run(self):
        rec = self.cache("c", cap=10 * KB)
        for i, n in enumerate("abcd"):
            self.file(rec, n, age_s=400 - i * 100)
        cb.run_eviction()
        self.assertEqual(len(self.names(rec)), 4)
        os.environ["NETRADIO_C_CACHE_GB"] = str(2 * KB / cb.GB)   # 2 KB, set in the environment
        rec = self.cache("c", cap=10 * KB)
        summary = cb.run_eviction()
        self.assertEqual(self.names(rec), ["c", "d"])
        self.assertEqual(summary["evicted"]["c"], 2)
        self.assertFalse(cb.status()["caches"][0]["over_cap"])

    def test_max_age_evicts_by_time(self):
        rec = self.cache("c", max_age=7)
        self.file(rec, "old", age_s=10 * 86400)
        self.file(rec, "sub/older", age_s=20 * 86400)
        self.file(rec, "new", age_s=86400)
        cb.run_eviction()
        self.assertEqual(self.names(rec), ["new", "sub"])
        self.assertEqual(os.listdir(os.path.join(rec["dir"], "sub")), [])
        self.assertEqual({e["reason"] for e in self.events() if e["event"] == "evict"}, {"age"})

    def test_the_floor_refuses_every_cache_at_once(self):
        a = self.cache("a", cap=None)
        b = self.cache("b", cap=10 * KB)
        self.file(a, "x")
        self.file(b, "y")
        self.base = 90 * KB                         # 92 % used
        for name in ("a", "b"):
            self.assertFalse(cb.reserve(name, None))
            self.assertFalse(cb.reserve(name, 10))
        self.assertEqual(self.names(a), ["x"])       # a refusal evicts nothing
        self.assertEqual(self.names(b), ["y"])
        self.assertEqual({e["reason"] for e in self.events()}, {"floor"})

    def test_a_planned_write_that_would_pass_the_floor_is_refused(self):
        rec = self.cache("a")
        self.base = 80 * KB
        self.assertTrue(cb.reserve("a", KB))
        self.assertFalse(cb.reserve("a", 3 * KB))
        self.assertTrue(rec)

    def test_a_breached_floor_evicts_by_the_ranking_each_cache_in_its_own_order(self):
        first = self.cache("first", rank=1)
        second = self.cache("second", rank=2, order="newest-added")
        never = self.cache("never", rank=None)
        self.file(first, "f1", 4 * KB, age_s=300)
        self.file(first, "f2", 4 * KB, age_s=200)
        self.file(second, "s1", 4 * KB, age_s=300)
        self.file(second, "s2", 4 * KB, age_s=100)
        self.file(never, "n1", 4 * KB, age_s=900)
        # 60 + 20 = 80 KB used; the floor is 70 %: 12 KB must go, which is 3 entries
        os.environ["NETRADIO_DISK_MAX_PCT"] = "70"
        self.base = 60 * KB
        summary = cb.run_eviction()
        self.assertEqual(self.names(first), [])
        self.assertEqual(self.names(second), ["s1"])   # its own order: the newest went first
        self.assertEqual(self.names(never), ["n1"])
        self.assertEqual(summary["evicted"], {"first": 2, "second": 1, "never": 0})
        self.assertEqual({e["reason"] for e in self.events()}, {"floor"})

    def test_under_the_floor_and_the_caps_nothing_is_evicted(self):
        rec = self.cache("c", cap=10 * KB, rank=1)
        self.file(rec, "a")
        self.base = 50 * KB
        cb.run_eviction()
        self.assertEqual(self.names(rec), ["a"])
        self.assertEqual(self.events(), [])

    def test_two_runs_on_one_directory_serialise(self):
        active = []
        overlap = []

        def slow_pin(_path):
            active.append(1)
            if len(active) > 1:
                overlap.append(1)
            time.sleep(0.02)
            active.pop()
            return True

        rec = self.cache("c", cap=KB, pinned=slow_pin)
        for n in "abcde":
            self.file(rec, n)
        threads = [threading.Thread(target=cb.run_eviction) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(overlap, [])

    def test_the_lock_is_machine_wide(self):
        """A second process holding the lock file holds every run here."""
        self.cache("c")
        os.makedirs(self.root, exist_ok=True)
        holder = subprocess.Popen(
            [sys.executable, "-c",
             "import fcntl, os, sys, time\n"
             "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT)\n"
             "fcntl.flock(fd, fcntl.LOCK_EX)\n"
             "print('held', flush=True)\n"
             "time.sleep(0.6)\n",
             os.path.join(self.root, cb.LOCK_NAME)],
            stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.wait)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        started = time.monotonic()
        cb.run_eviction()
        self.assertGreater(time.monotonic() - started, 0.3)
        holder.stdout.close()


class DeletionsThroughTheApi(CacheBudgetBase):

    def test_remove_deletes_records_the_reason_and_updates_the_accounting(self):
        rec = self.cache("c")
        a = self.file(rec, "a", 3 * KB)
        self.file(rec, "b", 2 * KB)
        self.assertEqual(cb.status()["caches"][0]["size"], 5 * KB)
        self.assertTrue(cb.remove("c", a, "heard"))
        row = cb.status()["caches"][0]
        self.assertEqual((row["size"], row["entries"], row["removed_since_start"]), (2 * KB, 1, 1))
        [ev] = self.events()
        self.assertEqual((ev["event"], ev["entry"], ev["reason"], ev["bytes"]),
                         ("remove", "a", "heard", 3 * KB))

    def test_remove_refuses_a_path_outside_the_cache(self):
        rec = self.cache("c")
        other = self.cache("d")
        outside = self.file(other, "x")
        self.assertFalse(cb.remove("c", outside, "heard"))
        self.assertFalse(cb.remove("c", os.path.join(rec["dir"], "..", "d", "x"), "heard"))
        self.assertFalse(cb.remove("c", rec["dir"], "heard"))
        self.assertTrue(os.path.exists(outside))
        self.assertEqual([(e["event"], e["reason"], e["why"]) for e in self.events()],
                         [("refuse", "heard", "outside the cache")] * 3)

    def test_remove_refuses_a_pinned_entry(self):
        rec = self.cache("c", pinned=lambda p: True)
        a = self.file(rec, "a")
        self.assertFalse(cb.remove("c", a, "broken-entry"))
        self.assertTrue(os.path.exists(a))
        [ev] = self.events()
        self.assertEqual((ev["event"], ev["reason"], ev["why"]),
                         ("refuse", "broken-entry", "pinned"))


class HeadroomAndOverflow(CacheBudgetBase):

    def test_a_planned_reserve_stops_at_cap_minus_headroom(self):
        rec = self.cache("c", cap=4 * KB, headroom=KB, pinned=lambda p: True)
        self.file(rec, "a", 2 * KB)
        self.assertTrue(cb.reserve("c", KB))             # 3 KB: exactly cap - headroom
        self.assertFalse(cb.reserve("c", KB + 1))        # past it, and nothing to evict

    def test_a_planned_reserve_evicts_down_to_cap_minus_headroom(self):
        rec = self.cache("c", cap=4 * KB, headroom=KB)
        self.file(rec, "a", age_s=300)
        self.file(rec, "b", age_s=200)
        self.file(rec, "c", age_s=100)
        self.assertTrue(cb.reserve("c", KB))
        self.assertEqual(self.names(rec), ["b", "c"])

    def test_an_unplanned_write_may_run_into_the_headroom_and_past_the_cap(self):
        rec = self.cache("c", cap=4 * KB, headroom=KB)
        self.file(rec, "a", age_s=300)
        self.file(rec, "b", age_s=200)
        self.file(rec, "c", KB // 2, age_s=100)          # 2.5 KB: inside the headroom's reach
        self.assertTrue(cb.reserve("c", None))           # under the cap: admitted, nothing evicted
        self.assertEqual(self.names(rec), ["a", "b", "c"])
        new = self.file(rec, "new", 3 * KB)              # ran long: 5.5 KB, past the cap
        self.assertTrue(os.path.exists(new))             # never stopped part-way
        cb.commit("c", new)
        self.assertEqual(self.names(rec), ["c", "new"])  # oldest others went, never the new file

    def test_an_unplanned_reserve_at_the_cap_evicts_below_it(self):
        rec = self.cache("c", cap=2 * KB)
        self.file(rec, "a", age_s=300)
        self.file(rec, "b", age_s=200)
        self.assertTrue(cb.reserve("c", None))
        self.assertEqual(self.names(rec), ["b"])

    def test_commit_never_evicts_the_file_just_written_even_when_it_is_the_oldest(self):
        rec = self.cache("c", cap=2 * KB)
        self.file(rec, "a", age_s=200)
        self.file(rec, "b", age_s=100)
        new = self.file(rec, "new", age_s=9999)          # a copy that kept its old mtime
        cb.commit("c", new)
        self.assertEqual(self.names(rec), ["b", "new"])

    def test_commit_on_a_breached_floor_runs_the_eviction_at_once(self):
        other = self.cache("other", rank=1)
        rec = self.cache("c", rank=2)
        self.file(other, "o1", 5 * KB)
        new = self.file(rec, "new", 5 * KB)
        self.base = 75 * KB                              # 85 %: past the floor
        cb.commit("c", new)
        self.assertEqual(self.names(other), [])
        self.assertEqual(self.names(rec), ["new"])

    def test_commit_under_the_cap_and_the_floor_evicts_nothing(self):
        rec = self.cache("c", cap=4 * KB)
        self.file(rec, "a")
        new = self.file(rec, "new")
        cb.commit("c", new)
        self.assertEqual(self.names(rec), ["a", "new"])
        self.assertEqual([e["event"] for e in self.events()], ["admit"])

    def test_commit_refuses_a_path_outside_the_cache(self):
        self.cache("c")
        with self.assertRaises(ValueError):
            cb.commit("c", os.path.join(self.tmp, "elsewhere"))

    def test_an_unregistered_name_is_admitted_and_left_alone(self):
        self.assertTrue(cb.reserve("nobody", 10 ** 12))
        self.assertFalse(cb.remove("nobody", os.path.join(self.tmp, "x"), "heard"))
        self.assertFalse(cb.run_eviction("nobody")["ran"])


class EventLog(CacheBudgetBase):

    def test_every_admit_evict_and_remove_writes_one_record(self):
        rec = self.cache("c", cap=2 * KB)
        a = self.file(rec, "a", age_s=300)
        cb.commit("c", a)
        b = self.file(rec, "b", age_s=200)
        cb.commit("c", b)
        new = self.file(rec, "new")
        cb.commit("c", new)                              # over the cap: evicts a
        cb.remove("c", b, "heard")
        self.assertEqual([(e["event"], e["entry"]) for e in self.events()],
                         [("admit", "a"), ("admit", "b"), ("admit", "new"),
                          ("evict", "a"), ("remove", "b")])
        for e in self.events():
            self.assertEqual(e["cache"], "c")
            self.assertIn("at", e)

    def test_a_full_run_drops_records_older_than_the_retention(self):
        self.cache("c")
        os.makedirs(self.root, exist_ok=True)
        with open(cb.events_path(), "w") as fh:
            fh.write(json.dumps({"at": "2020-01-01T00:00:00+00:00", "event": "admit"}) + "\n")
            fh.write(json.dumps({"at": "2020-01-01T00:00:00", "event": "admit"}) + "\n")
            fh.write("torn\n")
            fh.write(json.dumps({"at": cb._now().isoformat(), "event": "admit"}) + "\n")
        cb.run_eviction()
        self.assertEqual(len(self.events()), 1)


class Status(CacheBudgetBase):

    def test_one_row_per_cache(self):
        rec = self.cache("clips", cap=250 * cb.MB, refill="re-decode", rank=11)
        self.file(rec, "a", 2 * KB)
        cb.run_eviction()
        st = cb.status()
        self.assertTrue(st["enabled"])
        self.assertEqual(st["floor_pct"], 82)
        [row] = st["caches"]
        for key, want in (("name", "clips"), ("cap", 250 * cb.MB), ("size", 2 * KB),
                          ("entries", 1), ("pinned", 0), ("refill", "re-decode"),
                          ("rank", 11), ("evicted_since_start", 0)):
            self.assertEqual(row[key], want, key)
        self.assertIsNotNone(row["last_run"])


if __name__ == "__main__":
    unittest.main()
