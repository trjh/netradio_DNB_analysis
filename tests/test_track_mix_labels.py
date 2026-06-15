"""Tests for F3 — track-mix → AUTO GENERATED labels, gated by a by-ear confirm.

The decision store, emitter rows, the labels/ guard, and decide-by-clip-id are pure I/O
and run anywhere. Clip generation stubs the audio/ffmpeg layers. The emitter is checked
for round-trip: the rows it writes recover the confirmed rate via
track_mix.track_sync_groundtruth.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import numpy as np  # noqa: E402

from streamalign import track_mix, track_mix_labels as tml  # noqa: E402


class DecisionStoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_confirm_then_reject_last_wins(self):
        tml.confirm_track(8, rate=1.01, offset_orig_s=2.0, capture="d065-087",
                          labels_dir=self.dir)
        self.assertEqual(tml.decision_for(8, labels_dir=self.dir), "confirm")
        tml.reject_track(8, rate=1.01, capture="d065-087", note="smeared",
                         labels_dir=self.dir)
        self.assertEqual(tml.decision_for(8, labels_dir=self.dir), "reject")
        dec = tml.load_decisions(self.dir)
        self.assertEqual(len(dec), 1)              # one row per track (last wins)
        self.assertEqual(dec[8]["note"], "smeared")

    def test_header_and_unknown_track(self):
        tml.confirm_track(10, rate=1.0, labels_dir=self.dir)
        with open(os.path.join(self.dir, tml.DECISIONS_NAME)) as f:
            self.assertTrue(f.readline().startswith("#"))
        self.assertIsNone(tml.decision_for(999, labels_dir=self.dir))


class SyncRowsTests(unittest.TestCase):
    def test_rows_are_auto_generated_and_well_formed(self):
        rows = tml.sync_rows_for_track(8, 100.0, 300.0, 1.01, 2.5)
        texts = [t for _a, _b, t in rows]
        self.assertTrue(all(t.endswith("AUTO GENERATED") for t in texts))
        self.assertTrue(any(t.startswith("orig008 sync: A") for t in texts))
        self.assertTrue(any(t.startswith("track008 sync: B") for t in texts))
        self.assertTrue(any("orig-map: rate=1.01000" in t for t in texts))

    def test_rows_round_trip_the_rate(self):
        # write rows to a temp labels-style dir; track_sync_groundtruth must recover rate
        out = tempfile.mkdtemp()
        rows = tml.sync_rows_for_track(8, 100.0, 300.0, 1.01, 0.0)
        with open(os.path.join(out, "d065-087.trackmix.auto.labels.tsv"), "w") as f:
            for a, b, text in rows:
                f.write("%.6f\t%.6f\t%s\n" % (a, b, text))
        gt = track_mix.track_sync_groundtruth(out)
        self.assertAlmostEqual(gt[8]["rate"], 1.01, places=4)


class EmitTests(unittest.TestCase):
    def setUp(self):
        self.labels = tempfile.mkdtemp()
        self.out = tempfile.mkdtemp()
        self.meta = {"8": {"master_begin_seconds": 1200.0, "master_end_seconds": 1400.0},
                     "10": {"master_begin_seconds": 1500.0, "master_end_seconds": 1600.0}}
        self._rs = tml._gt.resolve_starts
        tml._gt.resolve_starts = staticmethod(lambda labels_dir=None: {"d065-087": 1000.0})

    def tearDown(self):
        tml._gt.resolve_starts = self._rs

    def _results(self):
        return [{"track": 8, "capture": "d065-087", "rate": 1.01, "offset_orig_s": 0.0},
                {"track": 10, "capture": "d065-087", "rate": 0.99, "offset_orig_s": 5.0}]

    def test_only_confirmed_emitted(self):
        tml.confirm_track(8, rate=1.01, capture="d065-087", labels_dir=self.labels)
        # track 10 left undecided
        emitted = tml.emit_track_labels(self._results(), self.out, self.meta,
                                        labels_dir=self.labels)
        self.assertEqual(emitted, {8: "d065-087"})
        path = os.path.join(self.out, "d065-087.trackmix.auto.labels.tsv")
        with open(path) as f:
            text = f.read()
        self.assertIn("track008 sync: B", text)
        self.assertNotIn("track010", text)        # unconfirmed not emitted
        # round-trips back to rate 1.01
        gt = track_mix.track_sync_groundtruth(self.out)
        self.assertAlmostEqual(gt[8]["rate"], 1.01, places=4)

    def test_refuses_to_emit_into_labels_dir(self):
        tml.confirm_track(8, rate=1.01, capture="d065-087", labels_dir=self.labels)
        with self.assertRaises(ValueError):
            tml.emit_track_labels(self._results(), self.labels, self.meta,
                                  labels_dir=self.labels)

    def test_rejected_track_not_emitted(self):
        tml.reject_track(8, rate=1.01, capture="d065-087", labels_dir=self.labels)
        emitted = tml.emit_track_labels(self._results(), self.out, self.meta,
                                        labels_dir=self.labels)
        self.assertEqual(emitted, {})


class AlignClipTests(unittest.TestCase):
    def test_aligned_overlay_is_coherent_misaligned_is_not(self):
        sr = tml._audio.SR
        sig = np.sin(np.arange(int(30 * sr)) * 0.02)
        # rate 1, offset 0, orig == mix region -> overlay reinforces (corr ~ 1)
        clip = tml.make_align_clip(sig, sig, 1.0, 0.0, sr=sr, dur_s=10.0)
        self.assertEqual(len(clip), int(10 * sr))
        aligned_corr = np.corrcoef(clip, sig[:len(clip)])[0, 1]
        # a wrong offset (5 s into the original) decorrelates the overlay
        bad = tml.make_align_clip(sig, sig, 1.0, 5.0, sr=sr, dur_s=10.0)
        bad_corr = np.corrcoef(bad, sig[:len(bad)])[0, 1]
        self.assertGreater(aligned_corr, 0.99)
        self.assertLess(bad_corr, aligned_corr)

    def test_empty_excerpt_returns_none(self):
        sr = tml._audio.SR
        self.assertIsNone(tml.make_align_clip(np.zeros(10), np.zeros(0), 1.0, 0.0, sr=sr))


class _AudioStub:
    SR = tml._audio.SR
    lengths = {"d065-087": 1500.0}

    @staticmethod
    def find_audio_file(stem, audio_dir=None):
        return stem in _AudioStub.lengths or stem.endswith(".mp3")

    @staticmethod
    def load_audio(stem, audio_dir=None):
        secs = _AudioStub.lengths.get(stem, 200.0)
        return np.sin(np.arange(int(secs * _AudioStub.SR)) * 0.02)


class GenerateClipsTests(unittest.TestCase):
    def setUp(self):
        self.out = tempfile.mkdtemp()
        self.meta = {"8": {"master_begin_seconds": 1200.0, "master_end_seconds": 1260.0}}
        self._a, self._rs, self._fo, self._wr = (
            tml._audio, tml._gt.resolve_starts, track_mix.find_original,
            tml._clips.write_clip)
        tml._audio = _AudioStub
        tml._gt.resolve_starts = staticmethod(lambda labels_dir=None: {"d065-087": 1000.0})
        track_mix.find_original = staticmethod(lambda n, sd: "008-original.mp3")
        self._written = []
        tml._clips.write_clip = staticmethod(lambda arr, path, sr=0: self._written.append(path))

    def tearDown(self):
        tml._audio, tml._clips.write_clip = self._a, self._wr
        tml._gt.resolve_starts, track_mix.find_original = self._rs, self._fo

    def test_writes_manifest_and_sidecar(self):
        results = [{"track": 8, "capture": "d065-087", "rate": 1.01, "offset_orig_s": 0.0}]
        entries = tml.generate_review_clips(results, "srcs", self.meta, self.out)
        self.assertEqual(len(entries), 1)
        cid = entries[0]["id"]
        self.assertEqual(cid, "trackmix_008_d065-087")
        self.assertTrue(self._written[0].endswith(cid + ".mp3"))
        with open(os.path.join(self.out, "manifest.json")) as f:
            self.assertEqual(json.load(f)["clips"][0]["id"], cid)
        sc = tml._load_sidecar(self.out)
        self.assertEqual(sc[cid]["track"], 8)

    def test_decide_clip_confirm_and_unknown(self):
        results = [{"track": 8, "capture": "d065-087", "rate": 1.01, "offset_orig_s": 0.0}]
        tml.generate_review_clips(results, "srcs", self.meta, self.out)
        labels = tempfile.mkdtemp()
        tml.decide_clip("trackmix_008_d065-087", "confirm", self.out, labels_dir=labels)
        self.assertEqual(tml.decision_for(8, labels_dir=labels), "confirm")
        with self.assertRaises(KeyError):
            tml.decide_clip("nope", "confirm", self.out, labels_dir=labels)
        with self.assertRaises(ValueError):
            tml.decide_clip("trackmix_008_d065-087", "maybe", self.out, labels_dir=labels)


if __name__ == "__main__":
    unittest.main()
