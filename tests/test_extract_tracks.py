"""extract_tracks.py: flac cuts, the variable's directory, and the policy's door.

The cuts land in the `stream_tracks` cache as FLAC (the argv names `-f flac` itself -- the
in-flight `.tmp` name's extension would name no container), the output directory comes from
NETRADIO_STREAM_TRACKS_CACHE_DIR (else $NETRADIO_CACHE_ROOT/stream_tracks), and each cut
that lands inside the registered cache goes through the policy: `reserve` before the
write, `commit` after. All synthetic: ffmpeg never runs (the subprocess seam is stubbed),
so the whole thing works on a bare checkout.
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)

import cache_budget                                # noqa: E402
import extract_tracks                              # noqa: E402

CACHE_ENV = ("NETRADIO_CACHE_ROOT", "NETRADIO_DOWNLOAD_ROOT", "NETRADIO_DISK_MAX_PCT",
             "NETRADIO_CACHE_EVENTS_DAYS")


class TheTracksCache(unittest.TestCase):
    KB = 1000

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="stream-tracks-")
        self.addCleanup(self._cleanup)
        self._saved = {k: os.environ.get(k) for k in list(os.environ)
                       if k.startswith("NETRADIO_") and ("CACHE" in k or k in CACHE_ENV)}
        for k in self._saved:
            os.environ.pop(k, None)
        os.environ["NETRADIO_CACHE_ROOT"] = self.tmp
        self._registry = dict(cache_budget._REGISTRY), dict(cache_budget._STATS)
        cache_budget._REGISTRY.clear()
        cache_budget._STATS.clear()
        extract_tracks.register_cache()
        self.addCleanup(setattr, cache_budget, "_disk_usage", cache_budget._disk_usage)
        cache_budget._disk_usage = self._fake_volume

    def _cleanup(self):
        import shutil
        cache_budget._REGISTRY.clear()
        cache_budget._REGISTRY.update(self._registry[0])
        cache_budget._STATS.clear()
        cache_budget._STATS.update(self._registry[1])
        for k in [k for k in list(os.environ)
                  if k.startswith("NETRADIO_") and ("CACHE" in k or k in CACHE_ENV)]:
            os.environ.pop(k, None)
        os.environ.update(self._saved)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fake_volume(self, _path):
        return (100 * 1000 * self.KB, 0, 100 * 1000 * self.KB)

    def test_the_default_directory_is_the_variables(self):
        os.environ["NETRADIO_STREAM_TRACKS_CACHE_DIR"] = os.path.join(self.tmp, "elsewhere")
        self.assertEqual(extract_tracks.tracks_dir(), os.path.join(self.tmp, "elsewhere"))
        os.environ.pop("NETRADIO_STREAM_TRACKS_CACHE_DIR")
        self.assertEqual(extract_tracks.tracks_dir(), os.path.join(self.tmp, "stream_tracks"))
        os.environ.pop("NETRADIO_CACHE_ROOT")
        self.assertIsNone(extract_tracks.tracks_dir(),
                          "no directory at all while the policy is dark -- never a fallback")

    def test_resolve_out_the_default_must_be_a_registered_cache(self):
        out, why = extract_tracks.resolve_out()
        self.assertEqual((out, why), (os.path.join(self.tmp, "stream_tracks"), None))
        # dark: no directory at all
        os.environ.pop("NETRADIO_CACHE_ROOT")
        out, why = extract_tracks.resolve_out()
        self.assertIsNone(out)
        self.assertIn("no tracks directory", why)

    def test_resolve_out_refuses_a_directory_the_policy_refused_to_register(self):
        """The misconfiguration the reviewer reproduced: the directory variable names the
        cache root itself, the policy refuses the registration (no cache holds its own
        root), and cutting into it would be an unbounded cache with no accounting. The tool
        refuses and names the setting, as the harvester refuses a cache that did not
        register; only an explicit --out may go outside the policy."""
        os.environ["NETRADIO_STREAM_TRACKS_CACHE_DIR"] = self.tmp      # == the cache root
        extract_tracks.register_cache()                                # refused, by design
        # the refusal leaves the process's earlier registration standing (the twin replaces
        # a record only on success), so it is the mismatch that must trip the refusal
        self.assertNotEqual(cache_budget.dir_of("stream_tracks"), self.tmp)
        out, why = extract_tracks.resolve_out()
        self.assertIsNone(out)
        self.assertIn("did not register on the policy", why)
        self.assertIn("NETRADIO_STREAM_TRACKS_CACHE_DIR", why)
        # an explicit --out is the operator's own directory: kept, with no policy
        out, why = extract_tracks.resolve_out(os.path.join(self.tmp, "my-tracks"))
        self.assertEqual((out, why), (os.path.join(self.tmp, "my-tracks"), None))
        # and the refusal exits a real run before anything is cut
        argv = [sys.executable, os.path.join(SCRIPTS, "extract_tracks.py")]
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=120)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("did not register on the policy", proc.stdout + proc.stderr)

    def test_a_dry_run_plans_without_a_directory(self):
        """--dry-run writes nothing, so it needs no resolvable directory: the module's own
        docstring offers it as the first thing to try on a bare clone, where no root and no
        --out exist yet. It must plan the cuts, not refuse with the message a real run gets.
        No capture is placed, so every track skips -- the point is that the run completes."""
        os.environ.pop("NETRADIO_CACHE_ROOT")
        os.environ.pop("NETRADIO_STREAM_TRACKS_CACHE_DIR", None)
        self.assertIsNone(extract_tracks.resolve_out()[0], "the dark policy: no directory")
        argv = ["extract_tracks.py", "--dry-run"]
        with unittest.mock.patch.object(sys, "argv", argv), \
                unittest.mock.patch.object(extract_tracks, "positions", lambda: {}), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            extract_tracks.main()               # planning only: must not sys.exit on the refusal
        self.assertIn("capture(s) with PRECISE timing", out.getvalue())
        self.assertNotIn("no tracks directory", out.getvalue(),
                         "the dry run reached the directory refusal a real run gets")

    def test_the_registration(self):
        rec = cache_budget.register("stream_tracks",
                                    cap=int(extract_tracks.STREAM_TRACKS_CACHE_GB * cache_budget.GB),
                                    max_age=extract_tracks.STREAM_TRACKS_CACHE_MAX_AGE_DAYS,
                                    refill="re-extract", rank=8)  # re-read: returns the record
        self.assertEqual((rec["cap"], rec["max_age"], rec["refill"], rec["rank"]),
                         (2 * cache_budget.GB, 14, "re-extract", 8))
        self.assertEqual(rec["dir"], os.path.join(self.tmp, "stream_tracks"))

    def test_the_argv_carries_flac_and_lands_whole_under_the_final_name(self):
        """The cut's output is FLAC, and the final name never exists as a half-written file:
        ffmpeg writes a `.tmp` (the policy's write-in-progress mark, whose extension says
        nothing -- the container is named for it explicitly) that is renamed into place once
        it has the whole cut."""
        argvs = []
        out = os.path.join(self.tmp, "stream_tracks", "001 - A - B.flac")
        os.makedirs(os.path.dirname(out), exist_ok=True)   # main() makes the directory first

        def fake_run(argv, **kwargs):
            argvs.append(argv)
            with open(argv[-1], "wb") as fh:      # ffmpeg's output, simulated
                fh.write(b"fLaC")

        with unittest.mock.patch.object(extract_tracks.subprocess, "run", fake_run), \
                unittest.mock.patch.object(extract_tracks._audio, "find_audio_file",
                                          lambda stem: os.path.join(self.tmp, "capture.wav")):
            self.assertTrue(extract_tracks.cut("d000-018", 0.0, 30.0, 0.0, out))
        self.assertEqual(len(argvs), 1, "one ffmpeg invocation per cut")
        argv = argvs[0]
        self.assertEqual(argv[:1], ["ffmpeg"])
        self.assertTrue(argv[-1].endswith(".tmp"), "the in-flight write is the policy's mark")
        i = argv.index("-f")
        self.assertEqual(argv[i:i + 2], ["-f", "flac"], "the tmp's extension names no container")
        self.assertFalse(os.path.exists(argv[-1]), "the tmp is consumed by the rename")
        self.assertTrue(out.endswith(".flac") and os.path.isfile(out))

    def test_a_cut_inside_the_cache_reserves_and_commits(self):
        self.assertTrue(cache_budget.registered("stream_tracks"))
        out = os.path.join(self.tmp, "stream_tracks", "001 - A - B.flac")

        os.makedirs(os.path.dirname(out), exist_ok=True)

        def fake_run(argv, **kwargs):
            with open(argv[-1], "wb") as fh:
                fh.write(b"fLaC" * 1000)

        with unittest.mock.patch.object(extract_tracks.subprocess, "run", fake_run), \
                unittest.mock.patch.object(extract_tracks._audio, "find_audio_file",
                                          lambda stem: os.path.join(self.tmp, "capture.wav")):
            self.assertTrue(extract_tracks.cut("d000-018", 0.0, 30.0, 0.0, out))
        self.assertTrue(os.path.isfile(out))
        rows = cache_budget.status()["caches"]
        self.assertEqual([r["entries"] for r in rows if r["name"] == "stream_tracks"], [1],
                         "the cut is accounted as the cache's entry")
        path = cache_budget.events_path()
        with open(path) as fh:
            events = [json.loads(line) for line in fh]
        self.assertEqual([(e["event"], e["cache"], e["entry"]) for e in events
                          if e["event"] == "admit"],
                         [("admit", "stream_tracks", "001 - A - B.flac")])

    def test_a_cut_the_policy_refuses_room_for_is_not_made(self):
        cache_budget._disk_usage = lambda _p: (100 * self.KB, 50 * self.KB, 50 * self.KB)
        os.environ["NETRADIO_DISK_MAX_PCT"] = "0"      # past the floor: every reserve refuses
        extract_tracks.register_cache()
        out = os.path.join(self.tmp, "stream_tracks", "001 - A - B.flac")

        def boom(argv, **kwargs):
            self.fail("no ffmpeg must run for a refused cut")

        with unittest.mock.patch.object(extract_tracks.subprocess, "run", boom), \
                unittest.mock.patch.object(extract_tracks._audio, "find_audio_file",
                                          lambda stem: os.path.join(self.tmp, "capture.wav")):
            self.assertFalse(extract_tracks.cut("d000-018", 0.0, 30.0, 0.0, out))
        self.assertFalse(os.path.exists(out))

    def test_a_cut_outside_the_registered_cache_is_written_but_not_accounted(self):
        """--out naming another directory is the operator's own: the write works as before,
        and the policy is not asked to account a path outside its cache."""
        outside = os.path.join(self.tmp, "my-tracks")
        os.makedirs(outside)
        out = os.path.join(outside, "001 - A - B.flac")

        def fake_run(argv, **kwargs):
            with open(argv[-1], "wb") as fh:
                fh.write(b"fLaC")

        with unittest.mock.patch.object(extract_tracks.subprocess, "run", fake_run), \
                unittest.mock.patch.object(extract_tracks._audio, "find_audio_file",
                                          lambda stem: os.path.join(self.tmp, "capture.wav")):
            self.assertTrue(extract_tracks.cut("d000-018", 0.0, 30.0, 0.0, out))
        self.assertTrue(os.path.isfile(out))
        self.assertEqual(cache_budget.status()["caches"][0]["entries"], 0)

    def test_reassembly_parts_live_outside_the_cache_and_survive_a_full_one(self):
        """A multi-piece track's parts are written in a scratch directory OUTSIDE the cache,
        so the run that makes room for the assembled file evicts old ENTRIES, never the parts
        it is about to read. A concat that lost a part mid-flight would leave a partial track
        wearing the final name."""
        tracks_dir = os.path.join(self.tmp, "stream_tracks")
        os.makedirs(tracks_dir, exist_ok=True)
        old = []
        for name in ("001 - Old - One.flac", "001 - Old - Two.flac", "001 - Old - Three.flac"):
            path = os.path.join(tracks_dir, name)
            with open(path, "wb") as fh:
                fh.write(b"x" * 300 * self.KB)     # three old entries, 900 KB together
            old.append(path)
        os.environ["NETRADIO_STREAM_TRACKS_CACHE_GB"] = "0.0007"   # 700 KB: the cache is full
        extract_tracks.register_cache()
        out = os.path.join(tracks_dir, "002 - A - B.flac")
        parts_seen_by_concat = []

        def fake_cut(stem, m_from, m_to, cstart, out_path):
            self.assertNotEqual(os.path.dirname(out_path), tracks_dir,
                                "a part is scratch, written outside the cache")
            with open(out_path, "wb") as fh:
                fh.write(b"fLaC" * 75000)          # a 300 KB part
            return True

        def fake_concat(argv, **kwargs):
            lst = argv[argv.index("-i") + 1]
            with open(lst) as fh:
                names = [line.split("'")[1] for line in fh if line.startswith("file ")]
            parts_seen_by_concat.extend(names)
            for name in names:
                self.assertTrue(os.path.isfile(name),
                                "every part is still on disk when concat reads it")
            with open(argv[-1], "wb") as fh:
                fh.write(b"fLaC" * 150000)         # the assembled track, 600 KB
            return unittest.mock.Mock(returncode=0)

        pieces = [("d000-018", 0.0, 30.0), ("d001-026b", 30.0, 60.0)]
        with unittest.mock.patch.object(extract_tracks, "cut", fake_cut), \
                unittest.mock.patch.object(extract_tracks.subprocess, "run", fake_concat):
            self.assertTrue(extract_tracks.assemble_track(
                pieces, {"d000-018": 0.0, "d001-026b": 0.0}, out))
        self.assertTrue(os.path.isfile(out), "the assembled track landed")
        self.assertEqual(len(parts_seen_by_concat), 2)
        self.assertFalse([p for p in old if os.path.exists(p)],
                         "the old entries went, so the assembled track fits")
        self.assertEqual([n for n in os.listdir(tracks_dir) if n.startswith("001")], [],
                         "no part or list file is left in the cache")

    def test_a_cut_in_progress_is_never_an_entry_an_eviction_can_take(self):
        """The finding's race, reproduced: another writer's `reserve` runs while ffmpeg is
        mid-write. The in-flight cut sits under the policy's `.tmp` mark, so the run makes
        its room out of the old ENTRIES -- the half-written cut is held, and cut() cannot
        report success over a final path that is not there."""
        tracks_dir = os.path.join(self.tmp, "stream_tracks")
        os.makedirs(tracks_dir, exist_ok=True)
        old = os.path.join(tracks_dir, "001 - Old - One.flac")
        with open(old, "wb") as fh:
            fh.write(b"x" * 400 * self.KB)         # one entry, most of the cap
        os.environ["NETRADIO_STREAM_TRACKS_CACHE_GB"] = "0.0006"   # 600 KB: room must be made
        extract_tracks.register_cache()
        out = os.path.join(tracks_dir, "002 - A - B.flac")
        during = {}

        def fake_run(argv, **kwargs):
            tmp = argv[-1]
            self.assertTrue(tmp.endswith(".tmp"))
            with open(tmp, "wb") as fh:
                fh.write(b"fLaC" * 25000)            # half the cut is on disk
            during["reserve"] = cache_budget.reserve("stream_tracks", 250 * self.KB)
            self.assertTrue(os.path.exists(tmp),
                            "the in-flight write is held by the policy, never evicted")
            self.assertFalse(os.path.exists(out), "the final name does not exist yet")
            with open(tmp, "ab") as fh:
                fh.write(b"!" * 25000)              # ... and ffmpeg finishes
            return unittest.mock.Mock(returncode=0)

        with unittest.mock.patch.object(extract_tracks.subprocess, "run", fake_run), \
                unittest.mock.patch.object(extract_tracks._audio, "find_audio_file",
                                          lambda stem: os.path.join(self.tmp, "capture.wav")):
            self.assertTrue(extract_tracks.cut("d000-018", 0.0, 30.0, 0.0, out))
        self.assertEqual(during, {"reserve": True}, "the concurrent reserve found its room")
        self.assertFalse(os.path.exists(old), "out of the old entry, not the cut in flight")
        self.assertTrue(os.path.isfile(out), "the whole cut landed under the final name")
        self.assertFalse([n for n in os.listdir(tracks_dir) if n.endswith(".tmp")])

    def test_an_assembly_in_progress_is_never_an_entry_an_eviction_can_take(self):
        """The same race through the reassembly: the parts are scratch outside the cache, the
        assembled track is written under the `.tmp` mark, and a concurrent reserve mid-concat
        takes the old entries and holds the in-flight write."""
        tracks_dir = os.path.join(self.tmp, "stream_tracks")
        os.makedirs(tracks_dir, exist_ok=True)
        old = os.path.join(tracks_dir, "001 - Old - One.flac")
        with open(old, "wb") as fh:
            fh.write(b"x" * 400 * self.KB)
        os.environ["NETRADIO_STREAM_TRACKS_CACHE_GB"] = "0.0006"
        extract_tracks.register_cache()
        out = os.path.join(tracks_dir, "002 - A - B.flac")
        during = {}

        def fake_cut(stem, m_from, m_to, cstart, out_path):
            self.assertNotEqual(os.path.dirname(out_path), tracks_dir,
                                "the parts stay in scratch, outside the cache")
            with open(out_path, "wb") as fh:
                fh.write(b"fLaC" * 75000)
            return True

        def fake_concat(argv, **kwargs):
            tmp = argv[-1]
            self.assertTrue(tmp.endswith(".tmp"))
            with open(tmp, "wb") as fh:
                fh.write(b"fLaC" * 75000)           # half the assembled track
            during["reserve"] = cache_budget.reserve("stream_tracks", 250 * self.KB)
            self.assertTrue(os.path.exists(tmp))
            with open(tmp, "ab") as fh:
                fh.write(b"!" * 75000)
            return unittest.mock.Mock(returncode=0)

        pieces = [("d000-018", 0.0, 30.0), ("d001-026b", 30.0, 60.0)]
        with unittest.mock.patch.object(extract_tracks, "cut", fake_cut), \
                unittest.mock.patch.object(extract_tracks.subprocess, "run", fake_concat):
            self.assertTrue(extract_tracks.assemble_track(
                pieces, {"d000-018": 0.0, "d001-026b": 0.0}, out))
        self.assertEqual(during, {"reserve": True})
        self.assertFalse(os.path.exists(old), "the old entry made the room")
        self.assertTrue(os.path.isfile(out), "the assembled track landed whole")
        self.assertEqual(sorted(n for n in os.listdir(tracks_dir) if n.endswith(".flac")),
                         ["002 - A - B.flac"], "no part or tmp is left in the cache")

    def test_a_cut_evicted_between_its_rename_and_its_commit_reports_failure(self):
        """The reviewer's interleave: another writer's `reserve` runs after the rename and
        before the commit, and takes the just-published cut (it is not yet recorded, and
        carries no in-progress mark). The cut must report the failure, never a success over
        a path that is not there."""
        tracks_dir = os.path.join(self.tmp, "stream_tracks")
        os.makedirs(tracks_dir, exist_ok=True)
        out = os.path.join(tracks_dir, "001 - A - B.flac")
        os.environ["NETRADIO_STREAM_TRACKS_CACHE_GB"] = "0.0000001"   # 100 bytes: a cut overflows it
        extract_tracks.register_cache()
        real_replace = os.replace

        def racing_replace(a, b):
            real_replace(a, b)
            cache_budget.reserve("stream_tracks", None)   # the evictor, mid-landing

        def fake_run(argv, **kwargs):
            with open(argv[-1], "wb") as fh:
                fh.write(b"fLaC" * 250)                    # a 1000-byte cut
            return unittest.mock.Mock(returncode=0)

        with unittest.mock.patch.object(extract_tracks.subprocess, "run", fake_run), \
                unittest.mock.patch.object(extract_tracks._audio, "find_audio_file",
                                          lambda stem: os.path.join(self.tmp, "capture.wav")), \
                unittest.mock.patch("os.replace", side_effect=racing_replace):
            self.assertFalse(extract_tracks.cut("d000-018", 0.0, 30.0, 0.0, out),
                             "a landing that did not survive is a failed cut")
        self.assertFalse(os.path.exists(out))

    def test_an_assembly_evicted_between_its_rename_and_its_commit_reports_failure(self):
        """The same interleave through the reassembly: the assembled track is published under
        the final name and a concurrent reserve takes it before the commit; the assembly
        reports the failure and leaves nothing behind."""
        tracks_dir = os.path.join(self.tmp, "stream_tracks")
        os.makedirs(tracks_dir, exist_ok=True)
        out = os.path.join(tracks_dir, "002 - A - B.flac")
        os.environ["NETRADIO_STREAM_TRACKS_CACHE_GB"] = "0.0000001"
        extract_tracks.register_cache()
        real_replace = os.replace
        landed = {}

        def racing_replace(a, b):
            real_replace(a, b)
            if os.path.dirname(b) == tracks_dir:
                # the eviction the finding reproduces: between the rename and the commit
                cache_budget.reserve("stream_tracks", None)
                landed[b] = os.path.exists(b)

        def fake_cut(stem, m_from, m_to, cstart, out_path):
            with open(out_path, "wb") as fh:
                fh.write(b"fLaC" * 250)
            return True

        def fake_concat(argv, **kwargs):
            with open(argv[-1], "wb") as fh:
                fh.write(b"fLaC" * 250)
            return unittest.mock.Mock(returncode=0)

        pieces = [("d000-018", 0.0, 30.0), ("d001-026b", 30.0, 60.0)]
        with unittest.mock.patch.object(extract_tracks, "cut", fake_cut), \
                unittest.mock.patch.object(extract_tracks.subprocess, "run", fake_concat), \
                unittest.mock.patch("os.replace", side_effect=racing_replace):
            self.assertFalse(extract_tracks.assemble_track(
                pieces, {"d000-018": 0.0, "d001-026b": 0.0}, out))
        self.assertEqual(landed, {out: False}, "the published track was taken before the commit")
        self.assertFalse(os.path.exists(out))
        self.assertEqual(os.listdir(tracks_dir), [], "no part or tmp is left in the cache")

    def test_a_cut_that_dies_part_way_leaves_no_tmp_in_the_cache(self):
        """ffmpeg failing (a capture that will not decode) must not leave its `.tmp` inside
        the cache: there it counts against the cap and the policy holds it from eviction for
        an hour. The writer knows the write is over, so it clears it now -- and the final
        name was never created, so nothing half-cut wears it."""
        tracks_dir = os.path.join(self.tmp, "stream_tracks")
        os.makedirs(tracks_dir, exist_ok=True)
        out = os.path.join(tracks_dir, "001 - A - B.flac")

        def failing_ffmpeg(argv, **kwargs):
            with open(argv[-1], "wb") as fh:
                fh.write(b"fLaC" * 100)          # half a cut on disk...
            raise subprocess.CalledProcessError(1, argv)     # ...and ffmpeg gives up

        with unittest.mock.patch.object(extract_tracks.subprocess, "run", failing_ffmpeg), \
                unittest.mock.patch.object(extract_tracks._audio, "find_audio_file",
                                          lambda stem: os.path.join(self.tmp, "capture.wav")):
            with self.assertRaises(subprocess.CalledProcessError):
                extract_tracks.cut("d000-018", 0.0, 30.0, 0.0, out)
        self.assertEqual(os.listdir(tracks_dir), [],
                         "no half-written cut is left in the cache, under any name")

    def test_an_assembly_that_dies_part_way_leaves_no_tmp_in_the_cache(self):
        """The same through the reassembly: the concat fails, and neither the scratch parts
        nor the assembled file's `.tmp` is left behind."""
        tracks_dir = os.path.join(self.tmp, "stream_tracks")
        os.makedirs(tracks_dir, exist_ok=True)
        out = os.path.join(tracks_dir, "002 - A - B.flac")
        scratch = {}

        def fake_cut(stem, m_from, m_to, cstart, out_path):
            scratch["dir"] = os.path.dirname(out_path)
            with open(out_path, "wb") as fh:
                fh.write(b"fLaC" * 100)
            return True

        def failing_concat(argv, **kwargs):
            with open(argv[-1], "wb") as fh:
                fh.write(b"fLaC" * 100)
            raise subprocess.CalledProcessError(1, argv)

        pieces = [("d000-018", 0.0, 30.0), ("d001-026b", 30.0, 60.0)]
        with unittest.mock.patch.object(extract_tracks, "cut", fake_cut), \
                unittest.mock.patch.object(extract_tracks.subprocess, "run", failing_concat):
            with self.assertRaises(subprocess.CalledProcessError):
                extract_tracks.assemble_track(pieces, {"d000-018": 0.0, "d001-026b": 0.0}, out)
        self.assertEqual(os.listdir(tracks_dir), [],
                         "no half-assembled track is left in the cache")
        self.assertFalse(os.path.isdir(scratch["dir"]), "the scratch directory went too")

    def test_the_help_names_the_variable_the_code_reads(self):
        """The --out help is the operator-facing spelling of the default's override; a name
        that differs by one letter silently gets the default instead."""
        proc = subprocess.run([sys.executable,
                               os.path.join(SCRIPTS, "extract_tracks.py"), "--help"],
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("NETRADIO_STREAM_TRACKS_CACHE_DIR", proc.stdout)
        self.assertNotIn("NETRUDIO", proc.stdout)


if __name__ == "__main__":
    unittest.main()
