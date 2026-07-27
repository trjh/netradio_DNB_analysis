"""The harvester reads the player's listen queue — one queue, two stores.

The player OWNS `listen_queue.json`; the harvester only ever reads it (two writers on one JSON
file is how you lose the file). These tests pin the read side: which entries become candidates,
which are retired, and — the one that actually costs something if it breaks — that the harvester
never queues Tim's own uploads of the mystery clips, which would "match" at 0.00 and mean nothing.

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
        cand, retired = harvest.listen_queue_split()
        self.assertEqual(cand, ["https://y/a", "https://y/b"])
        self.assertEqual(retired, set())

    def test_a_human_ruling_retires_an_entry(self):
        """Heard, discarded, ignored or duplicate all mean: nothing left here for the matcher."""
        for flag in ("listened", "discarded", "ignored", "duplicate"):
            with self.subTest(flag=flag):
                self._queue([{"url": "https://y/x", "title": "X", flag: True}])
                cand, retired = harvest.listen_queue_split()
                self.assertEqual(cand, [])
                self.assertEqual(retired, {"https://y/x"})

    def test_never_queues_tims_own_mystery_clips(self):
        """A harvester that "finds" the clip it is searching FOR has rediscovered its own question
        and reports a triumphant 0.00. Listen-queue entries carry no channel field, so the
        channel-level guard cannot catch this — the title is all we have."""
        self._queue([{"url": "https://y/own", "title": "Mystery Track 7"},
                     {"url": "https://y/own2", "title": "netradio mystery track 4 (clip)"}])
        cand, retired = harvest.listen_queue_split()
        self.assertEqual(cand, [])
        self.assertEqual(retired, {"https://y/own", "https://y/own2"})

    def test_real_records_with_mystery_in_the_name_are_still_searched(self):
        """The guard must be narrow: these are actual records in the queue today."""
        self._queue([{"url": "https://y/1", "title": "No Mystery (1996)"},
                     {"url": "https://y/2", "title": "Mystery Blend Atmospheric"},
                     {"url": "https://y/3", "title": "Mystery Science Theater 3000 Love Theme"}])
        cand, _ = harvest.listen_queue_split()
        self.assertEqual(len(cand), 3)      # none of these are Tim's clips

    def test_a_half_written_queue_file_is_survived_not_crashed(self):
        """The player writes this file continuously; we may read it mid-write."""
        fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        fh.write('{"items": [{"url": "https://y/a"')      # truncated
        fh.close()
        harvest.LISTEN_QUEUE = fh.name
        self.addCleanup(os.unlink, fh.name)
        self.assertEqual(harvest.listen_queue_split(), ([], set()))

    def test_inert_when_the_player_is_not_there(self):
        harvest.LISTEN_QUEUE = ""
        self.assertEqual(harvest.listen_queue_split(), ([], set()))


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
        cand, retired = harvest.listen_queue_split()
        self.assertEqual(cand, ["https://y/a", "https://y/b", "https://y/c"])
        self.assertEqual(retired, set())

    def test_manifest_named_directly_also_works(self):
        self._shards([("shard-0000.json", [{"url": "https://y/a"}]),
                      ("shard-0001.json", [{"url": "https://y/b"}])], point_at="manifest")
        cand, _ = harvest.listen_queue_split()
        self.assertEqual(cand, ["https://y/a", "https://y/b"])

    def test_rulings_and_own_clips_still_apply_across_shards(self):
        self._shards([("shard-0000.json", [{"url": "https://y/a"},
                                           {"url": "https://y/heard", "listened": True}]),
                      ("shard-0001.json", [{"url": "https://y/own", "title": "Mystery Track 3"}])])
        cand, retired = harvest.listen_queue_split()
        self.assertEqual(cand, ["https://y/a"])
        self.assertEqual(retired, {"https://y/heard", "https://y/own"})

    def test_a_shard_named_by_the_manifest_but_missing_is_survived(self):
        d = self._shards([("shard-0000.json", [{"url": "https://y/a"}]),
                          ("shard-0001.json", [{"url": "https://y/b"}])])
        os.unlink(os.path.join(d, "shard-0001.json"))
        self.assertEqual(harvest.listen_queue_split(), ([], set()))

    def test_an_invalid_json_shard_is_survived(self):
        d = self._shards([("shard-0000.json", [{"url": "https://y/a"}])])
        with open(os.path.join(d, "shard-0000.json"), "w", encoding="utf-8") as fh:
            fh.write('[{"url": "https://y/a"')       # truncated mid-write
        self.assertEqual(harvest.listen_queue_split(), ([], set()))

    def test_a_missing_manifest_is_survived(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        harvest.LISTEN_QUEUE = d                       # a dir with no index.json yet
        self.assertEqual(harvest.listen_queue_split(), ([], set()))


@unittest.skipIf(harvest is None, "harvest.py needs the librosa venv (.venv) — skipping")
class RetryAfterCooling(unittest.TestCase):
    """`retry_after` (ISO YYYY-MM-DD) holds a URL back from the network while its date is in the
    future -- exactly the player's rule. Cooling gates fetching and NOTHING else: the URL is not a
    candidate, but it is NOT retired either, so it rejoins on its own once the date passes."""

    def _queue(self, items):
        fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump({"items": items}, fh)
        fh.close()
        harvest.LISTEN_QUEUE = fh.name
        self.addCleanup(os.unlink, fh.name)

    def _today(self):
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def test_a_future_retry_after_is_neither_candidate_nor_retired(self):
        self._queue([{"url": "https://y/cool", "retry_after": "2999-01-01"}])
        cand, retired = harvest.listen_queue_split()
        self.assertEqual(cand, [])
        self.assertEqual(retired, set())               # cooling never retires

    def test_past_today_absent_or_nonstring_retry_after_is_a_candidate(self):
        for ra in ("2000-01-01", self._today(), None, 12345, {"nope": 1}):
            with self.subTest(retry_after=ra):
                item = {"url": "https://y/c"}
                if ra is not None:
                    item["retry_after"] = ra
                self._queue([item])
                cand, retired = harvest.listen_queue_split()
                self.assertEqual(cand, ["https://y/c"])
                self.assertEqual(retired, set())

    def test_a_ruling_wins_over_cooling(self):
        """A cooling item that has ALSO been ruled on stays retired -- retirement is permanent-ish
        and outranks a temporary network cooldown."""
        self._queue([{"url": "https://y/x", "retry_after": "2999-01-01", "not_a_match": True}])
        cand, retired = harvest.listen_queue_split()
        self.assertEqual(cand, [])
        self.assertEqual(retired, {"https://y/x"})


@unittest.skipIf(harvest is None, "harvest.py needs the librosa venv (.venv) — skipping")
class SyncIntoOurQueue(unittest.TestCase):
    def _queue(self, items):
        fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump({"items": items}, fh)
        fh.close()
        harvest.LISTEN_QUEUE = fh.name
        self.addCleanup(os.unlink, fh.name)

    def test_new_entries_flow_in_and_ruled_on_ones_flow_out(self):
        self._queue([{"url": "https://y/new", "title": "new"},
                     {"url": "https://y/heard", "title": "heard", "listened": True}])
        q = {"pending": ["https://y/heard", "https://y/keep"], "done": []}
        added, dropped = harvest.sync_listen_queue(q)
        self.assertEqual((added, dropped), (1, 1))
        self.assertEqual(q["pending"], ["https://y/keep", "https://y/new"])

    def test_never_re_analyses_something_already_done(self):
        self._queue([{"url": "https://y/done", "title": "done"}])
        q = {"pending": [], "done": ["https://y/done"]}
        added, _ = harvest.sync_listen_queue(q)
        self.assertEqual(added, 0)
        self.assertEqual(q["pending"], [])

    def test_a_ruling_does_not_erase_the_record_of_work_done(self):
        """`done` is our record of work completed. A human ruling retires it from PENDING, but
        must not remove it from `done` — else a re-add would re-analyse it from scratch."""
        self._queue([{"url": "https://y/x", "title": "x", "listened": True}])
        q = {"pending": [], "done": ["https://y/x"]}
        harvest.sync_listen_queue(q)
        self.assertEqual(q["done"], ["https://y/x"])

    def test_long_mixes_are_not_filtered_out(self):
        """A record can hide inside an hour-long DJ mix, and the match reports WHERE it hit."""
        self._queue([{"url": "https://y/mix", "title": "3 HOUR JUNGLE MIX 1998"}])
        q = {"pending": [], "done": []}
        added, _ = harvest.sync_listen_queue(q)
        self.assertEqual(added, 1)


if __name__ == "__main__":
    unittest.main()
