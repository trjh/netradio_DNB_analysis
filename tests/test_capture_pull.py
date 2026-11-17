"""The capture pull: a decode of a capture this disk does not hold pulls it from the audio bucket.

`find_audio_file` stays a local probe (`.wav`/`.au`, never `.mp3`, never a bucket call);
`capture_nameable` adds the `stream_master` cache and a held listing of `stream/master/`;
`load_audio` pulls on its miss path into `stream_master`, checked by the object's SHA-256
metadata, and keys the decoded array by that hash. Everything runs over `tests/bucket_fake.py`:
no network, no `aws`, no ffmpeg (the decode is a stub that records what it was handed).
"""

import contextlib
import hashlib
import io
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))
sys.path.insert(0, HERE)

from optional_imports import import_or_skip  # noqa: E402

np = import_or_skip("numpy")
audio = import_or_skip("streamalign.audio") if np else None

import cache_budget  # noqa: E402
from bucket_fake import AUDIO_BUCKET, FakeAws, FakeProc, isolate_caches  # noqa: E402

STEM = "d100-119"
KEY = "stream/master/%s.wav" % STEM
BODY = b"RIFF" + b"\x01" * 4000
SAMPLES = 16000 * 3


@unittest.skipUnless(np, "needs numpy")
class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="capture-pull-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.fake = isolate_caches(self, os.path.join(self.tmp, "root"), fake=FakeAws())
        self.audio_dir = os.path.join(self.tmp, "captures")
        os.makedirs(self.audio_dir)
        audio.register_cache()
        audio.register_master_cache()
        self.master_dir = cache_budget.dir_of(audio.STREAM_MASTER)
        self.decodes = []           # (path, pinned while decoding)
        patcher = mock.patch.object(audio, "_ffmpeg_decode", self._decode)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _decode(self, path, sr, mono):
        self.decodes.append((path, audio._pinned_entry(path)))
        return np.zeros(SAMPLES, dtype="float32")

    def add(self, key=KEY, body=BODY, sha256=True):
        self.fake.add(AUDIO_BUCKET, key, body, sha256=sha256)

    def local(self, name, body=b"x" * 64):
        path = os.path.join(self.audio_dir, name)
        with open(path, "wb") as fh:
            fh.write(body)
        return path

    def pulled(self):
        return os.path.join(self.master_dir, STEM + ".wav")


class TheLocalProbe(Base):
    def test_matches_the_wav_and_the_au_never_the_mp3_and_never_asks_the_bucket(self):
        self.add()
        self.add("stream/master/b.au")
        self.local("a.wav")
        self.local("b.au")
        self.local("c.mp3")
        self.assertTrue(audio.find_audio_file("a", self.audio_dir).endswith("a.wav"))
        self.assertTrue(audio.find_audio_file("b.mp3", self.audio_dir).endswith("b.au"))
        self.assertIsNone(audio.find_audio_file("c", self.audio_dir))
        self.assertIsNone(audio.find_audio_file(STEM, self.audio_dir))
        self.assertEqual(self.fake.calls, [])

    def test_groundtruth_over_absent_captures_pulls_nothing(self):
        from streamalign import __main__ as cli
        self.add()
        args = mock.Mock(labels=self.tmp)
        with mock.patch.object(cli._gt, "resolve_starts", return_value={STEM: 0.0, "x": 9.0}), \
                mock.patch.object(audio, "AUDIO_DIR", self.audio_dir), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            cli._cmd_groundtruth(args)
        self.assertIn("NO-AUDIO", out.getvalue())
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.decodes, [])


class TheNameableCheck(Base):
    def test_names_a_capture_only_the_bucket_holds(self):
        self.add()
        self.assertTrue(audio.capture_nameable(STEM, self.audio_dir))
        self.assertFalse(audio.capture_nameable("d999-000", self.audio_dir))
        self.assertEqual(self.fake.ops("cp-down"), [], "a membership check never pulls")

    def test_a_loop_over_every_capture_costs_one_listing(self):
        self.add()
        for _ in range(20):
            audio.capture_nameable(STEM, self.audio_dir)
            audio.capture_nameable("d999-000", self.audio_dir)
        self.assertEqual(self.fake.ops("list-objects-v2"), ["list-objects-v2"])

    def test_with_no_audio_bucket_set_the_disk_alone(self):
        self.add()
        os.environ.pop("NETRADIO_AUDIO_BUCKET")
        self.assertFalse(audio.capture_nameable(STEM, self.audio_dir))
        self.local("e.wav")
        self.assertTrue(audio.capture_nameable("e", self.audio_dir))
        self.assertEqual(self.fake.calls, [])


class ThePull(Base):
    def test_a_miss_pulls_once_pins_for_the_decode_and_unpins_after(self):
        self.add()
        signal = audio.load_audio(STEM, audio_dir=self.audio_dir)
        self.assertEqual(len(signal), SAMPLES)
        self.assertEqual(self.fake.ops("cp-down"), ["cp-down"])
        self.assertEqual(self.decodes, [(self.pulled(), True)], "decoded the pulled file, pinned")
        self.assertFalse(audio._pinned_entry(self.pulled()), "unpinned after the decode")
        with open(self.pulled(), "rb") as fh:
            self.assertEqual(fh.read(), BODY)
        entries = [os.path.basename(e["path"]) if isinstance(e, dict) else os.path.basename(e[0])
                   for e in cache_budget.entries(audio.STREAM_MASTER)]
        self.assertIn(STEM + ".wav", entries, "the pulled file is an ordinary entry")

    def test_fetch_audio_file_pulls_and_then_returns_the_held_file_without_a_call(self):
        self.add()
        path = audio.fetch_audio_file(STEM)
        self.assertEqual(path, self.pulled())
        calls = len(self.fake.calls)
        self.assertEqual(audio.fetch_audio_file(STEM), path)
        self.assertEqual(len(self.fake.calls), calls)

    def test_a_held_file_is_decoded_with_no_bucket_call_and_its_array_reused(self):
        self.add()
        audio.load_audio(STEM, audio_dir=self.audio_dir)
        calls, decodes = len(self.fake.calls), len(self.decodes)
        audio.load_audio(STEM, audio_dir=self.audio_dir)
        self.assertEqual(len(self.fake.calls), calls, "no bucket call")
        self.assertEqual(len(self.decodes), decodes, "no decode")

    def test_after_eviction_one_head_finds_the_array_by_the_objects_hash(self):
        self.add()
        audio.load_audio(STEM, audio_dir=self.audio_dir)
        self.assertTrue(cache_budget.remove(audio.STREAM_MASTER, self.pulled(), "test"))
        self.assertFalse(os.path.exists(self.pulled() + audio._OBJECT_META),
                         "the companion goes with its file")
        self.fake.calls.clear()
        decodes = len(self.decodes)
        signal = audio.load_audio(STEM, audio_dir=self.audio_dir)
        self.assertEqual(len(signal), SAMPLES)
        self.assertEqual(self.fake.ops("head-object"), ["head-object"])
        self.assertEqual(self.fake.ops("cp-down"), [], "no pull")
        self.assertEqual(len(self.decodes), decodes, "no decode")

    def test_an_object_with_no_sha256_metadata_is_refused_and_nothing_decoded(self):
        self.add(sha256=False)
        with self.assertRaises(FileNotFoundError) as caught:
            audio.load_audio(STEM, audio_dir=self.audio_dir)
        self.assertIn("sha256", str(caught.exception))
        self.assertEqual(self.fake.ops("cp-down"), [])
        self.assertEqual(self.decodes, [])
        self.assertIsNone(audio.fetch_audio_file(STEM))

    def test_a_hash_mismatch_is_refused_and_nothing_decoded(self):
        self.add()
        self.fake.served[(AUDIO_BUCKET, KEY)] = b"RIFF" + b"\x02" * 4000   # same size
        with self.assertRaises(FileNotFoundError) as caught:
            audio.load_audio(STEM, audio_dir=self.audio_dir)
        self.assertIn("sha256 mismatch", str(caught.exception))
        self.assertEqual(self.decodes, [])
        self.assertFalse(os.path.exists(self.pulled()))
        self.assertEqual([f for f in os.listdir(self.master_dir) if f.endswith(".part")], [])

    def test_a_size_mismatch_is_refused_and_nothing_decoded(self):
        self.add()
        self.fake.served[(AUDIO_BUCKET, KEY)] = BODY[:-10]
        with self.assertRaises(FileNotFoundError) as caught:
            audio.load_audio(STEM, audio_dir=self.audio_dir)
        self.assertIn("size mismatch", str(caught.exception))
        self.assertEqual(self.decodes, [])
        self.assertFalse(os.path.exists(self.pulled()))

    def test_no_answer_from_the_bucket_fails_the_load_as_missing(self):
        self.add()
        self.fake.fail_next("list-objects-v2", FakeProc(returncode=255, stderr="Could not connect"))
        with self.assertRaises(FileNotFoundError):
            audio.load_audio(STEM, audio_dir=self.audio_dir)
        self.assertEqual(self.decodes, [])

    def test_with_no_audio_bucket_set_nothing_is_pulled_and_the_miss_fails_as_today(self):
        self.add()
        os.environ.pop("NETRADIO_AUDIO_BUCKET")
        with self.assertRaises(FileNotFoundError) as caught:
            audio.load_audio(STEM, audio_dir=self.audio_dir)
        self.assertIn("no audio for", str(caught.exception))
        self.assertEqual(self.fake.calls, [])
        self.assertIsNone(audio.fetch_audio_file(STEM))

    def test_a_capture_on_this_disk_is_never_pulled(self):
        self.add()
        self.local(STEM + ".wav")
        audio.load_audio(STEM, audio_dir=self.audio_dir)
        self.assertEqual(self.fake.calls, [])
        self.assertTrue(self.decodes[0][0].startswith(self.audio_dir))

    def test_the_object_hash_keys_the_array(self):
        self.add()
        audio.load_audio(STEM, audio_dir=self.audio_dir)
        sha = hashlib.sha256(BODY).hexdigest()
        want = audio._object_cache_key(sha, audio.SR, True) + ".npy"
        self.assertIn(want, os.listdir(cache_budget.dir_of(audio.CACHE)))


class TheGatesReachABucketOnlyCapture(Base):
    def test_inspect_worker_resolve(self):
        from streamalign import inspect_worker
        self.add()
        with mock.patch.object(audio, "AUDIO_DIR", self.audio_dir), \
                mock.patch("streamalign.track_mix.find_original", return_value="/o/072-x.flac"):
            stem, orig = inspect_worker._resolve(STEM, 72, self.tmp)
        self.assertEqual(stem, STEM)
        self.assertEqual(self.fake.ops("cp-down"), [], "the gate names it; the decode pulls")

    def test_track_mix_select_capture(self):
        from streamalign import track_mix
        self.add()
        cap, cstart = track_mix._select_capture([STEM + ".wav"], 0.5, 2.5, {STEM: 0.0},
                                                audio_dir=self.audio_dir)
        self.assertEqual((cap, cstart), (STEM, 0.0))
        self.assertEqual(self.fake.ops("cp-down"), ["cp-down"])

    def test_extract_tracks_cut_pulls_before_ffmpeg_runs(self):
        import extract_tracks
        self.add()
        seen = []

        def _ffmpeg(cmd, check=False):
            src = cmd[cmd.index("-i") + 1]
            seen.append((src, os.path.isfile(src)))
            with open(cmd[-1], "wb") as fh:
                fh.write(b"fLaC")

        out = os.path.join(self.tmp, "cut.flac")
        with mock.patch.object(audio, "AUDIO_DIR", self.audio_dir), \
                mock.patch.object(extract_tracks.subprocess, "run", _ffmpeg):
            self.assertTrue(extract_tracks.cut(STEM, 1.0, 2.0, 0.0, out))
        self.assertEqual(seen, [(self.pulled(), True)])


class TheNeighbourScan(Base):
    def test_an_evicted_neighbour_counts_nothing_pulled_or_decoded_and_the_warning_names_it(self):
        from streamalign import hints
        self.add()
        self.local("d080-099.wav")
        warnings = []
        with mock.patch.object(audio, "recorded_durations",
                               return_value={STEM: 1200.0, "d080-099": 1200.0}), \
                mock.patch.object(audio, "duration_seconds", return_value=1200.0) as dur:
            got = hints.overlapping_neighbours("d080-099", {"d080-099": 0.0, STEM: 600.0},
                                               audio_dir=self.audio_dir, warn=warnings.append)
        self.assertEqual([g[0] for g in got], [STEM])
        self.assertAlmostEqual(got[0][1], 600.0)
        self.assertEqual(self.fake.ops("cp-down"), [])
        self.assertEqual(self.decodes, [])
        self.assertEqual([c.args[0] for c in dur.call_args_list], ["d080-099"],
                         "only the capture on this disk is measured from its audio")
        self.assertEqual(len(warnings), 1)
        self.assertIn(STEM, warnings[0])
        self.assertIn("stream-files.json", warnings[0])


if __name__ == "__main__":
    unittest.main()
