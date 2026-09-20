"""The harvester must actually RUN — not merely parse.

This file exists because of a bug I shipped. `harvest.py` called `selftest.offline()` without ever
importing `selftest`. That is a NameError at *runtime*, not a SyntaxError, so:

  * `ast.parse()` passed,
  * the whole test suite passed (nothing exercised `run()`),
  * the PR merged, and
  * the harvester crashed on the first line of work, every time it was started, silently going
    "off" the moment the user turned it on.

A missing name is exactly what a linter catches and a unit test does not, so the first test here is
a pyflakes pass over the scripts — cheap, and it would have caught it. The rest pin the two guards
that travel with the harvester: the excerpt hard cap, and the purge-audio eviction path.
"""

import contextlib
import io
import json
import os
import shutil
import time
import unittest.mock
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)

import cache_budget                                # noqa: E402  (the machine's one cache policy)

try:
    import harvest
except Exception:                       # librosa/numba absent -> not this test's job
    harvest = None


# The Audacity-era tools. `pipeclient.py` still calls `raw_input`, so it has not run since Python 2;
# SCRIPTS.md already files all three under "Retired". They are full of undefined names and always
# were. Naming them here is the honest way to lint everything else: an exclusion you can see beats
# a check narrowed until it passes.
# librosa/soundfile are imported lazily by the paths these two classes exercise, so a
# bare clone imports everything fine and then errors at call time. Probe once; skip clean.
try:
    import librosa                      # noqa: F401
    import soundfile                    # noqa: F401
    HAVE_AUDIO = True
except ImportError:                     # only the third-party deps -- anything else raises
    HAVE_AUDIO = False


RETIRED = ("alignfinder.py", "pipeclient.py", "splitexport.py")


def _live_scripts():
    """Every script we still run — the whole of scripts/ and streamalign/, minus the retired ones."""
    out = []
    for root in (SCRIPTS, os.path.join(SCRIPTS, "streamalign")):
        for name in sorted(os.listdir(root)):
            if name.endswith(".py") and name not in RETIRED:
                out.append(os.path.join(root, name))
    return out


class NoUndefinedNames(unittest.TestCase):
    """A NameError in a rarely-taken branch is invisible to a test suite and fatal in production.

    This check used to cover exactly two files, `harvest.py` and `selftest.py`, hardcoded. That was
    enough to catch the missing `import urllib` I wrote on 2026-07-13 -- but only because I happened
    to be editing one of the two. Every other script in this repo was unguarded. So: lint them all.

    It also used to `skipTest` when pyflakes was absent, which is the failure mode this repo has a
    name for -- *a skip is not a pass*. A missing linter is now a FAILURE, because a green suite
    that silently checked nothing is worse than a red one that tells you why.
    """

    def test_pyflakes_is_available_at_all(self):
        try:
            import pyflakes  # noqa: F401
        except ImportError:
            self.fail("pyflakes is not installed, so the undefined-name guard checked NOTHING. "
                      "A skip is not a pass. Install it:  .venv/bin/pip install pyflakes")

    def test_no_undefined_names_in_any_live_script(self):
        try:
            import pyflakes  # noqa: F401
        except ImportError:
            self.fail("pyflakes is not installed -- see test_pyflakes_is_available_at_all")

        scripts = _live_scripts()
        self.assertGreater(len(scripts), 10, "we should be linting the whole repo, not a handful")

        r = subprocess.run([sys.executable, "-m", "pyflakes"] + scripts,
                           capture_output=True, text=True)
        # undefined names are the fatal class; unused imports are noise we tolerate
        fatal = [l for l in r.stdout.splitlines() if "undefined name" in l]
        self.assertEqual(fatal, [], "undefined names — each one is a NameError waiting to happen:\n"
                                    + "\n".join(fatal))

    @unittest.skipIf(harvest is None, "needs the librosa venv")
    def test_run_can_reach_its_dependencies(self):
        """The specific bug: run() calls selftest.offline(). If the import is missing this is a
        NameError the moment the harvester starts."""
        self.assertTrue(hasattr(harvest, "selftest"))
        self.assertTrue(callable(harvest.selftest.offline))


@unittest.skipIf(harvest is None, "needs the librosa venv")
@unittest.skipUnless(HAVE_AUDIO, "audio deps unavailable -- see requirements-streamalign.txt")
class ExcerptsAreExcerpts(unittest.TestCase):
    """The copyright posture rests on one claim: what we keep is far too short to be a copy.

    It has already failed once — 2.1 GB of full-length audio was found in the candidates directory,
    including a 108-minute DJ mix retained whole. So the length is now enforced by the code, and
    this is the test that says so.
    """

    def _write(self, samples_s, at_s):
        import numpy as np
        from streamalign import audio as _audio
        import soundfile as sf
        path = os.path.join(tempfile.mkdtemp(), "x.wav")
        harvest.write_excerpt(np.zeros(int(samples_s * _audio.SR), dtype="float32"), at_s, path)
        return sf.info(path).duration if os.path.exists(path) else 0.0

    def test_a_long_mix_yields_a_short_excerpt(self):
        """The 108-minute-mix case, exactly."""
        self.assertLessEqual(self._write(6475, 3000.0), harvest.EXCERPT_S + 0.5)

    def test_the_excerpt_is_never_longer_than_the_cap_wherever_the_match_lands(self):
        for at in (0.0, 5.0, 900.0, 6400.0):
            with self.subTest(at=at):
                self.assertLessEqual(self._write(6475, at), harvest.EXCERPT_S + 0.5)

    def test_a_short_candidate_is_not_padded(self):
        self.assertLessEqual(self._write(10, 5.0), 10.5)


@unittest.skipIf(harvest is None, "needs the librosa venv")
class EvictingAPurgedLead(unittest.TestCase):
    """The second crash-on-every-start bug, and the same shape as the first.

    `--purge-audio` pops "audio" from every match -- by design: a lead is a URL, not a copy of a
    record. But the eviction path still did `os.unlink(dead["audio"])`, catching only OSError. So
    the first match good enough to displace anyone raised KeyError and killed the harvester. Every
    board was already full (12 of 12, for each of MT4/6/7), so it died on essentially its first
    piece of real work, every start -- and the watchdog dutifully restarted it into the same wall.

    A unit test does not catch this; nothing exercised the branch. So the branch is exercised here.
    """

    def _state(self, n, with_audio=False):
        return {"matches": [{"mystery": 4, "cost": 0.01 * i, "url": "u%d" % i,
                             **({"audio": "/nonexistent/%d.wav" % i} if with_audio else {})}
                            for i in range(n)],
                "kept": n if with_audio else 0}

    def test_a_full_board_of_purged_leads_evicts_without_raising(self):
        """The production state exactly: every row lacks "audio"."""
        state = self._state(harvest.KEEP_TOP + 1)
        harvest.evict_overfull(state, 4)                  # used to raise KeyError: 'audio'
        self.assertEqual(len(state["matches"]), harvest.KEEP_TOP)

    def test_the_worst_lead_is_the_one_dropped(self):
        state = self._state(harvest.KEEP_TOP + 1)
        harvest.evict_overfull(state, 4)
        costs = [m["cost"] for m in state["matches"]]
        self.assertEqual(max(costs), 0.01 * (harvest.KEEP_TOP - 1))   # the priciest row is gone

    def test_a_missing_file_does_not_raise_either(self):
        """The OSError case still has to work -- the file may already be gone."""
        state = self._state(harvest.KEEP_TOP + 1, with_audio=True)
        harvest.evict_overfull(state, 4)
        self.assertEqual(len(state["matches"]), harvest.KEEP_TOP)

    def test_an_underfull_board_is_left_alone(self):
        state = self._state(3)
        harvest.evict_overfull(state, 4)
        self.assertEqual(len(state["matches"]), 3)

    def test_other_mysteries_are_untouched(self):
        state = self._state(harvest.KEEP_TOP + 1)
        state["matches"].append({"mystery": 7, "cost": 0.99, "url": "keep-me"})
        harvest.evict_overfull(state, 4)
        self.assertIn("keep-me", [m["url"] for m in state["matches"]])


# The cache-policy names the policy tests save and restore around a test (the same set
# tests/test_cache_budget.py uses): the `CACHE` family plus the machine-wide settings.
CACHE_ENV = ("NETRADIO_CACHE_ROOT", "NETRADIO_DOWNLOAD_ROOT", "NETRADIO_DISK_MAX_PCT",
             "NETRADIO_CACHE_EVENTS_DAYS")


@unittest.skipIf(harvest is None, "needs the librosa venv")
class TheHarvestersCachesOnThePolicy(unittest.TestCase):
    """The harvester's two caches, `chroma` and `candidates`, register on the machine's one
    cache policy: their directories come from the same variable family every other cache
    reads, the excerpt board gives up the WORST excerpt of a mystery first (the rule KEEP_TOP
    applies to the board, not plain oldest-added), and the TTL sweep deletes through the
    policy's door so every removal is recorded. While the root is unset there is no cache
    directory at all -- never an unbounded fallback, which is the growth this ends."""

    KB = 1000

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="harvest-caches-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        saved = {k: os.environ.get(k) for k in list(os.environ)
                 if k.startswith("NETRADIO_") and ("CACHE" in k or k in CACHE_ENV)}
        for k in saved:
            os.environ.pop(k, None)
        self.addCleanup(self._restore_env, saved)
        os.environ["NETRADIO_CACHE_ROOT"] = os.path.join(self.tmp, "root")
        self.addCleanup(self._restore_registry, dict(cache_budget._REGISTRY), dict(cache_budget._STATS))
        cache_budget._REGISTRY.clear()
        cache_budget._STATS.clear()
        self.addCleanup(setattr, cache_budget, "_disk_usage", cache_budget._disk_usage)
        cache_budget._disk_usage = self._fake_volume   # an empty volume: the floor never trips
        harvest.register_caches()
        self.chroma_dir = os.path.join(self.tmp, "root", "chroma")
        self.keep_dir = os.path.join(self.tmp, "root", "candidates")

    @staticmethod
    def _restore_env(saved):
        for k in [k for k in list(os.environ)
                  if k.startswith("NETRADIO_") and ("CACHE" in k or k in CACHE_ENV)]:
            os.environ.pop(k, None)
        for k, v in saved.items():
            os.environ[k] = v

    @staticmethod
    def _restore_registry(registry, stats):
        cache_budget._REGISTRY.clear()
        cache_budget._REGISTRY.update(registry)
        cache_budget._STATS.clear()
        cache_budget._STATS.update(stats)

    def _fake_volume(self, _path):
        return (100 * 1000 * self.KB, 0, 100 * 1000 * self.KB)

    def _excerpt(self, name, size, age_s=0):
        """A board entry: `MT<n>-<cost>-<hash>.wav`, at an age, holding `size` bytes."""
        os.makedirs(self.keep_dir, exist_ok=True)
        path = os.path.join(self.keep_dir, name)
        with open(path, "wb") as fh:
            fh.write(b"x" * size)
        t = time.time() - age_s
        os.utime(path, (t, t))
        return path

    def _events(self):
        path = cache_budget.events_path()
        if not os.path.isfile(path):
            return []
        with open(path) as fh:
            return [json.loads(line) for line in fh]

    def test_the_two_registrations(self):
        rows = {r["name"]: r for r in cache_budget.status()["caches"]}
        self.assertEqual(set(rows), {"chroma", "candidates"})
        chroma, candidates = rows["chroma"], rows["candidates"]
        # `chroma`: the signature working cache, refilled from the signature bucket by key
        self.assertEqual((chroma["dir"], chroma["max_age_days"], chroma["order"]),
                         (self.chroma_dir, 14, "oldest-added"))
        self.assertEqual(chroma["cap"], cache_budget.DEFAULT_CAP)
        self.assertEqual((chroma["refill"], chroma["rank"]), ("bucket:chroma/", 4))
        # `candidates`: the excerpt board, the worst excerpt of a mystery first
        self.assertEqual((candidates["dir"], candidates["order"]),
                         (self.keep_dir, "by-score"))
        self.assertEqual(candidates["cap"], 250 * cache_budget.MB)
        self.assertEqual(candidates["max_age_days"], harvest.KEEP_TTL_DAYS)
        self.assertEqual((candidates["refill"], candidates["rank"]), ("re-cut", 10))
        # the module's path constants follow the registry
        self.assertEqual((harvest.CACHE, harvest.KEEP), (self.chroma_dir, self.keep_dir))

    def test_the_variable_family_overrides_every_setting_it_names(self):
        os.environ["NETRADIO_CHROMA_CACHE_DIR"] = os.path.join(self.tmp, "elsewhere-chroma")
        os.environ["NETRADIO_CHROMA_CACHE_MAX_AGE_DAYS"] = "7"
        os.environ["NETRADIO_CANDIDATES_CACHE_DIR"] = os.path.join(self.tmp, "elsewhere-candidates")
        os.environ["NETRADIO_CANDIDATES_CACHE_GB"] = "1"
        harvest.register_caches()
        rows = {r["name"]: r for r in cache_budget.status()["caches"]}
        self.assertEqual(rows["chroma"]["dir"], os.path.join(self.tmp, "elsewhere-chroma"))
        self.assertEqual(rows["chroma"]["max_age_days"], 7)
        self.assertEqual(rows["candidates"]["dir"], os.path.join(self.tmp, "elsewhere-candidates"))
        self.assertEqual(rows["candidates"]["cap"], 1 * cache_budget.GB)

    def test_dark_without_a_root_neither_cache_exists_at_all(self):
        os.environ.pop("NETRADIO_CACHE_ROOT")
        harvest.register_caches()
        self.assertFalse(cache_budget.registered("chroma"))
        self.assertFalse(cache_budget.registered("candidates"))
        self.assertIsNone(harvest._chroma_dir())
        self.assertIsNone(harvest._keep_dir())

    @unittest.skipUnless(HAVE_AUDIO,
                        "write_excerpt writes a real excerpt -- see requirements-streamalign.txt")
    def test_an_excerpt_past_the_cap_evicts_the_worst_of_its_mystery_first(self):
        # A board of MT4 excerpts whose WORST is the NEWEST file: a plain oldest-added order
        # would keep the worst and drop the best; `by-score` must take the worst first.
        self._excerpt("MT4-0.0500-best.wav", 200 * self.KB, age_s=20 * 86400)
        self._excerpt("MT4-0.0600-middling.wav", 200 * self.KB, age_s=10 * 86400)
        worst = self._excerpt("MT4-0.0650-worst.wav", 200 * self.KB, age_s=86400)
        fresh = os.path.join(self.keep_dir, "MT4-0.0400-new.wav")
        os.environ["NETRADIO_CANDIDATES_CACHE_GB"] = "0.0007"    # 700 KB: the board cannot hold it all
        harvest.register_caches()
        import numpy as np
        self.assertTrue(harvest.write_excerpt(
            np.zeros(int(10 * 16000), dtype="float32"), 5.0, fresh),
            "an excerpt short of the cap and the floor must be kept")
        self.assertFalse(os.path.exists(worst), "the worst excerpt of the mystery went first")
        self.assertFalse(os.path.exists(os.path.join(self.keep_dir, "MT4-0.0600-middling.wav")))
        self.assertTrue(os.path.exists(os.path.join(self.keep_dir, "MT4-0.0500-best.wav")),
                        "the best excerpt outlives the worst, whatever their ages")
        self.assertTrue(os.path.exists(fresh), "never the entry just written")
        evictions = [e for e in self._events() if e["event"] == "evict"]
        self.assertEqual([e["entry"] for e in evictions][:2],
                         ["MT4-0.0650-worst.wav", "MT4-0.0600-middling.wav"],
                         "the policy's own record of the order it evicted in")

    def test_an_excerpt_older_than_30_days_is_evicted(self):
        old = self._excerpt("MT4-0.0600-old.wav", 100, age_s=40 * 86400)
        fresh = self._excerpt("MT4-0.0550-fresh.wav", 100)
        harvest.sweep_excerpts()
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(fresh))
        removals = [e for e in self._events() if e["event"] == "remove"]
        self.assertEqual([(e["entry"], e["reason"]) for e in removals],
                         [("MT4-0.0600-old.wav", "expired")],
                         "the sweep deletes through the policy, which records the reason")

    @unittest.skipUnless(HAVE_AUDIO,
                        "write_excerpt writes a real excerpt -- see requirements-streamalign.txt")
    def test_a_refused_excerpt_is_not_kept(self):
        """The write the policy refuses (the disk past its floor) leaves nothing on disk, so
        run() neither counts it as kept nor names it as the lead's audio: the lead survives."""
        cache_budget._disk_usage = lambda _p: (100 * self.KB, 50 * self.KB, 50 * self.KB)
        os.environ["NETRADIO_DISK_MAX_PCT"] = "0"
        path = os.path.join(self.keep_dir, "MT4-0.0500-refused.wav")
        import numpy as np
        self.assertFalse(harvest.write_excerpt(np.zeros(16000, dtype="float32"), 0.5, path))
        self.assertFalse(os.path.exists(path))
        self.assertTrue([e for e in self._events() if e["event"] == "refuse"],
                        "the refusal is recorded like every other policy answer")

    @unittest.skipUnless(HAVE_AUDIO,
                        "write_excerpt writes a real excerpt -- see requirements-streamalign.txt")
    def test_an_excerpt_evicted_between_its_rename_and_its_commit_is_not_kept(self):
        """The rename and the commit are two calls; another writer's `reserve` that starts
        between them takes an excerpt the policy has not recorded yet. The write must not
        report a kept excerpt that is not on disk."""
        import numpy as np
        # The cap admits the planned excerpt and nothing else beside it: 300 KB against a
        # ~32 KB excerpt, so another writer asking for 300 KB of room must take the excerpt.
        os.environ["NETRADIO_CANDIDATES_CACHE_GB"] = "0.0003"
        harvest.register_caches()
        real_replace = os.replace

        def racing_replace(a, b):
            real_replace(a, b)
            # another writer asks for room, and the not-yet-recorded excerpt is the only
            # entry to give up -- the exact interval between rename and commit
            cache_budget.reserve(harvest.CANDIDATES_CACHE, 300 * self.KB)

        path = os.path.join(self.keep_dir, "MT4-0.0500-raced.wav")
        with unittest.mock.patch("os.replace", side_effect=racing_replace):
            self.assertFalse(harvest.write_excerpt(np.zeros(16000, dtype="float32"), 0.5, path))
        self.assertFalse(os.path.exists(path), "the excerpt was taken, not kept")
        self.assertFalse(os.path.exists(self.keep_dir) and
                         "PROVENANCE.txt" in os.listdir(self.keep_dir),
                         "a landing that did not survive writes no board note")

    def test_the_boards_provenance_note_is_pinned(self):
        """PROVENANCE.txt's name parses to no cost, so the by-score order counts it as an
        ordinary entry -- an age-, cap- or floor-driven eviction run could take the
        directory's one line of "this is not a music library", and `_write_provenance`
        would not restore it until the next kept excerpt. The registration pins it instead."""
        old = self._excerpt("MT4-0.0600-old.wav", 100, age_s=40 * 86400)
        note = os.path.join(self.keep_dir, "PROVENANCE.txt")
        with open(note, "w") as fh:
            fh.write("note")
        t = time.time() - 40 * 86400                       # as old as anything it outlives
        os.utime(note, (t, t))
        cache_budget.run_eviction("candidates")
        self.assertFalse(os.path.exists(old),
                         "the run really evicted -- the note survived for a reason")
        self.assertTrue(os.path.exists(note),
                        "the pinned entry is never an eviction's to take")
        removals = [e for e in self._events() if e["event"] == "evict"]
        self.assertEqual([e["entry"] for e in removals], ["MT4-0.0600-old.wav"],
                         "the policy's own record: the note was never a candidate")


@unittest.skipIf(harvest is None, "needs the librosa venv")
class TheOnDemandRescanRefusesADarkPolicy(unittest.TestCase):
    """`--rescan` is the one cache-reading mode that used to run dark: `unscored_pairs`
    counted the pairs (a bucket-held signature reads as held), `_load_sig` answered None for
    every one, and the run stamped `rescan_pending` to 0 over "Every cached signature has now
    met every mystery" -- a completion claim about work that never ran, where every sibling
    mode refuses (run(), --migrate-sigs, --requeue-missing-sigs).
    It refuses now, before the query set is even read, with the message --migrate-sigs uses."""

    def test_rescan_refuses_before_the_query_set_is_read(self):
        tmp = tempfile.mkdtemp(prefix="rescan-dark-")
        self.addCleanup(shutil.rmtree, tmp, True)
        paths = harvest.STATE_DIR, harvest.STATE, harvest.LEDGER
        harvest.STATE_DIR = os.path.join(tmp, "harvest")
        harvest.STATE = os.path.join(tmp, "state.json")
        harvest.LEDGER = os.path.join(tmp, "ledger.json")
        self.addCleanup(lambda: (setattr(harvest, "STATE_DIR", paths[0]),
                                  setattr(harvest, "STATE", paths[1]),
                                  setattr(harvest, "LEDGER", paths[2])))
        saved = {k: os.environ.get(k) for k in list(os.environ)
                 if k.startswith("NETRADIO_") and ("CACHE" in k or k in CACHE_ENV)}
        for k in saved:
            os.environ.pop(k, None)
        registry = dict(cache_budget._REGISTRY), dict(cache_budget._STATS)
        attrs = (harvest.CACHE, harvest.KEEP, harvest._CACHE_AT_IMPORT,
                 harvest._KEEP_AT_IMPORT)

        def restore():
            cache_budget._REGISTRY.clear()
            cache_budget._REGISTRY.update(registry[0])
            cache_budget._STATS.clear()
            cache_budget._STATS.update(registry[1])
            for k, v in saved.items():
                os.environ[k] = v
            for name, value in zip(("CACHE", "KEEP", "_CACHE_AT_IMPORT",
                                    "_KEEP_AT_IMPORT"), attrs):
                setattr(harvest, name, value)
        self.addCleanup(restore)
        cache_budget._REGISTRY.clear()
        cache_budget._STATS.clear()
        harvest.register_caches()             # re-read: the signature cache is dark
        self.assertIsNone(harvest._chroma_dir())

        def refused(what):
            return lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("the refusal must come before the %s is read" % what))

        argv = ["harvest.py", "--rescan"]
        with unittest.mock.patch.object(sys, "argv", argv), \
                unittest.mock.patch.object(harvest, "queries",
                                          refused("query set")), \
                unittest.mock.patch.object(harvest, "load_rulings",
                                          refused("rulings file")), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            harvest.main()
        self.assertIn("the signature cache is dark", out.getvalue())
        self.assertIn("NETRADIO_CACHE_ROOT", out.getvalue())
        self.assertFalse(os.path.exists(harvest.STATE),
                         "a refused rescan writes no state: rescan_pending was never "
                         "stamped to a completion it did not do")

    def test_rescan_refuses_without_the_rulings_file_too(self):
        """The rescan scores the corpus, so it needs the retired set as much as the run does:
        without the file it would score records already rejected and stamp `rescan_pending`
        over them. The cache is lit for this one (the refusal under test is the file's), and
        the query set must never be read past it."""
        tmp = tempfile.mkdtemp(prefix="rescan-norulings-")
        self.addCleanup(shutil.rmtree, tmp, True)
        paths = harvest.STATE, harvest.LEDGER, harvest.RULINGS, harvest.STATE_DIR
        harvest.STATE = os.path.join(tmp, "state.json")
        harvest.LEDGER = os.path.join(tmp, "ledger.json")
        harvest.RULINGS = os.path.join(tmp, "rulings.json")       # never written
        harvest.STATE_DIR = tmp        # main()'s own makedirs lands on the throwaway
        self.addCleanup(lambda: (setattr(harvest, "STATE", paths[0]),
                                 setattr(harvest, "LEDGER", paths[1]),
                                 setattr(harvest, "RULINGS", paths[2]),
                                 setattr(harvest, "STATE_DIR", paths[3])))
        with unittest.mock.patch.object(harvest, "_chroma_dir",
                                        lambda: os.path.join(tmp, "chroma")), \
                unittest.mock.patch.object(sys, "argv", ["harvest.py", "--rescan"]), \
                unittest.mock.patch.object(
                    harvest, "queries",
                    lambda state=None: (_ for _ in ()).throw(
                        AssertionError("the refusal must come before the query set is read"))), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            harvest.main()
        text = out.getvalue()
        self.assertIn("the rulings file", text)
        self.assertIn(harvest.RULINGS, text)
        self.assertFalse(os.path.exists(harvest.STATE),
                         "a refused rescan writes no state")


@unittest.skipIf(harvest is None, "needs the librosa venv")
class AFailedLoadIsNotACompletedScore(unittest.TestCase):
    """`rescan` used to return `len(pairs)` whether or not each signature loaded: a held
    signature whose fetch or load failed was not recorded in `state["scored"]`, but the
    count said it was, so `rescan_pending` went to 0 and the harvester printed "Every
    held signature has now met every mystery" over work that never ran. A pair whose
    `_load_sig` returns None is not scored: it stays in the pending count and is tried
    again on the next pass, and the count never claims a completion the cache did not
    let happen.
    """

    def setUp(self):
        import numpy as np
        self.tmp = tempfile.mkdtemp(prefix="rescan-load-")
        self.cache = os.path.join(self.tmp, "chroma")
        os.makedirs(self.cache)
        self._chroma_dir = harvest._chroma_dir
        harvest._chroma_dir = lambda: self.cache
        self._paths = harvest.LEDGER, harvest.STATE
        harvest.LEDGER = os.path.join(self.tmp, "ledger.json")
        harvest.STATE = os.path.join(self.tmp, "state.json")
        self.np = np
        self.addCleanup(self._restore)

    def _restore(self):
        harvest._chroma_dir = self._chroma_dir
        harvest.LEDGER, harvest.STATE = self._paths
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _ledger(self, *keys):
        return {k: harvest._row(k, 1, 1.0, "signed", None, "then", "e", {}) for k in keys}

    def _sig_file(self, key, chroma):
        """Write a real signature file the held check finds and _load_sig reads."""
        self.np.save(os.path.join(self.cache, key + ".npy"), chroma)

    def test_a_failed_load_is_not_counted_and_stays_pending(self):
        state = {"matches": [], "kept": 0, "scored": {}}
        good, bad = "u" + "a" * 20, "u" + "b" * 20
        ledger = self._ledger(good, bad)
        qs = [(4, None, "4:fp")]
        chroma = self.np.zeros((12, 8), dtype="float32")
        # Only the good key has a signature file on disk; the bad key is held only by the
        # bucket's listing, so unscored_pairs proposes both -- but _load_sig returns None
        # for the bad key (a bucket fetch that failed, or a corrupt load).
        self._sig_file(good, chroma)
        held = {bad + ".npy": "e", good + ".npy": "e"}

        def _load(key):
            path = os.path.join(self.cache, key + ".npy")
            try:
                return self.np.load(path).astype("float32")
            except (OSError, ValueError):
                return None

        # A no-match cost: the good key loads and is scored (recorded), but no hit is added.
        with unittest.mock.patch.object(harvest, "_load_sig", _load), \
                unittest.mock.patch.object(harvest._cm, "match",
                                          return_value=(1.0, 0, 0.0)), \
                unittest.mock.patch.object(harvest, "_remote_objects",
                                           lambda max_age_s=900: held):
            n = harvest.rescan(state, ledger, set(), qs)
        # Only the loadable pair was scored; the failed load is not counted.
        self.assertEqual(n, 1)
        # The loadable pair is recorded as scored; the failed one is not.
        self.assertEqual(state["scored"]["4:fp"], [good + ".npy"])
        # The failed pair is still pending: a fresh rescan still proposes it.
        with unittest.mock.patch.object(harvest, "_load_sig", _load), \
                unittest.mock.patch.object(harvest._cm, "match",
                                          return_value=(1.0, 0, 0.0)), \
                unittest.mock.patch.object(harvest, "_remote_objects",
                                           lambda max_age_s=900: held):
            still = harvest.unscored_pairs(state, ledger, set(), qs)
        self.assertEqual([p[3] for p in still], [bad],
                         "the failed-load pair stays pending, not stamped complete")

    def test_a_successful_no_match_score_is_counted(self):
        """A pair that loaded but did not match is still a completed score -- `rescan` must
        not over-correct and leave no-match pairs pending too. `score_cached` records the
        pair in `state["scored"]` once the signature loaded, whether it matched or not."""
        state = {"matches": [], "kept": 0, "scored": {}}
        key = "u" + "c" * 20
        ledger = self._ledger(key)
        qs = [(4, None, "4:fp")]
        chroma = self.np.zeros((12, 8), dtype="float32")
        self._sig_file(key, chroma)
        # A match cost above KEEP_CEILING -> score_cached returns None, but the pair IS
        # recorded as scored (the signature loaded and was considered).
        with unittest.mock.patch.object(harvest._cm, "match",
                                       return_value=(1.0, 0, 0.0)):
            n = harvest.rescan(state, ledger, set(), qs)
        self.assertEqual(n, 1, "a loaded-but-no-match pair is a completed score")
        self.assertEqual(state["scored"]["4:fp"], [key + ".npy"])


if __name__ == "__main__":
    unittest.main()


@unittest.skipIf(harvest is None, "needs the librosa venv")
class ANewMysteryMustSeeTheWholeCorpus(unittest.TestCase):
    """The harvester only ever walked `pending`. Once a URL reached `done` it was never looked at
    again -- so a mystery whose clip arrives LATER was scored only against candidates fetched after
    it. Every signature gathered before that point (the entire corpus, ~900 of them, built over
    weeks) would silently never be tested against MT8-MT11.

    Tim assumed the opposite, reasonably: "when they come, I assume they'll be searched against all
    current chroma signatures." The code did not honour that. Now it does, and it REMEMBERS which
    (signature, mystery) pairs it has already scored, so the work is done exactly once.
    """

    def _state(self):
        return {"matches": [], "kept": 0, "scored": {}}

    def _ledger(self, *urls):
        """One signed row per URL: since the first-start seed, the ledger's signed rows ARE
        the corpus -- every key the pool has ever held, not only this machine's decodes."""
        return {harvest._sig_key(u)[:-4]:
                harvest._row(harvest._sig_key(u)[:-4], 1, 1.0, "signed", None, "then",
                             "e", {}) for u in urls}

    def _held(self, *urls):
        """The bucket's listing as the held check reads it: one object per key."""
        return {harvest._sig_key(u): "e" for u in urls}

    def test_it_knows_what_it_has_already_scored(self):
        state, ledger = self._state(), self._ledger("u1", "u2")
        state["scored"]["4:fp"] = [harvest._sig_key("u1")]
        # The signature cache has no directory while the policy is dark, so the held check
        # falls to the bucket listing; a bucket holding both keys is enough.
        with unittest.mock.patch("os.path.exists", return_value=False), \
             unittest.mock.patch.object(harvest, "CACHE", "sig-cache-for-tests"), \
             unittest.mock.patch.object(harvest, "_remote_objects",
                                        lambda max_age_s=900: self._held("u1", "u2")):
            pairs = harvest.unscored_pairs(state, ledger, set(), [(4, None, "4:fp")])
        self.assertEqual([p[3] for p in pairs],
                         [harvest._sig_key("u2")[:-4]])   # u1 already met MT4; u2 is left

    def test_a_brand_new_mystery_re_scores_the_ENTIRE_pool(self):
        """The MT8 case: its clip lands, and every signature the pool holds must meet it."""
        state, ledger = self._state(), self._ledger("u1", "u2", "u3")
        state["scored"]["4:fp"] = [harvest._sig_key(u) for u in ("u1", "u2", "u3")]   # MT4 is done
        with unittest.mock.patch("os.path.exists", return_value=False), \
             unittest.mock.patch.object(harvest, "CACHE", "sig-cache-for-tests"), \
             unittest.mock.patch.object(harvest, "_remote_objects",
                                        lambda max_age_s=900: self._held("u1", "u2", "u3")):
            pairs = harvest.unscored_pairs(state, ledger, set(),
                                           [(4, None, "4:fp"), (8, None, "8:fp")])
        self.assertEqual(sorted(p[3] for p in pairs),
                         sorted(harvest._sig_key(u)[:-4] for u in ("u1", "u2", "u3")))  # all
        self.assertTrue(all(p[0] == 8 for p in pairs))                             # and only MT8
        # A ruled-out record is never offered again, not even for that new mystery --
        # that case moved to tests/test_harvest_rulings.py with the rest of the retirement.


@unittest.skipIf(harvest is None, "needs the librosa venv")
class ABetterClipMustNotInheritTheOldOnesVerdicts(unittest.TestCase):
    """Tim: "when I get the chance to add a new one, I don't want any false negatives from the 23s
    version."

    He was right to worry. `state["scored"]` was keyed on the mystery NUMBER, so a re-cut MT7 clip
    would have inherited every pairing made against the 23-second one: the harvester would think it
    had already asked, and never re-score a single signature against the better question. Silent,
    and exactly the false negatives he named.

    So the key carries a FINGERPRINT OF THE CLIP'S CONTENTS. Change the clip, and every pairing
    against the old one is void.
    """

    def _q(self, num, fp):
        return [(num, None, "%d:%s" % (num, fp))]

    def _ledger(self, *urls):
        return {harvest._sig_key(u)[:-4]:
                harvest._row(harvest._sig_key(u)[:-4], 1, 1.0, "signed", None, "then",
                             "e", {}) for u in urls}

    def _held(self, *urls):
        return {harvest._sig_key(u): "e" for u in urls}

    def test_re_cutting_the_clip_voids_every_old_pairing(self):
        state = {"matches": [], "scored": {"7:oldclip123": [harvest._sig_key(u)
                                                            for u in ("u1", "u2", "u3")]}}
        ledger = self._ledger("u1", "u2", "u3")
        with unittest.mock.patch("os.path.exists", return_value=False), \
             unittest.mock.patch.object(harvest, "CACHE", "sig-cache-for-tests"), \
             unittest.mock.patch.object(harvest, "_remote_objects",
                                        lambda max_age_s=900: self._held("u1", "u2", "u3")):
            pairs = harvest.unscored_pairs(state, ledger, set(), self._q(7, "NEWclip456"))
        self.assertEqual(sorted(p[3] for p in pairs),
                         sorted(harvest._sig_key(u)[:-4] for u in ("u1", "u2", "u3")),
                         "a new clip must ask the WHOLE pool again")

    def test_the_same_clip_is_not_re_scored(self):
        state = {"matches": [], "scored": {"7:same": [harvest._sig_key("u1")]}}
        ledger = self._ledger("u1")
        with unittest.mock.patch("os.path.exists", return_value=False), \
             unittest.mock.patch.object(harvest, "CACHE", "sig-cache-for-tests"), \
             unittest.mock.patch.object(harvest, "_remote_objects",
                                        lambda max_age_s=900: self._held("u1")):
            self.assertEqual(harvest.unscored_pairs(state, ledger, set(), self._q(7, "same")),
                             [])

    def test_forget_drops_the_leads_and_the_pairings(self):
        state = {"matches": [{"mystery": 7, "url": "a"}, {"mystery": 7, "url": "b"},
                             {"mystery": 4, "url": "keep"}],
                 "scored": {"7:x": ["s1"], "4:y": ["s2"]}}
        leads, pairs = harvest.forget(state, 7)
        self.assertEqual((leads, pairs), (2, 1))
        self.assertEqual([m["mystery"] for m in state["matches"]], [4])   # MT4 untouched
        self.assertEqual(list(state["scored"]), ["4:y"])

    def test_a_clip_too_short_to_distinguish_records_is_refused(self):
        """MT7's 23s clip produced five 'confident' false positives within 0.0007 of each other."""
        self.assertEqual(harvest.MIN_QUERY_S, 60.0)
        self.assertLess(23, harvest.MIN_QUERY_S)


@unittest.skipIf(harvest is None, "needs the librosa venv")
@unittest.skipUnless(HAVE_AUDIO, "audio deps unavailable -- see requirements-streamalign.txt")
class TheLiveCanaryMustNotCrashTheHarvester(unittest.TestCase):
    """The third crash-on-a-rarely-taken-branch, caught before it fired.

    `queries()` grew a third field (a query key fingerprinting the clip). `selftest`'s rival loop
    still said `for num, q in ...` -- two names, three values -- so the next daily canary would have
    raised ValueError and killed the harvester, ~15 hours after merge.

    NOTHING caught it. Not the suite: no test drove `live()`, and a first attempt at one was
    WORTHLESS -- a fake fetch makes live() return long before the loop, so the test passed against
    the broken code. Not pyflakes either: an unpack arity error is not an undefined name, so the
    lint guard added for exactly this family is blind to it.

    Same shape as the KeyError before it. A branch no test exercises is a branch that fails in
    production -- so the branch is now a function (`best_rival_cost`), and the function is tested
    against the REAL shape `queries()` returns.
    """

    def test_it_survives_the_real_shape_queries_returns(self):
        import selftest
        import numpy as np
        chroma = np.ones((12, 40), dtype="float32")
        triples = [(4, chroma, "4:abc123"), (6, chroma, "6:def456")]   # what queries() yields TODAY
        try:
            cost, n = selftest.best_rival_cost(triples, chroma)
        except ValueError as e:
            self.fail("cannot walk the query list: %s" % e)            # the exact bug
        self.assertIsInstance(cost, float)
        self.assertEqual(n, 2)

    def test_the_two_field_contract_still_works(self):
        """Its stated contract is (number, chroma). Extra fields are the caller's business."""
        import selftest
        import numpy as np
        chroma = np.ones((12, 40), dtype="float32")
        cost, n = selftest.best_rival_cost([(4, chroma)], chroma)
        self.assertIsInstance(cost, float)
        self.assertEqual(n, 1)

    def test_no_mysteries_is_not_a_free_pass(self):
        """With nothing to beat, the canary must not be handed a win by default."""
        import selftest
        import numpy as np
        cost, n = selftest.best_rival_cost([], np.ones((12, 40), dtype="float32"))
        self.assertEqual((cost, n), (1.0, 0))   # no rival = nothing beaten, not a free pass


@unittest.skipIf(harvest is None, "needs the librosa venv")
class TheRunTakesTheWriterLock(unittest.TestCase):
    """The writer lock is harvest.py's own: the two writers are run() and the hand tool
    --sign-one, and each must keep taking the lock, one at a time, or two processes interleave
    their writes of the same state.json and ledger.json. Pinned here because the lock outlived
    the runtime it was shared with: a run that quietly stopped taking it would bring the
    two-writers-one-file loss back."""

    def test_run_refuses_to_start_while_another_writer_holds_the_lock(self):
        tmp = tempfile.mkdtemp(prefix="writer-lock-")
        self.addCleanup(shutil.rmtree, tmp, True)
        paths = (harvest.STATE_DIR, harvest.STATE, harvest.LEDGER,
                 harvest.WRITER_LOCK, harvest.RULINGS)
        harvest.STATE_DIR = os.path.join(tmp, "harvest")   # the lock's makedirs, on the throwaway
        harvest.STATE = os.path.join(tmp, "state.json")
        harvest.LEDGER = os.path.join(tmp, "ledger.json")
        harvest.WRITER_LOCK = os.path.join(tmp, "writer.lock")
        # a throwaway rulings file, so the refusal under test is the lock's and not the file's
        harvest.RULINGS = os.path.join(tmp, "rulings.json")
        harvest._save(harvest.RULINGS, {})
        self.addCleanup(lambda: (setattr(harvest, "STATE_DIR", paths[0]),
                                 setattr(harvest, "STATE", paths[1]),
                                 setattr(harvest, "LEDGER", paths[2]),
                                 setattr(harvest, "WRITER_LOCK", paths[3]),
                                 setattr(harvest, "RULINGS", paths[4])))
        first = harvest.acquire_writer_lock()
        self.assertIsNotNone(first)
        self.addCleanup(first.close)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            harvest.run(None)
        self.assertIn("ONE writer, always", out.getvalue())
        self.assertFalse(os.path.exists(harvest.STATE),
                         "a refused start writes no state: the second writer never ran")

        # the lock free again -> the SAME run gets past the gate (and past the rulings read)
        class _Past(Exception):
            pass

        def boom(state=None):
            raise _Past()

        first.close()
        # run() opens the lock file itself on the way in; the boom raised out of queries()
        # would leave that handle open (a ResourceWarning over the temp lock file), so this
        # phase takes the lock through a recorder and the cleanup closes whatever it got.
        real_acquire = harvest.acquire_writer_lock
        acquired = []
        self.addCleanup(lambda: [fh.close() for fh in acquired])

        def record_then_return():
            fh = real_acquire()
            if fh is not None:
                acquired.append(fh)
            return fh

        with unittest.mock.patch.object(harvest, "acquire_writer_lock", record_then_return), \
                unittest.mock.patch.object(harvest, "queries", boom), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(_Past):
                harvest.run(None)
