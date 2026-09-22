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
added alongside: the excerpt hard cap, and the bot-wall halt.
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
class TheBotWall(unittest.TestCase):
    """"Sign in to confirm you're not a bot" carries no 403 and no 429, so it slipped straight past
    the host-backoff logic. The harvester ground through the queue failing identically on every
    item, analysing nothing, and the dashboard cheerfully said "waiting on youtube.com" in yellow."""

    def test_the_real_error_youtube_actually_sends_is_recognised(self):
        real = ("ERROR: [youtube] T6BZ5BYdp_I: Sign in to confirm you're not a bot. "
                "Use --cookies-from-browser or --cookies for the authentication.")
        self.assertTrue(harvest.is_bot_wall(real))

    def test_it_is_not_confused_with_an_ordinary_failure(self):
        for benign in ("HTTP Error 404: Not Found", "Video unavailable", "", None,
                       "HTTP Error 429: Too Many Requests"):   # 429 IS handled -- by backoff
            with self.subTest(err=benign):
                self.assertFalse(harvest.is_bot_wall(benign))

    def test_cookies_are_off_unless_asked_for(self):
        for k in ("NETRADIO_YTDLP_COOKIES", "NETRADIO_YTDLP_COOKIES_FROM_BROWSER"):
            os.environ.pop(k, None)
        self.assertEqual(harvest.cookie_args(), [])

    def test_a_browser_can_be_named(self):
        os.environ["NETRADIO_YTDLP_COOKIES_FROM_BROWSER"] = "chrome"
        self.addCleanup(os.environ.pop, "NETRADIO_YTDLP_COOKIES_FROM_BROWSER", None)
        self.assertEqual(harvest.cookie_args(), ["--cookies-from-browser", "chrome"])

    def test_a_cookie_file_wins_and_must_actually_exist(self):
        os.environ["NETRADIO_YTDLP_COOKIES"] = "/nope/missing.txt"
        self.addCleanup(os.environ.pop, "NETRADIO_YTDLP_COOKIES", None)
        self.assertEqual(harvest.cookie_args(), [])          # a path that isn't there is not a cookie

        fh = tempfile.NamedTemporaryFile(suffix=".txt", delete=False)
        fh.close()
        self.addCleanup(os.unlink, fh.name)
        os.environ["NETRADIO_YTDLP_COOKIES"] = fh.name
        self.assertEqual(harvest.cookie_args(), ["--cookies", fh.name])


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


class ARulingSpendsTheExcerpt(unittest.TestCase):
    """The excerpt exists so a human can confirm or reject the lead by ear. Once they have --
    match, not-a-match, heard -- that purpose is spent, and only the 30-day TTL sweep would ever
    have reclaimed the audio. `drop_ruled_excerpts` reclaims it on the next pass instead.

    The LEAD must survive whole: the score is the record, the audio was only ever the evidence.
    The deletion itself goes through the cache policy's door, so the excerpt board must be
    registered over this test's directory (a throwaway root, put back afterwards).
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self._saved = {k: os.environ.get(k) for k in list(os.environ)
                       if k.startswith("NETRADIO_") and ("CACHE" in k or k in CACHE_ENV)}
        for k in self._saved:
            os.environ.pop(k, None)
        self.addCleanup(self._restore)
        # The policy's root lives OUTSIDE the excerpt board (no cache may hold its own root),
        # in a second throwaway directory.
        self._root = tempfile.mkdtemp(prefix="candidates-policy-root-")
        os.environ["NETRADIO_CACHE_ROOT"] = self._root
        os.environ["NETRADIO_CANDIDATES_CACHE_DIR"] = self.dir
        self._registry = dict(cache_budget._REGISTRY), dict(cache_budget._STATS)
        cache_budget._REGISTRY.clear()
        cache_budget._STATS.clear()
        harvest.register_caches()

    def _restore(self):
        cache_budget._REGISTRY.clear()
        cache_budget._REGISTRY.update(self._registry[0])
        cache_budget._STATS.clear()
        cache_budget._STATS.update(self._registry[1])
        for k in [k for k in list(os.environ)
                  if k.startswith("NETRADIO_") and ("CACHE" in k or k in CACHE_ENV)]:
            os.environ.pop(k, None)
        os.environ.update(self._saved)
        shutil.rmtree(self._root, ignore_errors=True)

    def _wav(self, name):
        path = os.path.join(self.dir, name)
        open(path, "wb").close()
        return path

    def test_a_ruled_leads_audio_goes_and_its_numbers_stay(self):
        wav = self._wav("MT4-0.0603-aaaa.wav")
        state = {"kept": 1, "matches": [{"mystery": 4, "cost": 0.0603, "url": "u1",
                                         "at_s": 12.0, "verdict": "near", "audio": wav}]}
        self.assertEqual(harvest.drop_ruled_excerpts(state, {"u1"}), 1)
        self.assertFalse(os.path.exists(wav))
        m = state["matches"][0]
        self.assertNotIn("audio", m)
        self.assertEqual((m["url"], m["cost"], m["at_s"]), ("u1", 0.0603, 12.0))
        self.assertEqual(state["kept"], 0)

    def test_an_unruled_lead_keeps_its_excerpt(self):
        wav = self._wav("MT4-0.0603-bbbb.wav")
        state = {"kept": 1, "matches": [{"mystery": 4, "cost": 0.0603, "url": "u1", "audio": wav}]}
        self.assertEqual(harvest.drop_ruled_excerpts(state, {"someone-else"}), 0)
        self.assertTrue(os.path.exists(wav))
        self.assertEqual(state["matches"][0]["audio"], wav)
        self.assertEqual(state["kept"], 1)

    def test_a_ruled_lead_with_no_audio_is_a_no_op(self):
        """The normal case after --purge-audio, and for every rescan-found lead."""
        state = {"kept": 0, "matches": [{"mystery": 4, "cost": 0.06, "url": "u1"}]}
        self.assertEqual(harvest.drop_ruled_excerpts(state, {"u1"}), 0)
        self.assertEqual(state["kept"], 0)

    def test_an_already_gone_file_still_clears_the_row(self):
        """TTL sweep or a hand-rm got there first; the row must stop advertising audio anyway."""
        state = {"kept": 1, "matches": [{"mystery": 4, "cost": 0.06, "url": "u1",
                                         "audio": os.path.join(self.dir, "never-existed.wav")}]}
        self.assertEqual(harvest.drop_ruled_excerpts(state, {"u1"}), 1)
        self.assertNotIn("audio", state["matches"][0])
        self.assertEqual(state["kept"], 0)

    def test_kept_never_goes_negative(self):
        state = {"kept": 0, "matches": [{"mystery": 4, "cost": 0.06, "url": "u1",
                                         "audio": self._wav("MT4-0.06-cccc.wav")}]}
        harvest.drop_ruled_excerpts(state, {"u1"})
        self.assertEqual(state["kept"], 0)


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
        self.assertEqual(harvest.sig_path("https://example.invalid/x"),
                         os.path.join(self.chroma_dir, harvest._sig_key("https://example.invalid/x")))

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
        self.assertIsNone(harvest.sig_path("https://example.invalid/x"))

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

    def test_purge_audio_deletes_through_the_policy(self):
        """Every deletion of a cache entry goes through the policy's one door, so it is
        checked against the cache's directory and recorded with its reason. A bare
        `os.unlink` here would empty the board behind the policy's back, leaving its
        accounting and its event log describing a cache that no longer holds what they say."""
        state = os.path.join(self.tmp, "state.json")
        saved = harvest.STATE
        harvest.STATE = state
        self.addCleanup(setattr, harvest, "STATE", saved)
        with open(state, "w") as fh:
            json.dump({"matches": [{"url": "u1", "audio": "x"}], "kept": 1}, fh)
        self._excerpt("MT4-0.0600-one.wav", 100)
        self._excerpt("MT4-0.0700-two.wav", 100)
        note = os.path.join(self.keep_dir, "PROVENANCE.txt")
        with open(note, "w") as fh:
            fh.write("note")
        harvest.purge_audio()
        self.assertEqual(sorted(os.listdir(self.keep_dir)), ["PROVENANCE.txt"],
                         "every excerpt went; the note is not audio and stays")
        removals = [e for e in self._events() if e["event"] == "remove"]
        self.assertEqual(sorted((e["entry"], e["reason"]) for e in removals),
                         [("MT4-0.0600-one.wav", "purge-audio"),
                          ("MT4-0.0700-two.wav", "purge-audio")],
                         "the policy recorded each removal, with the caller's reason")

    def test_a_board_trimmed_back_to_keep_top_deletes_through_the_policy(self):
        """`evict_overfull` trims a mystery's board to the best KEEP_TOP. The row goes from
        the state whatever happens, but the FILE is the policy's to delete and to record --
        a bare unlink would leave the cache's accounting describing an entry that is gone."""
        matches, paths = [], []
        for i in range(harvest.KEEP_TOP + 1):
            name = "MT4-%.4f-%02d.wav" % (0.01 * i, i)
            paths.append(self._excerpt(name, 100))
            matches.append({"mystery": 4, "cost": 0.01 * i, "url": "u%d" % i,
                            "audio": paths[-1]})
        state = {"matches": matches, "kept": len(matches)}
        harvest.evict_overfull(state, 4)
        self.assertEqual(len(state["matches"]), harvest.KEEP_TOP)
        self.assertEqual(state["kept"], harvest.KEEP_TOP)
        self.assertFalse(os.path.exists(paths[-1]), "the priciest row's excerpt went")
        removals = [e for e in self._events() if e["event"] == "remove"]
        self.assertEqual([(e["entry"], e["reason"]) for e in removals],
                         [(os.path.basename(paths[-1]), "board-overfull")],
                         "through the policy's door, recorded with its reason")

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
    mode refuses (run(), the collector's two gates, --migrate-sigs, --requeue-missing-sigs).
    It refuses now, before the query set is even read, with the message --migrate-sigs uses."""

    def test_rescan_refuses_before_the_query_set_is_read(self):
        tmp = tempfile.mkdtemp(prefix="rescan-dark-")
        self.addCleanup(shutil.rmtree, tmp, True)
        paths = harvest.STATE_DIR, harvest.STATE, harvest.QUEUE
        harvest.STATE_DIR = os.path.join(tmp, "harvest")
        harvest.STATE = os.path.join(tmp, "state.json")
        harvest.QUEUE = os.path.join(tmp, "queue.json")
        self.addCleanup(lambda: (setattr(harvest, "STATE_DIR", paths[0]),
                                  setattr(harvest, "STATE", paths[1]),
                                  setattr(harvest, "QUEUE", paths[2])))
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
                unittest.mock.patch.object(harvest, "listen_queue_split",
                                          refused("ruled-on set")), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            harvest.main()
        self.assertIn("the signature cache is dark", out.getvalue())
        self.assertIn("NETRADIO_CACHE_ROOT", out.getvalue())
        self.assertFalse(os.path.exists(harvest.STATE),
                         "a refused rescan writes no state: rescan_pending was never "
                         "stamped to a completion it did not do")


@unittest.skipIf(harvest is None, "needs the librosa venv")
class TheOtherCacheReadingModesRefuseADarkPolicy(unittest.TestCase):
    """`--rescan` is not the only mode that reads the signature cache, and the other two
    refuse for their own reasons. Both refusals are the branch's, and neither was held by a
    test: deleting either left the whole suite green.

    `--migrate-sigs` walks the cache directory. With the cache dark there is no directory
    at all, so without the gate it reaches `os.path.isdir(None)` and dies with a TypeError
    where it should print the setting to fix.

    `requeue_missing_sigs` asks of every done URL "is its signature still held?". A dark
    cache has no local half, so every answer is no; with the store dark too, the remote
    half is empty as well, and the whole corpus reads as lost -- either a false "the store
    broke" alert or a requeue of everything. It is the same epistemic refusal the
    unlistable bucket gets: cannot tell, do nothing."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dark-modes-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._paths = harvest.STATE_DIR, harvest.STATE, harvest.QUEUE, harvest.WRITER_LOCK
        harvest.STATE_DIR = os.path.join(self.tmp, "harvest")
        harvest.STATE = os.path.join(self.tmp, "state.json")
        harvest.QUEUE = os.path.join(self.tmp, "queue.json")
        harvest.WRITER_LOCK = os.path.join(self.tmp, "writer.lock")
        self.addCleanup(self._restore_paths)
        self._saved = {k: os.environ.get(k) for k in list(os.environ)
                       if k.startswith("NETRADIO_") and ("CACHE" in k or k in CACHE_ENV)}
        for k in self._saved:
            os.environ.pop(k, None)
        self._attrs = (harvest.CACHE, harvest.KEEP, harvest._CACHE_AT_IMPORT,
                       harvest._KEEP_AT_IMPORT)
        self._registry = dict(cache_budget._REGISTRY), dict(cache_budget._STATS)
        self.addCleanup(self._restore_policy)
        cache_budget._REGISTRY.clear()
        cache_budget._STATS.clear()
        harvest.register_caches()             # re-read: the signature cache is dark
        self.assertIsNone(harvest._chroma_dir())

    def _restore_paths(self):
        (harvest.STATE_DIR, harvest.STATE, harvest.QUEUE,
         harvest.WRITER_LOCK) = self._paths

    def _restore_policy(self):
        cache_budget._REGISTRY.clear()
        cache_budget._REGISTRY.update(self._registry[0])
        cache_budget._STATS.clear()
        cache_budget._STATS.update(self._registry[1])
        for k in [k for k in list(os.environ)
                  if k.startswith("NETRADIO_") and ("CACHE" in k or k in CACHE_ENV)]:
            os.environ.pop(k, None)
        os.environ.update(self._saved)
        for name, value in zip(("CACHE", "KEEP", "_CACHE_AT_IMPORT", "_KEEP_AT_IMPORT"),
                               self._attrs):
            setattr(harvest, name, value)

    def test_migrate_sigs_refuses_instead_of_crashing_on_a_directory_that_is_none(self):
        """The store is configured -- so the mode is past its own sigstore gate -- and the
        cache is dark. It must name the setting and stop, not walk a directory that does
        not exist: `os.path.isdir(None)` is a TypeError, a traceback where an operator
        needs a sentence."""
        uploaded = []
        argv = ["harvest.py", "--migrate-sigs"]
        with unittest.mock.patch.object(sys, "argv", argv), \
                unittest.mock.patch.object(harvest.sigstore, "enabled", lambda: True), \
                unittest.mock.patch.object(harvest.sigstore, "put",
                                          lambda *a: uploaded.append(a) or True), \
                unittest.mock.patch.object(harvest.sigstore, "evict_cold",
                                          lambda *a: (_ for _ in ()).throw(
                                              AssertionError("refused before any eviction"))), \
                unittest.mock.patch.object(harvest, "queries",
                                          lambda state=None: (_ for _ in ()).throw(
                                              AssertionError("refused before the query set"))), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            harvest.main()                     # no TypeError escapes: that is half the test
        self.assertIn("the signature cache is dark", out.getvalue())
        self.assertIn("NETRADIO_CACHE_ROOT", out.getvalue())
        self.assertEqual(uploaded, [], "nothing was uploaded, and nothing was listed")
        self.assertFalse(os.path.exists(harvest.STATE), "a refused migrate writes no state")

    def test_requeue_missing_sigs_cannot_tell_lost_from_held_and_does_nothing(self):
        """A dark cache makes every done signature read as lost. The refusal says so and
        stops: nothing is requeued, and no "the store broke" alert is stamped over a
        corpus that is intact."""
        state = harvest.blank_state()
        q = {"pending": [], "done": ["https://example.invalid/%d" % i for i in range(10)]}
        with unittest.mock.patch.object(harvest.sigstore, "enabled", lambda: False):
            res = harvest.requeue_missing_sigs(state, q, set())
        self.assertIn("the signature cache is dark", res["why"])
        self.assertIn("NETRADIO_CACHE_ROOT", res["why"])
        self.assertEqual((res["requeued"], res["reported"]), (0, False))
        self.assertEqual(res["missing"], 0, "nothing was even counted as lost")
        self.assertEqual(q["done"], ["https://example.invalid/%d" % i for i in range(10)],
                         "the corpus stayed where it was")
        self.assertEqual(q["pending"], [], "and nothing was requeued behind it")
        self.assertEqual(state.get("issues") or [], [],
                         "no false 'the store broke' row was stamped")

    def test_the_on_demand_requeue_mode_prints_that_refusal_and_writes_nothing(self):
        """...and the CLI mode that calls it reports the refusal rather than a recovery.
        `--requeue-missing-sigs` has no gate of its own: this refusal is the only one."""
        with open(harvest.QUEUE, "w") as fh:
            json.dump({"pending": [], "done": ["https://example.invalid/x"]}, fh)
        argv = ["harvest.py", "--requeue-missing-sigs"]
        with unittest.mock.patch.object(sys, "argv", argv), \
                unittest.mock.patch.object(harvest.sigstore, "enabled", lambda: False), \
                unittest.mock.patch.object(harvest, "listen_queue_split",
                                          lambda: ([], set())), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            harvest.main()
        self.assertIn("the signature cache is dark", out.getvalue())
        self.assertFalse(os.path.exists(harvest.STATE),
                         "nothing was requeued or reported, so no state was written")
        with open(harvest.QUEUE) as fh:
            self.assertEqual(json.load(fh),
                             {"pending": [], "done": ["https://example.invalid/x"]},
                             "the queue is untouched")


if __name__ == "__main__":
    unittest.main()


@unittest.skipIf(harvest is None, "needs the librosa venv")
class OurOwnUploadsAreNeverAnalysed(unittest.TestCase):
    """A harvester that "finds" one of Tim's own uploads has rediscovered its own question and
    would report a triumphant ~0.00.

    The guard used to match ONLY the title `Mystery Track N`, justified by "listen-queue entries
    carry no channel or uploader field, only a title". That was false -- they carry `origin` -- and
    it cost us: NINE of his uploads sat in the PENDING queue, uncaught, because they are titled
    "ID #1", "ID #2" and "Wave Forms [in the mix, low quality]". None contains the word "mystery".
    The last is an excerpt of the mix itself.
    """

    def test_the_nine_real_titles_that_slipped_through(self):
        for title in ("ID #1", "ID #2", "Wave Forms [in the mix, low quality]",
                      "Bunny!", "Canadian geese in Strandhill", "Crepe Suzette Supremo"):
            with self.subTest(title=title):
                item = {"title": title, "origin": "subscription:  Tim Hunter"}
                self.assertTrue(harvest._is_own_clip(item), "%r must be refused" % title)

    def test_the_title_net_still_catches_a_clip_with_no_origin(self):
        """Belt and braces: an entry that never carried an origin is still caught by its title."""
        self.assertTrue(harvest._is_own_clip({"title": "Mystery Track 8", "origin": ""}))
        self.assertTrue(harvest._is_own_clip({"title": "Netradio Mystery 3"}))

    def test_a_real_record_is_still_analysed(self):
        """The guard must not be so broad that it refuses the corpus we are searching. Real records
        really are called things like this."""
        for title in ("No Mystery", "Mystery Blend", "Big Bud - Tahoe"):
            with self.subTest(title=title):
                item = {"title": title, "origin": "subscription:Back 2 The Old Skool Era"}
                self.assertFalse(harvest._is_own_clip(item))


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

    def test_it_knows_what_it_has_already_scored(self):
        state, q = self._state(), {"done": ["u1", "u2"], "pending": []}
        state["scored"]["4:fp"] = [harvest._sig_key("u1")]
        # The signature cache has no directory while the policy is dark, so sig_path would be
        # None and every candidate would read as unheld; CACHE is patched to a stand-in
        # directory and os.path.exists is made to say the signature is there.
        with unittest.mock.patch("os.path.exists", return_value=True), \
             unittest.mock.patch.object(harvest, "CACHE", "sig-cache-for-tests"):
            pairs = harvest.unscored_pairs(state, q, set(), [(4, None, "4:fp")])
        self.assertEqual([p[3] for p in pairs], ["u2"])      # u1 already met MT4; only u2 is left

    def test_a_brand_new_mystery_re_scores_the_ENTIRE_cache(self):
        """The MT8 case: its clip lands, and every signature we already hold must meet it."""
        state, q = self._state(), {"done": ["u1", "u2", "u3"], "pending": []}
        state["scored"]["4:fp"] = [harvest._sig_key(u) for u in ("u1", "u2", "u3")]   # MT4 is done
        with unittest.mock.patch("os.path.exists", return_value=True), \
             unittest.mock.patch.object(harvest, "CACHE", "sig-cache-for-tests"):
            pairs = harvest.unscored_pairs(state, q, set(),
                                           [(4, None, "4:fp"), (8, None, "8:fp")])
        self.assertEqual(sorted(p[3] for p in pairs), ["u1", "u2", "u3"])          # all, for MT8
        self.assertTrue(all(p[0] == 8 for p in pairs))                             # and only MT8

    def test_a_ruled_out_record_is_never_offered_again_not_even_for_a_new_mystery(self):
        """'not a match' means not a match for ANYTHING we are waiting for. Without this, the day
        MT8 lands, every record Tim already rejected comes straight back at him."""
        state, q = self._state(), {"done": ["keep", "rejected"], "pending": []}
        with unittest.mock.patch("os.path.exists", return_value=True), \
             unittest.mock.patch.object(harvest, "CACHE", "sig-cache-for-tests"):
            pairs = harvest.unscored_pairs(state, q, {"rejected"}, [(8, None, "8:fp")])
        self.assertEqual([p[3] for p in pairs], ["keep"])

    def test_not_a_match_retires_an_entry(self):
        """The player writes the flag; the harvester must honour it."""
        self.assertIn("not_a_match", harvest.RULED_ON)


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

    def test_re_cutting_the_clip_voids_every_old_pairing(self):
        state = {"matches": [], "scored": {"7:oldclip123": [harvest._sig_key(u)
                                                            for u in ("u1", "u2", "u3")]}}
        q = {"done": ["u1", "u2", "u3"], "pending": []}
        with unittest.mock.patch("os.path.exists", return_value=True), \
             unittest.mock.patch.object(harvest, "CACHE", "sig-cache-for-tests"):
            pairs = harvest.unscored_pairs(state, q, set(), self._q(7, "NEWclip456"))
        self.assertEqual(sorted(p[3] for p in pairs), ["u1", "u2", "u3"],
                         "a new clip must ask the WHOLE corpus again")

    def test_the_same_clip_is_not_re_scored(self):
        state = {"matches": [], "scored": {"7:same": [harvest._sig_key("u1")]}}
        q = {"done": ["u1"], "pending": []}
        with unittest.mock.patch("os.path.exists", return_value=True), \
             unittest.mock.patch.object(harvest, "CACHE", "sig-cache-for-tests"):
            self.assertEqual(harvest.unscored_pairs(state, q, set(), self._q(7, "same")), [])

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
