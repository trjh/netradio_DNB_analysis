"""The retired set is a file another process writes -- the search reads only the keys.

The harvester used to derive its never-again set -- every key the search must not propose,
for any mystery, present or future -- from the listen queue's ruling flags, read directly every
pass. The rulings are the writer's own, so the file it computes them into is too:
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
        # The file writes the bare key (`u<sha1[:20]>`), and that is what the reader gets back:
        # the same stem every file, row and signature carries, with no suffix to strip or
        # append on the way in (a caller that wants a signature-file name appends `.npy`).
        bare = "u" + "a" * 20
        self._file({bare: "listened", "u" + "b" * 20: "not_a_match"})
        self.assertEqual(harvest.load_rulings(), {bare, "u" + "b" * 20})

    def test_the_reasons_are_ignored(self):
        # The reasons are for the human reading the file; the search reads the keys alone --
        # `not_a_match` and `listened` retire identically, and an odd or empty reason string
        # must not change what is (or is not) retired.
        bare = "u" + "a" * 20
        for reason in ("listened", "not_a_match", "own", ""):
            with self.subTest(reason=reason):
                self._file({bare: reason})
                self.assertEqual(harvest.load_rulings(), {bare})

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
    MT8 lands, every record already rejected comes straight back."""

    def setUp(self):
        self._rulings = harvest.RULINGS
        self.addCleanup(setattr, harvest, "RULINGS", self._rulings)
        harvest.RULINGS = os.path.join(tempfile.mkdtemp(prefix="ruled_"), "rulings.json")
        self.addCleanup(shutil.rmtree, os.path.dirname(harvest.RULINGS), True)

    def _ledger(self, *urls):
        """One signed row per URL, the way the seed and the sign path write them."""
        return {harvest._sig_key(u)[:-4]:
                harvest._row(harvest._sig_key(u)[:-4], 1, 1.0, "signed", None, "then",
                             "e", {}) for u in urls}

    def _objects(self, *urls):
        """The bucket's listing as unscored_pairs reads it: one object per held key."""
        return {harvest._sig_key(u): "e" for u in urls}

    def _rule_out(self, *urls):
        """Write the rulings file with one key per URL, the way the rulings' writer does."""
        harvest._save(harvest.RULINGS, {harvest._sig_key(u)[:-4]: "not_a_match" for u in urls})

    def test_never_offered_again_not_even_for_a_new_mystery(self):
        self._rule_out("https://y/rejected")
        state = {"matches": [], "kept": 0, "scored": {}}
        ledger = self._ledger("https://y/keep", "https://y/rejected")
        # The signature cache has no directory while the policy is dark, so the held check
        # falls to the bucket listing; a bucket holding both keys is enough.
        with unittest.mock.patch("os.path.exists", return_value=False), \
             unittest.mock.patch.object(harvest, "CACHE", "sig-cache-for-tests"), \
             unittest.mock.patch.object(harvest, "_remote_objects",
                                        lambda max_age_s=900:
                                        self._objects("https://y/keep",
                                                      "https://y/rejected")):
            pairs = harvest.unscored_pairs(state, ledger, harvest.load_rulings(),
                                           [(8, None, "8:fp")])
        self.assertEqual([p[3] for p in pairs], [harvest._sig_key("https://y/keep")[:-4]])

    def test_the_rescan_skips_it_too(self):
        # --rescan and the loop's chunked rescan go through the same door: the ruled key never
        # reaches the scorer, so it cannot be proposed by the one mode a new mystery runs first.
        self._rule_out("https://y/rejected")
        state = {"matches": [], "kept": 0, "scored": {}}
        ledger = self._ledger("https://y/keep", "https://y/rejected")
        scored = []
        with unittest.mock.patch("os.path.exists", return_value=False), \
             unittest.mock.patch.object(harvest, "CACHE", "sig-cache-for-tests"), \
             unittest.mock.patch.object(harvest, "_remote_objects",
                                        lambda max_age_s=900:
                                        self._objects("https://y/keep",
                                                      "https://y/rejected")), \
             unittest.mock.patch.object(harvest, "score_cached",
                                        lambda st, n, qc, qk, key: scored.append(key)):
            n = harvest.rescan(state, ledger, harvest.load_rulings(), [(8, None, "8:fp")])
        self.assertEqual((scored, n), ([harvest._sig_key("https://y/keep")[:-4]], 1))


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

    def test_a_ruled_leads_audio_goes_and_its_numbers_stay(self):
        wav = self._wav("MT4-0.0603-aaaa.wav")
        state = {"kept": 1, "matches": [{"mystery": 4, "cost": 0.0603, "key": "u1",
                                         "at_s": 12.0, "verdict": "near", "audio": wav}]}
        self.assertEqual(harvest.drop_ruled_excerpts(state, {"u1"}), 1)
        self.assertFalse(os.path.exists(wav))
        m = state["matches"][0]
        self.assertNotIn("audio", m)
        self.assertEqual((m["key"], m["cost"], m["at_s"]), ("u1", 0.0603, 12.0))
        self.assertEqual(state["kept"], 0)

    def test_an_unruled_lead_keeps_its_excerpt(self):
        wav = self._wav("MT4-0.0603-bbbb.wav")
        state = {"kept": 1, "matches": [{"mystery": 4, "cost": 0.0603, "key": "u1", "audio": wav}]}
        self.assertEqual(harvest.drop_ruled_excerpts(state, {"u" + "c" * 20}), 0)
        self.assertTrue(os.path.exists(wav))
        self.assertEqual(state["matches"][0]["audio"], wav)
        self.assertEqual(state["kept"], 1)

    def test_a_ruled_lead_with_no_audio_is_a_no_op(self):
        """The normal case after --purge-audio, and for every rescan-found lead."""
        state = {"kept": 0, "matches": [{"mystery": 4, "cost": 0.06, "key": "u1"}]}
        self.assertEqual(harvest.drop_ruled_excerpts(state, {"u1"}), 0)
        self.assertEqual(state["kept"], 0)

    def test_an_already_gone_file_still_clears_the_row(self):
        """TTL sweep or a hand-rm got there first; the row must stop advertising audio anyway."""
        state = {"kept": 1, "matches": [{"mystery": 4, "cost": 0.06, "key": "u1",
                                         "audio": os.path.join(self.dir, "never-existed.wav")}]}
        self.assertEqual(harvest.drop_ruled_excerpts(state, {"u1"}), 1)
        self.assertNotIn("audio", state["matches"][0])
        self.assertEqual(state["kept"], 0)

    def test_kept_never_goes_negative(self):
        state = {"kept": 0, "matches": [{"mystery": 4, "cost": 0.06, "key": "u1",
                                         "audio": self._wav("MT4-0.06-cccc.wav")}]}
        harvest.drop_ruled_excerpts(state, {"u1"})
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
        self._paths = (harvest.STATE, harvest.LEDGER, harvest.WRITER_LOCK, harvest.RULINGS,
                       harvest.STATE_DIR)
        harvest.STATE = os.path.join(self.tmp, "state.json")
        harvest.LEDGER = os.path.join(self.tmp, "ledger.json")
        harvest.WRITER_LOCK = os.path.join(self.tmp, "writer.lock")
        harvest.RULINGS = os.path.join(self.tmp, "rulings.json")       # never written
        harvest.STATE_DIR = self.tmp        # the lock's own makedirs lands on the throwaway
        self.addCleanup(lambda: (setattr(harvest, "STATE", self._paths[0]),
                                 setattr(harvest, "LEDGER", self._paths[1]),
                                 setattr(harvest, "WRITER_LOCK", self._paths[2]),
                                 setattr(harvest, "RULINGS", self._paths[3]),
                                 setattr(harvest, "STATE_DIR", self._paths[4])))

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
        # The same run, with the file in place, proceeds to the next gates -- with no query
        # set it says so, and then (this world has a dark cache policy and no directories)
        # it refuses on the cache, which proves the rulings gate opened: every line of that
        # comes after the refusal the first test pins.
        harvest._save(harvest.RULINGS, {})
        import contextlib
        import io
        out = io.StringIO()
        with unittest.mock.patch.object(harvest, "queries", lambda state=None: []), \
             contextlib.redirect_stdout(out):
            harvest.run(None)
        text = out.getvalue()
        self.assertIn("nothing to search for", text)
        self.assertIn("NETRADIO_CACHE_ROOT", text, "the next gate refused, so the run went on")


if __name__ == "__main__":
    unittest.main()
