"""The retired set is a file the queue's owner writes -- the search reads only the keys.

The harvester used to derive its never-again set -- every key the search must not propose,
for any mystery, present or future -- from the listen queue's ruling flags, read directly every
pass. The rulings are the queue owner's own, so the file it computes them into is too:
`.harvest/rulings.json`, `{key: reason}`, written whole and atomically at its start and after
every ruling. These tests pin the read side: which keys the file retires, that nothing else is
retired, that an absent or torn file refuses the run rather than emptying the set, and -- the
one that actually costs something if it breaks -- that a ruled-out record never comes back, not
even for a mystery that did not exist when the ruling was made.

No librosa import here: the module pulls in numpy/librosa at import, so the queue logic is
exercised through a stub-free import guard (skipped if the analysis venv is absent).
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))

try:
    import harvest
except Exception as exc:                    # librosa/numba not installed -> not this test's job
    harvest = None
    _why = str(exc)


@unittest.skipIf(harvest is None, "harvest.py needs the librosa venv (.venv) — skipping")
class TheRulingsFile(unittest.TestCase):
    """`load_rulings`: the keys, in the pool's own naming, or None when it cannot be read."""

    def _file(self, rulings):
        fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(rulings, fh)
        fh.close()
        self.addCleanup(os.unlink, fh.name)
        self.addCleanup(setattr, harvest, "RULINGS", harvest.RULINGS)
        harvest.RULINGS = fh.name

    def test_the_keys_arrive_in_the_pools_own_naming(self):
        # The file writes the bare key (`u<sha1[:20]>`); this side's name for a key is its
        # signature-file name, so every consumer compares `_sig_key(url)` directly.
        bare = "u" + "a" * 20
        self._file({bare: "listened", "u" + "b" * 20: "not_a_match"})
        self.assertEqual(harvest.load_rulings(), {bare + ".npy", "u" + "b" * 20 + ".npy"})

    def test_the_reasons_are_ignored(self):
        # The reasons are for the human reading the file; the search reads the keys alone --
        # `not_a_match` and `listened` retire identically, and an odd or empty reason string
        # must not change what is (or is not) retired.
        bare = "u" + "a" * 20
        for reason in ("listened", "not_a_match", "own", ""):
            with self.subTest(reason=reason):
                self._file({bare: reason})
                self.assertEqual(harvest.load_rulings(), {bare + ".npy"})

    def test_an_empty_file_retires_nothing(self):
        # A queue with no rulings at all is a real state, and a valid one -- an empty set, not
        # "cannot read": the distinction is what the run's refusal turns on.
        self._file({})
        self.assertEqual(harvest.load_rulings(), set())

    def test_an_absent_file_is_not_an_empty_one(self):
        absent = os.path.join(tempfile.mkdtemp(prefix="no_rulings_"), "rulings.json")
        self.addCleanup(shutil.rmtree, os.path.dirname(absent), True)
        self.addCleanup(setattr, harvest, "RULINGS", harvest.RULINGS)
        harvest.RULINGS = absent                 # never written
        self.assertIsNone(harvest.load_rulings())

    def test_a_torn_file_is_none_not_an_empty_set(self):
        # The writer replaces the file atomically, so a torn read should never happen -- but
        # "unreadable" must still land on the refuse side, never on "nothing is retired".
        fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        fh.write('{"u' + "a" * 20 + '": "list')      # truncated mid-value
        fh.close()
        self.addCleanup(os.unlink, fh.name)
        self.addCleanup(setattr, harvest, "RULINGS", harvest.RULINGS)
        harvest.RULINGS = fh.name
        self.assertIsNone(harvest.load_rulings())

    def test_a_wrong_shape_file_is_none(self):
        for shape in ("[]", '"nope"', "null"):
            with self.subTest(shape=shape):
                fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
                fh.write(shape)
                fh.close()
                self.addCleanup(os.unlink, fh.name)
                self.addCleanup(setattr, harvest, "RULINGS", harvest.RULINGS)
                harvest.RULINGS = fh.name
                self.assertIsNone(harvest.load_rulings())


@unittest.skipIf(harvest is None, "harvest.py needs the librosa venv (.venv) — skipping")
class ARuledKeyIsNeverProposed(unittest.TestCase):
    """The whole point of the set. `not a match` is deliberately GLOBAL: it means "not any
    Mystery Track", including the ones whose clips do not exist yet. Without that, the day
    MT8's clip lands, every record already rejected comes straight back."""

    def setUp(self):
        self._rulings = harvest.RULINGS
        self.addCleanup(setattr, harvest, "RULINGS", self._rulings)
        harvest.RULINGS = os.path.join(tempfile.mkdtemp(prefix="ruled_"), "rulings.json")
        self.addCleanup(shutil.rmtree, os.path.dirname(harvest.RULINGS), True)

    def _rule_out(self, *urls):
        """Write the rulings file with one key per URL, the way the queue's owner does."""
        harvest._save(harvest.RULINGS, {harvest._sig_key(u)[:-4]: "not_a_match" for u in urls})

    def test_never_offered_again_not_even_for_a_new_mystery(self):
        self._rule_out("https://y/rejected")
        state, q = {"matches": [], "kept": 0, "scored": {}}, {"done": ["https://y/keep",
                                                                     "https://y/rejected"]}
        # The signature cache has no directory while the policy is dark, so sig_path would be
        # None and every candidate would read as unheld; CACHE is patched to a stand-in
        # directory and os.path.exists is made to say the signature is there.
        with unittest.mock.patch("os.path.exists", return_value=True), \
             unittest.mock.patch.object(harvest, "CACHE", "sig-cache-for-tests"):
            pairs = harvest.unscored_pairs(state, q, harvest.load_rulings(), [(8, None, "8:fp")])
        self.assertEqual([p[3] for p in pairs], ["https://y/keep"])

    def test_the_rescan_skips_it_too(self):
        # --rescan and the loop's chunked rescan go through the same door: the ruled key never
        # reaches the scorer, so it cannot be proposed by the one mode a new mystery runs first.
        self._rule_out("https://y/rejected")
        state = {"matches": [], "kept": 0, "scored": {}}
        q = {"done": ["https://y/keep", "https://y/rejected"]}
        scored = []
        with unittest.mock.patch("os.path.exists", return_value=True), \
             unittest.mock.patch.object(harvest, "CACHE", "sig-cache-for-tests"), \
             unittest.mock.patch.object(harvest, "score_cached",
                                        lambda st, n, qc, qk, url, key: scored.append(url)):
            n = harvest.rescan(state, q, harvest.load_rulings(), [(8, None, "8:fp")])
        self.assertEqual((scored, n), (["https://y/keep"], 1))


@unittest.skipIf(harvest is None, "harvest.py needs the librosa venv (.venv) — skipping")
class RuledKeysNeverFlowIntoTheWorkingQueue(unittest.TestCase):
    """`sync_listen_queue` holds the retired set, and it gates the fold BOTH ways: a ruled key
    never flows in (however many times the queue still offers the entry), and a URL ruled on
    while it sat on our lists flows out. `done` is the one list that does not work that way --
    it is our record of work completed, and forgetting it would re-analyse on a re-add."""

    def setUp(self):
        self._rulings = harvest.RULINGS
        self.addCleanup(setattr, harvest, "RULINGS", self._rulings)
        harvest.RULINGS = os.path.join(tempfile.mkdtemp(prefix="ruled_"), "rulings.json")
        self.addCleanup(shutil.rmtree, os.path.dirname(harvest.RULINGS), True)

    def _queue(self, items):
        fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump({"items": items}, fh)
        fh.close()
        harvest.LISTEN_QUEUE = fh.name
        self.addCleanup(os.unlink, fh.name)
        self.addCleanup(setattr, harvest, "LISTEN_QUEUE", harvest.LISTEN_QUEUE)

    def _rule_out(self, *urls):
        harvest._save(harvest.RULINGS, {harvest._sig_key(u)[:-4]: "listened" for u in urls})

    def test_a_ruled_key_flows_out_and_never_back_in(self):
        # The queue still OFFERS the ruled entry (the ruling is recorded beside it, not
        # instead of it) -- the retired set is the only thing keeping it out of the working
        # queue, in both directions at once.
        self._rule_out("https://y/heard")
        self._queue([{"url": "https://y/new", "title": "new"},
                     {"url": "https://y/heard", "title": "heard", "listened": True}])
        q = {"pending": ["https://y/heard", "https://y/keep"], "done": []}
        added, dropped = harvest.sync_listen_queue(q, harvest.load_rulings())
        self.assertEqual((added, dropped), (1, 1))
        self.assertEqual(q["pending"], ["https://y/keep", "https://y/new"])

    def test_a_ruling_does_not_erase_the_record_of_work_done(self):
        self._rule_out("https://y/x")
        self._queue([{"url": "https://y/x", "title": "x", "listened": True}])
        q = {"pending": [], "done": ["https://y/x"]}
        harvest.sync_listen_queue(q, harvest.load_rulings())
        self.assertEqual(q["done"], ["https://y/x"])

    def test_an_own_upload_arrives_already_retired(self):
        # A harvester that "finds" one of the queue owner's own uploads has rediscovered its
        # own question and would report a triumphant ~0.00. The writer retires those keys on
        # arrival -- the harvester's own-clip rule moved there with the rest of the set -- so
        # the entries never become candidates, however ordinary they look.
        self._rule_out("https://y/own", "https://y/own2")
        self._queue([{"url": "https://y/own", "title": "Mystery Track 7"},
                     {"url": "https://y/own2", "title": "netradio mystery track 4 (clip)"}])
        q = {"pending": [], "done": []}
        added, _ = harvest.sync_listen_queue(q, harvest.load_rulings())
        self.assertEqual(added, 0)
        self.assertEqual(q["pending"], [])

    def test_a_ruling_takes_it_off_the_set_aside_list_too(self):
        # A URL waits on `retry_later` for parts, and the parts only come while the queue
        # still offers the entry. Once it has been ruled on there are none coming, so it
        # leaves by the same door `pending` uses.
        url = "https://example.invalid/watch?v=master"
        self._rule_out(url)
        self._queue([{"url": url, "duration": 21600}])
        q = {"pending": [], "done": [], "retry_later": [url]}
        added, dropped = harvest.sync_listen_queue(q, harvest.load_rulings())
        self.assertEqual((added, dropped), (0, 1))
        self.assertEqual(q["retry_later"], [],
                         "a retired URL left on the list is one for whatever drains it to trip "
                         "over -- it is not coming back as a candidate")

    def test_a_ruled_masters_too_long_refusal_is_silent(self):
        # A human who has heard it has retired it; the length backstop does not get a second
        # opinion. The queue's read refuses the six-hour master like any other, but the retired
        # set filters the refusal too -- an issue row about a decision already made is noise.
        url = "https://y/master"
        self._rule_out(url)
        self._queue([{"url": url, "duration": 21600, "listened": True}])
        q = {"pending": [url], "done": []}
        issues = []
        added, dropped = harvest.sync_listen_queue(q, harvest.load_rulings(), issues)
        self.assertEqual((added, dropped), (0, 1))
        self.assertEqual(q["pending"], [])
        self.assertEqual(issues, [])


@unittest.skipIf(harvest is None, "harvest.py needs the librosa venv (.venv) — skipping")
class ARulingSpendsTheExcerpt(unittest.TestCase):
    """The excerpt exists so a human can confirm or reject the lead by ear. Once they have --
    match, not-a-match, heard -- that purpose is spent, and only the 30-day TTL sweep would
    ever have reclaimed the audio. `drop_ruled_excerpts` reclaims it on the next pass instead.

    The LEAD must survive whole: the score is the record, the audio was only ever the
    evidence. The deletion itself goes through the cache policy's door, so the excerpt board
    must be registered over this test's directory (a throwaway root, put back afterwards)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        import cache_budget
        self._saved = {k: os.environ.get(k) for k in list(os.environ)
                       if k.startswith("NETRADIO_") and ("CACHE" in k
                                                         or k in ("NETRADIO_CACHE_ROOT",
                                                                  "NETRADIO_DOWNLOAD_ROOT",
                                                                  "NETRADIO_DISK_MAX_PCT",
                                                                  "NETRADIO_CACHE_EVENTS_DAYS"))}
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
        import cache_budget
        cache_budget._REGISTRY.clear()
        cache_budget._REGISTRY.update(self._registry[0])
        cache_budget._STATS.clear()
        cache_budget._STATS.update(self._registry[1])
        for k in [k for k in list(os.environ) if k.startswith("NETRADIO_") and ("CACHE" in k
                 or k in ("NETRADIO_CACHE_ROOT", "NETRADIO_DOWNLOAD_ROOT",
                          "NETRADIO_DISK_MAX_PCT", "NETRADIO_CACHE_EVENTS_DAYS"))]:
            os.environ.pop(k, None)
        os.environ.update(self._saved)
        shutil.rmtree(self._root, ignore_errors=True)

    def _wav(self, name):
        path = os.path.join(self.dir, name)
        open(path, "wb").close()
        return path

    def _ruled(self, *urls):
        return {harvest._sig_key(u) for u in urls}

    def test_a_ruled_leads_audio_goes_and_its_numbers_stay(self):
        wav = self._wav("MT4-0.0603-aaaa.wav")
        state = {"kept": 1, "matches": [{"mystery": 4, "cost": 0.0603, "url": "u1",
                                         "at_s": 12.0, "verdict": "near", "audio": wav}]}
        self.assertEqual(harvest.drop_ruled_excerpts(state, self._ruled("u1")), 1)
        self.assertFalse(os.path.exists(wav))
        m = state["matches"][0]
        self.assertNotIn("audio", m)
        self.assertEqual((m["url"], m["cost"], m["at_s"]), ("u1", 0.0603, 12.0))
        self.assertEqual(state["kept"], 0)

    def test_an_unruled_lead_keeps_its_excerpt(self):
        wav = self._wav("MT4-0.0603-bbbb.wav")
        state = {"kept": 1, "matches": [{"mystery": 4, "cost": 0.0603, "url": "u1", "audio": wav}]}
        self.assertEqual(harvest.drop_ruled_excerpts(state, self._ruled("someone-else")), 0)
        self.assertTrue(os.path.exists(wav))
        self.assertEqual(state["matches"][0]["audio"], wav)
        self.assertEqual(state["kept"], 1)

    def test_a_ruled_lead_with_no_audio_is_a_no_op(self):
        """The normal case after --purge-audio, and for every rescan-found lead."""
        state = {"kept": 0, "matches": [{"mystery": 4, "cost": 0.06, "url": "u1"}]}
        self.assertEqual(harvest.drop_ruled_excerpts(state, self._ruled("u1")), 0)
        self.assertEqual(state["kept"], 0)

    def test_an_already_gone_file_still_clears_the_row(self):
        """TTL sweep or a hand-rm got there first; the row must stop advertising audio anyway."""
        state = {"kept": 1, "matches": [{"mystery": 4, "cost": 0.06, "url": "u1",
                                         "audio": os.path.join(self.dir, "never-existed.wav")}]}
        self.assertEqual(harvest.drop_ruled_excerpts(state, self._ruled("u1")), 1)
        self.assertNotIn("audio", state["matches"][0])
        self.assertEqual(state["kept"], 0)

    def test_kept_never_goes_negative(self):
        state = {"kept": 0, "matches": [{"mystery": 4, "cost": 0.06, "url": "u1",
                                         "audio": self._wav("MT4-0.06-cccc.wav")}]}
        harvest.drop_ruled_excerpts(state, self._ruled("u1"))
        self.assertEqual(state["kept"], 0)


@unittest.skipIf(harvest is None, "harvest.py needs the librosa venv (.venv) — skipping")
class TheRunGate(unittest.TestCase):
    """`--run` refuses to start without the file, naming it -- the same refusal the process
    that starts the harvester makes, so a hand-run `harvest.py --run` cannot search on
    amnesia either. A search that has forgotten every ruling hands back records already
    rejected; an empty set is not a starting condition, it is a refusal."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rulings-gate-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._paths = harvest.STATE, harvest.QUEUE, harvest.WRITER_LOCK, harvest.RULINGS
        harvest.STATE = os.path.join(self.tmp, "state.json")
        harvest.QUEUE = os.path.join(self.tmp, "queue.json")
        harvest.WRITER_LOCK = os.path.join(self.tmp, "writer.lock")
        harvest.RULINGS = os.path.join(self.tmp, "rulings.json")       # never written
        self.addCleanup(lambda: (setattr(harvest, "STATE", self._paths[0]),
                                 setattr(harvest, "QUEUE", self._paths[1]),
                                 setattr(harvest, "WRITER_LOCK", self._paths[2]),
                                 setattr(harvest, "RULINGS", self._paths[3])))

    def test_refuses_without_the_file_naming_it(self):
        import contextlib
        import io
        out = io.StringIO()
        with unittest.mock.patch.object(
                harvest, "queries",
                lambda state=None: (_ for _ in ()).throw(
                    AssertionError("the refusal must come before the query set is read"))), \
             contextlib.redirect_stdout(out):
            harvest.run(None)
        text = out.getvalue()
        self.assertIn("the rulings file", text)
        self.assertIn(harvest.RULINGS, text, "the refusal names the file, so a hand-run can "
                                             "tell WHICH file is missing")

    def test_a_readable_file_lets_the_run_past_the_gate(self):
        # The same run, with the file in place, proceeds to the next gate -- this one's world
        # (STATE on a throwaway path) is what run() refuses on next, so reaching that refusal
        # proves the rulings gate opened.
        harvest._save(harvest.RULINGS, {})
        import contextlib
        import io
        out = io.StringIO()
        with unittest.mock.patch.object(harvest, "queries", lambda state=None: []), \
             contextlib.redirect_stdout(out):
            harvest.run(None)
        self.assertIn("nothing to search for", out.getvalue())


if __name__ == "__main__":
    unittest.main()
