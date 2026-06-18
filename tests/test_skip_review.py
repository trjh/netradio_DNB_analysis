"""Tests for the F1 skip-check runner + confirm/reject write-back.

The decision store, confirm/reject, apply_decisions and the candidate sidecar are
pure I/O and run anywhere. Candidate enumeration + clip generation stub the audio /
detection / ffmpeg layers (the captures live on Tim's disk, not in the repo), mirroring
the _Stub pattern in test_streamalign.py.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import numpy as np  # noqa: E402

from streamalign import emit_labels, groundtruth, skip_review  # noqa: E402


class DirectionTests(unittest.TestCase):
    def test_sign_convention_matches_emit(self):
        # negative offset step => skipper jumped AHEAD; positive => BACK.
        self.assertEqual(skip_review._direction(-1.25), ("ahead", 1.25))
        self.assertEqual(skip_review._direction(0.96), ("back", 0.96))

    def test_same_skip_matches_position_and_direction(self):
        self.assertTrue(skip_review._same_skip(20.0, -1.0, 20.8, -1.2))   # near + same dir
        self.assertFalse(skip_review._same_skip(20.0, -1.0, 20.0, 1.0))   # opposite dir
        self.assertFalse(skip_review._same_skip(20.0, -1.0, 30.0, -1.0))  # too far


class RejectionStoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_reject_adds_then_idempotent(self):
        self.assertEqual(skip_review.reject_skip("d100-119", 42.0, -1.0, "d099-118",
                                                 note="doubling", labels_dir=self.dir), "added")
        self.assertEqual(skip_review.reject_skip("d100-119", 42.4, -1.1, "d099-118",
                                                 labels_dir=self.dir), "already")
        rej = skip_review.load_rejections(self.dir)
        self.assertEqual(len(rej), 1)
        self.assertEqual(rej[0]["stem"], "d100-119")
        self.assertEqual(rej[0]["note"], "doubling")

    def test_is_rejected_tolerant_match(self):
        skip_review.reject_skip("d100-119", 42.0, -1.0, labels_dir=self.dir)
        self.assertTrue(skip_review.is_rejected("d100-119", 42.5, -0.9, labels_dir=self.dir))
        self.assertFalse(skip_review.is_rejected("d100-119", 42.5, 0.9, labels_dir=self.dir))  # dir flip
        self.assertFalse(skip_review.is_rejected("dXXX", 42.0, -1.0, labels_dir=self.dir))

    def test_file_has_header_and_is_tsv(self):
        skip_review.reject_skip("d100-119", 42.0, -1.0, labels_dir=self.dir)
        with open(os.path.join(self.dir, skip_review.REJECTIONS_NAME)) as f:
            lines = f.read().splitlines()
        self.assertTrue(lines[0].startswith("#"))
        self.assertEqual(lines[1].split("\t")[0], "d100-119")


class ConfirmTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_confirm_appends_hand_grammar_row(self):
        status = skip_review.confirm_skip("d100-119", 50.0, -1.248, reference="d099-118",
                                          before_s=49.4, after_s=50.6, labels_dir=self.dir)
        self.assertEqual(status, "added")
        path = os.path.join(self.dir, "d100-119.labels.tsv")
        with open(path) as f:
            row = f.read().splitlines()[-1]
        cols = row.split("\t")
        self.assertAlmostEqual(float(cols[0]), 49.4, places=3)
        self.assertAlmostEqual(float(cols[1]), 50.6, places=3)
        self.assertEqual(cols[2], "file note: skip ahead 1.248s verified d099-118")
        # NOT auto-generated: a confirmed skip is now ground truth (hand grammar)
        self.assertNotIn("AUTO GENERATED", row)

    def test_confirm_never_overwrites_existing_hand_content(self):
        path = os.path.join(self.dir, "d100-119.labels.tsv")
        with open(path, "w") as f:
            f.write("0.000000\t0.000000\tfile start sync: d100-119.wav 123.0 verified d099-118\n")
        skip_review.confirm_skip("d100-119", 50.0, -1.0, reference="d099-118", labels_dir=self.dir)
        with open(path) as f:
            content = f.read()
        self.assertIn("file start sync: d100-119.wav 123.0", content)   # original preserved
        self.assertIn("file note: skip ahead 1.000s verified d099-118", content)

    def test_confirm_idempotent_for_nearby_same_direction(self):
        skip_review.confirm_skip("d100-119", 50.0, -1.0, "d099-118", labels_dir=self.dir)
        self.assertEqual(
            skip_review.confirm_skip("d100-119", 50.7, -1.1, "d099-118", labels_dir=self.dir),
            "already")
        with open(os.path.join(self.dir, "d100-119.labels.tsv")) as f:
            self.assertEqual(len([ln for ln in f if "skip" in ln]), 1)


class ApplyDecisionsTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_drops_only_rejected_skips(self):
        skip_review.reject_skip("d100-119", 50.0, -1.0, labels_dir=self.dir)
        skip_maps = {"d100-119": [{"at_s": 50.2, "delta_s": -1.0},   # rejected
                                  {"at_s": 80.0, "delta_s": 0.5}],   # kept
                     "d120-139": [{"at_s": 10.0, "delta_s": -2.0}]}  # kept
        out = skip_review.apply_decisions(skip_maps, labels_dir=self.dir)
        self.assertEqual([s["at_s"] for s in out["d100-119"]], [80.0])
        self.assertEqual(len(out["d120-139"]), 1)

    def test_emit_labels_excludes_rejected_skip(self):
        skip_review.reject_skip("d900-901", 30.0, -1.0, labels_dir=self.dir)
        out = tempfile.mkdtemp()
        emit_labels.emit_labels({"d900-901": 0.0}, out, durations={"d900-901": 120.0},
                                skip_maps={"d900-901": [{"at_s": 30.0, "delta_s": -1.0},
                                                        {"at_s": 90.0, "delta_s": 0.7}]},
                                labels_dir=self.dir)
        with open(os.path.join(out, "d900-901.auto.labels.tsv")) as f:
            text = f.read()
        self.assertNotIn("skip ahead", text)   # the rejected one is gone
        self.assertIn("skip back 0.700s", text)  # the other survives


class OrientationTests(unittest.TestCase):
    def test_reference_is_better_anchored_capture(self):
        # b nearer the anchor (smaller start) => b is reference, a is skipper, seed kept
        self.assertEqual(skip_review._orient("d100-119", "d090-109", 5.0),
                         ("d100-119", "d090-109", 5.0))
        # a nearer the anchor => a is reference, b becomes skipper, seed negated
        self.assertEqual(skip_review._orient("d090-109", "d100-119", 5.0),
                         ("d100-119", "d090-109", -5.0))

    def test_filename_start_parsing(self):
        self.assertEqual(skip_review._filename_start("d356-375"), 356)
        self.assertEqual(skip_review._filename_start("d-25-005b"), 25)


class SidecarAndDecideTests(unittest.TestCase):
    def setUp(self):
        self.out = tempfile.mkdtemp()
        self.labels = tempfile.mkdtemp()
        # off_after - off_before == delta_s (-1.2); offsets chosen so reference-local
        # times stay positive (skipper_t - offset).
        self.cand = {"skipper": "d100-119", "reference": "d099-118", "at_s": 50.0,
                     "delta_s": -1.2, "before_s": 49.4, "after_s": 50.6,
                     "off_before_s": -10.0, "off_after_s": -11.2,
                     "seed_offset_s": -10.0, "conf": 0.95}
        skip_review.save_candidates(self.out, {"d100-119_d099-118_skip1": dict(self.cand,
                                                                               id="d100-119_d099-118_skip1")})

    def test_roundtrip_sidecar(self):
        loaded = skip_review.load_candidates(self.out)
        self.assertIn("d100-119_d099-118_skip1", loaded)
        self.assertEqual(loaded["d100-119_d099-118_skip1"]["delta_s"], -1.2)

    def test_decide_confirm_writes_hand_label(self):
        status, cand = skip_review.decide("d100-119_d099-118_skip1", "confirm", self.out,
                                          labels_dir=self.labels)
        self.assertEqual(status, "added")
        with open(os.path.join(self.labels, "d100-119.labels.tsv")) as f:
            self.assertIn("file note: skip ahead 1.200s verified d099-118", f.read())

    def test_decide_reject_writes_rejection(self):
        status, _ = skip_review.decide("d100-119_d099-118_skip1", "reject", self.out,
                                       labels_dir=self.labels, note="doubling")
        self.assertEqual(status, "added")
        self.assertTrue(skip_review.is_rejected("d100-119", 50.0, -1.2, labels_dir=self.labels))

    def test_decide_owner_override_transforms_coords_and_direction(self):
        # reattribute to the reference: times → reference-local (skipper_t - offset),
        # direction inverted (ahead→back), verified-ref points back at the skipper.
        skip_review.decide("d100-119_d099-118_skip1", "confirm", self.out,
                           labels_dir=self.labels, owner="d099-118")
        self.assertFalse(os.path.exists(os.path.join(self.labels, "d100-119.labels.tsv")))
        path = os.path.join(self.labels, "d099-118.labels.tsv")
        with open(path) as f:
            cols = f.read().splitlines()[-1].split("\t")
        # before 49.4 - (-10.0) = 59.4 ; after 50.6 - (-11.2) = 61.8
        self.assertAlmostEqual(float(cols[0]), 59.4, places=3)
        self.assertAlmostEqual(float(cols[1]), 61.8, places=3)
        self.assertEqual(cols[2], "file note: skip back 1.200s verified d100-119")

    def test_decide_owner_override_reject_uses_reference_coords(self):
        skip_review.decide("d100-119_d099-118_skip1", "reject", self.out,
                           labels_dir=self.labels, owner="d099-118")
        rej = skip_review.load_rejections(self.labels)
        self.assertEqual(len(rej), 1)
        self.assertEqual(rej[0]["stem"], "d099-118")
        self.assertGreater(rej[0]["delta_s"], 0)        # direction inverted (was -1.2)
        self.assertAlmostEqual(rej[0]["at_s"], 60.6, places=3)  # midpoint of 59.4..61.8
        self.assertEqual(rej[0]["reference"], "d100-119")

    def test_decide_rejects_owner_not_in_pair(self):
        with self.assertRaises(ValueError):
            skip_review.decide("d100-119_d099-118_skip1", "confirm", self.out,
                               labels_dir=self.labels, owner="d999-998")

    def test_reattribute_to_reference_needs_offsets(self):
        skip_review.save_candidates(self.out, {"x": {
            "skipper": "d100-119", "reference": "d099-118", "at_s": 50.0, "delta_s": -1.2,
            "before_s": 49.4, "after_s": 50.6, "id": "x"}})  # no off_before/after_s
        with self.assertRaises(ValueError):
            skip_review.decide("x", "confirm", self.out, labels_dir=self.labels,
                               owner="d099-118")

    def test_decide_unknown_id_raises(self):
        with self.assertRaises(KeyError):
            skip_review.decide("nope", "confirm", self.out, labels_dir=self.labels)


class _AudioStub:
    SR = skip_review._audio.SR
    lengths = {"d100-119": 1200.0, "d099-118": 1200.0}

    @staticmethod
    def find_audio_file(stem, audio_dir=None):
        return stem in _AudioStub.lengths

    @staticmethod
    def load_audio(stem, audio_dir=None):
        return np.zeros(int(_AudioStub.lengths[stem] * _AudioStub.SR), dtype="float32")


class EnumerateTests(unittest.TestCase):
    """enumerate_candidates with the audio/detection layers stubbed."""
    def setUp(self):
        self.labels = tempfile.mkdtemp()
        self._a, self._sk, self._so = skip_review._audio, skip_review._skips, skip_review._solve
        skip_review._audio = _AudioStub
        skip_review._solve = type("S", (), {
            "measure_edge_skipaware": staticmethod(lambda a, b: {"offset_s": 600.0, "conf": 0.95}),
            "_dedupe": staticmethod(self._so._dedupe),
        })
        skip_review._skips = type("K", (), {
            "characterise_overlap": staticmethod(lambda skp, ref, lo, hi, seed: {
                "walk": [(49.0, -10.0, 0.95), (51.0, -11.2, 0.95)],
                "skips": [{"at_s": 50.0, "delta_s": -1.2, "before_s": 49.0, "after_s": 51.0}]}),
        })

    def tearDown(self):
        skip_review._audio, skip_review._skips, skip_review._solve = self._a, self._sk, self._so

    def test_enumerate_orients_and_filters_rejected(self):
        cands = skip_review.enumerate_candidates(
            self.labels, pairs=[("d099-118", "d100-119")])
        self.assertEqual(len(cands), 1)
        c = cands[0]
        self.assertEqual(c["skipper"], "d100-119")    # higher start => skipper
        self.assertEqual(c["reference"], "d099-118")
        self.assertEqual(c["seed_offset_s"], -600.0)  # seed negated by the flip
        # now reject it and confirm a re-run drops it
        skip_review.reject_skip("d100-119", 50.0, -1.2, labels_dir=self.labels)
        self.assertEqual(skip_review.enumerate_candidates(
            self.labels, pairs=[("d099-118", "d100-119")]), [])


class GenerateClipsTests(unittest.TestCase):
    """generate_clips with audio + ffmpeg write + clip builder stubbed."""
    def setUp(self):
        self.out = tempfile.mkdtemp()
        self._a, self._clips_mk, self._clips_wr, self._walk = (
            skip_review._audio, skip_review._clips.make_skip_clip,
            skip_review._clips.write_clip, skip_review._skips.walk_overlap)
        skip_review._audio = _AudioStub
        skip_review._skips.walk_overlap = staticmethod(lambda *a, **k: [])
        skip_review._clips.make_skip_clip = staticmethod(
            lambda a, b, skip, walk, sr=0: (np.zeros(int(_AudioStub.SR * 5)),
                                            [{"t": 0.0, "label": "x"}]))
        self._written = []
        skip_review._clips.write_clip = staticmethod(
            lambda arr, path, sr=0: self._written.append(path))

    def tearDown(self):
        skip_review._audio = self._a
        skip_review._clips.make_skip_clip = self._clips_mk
        skip_review._clips.write_clip = self._clips_wr
        skip_review._skips.walk_overlap = self._walk

    def test_generates_manifest_and_sidecar(self):
        cand = {"skipper": "d100-119", "reference": "d099-118", "at_s": 50.0,
                "delta_s": -1.2, "before_s": 49.0, "after_s": 51.0,
                "seed_offset_s": -600.0, "conf": 0.95}
        entries = skip_review.generate_clips([cand], self.out)
        self.assertEqual(len(entries), 1)
        cid = entries[0]["id"]
        self.assertEqual(cid, "d100-119_d099-118_skip1")
        self.assertTrue(self._written[0].endswith(cid + ".mp3"))
        with open(os.path.join(self.out, "manifest.json")) as f:
            manifest = json.load(f)
        self.assertEqual(manifest["clips"][0]["id"], cid)
        sidecar = skip_review.load_candidates(self.out)
        self.assertIn(cid, sidecar)
        self.assertEqual(sidecar[cid]["delta_s"], -1.2)

    def test_rerun_prunes_rejected_clip_from_manifest_sidecar_and_disk(self):
        labels = tempfile.mkdtemp()
        cid = "d100-119_d099-118_skip1"
        # seed a prior run: manifest + sidecar + on-disk mp3 for the clip
        import streamalign.clips as _clips
        _clips._append_manifest(self.out, [{"id": cid, "audio": cid + ".mp3",
                                            "title": "old rejected"}])
        skip_review.save_candidates(self.out, {cid: {
            "id": cid, "skipper": "d100-119", "reference": "d099-118",
            "at_s": 50.0, "delta_s": -1.2, "before_s": 49.4, "after_s": 50.6}})
        mp3 = os.path.join(self.out, cid + ".mp3")
        open(mp3, "wb").close()
        # Tim rejects it; a rerun produces no matching candidate
        skip_review.reject_skip("d100-119", 50.0, -1.2, labels_dir=labels)
        skip_review.generate_clips([], self.out, labels_dir=labels)
        # gone from BOTH stores and disk; the player will never resurface it
        with open(os.path.join(self.out, "manifest.json")) as f:
            ids = [c.get("id") for c in json.load(f)["clips"]]
        self.assertNotIn(cid, ids)
        self.assertNotIn(cid, skip_review.load_candidates(self.out))
        self.assertFalse(os.path.exists(mp3))

    def test_rerun_keeps_unrelated_clips(self):
        labels = tempfile.mkdtemp()
        import streamalign.clips as _clips
        _clips._append_manifest(self.out, [{"id": "other_tool_clip", "audio": "x.mp3"}])
        skip_review.reject_skip("d100-119", 50.0, -1.2, labels_dir=labels)
        skip_review.generate_clips([], self.out, labels_dir=labels)
        with open(os.path.join(self.out, "manifest.json")) as f:
            ids = [c.get("id") for c in json.load(f)["clips"]]
        self.assertIn("other_tool_clip", ids)   # non-skip clips untouched


class CliSkipClipsTests(unittest.TestCase):
    """The skip-clips CLI must prune rejected clips even when enumeration is empty."""
    def setUp(self):
        import argparse
        import streamalign.__main__ as main_mod
        import streamalign.clips as _clips
        self.main_mod = main_mod
        self.out = tempfile.mkdtemp()
        self.labels = tempfile.mkdtemp()
        self.cid = "d100-119_d099-118_skip1"
        _clips._append_manifest(self.out, [{"id": self.cid, "audio": self.cid + ".mp3"}])
        skip_review.save_candidates(self.out, {self.cid: {
            "id": self.cid, "skipper": "d100-119", "reference": "d099-118",
            "at_s": 50.0, "delta_s": -1.2, "before_s": 49.4, "after_s": 50.6}})
        self.mp3 = os.path.join(self.out, self.cid + ".mp3")
        open(self.mp3, "wb").close()
        skip_review.reject_skip("d100-119", 50.0, -1.2, labels_dir=self.labels)
        self._enum = skip_review.enumerate_candidates
        skip_review.enumerate_candidates = staticmethod(lambda *a, **k: [])
        self.args = argparse.Namespace(out=self.out, conf_min=0.7, labels=self.labels)

    def tearDown(self):
        skip_review.enumerate_candidates = self._enum

    def test_cli_prunes_rejected_when_no_new_candidates(self):
        self.main_mod._cmd_skip_clips(self.args)   # the documented rerun path
        with open(os.path.join(self.out, "manifest.json")) as f:
            ids = [c.get("id") for c in json.load(f)["clips"]]
        self.assertNotIn(self.cid, ids)
        self.assertNotIn(self.cid, skip_review.load_candidates(self.out))
        self.assertFalse(os.path.exists(self.mp3))


if __name__ == "__main__":
    unittest.main()
