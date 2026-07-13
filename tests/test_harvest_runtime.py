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

import os
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)

try:
    import harvest
except Exception:                       # librosa/numba absent -> not this test's job
    harvest = None


class NoUndefinedNames(unittest.TestCase):
    """A NameError in a rarely-taken branch is invisible to a test suite and fatal in production."""

    def test_pyflakes_is_clean_on_the_harvester(self):
        try:
            import pyflakes  # noqa: F401
        except ImportError:
            self.skipTest("pyflakes not installed")
        for script in ("harvest.py", "selftest.py"):
            with self.subTest(script=script):
                r = subprocess.run([sys.executable, "-m", "pyflakes",
                                    os.path.join(SCRIPTS, script)],
                                   capture_output=True, text=True)
                # undefined names are the fatal class; unused imports are noise we tolerate
                fatal = [l for l in r.stdout.splitlines() if "undefined name" in l]
                self.assertEqual(fatal, [], "\n".join(fatal))

    @unittest.skipIf(harvest is None, "needs the librosa venv")
    def test_run_can_reach_its_dependencies(self):
        """The specific bug: run() calls selftest.offline(). If the import is missing this is a
        NameError the moment the harvester starts."""
        self.assertTrue(hasattr(harvest, "selftest"))
        self.assertTrue(callable(harvest.selftest.offline))


@unittest.skipIf(harvest is None, "needs the librosa venv")
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


if __name__ == "__main__":
    unittest.main()
