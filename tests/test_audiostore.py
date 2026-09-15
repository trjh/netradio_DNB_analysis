"""audiostore: the harvester's READ side of the player's audio cache. All offline.

The aws CLI never runs -- the module's one subprocess seam (`audiostore._run`) is swapped for a
scripted recorder, as `test_sigstore` does. Two things are pinned above all:

  * **It only reads.** No call this module makes can be a delete or an upload, and no file under
    the player's download root is ever touched. The player owns the audio; the harvester borrows.
  * **"Unknown" is not "empty".** A dark store, or a listing that has never succeeded, answers
    None -- a caller that read that as "the bucket holds nothing" would fetch everything again.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))

import audiostore  # noqa: E402


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class Recorder:
    """Every `_run` call recorded; results popped per call. A result may be a callable taking
    the argv, so a scripted `s3 cp` can write the file it was asked for."""

    def __init__(self):
        self.calls = []
        self.results = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        if not self.results:
            return FakeProc()
        nxt = self.results.pop(0)
        return nxt(cmd) if callable(nxt) else nxt

    def verbs(self):
        return [" ".join(c[1:3]) for c in self.calls]


def listing(keys, token=None):
    return FakeProc(stdout=json.dumps({"Contents": [{"Key": k, "Size": 10} for k in keys],
                                       "NextContinuationToken": token}))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.aws = os.path.join(self.tmp.name, "aws")
        with open(self.aws, "w") as fh:
            fh.write("#!/bin/sh\n")
        os.chmod(self.aws, 0o755)
        self.rec = Recorder()
        self.addCleanup(setattr, audiostore, "_run", audiostore._run)
        audiostore._run = self.rec
        self.addCleanup(audiostore._LIST.update, {"at": 0.0, "ids": None})
        audiostore._LIST.update({"at": 0.0, "ids": None})
        self._env = {}
        for k in ("NETRADIO_AUDIO_BUCKET", "NETRADIO_AUDIO_S3_ENDPOINT",
                  "NETRADIO_AUDIO_AWS_PROFILE", "NETRADIO_SIG_S3_ENDPOINT",
                  "NETRADIO_SIG_AWS_PROFILE", "NETRADIO_AWS_CLI", "NETRADIO_DOWNLOAD_ROOT"):
            self._env[k] = os.environ.pop(k, None)
        self.addCleanup(self._restore)
        os.environ["NETRADIO_AWS_CLI"] = self.aws

    def _restore(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _bucket_on(self):
        os.environ["NETRADIO_AUDIO_BUCKET"] = "audio-test"

    def _root(self, entries):
        root = os.path.join(self.tmp.name, "queue")
        os.makedirs(root, exist_ok=True)
        with open(os.path.join(root, "index.json"), "w") as fh:
            json.dump({"schema": 1, "root": root, "entries": entries}, fh)
        os.environ["NETRADIO_DOWNLOAD_ROOT"] = root
        return root

    def _file(self, root, rel, size=8):
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(b"x" * size)
        return path


class Dark(Base):
    def test_no_bucket_means_no_call_and_no_answer(self):
        self.assertFalse(audiostore.enabled())
        self.assertIsNone(audiostore.bucket_ids())
        self.assertIsNone(audiostore.fetch("abc", self.tmp.name))
        self.assertEqual(self.rec.calls, [])

    def test_no_download_root_means_nothing_local(self):
        self.assertFalse(audiostore.local_enabled())
        self.assertEqual(audiostore.local_files(), {})
        self.assertIsNone(audiostore.local_path("abc"))

    def test_endpoint_and_profile_fall_back_to_the_signature_stores(self):
        self._bucket_on()
        os.environ["NETRADIO_SIG_S3_ENDPOINT"] = "https://sig.example"
        os.environ["NETRADIO_SIG_AWS_PROFILE"] = "sigprof"
        self.rec.results = [listing([])]
        audiostore.bucket_ids()
        cmd = self.rec.calls[0]
        self.assertEqual(cmd[cmd.index("--endpoint-url") + 1], "https://sig.example")
        self.assertEqual(cmd[cmd.index("--profile") + 1], "sigprof")
        os.environ["NETRADIO_AUDIO_S3_ENDPOINT"] = "https://audio.example"
        audiostore._LIST.update({"at": 0.0, "ids": None})
        self.rec.results = [listing([])]
        audiostore.bucket_ids()
        cmd = self.rec.calls[1]
        self.assertEqual(cmd[cmd.index("--endpoint-url") + 1], "https://audio.example")


class LocalFiles(Base):
    def test_only_entries_whose_file_exists_count(self):
        root = self._root({
            "have": {"bucket": "unplayed", "file": "unplayed/have.m4a"},
            "gone": {"bucket": "unplayed", "file": "unplayed/gone.m4a"},
            "failed": {"bucket": None, "file": None, "download_error": "x"},
            "trashed": {"bucket": "trash", "file": "trash/trashed.opus"},
        })
        have = self._file(root, "unplayed/have.m4a")
        trashed = self._file(root, "trash/trashed.opus")
        self.assertEqual(audiostore.local_files(),
                         {"have": os.path.realpath(have), "trashed": os.path.realpath(trashed)},
                         "a trashed file is still audio: the player has not deleted it yet")
        self.assertEqual(audiostore.local_path("have"), os.path.realpath(have))
        self.assertIsNone(audiostore.local_path("gone"))

    def test_a_file_outside_the_root_is_never_opened(self):
        root = self._root({"esc": {"bucket": "unplayed", "file": "../outside.m4a"}})
        self._file(os.path.dirname(root), "outside.m4a")
        self.assertEqual(audiostore.local_files(), {})

    def test_a_torn_or_wrong_shaped_index_reads_as_nothing_local(self):
        root = self._root({})
        with open(os.path.join(root, "index.json"), "w") as fh:
            fh.write('{"entries": {"a": ')
        self.assertEqual(audiostore.local_files(), {})
        with open(os.path.join(root, "index.json"), "w") as fh:
            json.dump(["not", "an", "object"], fh)
        self.assertEqual(audiostore.local_files(), {})
        self.assertTrue(os.path.exists(os.path.join(root, "index.json")),
                        "the player's index is read, never rewritten or removed")


class BucketListing(Base):
    def setUp(self):
        Base.setUp(self)
        self._bucket_on()

    def test_one_paginated_listing_gives_every_id_once(self):
        self.rec.results = [listing(["audio/a.m4a", "audio/b.opus"], token="t1"),
                            listing(["audio/c.webm", "audio/nested/x.m4a", "other/d.m4a"])]
        ids = audiostore.bucket_ids()
        self.assertEqual(ids, {"a", "b", "c"})
        self.assertEqual(len(self.rec.calls), 2)
        self.assertIn("--continuation-token", self.rec.calls[1])
        self.assertEqual(self.rec.calls[1][self.rec.calls[1].index("--continuation-token") + 1],
                         "t1")
        prefix = self.rec.calls[0][self.rec.calls[0].index("--prefix") + 1]
        self.assertEqual(prefix, "audio/")

    def test_the_listing_is_cached_for_five_minutes(self):
        self.rec.results = [listing(["audio/a.m4a"])]
        audiostore.bucket_ids()
        audiostore.bucket_ids()
        audiostore.bucket_ids()
        self.assertEqual(len(self.rec.calls), 1, "one LIST per five minutes, never one per ask")
        audiostore._LIST["at"] = 0.0                      # five minutes pass
        self.rec.results = [listing(["audio/a.m4a", "audio/b.m4a"])]
        self.assertEqual(audiostore.bucket_ids(), {"a", "b"})
        self.assertEqual(len(self.rec.calls), 2)

    def test_a_failed_first_listing_is_unknown_not_empty(self):
        self.rec.results = [FakeProc(returncode=1, stderr="boom")]
        self.assertIsNone(audiostore.bucket_ids())

    def test_a_failed_later_listing_keeps_the_stale_set(self):
        self.rec.results = [listing(["audio/a.m4a"])]
        self.assertEqual(audiostore.bucket_ids(), {"a"})
        audiostore._LIST["at"] = 0.0
        self.rec.results = [FakeProc(returncode=1)]
        self.assertEqual(audiostore.bucket_ids(), {"a"},
                         "stale-but-real beats 'cannot say' for a reader choosing what to sign")

    def test_available_ids_is_the_union_of_local_and_bucket(self):
        root = self._root({"loc": {"bucket": "keep", "file": "keep/loc.m4a"}})
        self._file(root, "keep/loc.m4a")
        self.rec.results = [listing(["audio/rem.m4a"])]
        self.assertEqual(audiostore.available_ids(), {"loc", "rem"})

    def test_available_ids_without_a_bucket_is_just_the_local_files(self):
        os.environ.pop("NETRADIO_AUDIO_BUCKET")
        root = self._root({"loc": {"bucket": "keep", "file": "keep/loc.m4a"}})
        self._file(root, "keep/loc.m4a")
        self.assertEqual(audiostore.available_ids(), {"loc"})
        self.assertEqual(self.rec.calls, [])


class Fetch(Base):
    def setUp(self):
        Base.setUp(self)
        self._bucket_on()
        self.dest = os.path.join(self.tmp.name, "job")

    def _cp_writing(self, nbytes):
        def _cp(cmd):
            with open(cmd[-2], "wb") as fh:
                fh.write(b"y" * nbytes)
            return FakeProc()
        return _cp

    def _head(self, key, size):
        return FakeProc(stdout=json.dumps({"Contents": [{"Key": key, "Size": size}]}))

    def test_found_by_prefix_copied_by_key_named_by_extension(self):
        self.rec.results = [self._head("audio/abc.opus", 5), self._cp_writing(5)]
        got = audiostore.fetch("abc", self.dest)
        self.assertEqual(got, os.path.join(self.dest, "audio.opus"))
        self.assertEqual(os.path.getsize(got), 5)
        head, cp = self.rec.calls
        self.assertEqual(head[head.index("--prefix") + 1], "audio/abc.",
                         "the dot keeps `abc` from reaching `abcd`'s object")
        self.assertEqual(cp[cp.index("cp") + 1], "s3://audio-test/audio/abc.opus")
        self.assertEqual([n for n in os.listdir(self.dest) if n.startswith("audio.part-")], [],
                         "the temporary name is gone once the copy is in place")

    def test_a_miss_is_none_and_copies_nothing(self):
        self.rec.results = [FakeProc(stdout=json.dumps({}))]
        self.assertIsNone(audiostore.fetch("abc", self.dest))
        self.assertEqual(len(self.rec.calls), 1)

    def test_a_short_copy_never_takes_the_final_name(self):
        self.rec.results = [self._head("audio/abc.m4a", 100), self._cp_writing(60)]
        self.assertIsNone(audiostore.fetch("abc", self.dest))
        self.assertEqual(os.listdir(self.dest), [], "no partial file under any name")

    def test_a_failed_copy_is_none(self):
        self.rec.results = [self._head("audio/abc.m4a", 100), FakeProc(returncode=1)]
        self.assertIsNone(audiostore.fetch("abc", self.dest))
        self.assertEqual(os.listdir(self.dest), [])

    def test_nothing_this_module_does_is_a_delete_or_an_upload(self):
        """The player owns the audio. Every verb this module ever sends to the CLI is a read."""
        root = self._root({"loc": {"bucket": "keep", "file": "keep/loc.m4a"}})
        self._file(root, "keep/loc.m4a")
        self.rec.results = [listing(["audio/x.m4a"]), self._head("audio/x.m4a", 3),
                            self._cp_writing(3)]
        audiostore.available_ids()
        audiostore.fetch("x", self.dest)
        audiostore.local_path("loc")
        for cmd in self.rec.calls:
            joined = " ".join(cmd)
            for forbidden in (" rm ", "delete-object", "delete-objects", " mv ", " sync ",
                              "put-object", "s3://audio-test/audio/x.m4a " + root):
                self.assertNotIn(forbidden, joined + " ")
            self.assertTrue(any(v in cmd for v in ("list-objects-v2", "cp")), cmd)
        self.assertTrue(os.path.exists(os.path.join(root, "keep/loc.m4a")))
        self.assertFalse(any(n for n in os.listdir(root) if n != "index.json" and n != "keep"))


if __name__ == "__main__":
    unittest.main()
