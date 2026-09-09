"""Chunk URLs: `#t=<a>,<b>` cuts the decode, and an unsplit master is refused whole.

A queue entry for audio longer than two hours arrives split into chunks — the same URL once per
slice, each carrying a W3C media fragment. yt-dlp ignores URL fragments, so if the harvester
ignored them too, every chunk would fetch, decode and sign the WHOLE master: the cost splitting
exists to avoid, and one signature filed under every chunk's key. Two things are pinned here:

  * **The cut is real, and only where it is asked for.** The ffmpeg leg gains `-ss` before `-i`
    and `-t` after it for a chunk URL, and for every other URL its argv is byte-identical to what
    it was before this existed — including a fragment that is malformed, which is not a chunk
    request and must not become a half-applied one.
  * **A refusal is not a truncation.** An unsplit master past the four-hour backstop is skipped
    with an issues row. Analysing its first four hours and filing that under the URL would be a
    partial answer wearing a complete one's clothes.

Nothing here touches the network: every `yt-dlp` and `ffmpeg` is a fake object and the queue is a
temporary file.
"""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)

try:
    import harvest                      # noqa: E402
except Exception:                       # librosa/numpy not installed -> not this test's job
    harvest = None


class _FakeProc:
    """Just enough of `Popen` for the decode path."""

    def __init__(self, argv):
        self.argv = argv
        self.returncode = 0
        self.stdout = io.BytesIO()
        self.stderr = io.BytesIO(b"")

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self.returncode

    def terminate(self):
        pass

    def kill(self):
        pass


@unittest.skipUnless(harvest is not None, "harvest.py needs librosa/numpy -- not this test's job")
class TheFragment(unittest.TestCase):
    """What counts as a chunk request, and what is just a URL with a `#` in it."""

    def test_a_well_formed_fragment_reads_as_a_cut(self):
        self.assertEqual(harvest.media_fragment("https://y/watch?v=a#t=0,600"), (0, 600))
        self.assertEqual(harvest.media_fragment("https://y/watch?v=a#t=7200,14400"), (7200, 14400))

    def test_no_fragment_is_no_cut(self):
        self.assertIsNone(harvest.media_fragment("https://y/watch?v=a"))
        self.assertIsNone(harvest.media_fragment("https://y/watch?v=a#"))

    def test_a_malformed_fragment_is_not_half_applied(self):
        """Each of these is a URL with a `#` in it, not an instruction to cut audio."""
        for frag in ("#t=5",              # one number: start of what, end of what?
                     "#t=600,60",         # backwards
                     "#t=60,60",          # empty slice
                     "#t=-1,5",           # negative
                     "#t=x,y",            # not numbers
                     "#t=1.5,2.5",        # not whole seconds
                     "#t=1,2,3",          # three
                     "#t=1,2&loop",       # something else rode along
                     "#start=1,2"):       # a different fragment entirely
            with self.subTest(frag=frag):
                self.assertIsNone(harvest.media_fragment("https://y/watch?v=a" + frag))

    def test_each_chunk_of_one_master_has_its_own_key(self):
        """The fragment stays in the URL, so the signature key is per chunk -- which is the whole
        mechanism: two chunks of one master must not share a cached signature."""
        base = "https://y/watch?v=long"
        first = harvest.sig_path(base + "#t=0,7200")
        second = harvest.sig_path(base + "#t=7200,14400")
        self.assertNotEqual(first, second)
        self.assertNotEqual(first, harvest.sig_path(base))


@unittest.skipUnless(harvest is not None, "harvest.py needs librosa/numpy -- not this test's job")
class TheFfmpegLeg(unittest.TestCase):
    """The argv the decode actually runs, for a chunk URL and for an ordinary one."""

    # What the ffmpeg leg was before chunks existed. An ordinary URL must still produce EXACTLY
    # this, element for element -- a fetch that changed shape for every candidate would be a much
    # bigger change than the one being made here.
    BEFORE = ["ffmpeg", "-v", "error", "-i", "pipe:0",
              "-ac", "1", "-ar", None, "-f", "f32le", "pipe:1"]

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.job = os.path.join(self.tmp, "job")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(harvest._STOP.update,
                        {"signum": 0, "child": None, "procs": [], "part": None})

    def _ffmpeg_argv(self, url):
        """Run the decode far enough to see ffmpeg's argv. It writes nothing: the fake decode
        spools no PCM, so the run ends at "no audio" before any signature could be written."""
        seen = {}

        def _popen(argv, **kwargs):
            if "ffmpeg" in argv[0]:
                seen["argv"] = argv
            return _FakeProc(argv)

        with mock.patch.object(harvest.subprocess, "Popen", _popen):
            result = harvest._fetch_and_sign(url, self.job)
        self.assertFalse(result["ok"])          # nothing decoded; no signature either way
        return seen["argv"]

    def _expected_before(self):
        return [str(harvest._audio.SR) if a is None else a for a in self.BEFORE]

    def test_an_ordinary_url_is_decoded_exactly_as_before(self):
        self.assertEqual(self._ffmpeg_argv("https://y/watch?v=plain"), self._expected_before())

    def test_a_malformed_fragment_is_decoded_exactly_as_before(self):
        for frag in ("#t=5", "#t=600,60", "#t=-1,5", "#t=x,y"):
            with self.subTest(frag=frag):
                self.assertEqual(self._ffmpeg_argv("https://y/watch?v=a" + frag),
                                 self._expected_before())

    def test_a_chunk_url_seeks_before_the_input_and_stops_after_it(self):
        """`-ss` BEFORE `-i` makes ffmpeg discard the head of the pipe; `-t` after it bounds what
        follows. Either one in the wrong place decodes the wrong audio."""
        argv = self._ffmpeg_argv("https://y/watch?v=a#t=7200,14400")
        self.assertEqual(argv[:6], ["ffmpeg", "-v", "error", "-ss", "7200", "-i"])
        self.assertEqual(argv[6], "pipe:0")
        self.assertEqual(argv[7:9], ["-t", "7200"])          # end - start, not end
        self.assertEqual(argv[9:], ["-ac", "1", "-ar", str(harvest._audio.SR),
                                    "-f", "f32le", "pipe:1"])
        self.assertLess(argv.index("-ss"), argv.index("-i"))
        self.assertGreater(argv.index("-t"), argv.index("-i"))

    def test_the_duration_is_the_length_of_the_slice(self):
        argv = self._ffmpeg_argv("https://y/watch?v=a#t=90,240")
        self.assertEqual(argv[argv.index("-ss") + 1], "90")
        self.assertEqual(argv[argv.index("-t") + 1], "150")

    def test_yt_dlp_is_still_handed_the_whole_url(self):
        """It ignores the fragment, and that is fine -- the cut happens downstream. What matters
        is that we do not mangle the URL it is asked to fetch."""
        seen = []

        def _popen(argv, **kwargs):
            seen.append(argv)
            return _FakeProc(argv)

        url = "https://y/watch?v=a#t=0,600"
        with mock.patch.object(harvest.subprocess, "Popen", _popen):
            harvest._fetch_and_sign(url, self.job)
        yt = [a for a in seen if "yt-dlp" in a[0]][0]
        self.assertEqual(yt[-1], url)

    def test_an_overlong_span_never_reaches_ffmpeg(self):
        """The decode is the LAST door, and it refuses on the same terms as the queue door.

        The queue never offers such a URL, but it is not the only way one arrives here -- a
        hand-run `--fetch-one`, or a working queue written before this rule existed. Nothing is
        spawned, so there is nothing to stop and nothing to clean up."""
        spawned = []

        def _popen(argv, **kwargs):
            spawned.append(argv)
            return _FakeProc(argv)

        with mock.patch.object(harvest.subprocess, "Popen", _popen):
            result = harvest._fetch_and_sign("https://y/watch?v=a#t=0,21600", self.job)
        self.assertEqual(spawned, [])
        self.assertFalse(result["ok"])
        self.assertIn("too long", result["error"])
        self.assertIn("6.0 h", result["error"])
        self.assertFalse(os.path.exists(os.path.join(self.job, "pcm.f32le.part")))

    def test_both_doors_ask_the_same_question(self):
        """Not two thresholds that happen to agree today: one predicate, called twice. A URL the
        queue refuses is refused by the decode, and one it admits is decoded."""
        for url in ("https://y/a#t=0,21600", "https://y/a#t=0,14401", "https://y/a#t=0,600",
                    "https://y/a#t=0,14400", "https://y/a", "https://y/a#t=x,y"):
            with self.subTest(url=url):
                refused_at_the_queue = harvest.too_long(url)
                spawned = []

                def _popen(argv, **kwargs):
                    spawned.append(argv)
                    return _FakeProc(argv)

                with mock.patch.object(harvest.subprocess, "Popen", _popen):
                    harvest._fetch_and_sign(url, self.job)
                self.assertEqual(refused_at_the_queue, spawned == [])


@unittest.skipUnless(harvest is not None, "harvest.py needs librosa/numpy -- not this test's job")
class TheTooLongBackstop(unittest.TestCase):
    """Four hours, unsplit: refused whole, with a row saying so."""

    def _queue(self, items):
        fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump({"items": items}, fh)
        fh.close()
        self.addCleanup(os.unlink, fh.name)
        self.addCleanup(setattr, harvest, "LISTEN_QUEUE", harvest.LISTEN_QUEUE)
        harvest.LISTEN_QUEUE = fh.name

    def test_an_unsplit_master_is_skipped_with_a_reason(self):
        self._queue([{"url": "https://y/master", "title": "6 HOUR SET", "duration": 21600}])
        issues = []
        cand, retired = harvest.listen_queue_split(issues)
        self.assertEqual(cand, [])
        self.assertEqual(retired, set())        # refused, not RULED ON -- no human said anything
        self.assertEqual(issues, [{"url": "https://y/master", "reason": "too_long"}])

    def test_a_chunk_of_that_same_master_is_accepted(self):
        """Same audio, same length, split: the chunk is what the backstop exists to make happen.

        Note the entry still declares the WHOLE master's duration, as a real one does. What will
        be decoded is the span, so the span is what decides."""
        self._queue([{"url": "https://y/master#t=0,7200", "title": "6 HOUR SET [1/3]",
                      "duration": 21600}])
        issues = []
        cand, _ = harvest.listen_queue_split(issues)
        self.assertEqual(cand, ["https://y/master#t=0,7200"])
        self.assertEqual(issues, [])

    def test_an_overlong_fragment_is_refused_like_any_other_long_audio(self):
        """A fragment is a CLAIM made upstream, not proof that the entry is short.

        `#t=0,21600` is six hours wearing a chunk's punctuation. Reading "it has a fragment" as
        "it is a chunk, so it is short" is exactly the assumption a backstop exists to survive:
        chunks are short *by construction*, and the construction lives upstream of here, where
        it can be wrong, stale or hand-written."""
        self._queue([{"url": "https://y/master#t=0,21600", "duration": 21600}])
        issues = []
        cand, retired = harvest.listen_queue_split(issues)
        self.assertEqual(cand, [])
        self.assertEqual(retired, set())
        self.assertEqual(issues, [{"url": "https://y/master#t=0,21600", "reason": "too_long"}])

    def test_a_late_chunk_is_measured_by_its_span_not_its_end(self):
        """A slice near the end of a long master has big numbers on both ends and a modest span."""
        self._queue([{"url": "https://y/m#t=18000,21600", "duration": 21600}])
        cand, _ = harvest.listen_queue_split()
        self.assertEqual(cand, ["https://y/m#t=18000,21600"])

    def test_the_span_governs_when_the_two_disagree(self):
        """An entry can carry both a declared duration and a span, and they can disagree. The
        span is what ffmpeg is told to decode, so the span is what the refusal weighs; the
        declared duration decides only when there is no fragment at all."""
        # A short slice of a long master: accepted, despite the six-hour duration.
        self.assertFalse(harvest.too_long("https://y/m#t=0,600", 21600))
        # A long slice of something that claims to be short: refused, despite the duration.
        self.assertTrue(harvest.too_long("https://y/m#t=0,21600", 60))
        # No fragment: the declared duration is all there is, so it decides.
        self.assertTrue(harvest.too_long("https://y/m", 21600))
        self.assertFalse(harvest.too_long("https://y/m", 600))

    def test_the_boundary_holds_at_exactly_four_hours(self):
        """Four hours passes, a second more does not -- pinned so the comparison cannot drift
        between `>` and `>=` unnoticed."""
        self.assertFalse(harvest.too_long("https://y/m#t=0,14400"))
        self.assertTrue(harvest.too_long("https://y/m#t=0,14401"))
        self.assertFalse(harvest.too_long("https://y/m#t=600,15000"))
        self.assertTrue(harvest.too_long("https://y/m#t=600,15001"))

    def test_a_long_mix_under_the_backstop_is_still_searched(self):
        """The rule is still that length is not a filter -- a record hides inside a DJ set, and
        the match reports where it hit. Four hours is a backstop, not a preference."""
        for seconds in (3600, 7200, 14400):     # an hour, two, and exactly four
            with self.subTest(seconds=seconds):
                self._queue([{"url": "https://y/mix", "title": "JUNGLE 1998",
                              "duration": seconds}])
                issues = []
                cand, _ = harvest.listen_queue_split(issues)
                self.assertEqual(cand, ["https://y/mix"])
                self.assertEqual(issues, [])

    def test_an_unreadable_duration_is_not_grounds_for_refusal(self):
        """Missing, null, a string, a bool: none of those is evidence that this is six hours long,
        and a backstop that fires on ignorance would silently starve the search."""
        for duration in (None, "21600", True, {"seconds": 21600}, float("nan")):
            with self.subTest(duration=duration):
                self._queue([{"url": "https://y/x", "title": "X", "duration": duration}])
                issues = []
                cand, _ = harvest.listen_queue_split(issues)
                self.assertEqual(cand, ["https://y/x"])
                self.assertEqual(issues, [])

    def test_a_ruling_still_wins(self):
        """A human who has heard it has retired it; the backstop does not get a second opinion."""
        self._queue([{"url": "https://y/master", "duration": 21600, "listened": True}])
        issues = []
        cand, retired = harvest.listen_queue_split(issues)
        self.assertEqual((cand, retired), ([], {"https://y/master"}))
        self.assertEqual(issues, [])

    def test_the_caller_may_ignore_the_issues_list(self):
        """Every existing caller passes nothing, and none of them should have to care."""
        self._queue([{"url": "https://y/master", "duration": 21600}])
        self.assertEqual(harvest.listen_queue_split(), ([], set()))

    def test_a_refused_url_also_leaves_our_pending_list(self):
        """It was queued before the split rule existed. A backstop that only stopped NEW arrivals
        would still let the whole master be fetched, which is the thing it is here to prevent."""
        self._queue([{"url": "https://y/master", "duration": 21600}])
        q = {"pending": ["https://y/master"], "done": []}
        issues = []
        added, dropped = harvest.sync_listen_queue(q, issues)
        self.assertEqual((added, dropped), (0, 1))
        self.assertEqual(q["pending"], [])
        self.assertEqual(issues, [{"url": "https://y/master", "reason": "too_long"}])

    def test_the_chunks_flow_in_while_the_master_flows_out(self):
        self._queue([{"url": "https://y/m", "duration": 21600},
                     {"url": "https://y/m#t=0,7200", "duration": 21600},
                     {"url": "https://y/m#t=7200,14400", "duration": 21600}])
        q = {"pending": ["https://y/m"], "done": []}
        added, dropped = harvest.sync_listen_queue(q)
        self.assertEqual((added, dropped), (2, 1))
        self.assertEqual(q["pending"], ["https://y/m#t=0,7200", "https://y/m#t=7200,14400"])


if __name__ == "__main__":
    unittest.main()
