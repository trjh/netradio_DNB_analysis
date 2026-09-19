"""The harvester reads the player's listen queue — one queue, two stores.

The player OWNS `listen_queue.json`; the harvester only ever reads it (two writers on one JSON
file is how you lose the file). These tests pin the read side: which entries become candidates,
and how the layout is told apart. Retirement no longer happens here at all — the ruling
flags and the own-clip rule are the queue owner's business, written into the rulings file the
harvester reads instead (see `tests/test_harvest_rulings.py`); this read answers one question
only: what does the queue OFFER?

No librosa import here: the module pulls in numpy/librosa at import, so the queue logic is
exercised through a stub-free import guard (skipped if the analysis venv is absent).
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))

try:
    import harvest
except Exception as exc:                    # librosa/numba not installed -> not this test's job
    harvest = None
    _why = str(exc)


@unittest.skipIf(harvest is None, "harvest.py needs the librosa venv (.venv) — skipping")
class ListenQueueSplit(unittest.TestCase):
    def _queue(self, items):
        fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump({"items": items}, fh)
        fh.close()
        harvest.LISTEN_QUEUE = fh.name
        self.addCleanup(os.unlink, fh.name)

    def test_unheard_entries_are_candidates(self):
        self._queue([{"url": "https://y/a", "title": "A"},
                     {"url": "https://y/b", "title": "B"}])
        cand = harvest.listen_queue_split()
        self.assertEqual(cand, ["https://y/a", "https://y/b"])

    def test_ruled_on_entries_are_offered_like_any_other(self):
        """The ruling flags are no longer this read's business: a heard or discarded entry is
        still OFFERED by the queue, and `sync_listen_queue` (holding the retired set from the
        rulings file) keeps it out of the working queue. If the read filtered here too, a
        missing rulings file would silently look like an empty queue."""
        for flag in ("listened", "discarded", "ignored", "duplicate", "not_a_match"):
            with self.subTest(flag=flag):
                self._queue([{"url": "https://y/x", "title": "X", flag: True}])
                cand = harvest.listen_queue_split()
                self.assertEqual(cand, ["https://y/x"])

    def test_a_clip_title_is_just_a_title(self):
        """The own-clip net (a clip upload titled `Mystery Track N`) moved to the queue's
        owner with the rest of the retirement; from here the entry is data like any other,
        and `Mystery Track 7` in a title is not this read's to refuse."""
        self._queue([{"url": "https://y/own", "title": "Mystery Track 7"},
                     {"url": "https://y/own2", "title": "netradio mystery track 4 (clip)"}])
        cand = harvest.listen_queue_split()
        self.assertEqual(cand, ["https://y/own", "https://y/own2"])

    def test_real_records_with_mystery_in_the_name_are_still_searched(self):
        """The queue's titles are not a filter here in any direction."""
        self._queue([{"url": "https://y/1", "title": "No Mystery (1996)"},
                     {"url": "https://y/2", "title": "Mystery Blend Atmospheric"},
                     {"url": "https://y/3", "title": "Mystery Science Theater 3000 Love Theme"}])
        cand = harvest.listen_queue_split()
        self.assertEqual(len(cand), 3)

    def test_a_half_written_queue_file_is_survived_not_crashed(self):
        """The player writes this file continuously; we may read it mid-write."""
        fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        fh.write('{"items": [{"url": "https://y/a"')      # truncated
        fh.close()
        harvest.LISTEN_QUEUE = fh.name
        self.addCleanup(os.unlink, fh.name)
        self.assertEqual(harvest.listen_queue_split(), [])

    def test_inert_when_the_player_is_not_there(self):
        harvest.LISTEN_QUEUE = ""
        self.assertEqual(harvest.listen_queue_split(), [])


@unittest.skipIf(harvest is None, "harvest.py needs the librosa venv (.venv) — skipping")
class ShardedListenQueue(unittest.TestCase):
    """The player migrated the single listen_queue.json to a sharded directory: an `index.json`
    manifest naming `shard-NNNN.json` files, each a bare JSON array of items. The harvester must
    read that layout too (still read-only, still crash-tolerant), pointed at either the directory
    or the manifest itself."""

    def _shards(self, shards, point_at="dir"):
        """Build a shard dir from {name: [items]} and aim LISTEN_QUEUE at the dir or the manifest.
        Returns the dir so a test can corrupt it further."""
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        manifest = {"schema": "netradio.listen-queue.v2", "shards": []}
        for name, items in shards:
            with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
                json.dump(items, fh)
            manifest["shards"].append({"name": name, "count": len(items)})
        with open(os.path.join(d, "index.json"), "w", encoding="utf-8") as fh:
            json.dump(manifest, fh)
        harvest.LISTEN_QUEUE = d if point_at == "dir" else os.path.join(d, "index.json")
        return d

    def test_items_concatenate_across_shards_in_manifest_order(self):
        self._shards([("shard-0000.json", [{"url": "https://y/a", "title": "A"}]),
                      ("shard-0001.json", [{"url": "https://y/b", "title": "B"},
                                           {"url": "https://y/c", "title": "C"}])])
        cand = harvest.listen_queue_split()
        self.assertEqual(cand, ["https://y/a", "https://y/b", "https://y/c"])

    def test_manifest_named_directly_also_works(self):
        self._shards([("shard-0000.json", [{"url": "https://y/a"}]),
                      ("shard-0001.json", [{"url": "https://y/b"}])], point_at="manifest")
        cand = harvest.listen_queue_split()
        self.assertEqual(cand, ["https://y/a", "https://y/b"])

    def test_flagged_and_own_entries_flow_in_across_shards_too(self):
        # The shards are concatenated whatever an entry's flags are -- the read is about what
        # the queue offers, and the retirement is the rulings file's (see
        # test_harvest_rulings.py).
        self._shards([("shard-0000.json", [{"url": "https://y/a"},
                                           {"url": "https://y/heard", "listened": True}]),
                      ("shard-0001.json", [{"url": "https://y/own", "title": "Mystery Track 3"}])])
        cand = harvest.listen_queue_split()
        self.assertEqual(cand, ["https://y/a", "https://y/heard", "https://y/own"])

    def test_a_shard_named_by_the_manifest_but_missing_is_survived(self):
        d = self._shards([("shard-0000.json", [{"url": "https://y/a"}]),
                          ("shard-0001.json", [{"url": "https://y/b"}])])
        os.unlink(os.path.join(d, "shard-0001.json"))
        self.assertEqual(harvest.listen_queue_split(), [])

    def test_an_invalid_json_shard_is_survived(self):
        d = self._shards([("shard-0000.json", [{"url": "https://y/a"}])])
        with open(os.path.join(d, "shard-0000.json"), "w", encoding="utf-8") as fh:
            fh.write('[{"url": "https://y/a"')       # truncated mid-write
        self.assertEqual(harvest.listen_queue_split(), [])

    def test_a_missing_manifest_is_survived(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        harvest.LISTEN_QUEUE = d                       # a dir with no index.json yet
        self.assertEqual(harvest.listen_queue_split(), [])

    def test_a_manifest_with_string_shard_entries_is_survived(self):
        # syntactically valid, wrong SHAPE: {"shards": ["shard-0000.json"]} must land in the
        # "try again next pass" net, not crash the harvester (local-review 2026-07-27 finding)
        d = self._shards([("shard-0000.json", [{"url": "https://y/a"}])])
        with open(os.path.join(d, "index.json"), "w", encoding="utf-8") as fh:
            json.dump({"shards": ["shard-0000.json"]}, fh)
        self.assertEqual(harvest.listen_queue_split(), [])

    def test_a_wrapped_shard_whose_items_is_a_string_is_survived(self):
        d = self._shards([("shard-0000.json", [{"url": "https://y/a"}])])
        with open(os.path.join(d, "shard-0000.json"), "w", encoding="utf-8") as fh:
            json.dump({"items": "nope"}, fh)
        self.assertEqual(harvest.listen_queue_split(), [])

    def test_a_non_list_shard_is_survived(self):
        d = self._shards([("shard-0000.json", [{"url": "https://y/a"}])])
        with open(os.path.join(d, "shard-0000.json"), "w", encoding="utf-8") as fh:
            json.dump("nope", fh)
        self.assertEqual(harvest.listen_queue_split(), [])

    def test_a_corrupt_item_is_dropped_without_starving_the_rest(self):
        # one non-object item must not hide the other thousands behind an empty read
        self._shards([("shard-0000.json", [{"url": "https://y/a"}, "corrupt",
                                           {"url": "https://y/b"}])])
        cand = harvest.listen_queue_split()
        self.assertEqual(cand, ["https://y/a", "https://y/b"])

    def test_a_non_object_manifest_is_survived(self):
        d = self._shards([("shard-0000.json", [{"url": "https://y/a"}])])
        with open(os.path.join(d, "index.json"), "w", encoding="utf-8") as fh:
            json.dump([1], fh)
        self.assertEqual(harvest.listen_queue_split(), [])

    def test_a_non_string_shard_name_is_survived(self):
        d = self._shards([("shard-0000.json", [{"url": "https://y/a"}])])
        with open(os.path.join(d, "index.json"), "w", encoding="utf-8") as fh:
            json.dump({"shards": [{"name": 1}]}, fh)
        self.assertEqual(harvest.listen_queue_split(), [])

    def test_a_symlinked_shard_never_escapes_the_dir(self):
        # a canonically NAMED shard that is a symlink elsewhere defeats the name check —
        # the resolved file must live in the queue dir (local-review finding)
        d = self._shards([("shard-0000.json", [{"url": "https://y/a"}])])
        outside = os.path.join(os.path.dirname(d), "symlink-target-%s.json" % os.path.basename(d))
        with open(outside, "w", encoding="utf-8") as fh:
            json.dump([{"url": "https://evil/x"}], fh)
        self.addCleanup(os.unlink, outside)
        os.unlink(os.path.join(d, "shard-0000.json"))
        os.symlink(outside, os.path.join(d, "shard-0000.json"))
        self.assertEqual(harvest.listen_queue_split(), [])

    def test_a_traversal_or_absolute_shard_name_never_escapes_the_dir(self):
        # containment: a tampered manifest must not make the harvester read an UNRELATED
        # file as the queue (local-review finding)
        d = self._shards([("shard-0000.json", [{"url": "https://y/a"}])])
        outside = os.path.join(os.path.dirname(d), "outside-%s.json" % os.path.basename(d))
        with open(outside, "w", encoding="utf-8") as fh:
            json.dump([{"url": "https://evil/x"}], fh)
        self.addCleanup(os.unlink, outside)
        for name in ("../" + os.path.basename(outside), outside, "shard-0000.json.bak"):
            with self.subTest(name=name):
                with open(os.path.join(d, "index.json"), "w", encoding="utf-8") as fh:
                    json.dump({"shards": [{"name": name}]}, fh)
                self.assertEqual(harvest.listen_queue_split(), [])

    def test_corrupt_item_fields_are_tolerated_per_item(self):
        # a mapping url, an int origin, an int title: each item is skipped or handled,
        # never a crash, and the healthy neighbours survive
        self._shards([("shard-0000.json", [{"url": {"nested": True}},
                                           {"url": "https://y/a", "origin": 5, "title": 7},
                                           {"url": "https://y/b"}])])
        cand = harvest.listen_queue_split()
        self.assertEqual(cand, ["https://y/a", "https://y/b"])


@unittest.skipIf(harvest is None, "harvest.py needs the librosa venv (.venv) — skipping")
class RetryAfterCooling(unittest.TestCase):
    """`retry_after` (ISO YYYY-MM-DD) holds a URL back from the network while its date is in the
    future -- exactly the player's rule. Cooling gates the OFFER and nothing else: the URL is not
    a candidate today, and it rejoins on its own once the date passes (the retirement that
    once outranked it here is the rulings file's now -- a cooling entry that is also ruled on
    is simply held back like any other)."""

    def _queue(self, items):
        fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump({"items": items}, fh)
        fh.close()
        harvest.LISTEN_QUEUE = fh.name
        self.addCleanup(os.unlink, fh.name)

    def _today(self):
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def test_a_future_retry_after_holds_the_url_back(self):
        self._queue([{"url": "https://y/cool", "retry_after": "2999-01-01"}])
        cand = harvest.listen_queue_split()
        self.assertEqual(cand, [])               # cooling never retires: the date passes and it rejoins

    def test_past_today_absent_or_nonstring_retry_after_is_a_candidate(self):
        # "tomorrow"/"9999" would compare lexically greater than any ISO date FOREVER — only a
        # parseable YYYY-MM-DD may cool, everything else is not-cooling (local-review finding)
        for ra in ("2000-01-01", self._today(), None, 12345, {"nope": 1},
                   "tomorrow", "9999", "2026-13-45", "", "2020-7-2", "2999-1-1"):
            with self.subTest(retry_after=ra):
                item = {"url": "https://y/c"}
                if ra is not None:
                    item["retry_after"] = ra
                self._queue([item])
                cand = harvest.listen_queue_split()
                self.assertEqual(cand, ["https://y/c"])


@unittest.skipIf(harvest is None, "harvest.py needs the librosa venv (.venv) — skipping")
class SyncIntoOurQueue(unittest.TestCase):
    def _queue(self, items):
        fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump({"items": items}, fh)
        fh.close()
        harvest.LISTEN_QUEUE = fh.name
        self.addCleanup(os.unlink, fh.name)

    def test_new_entries_flow_in(self):
        self._queue([{"url": "https://y/new", "title": "new"},
                     {"url": "https://y/heard", "title": "heard", "listened": True}])
        q = {"pending": ["https://y/keep"], "done": []}
        added, dropped = harvest.sync_listen_queue(q, set())
        self.assertEqual((added, dropped), (2, 0))
        self.assertEqual(q["pending"], ["https://y/keep", "https://y/new", "https://y/heard"])
        # A ruled-on entry flows in here too, and it is `tests/test_harvest_rulings.py` that
        # pins the retired set keeping it out; this test pins the fold's own half.

    def test_never_re_analyses_something_already_done(self):
        self._queue([{"url": "https://y/done", "title": "done"}])
        q = {"pending": [], "done": ["https://y/done"]}
        added, _ = harvest.sync_listen_queue(q, set())
        self.assertEqual(added, 0)
        self.assertEqual(q["pending"], [])

    def test_long_mixes_are_not_filtered_out(self):
        """A record can hide inside an hour-long DJ mix, and the match reports WHERE it hit."""
        self._queue([{"url": "https://y/mix", "title": "3 HOUR JUNGLE MIX 1998"}])
        q = {"pending": [], "done": []}
        added, _ = harvest.sync_listen_queue(q, set())
        self.assertEqual(added, 1)


if __name__ == "__main__":
    unittest.main()
