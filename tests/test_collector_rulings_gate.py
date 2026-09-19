"""The collector's two rulings gates, pinned in the required check's own dependency shape.

The split runtime's tests (tests/test_split.py) are soundfile-guarded -- the fold writes
real FLACs through it -- and the required CI check installs numpy and pyflakes only, so
there the whole module self-skips and the collector's two rulings gates (the fold loop's
and --once's, the cron entry point) were tested nowhere at all: a dropped gate would have
shipped green (local review, cycle netradio-build-2-2-20260919, iteration 5). The gates
need none of that: they are read-before-fold. This module drives both entry points to
their refusal with a real record sitting on the spool and the fold mocked to raise, so it
imports the collector with soundfile stubbed ONLY where the real one is absent, and only
for the import: the stub is an empty module, not a mock that would answer anything, so a
regression that reached the fold dies loudly on the first sf call instead of being
absorbed. With soundfile present -- a dev machine, or the check once the workflow installs
it -- nothing is stubbed and these run beside tests/test_split.py's own gate tests.

The stub, and the harvester and collector it let in, are dropped from sys.modules right
after the import: modules that skip on `import harvester` failing (tests/test_split.py's
guard, tests/test_harvest_child.py's) must keep failing that import honestly, or a stub
half a suite away would silently start standing in for the real thing.
"""

import contextlib
import io
import os
import sys
import tempfile
import types
import unittest
import unittest.mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))

try:
    import harvest
except Exception as exc:                    # numpy absent -> not this test's job
    harvest = None
    _why = str(exc)

if harvest is not None:
    try:
        import soundfile                    # noqa: F401  (present -> no stub, exactly as run)
        _stubbed = False
    except ImportError:                     # the required check's shape: numpy + pyflakes only
        _stubbed = True
        sys.modules["soundfile"] = types.ModuleType("soundfile")
    try:
        import harvester                    # noqa: E402  (submit_result: the spool's one writer)
        import collector                    # noqa: E402  (the two gates under test)
    finally:
        if _stubbed:                        # dropped, not left: see the module docstring
            for _name in ("soundfile", "harvester", "collector"):
                sys.modules.pop(_name, None)

URL = "https://youtu.be/gatevid0001"


@unittest.skipUnless(harvest, "harvest.py needs numpy (.venv, or CI's install) -- skipping")
class TheTwoGates(unittest.TestCase):
    """Both entry points refuse an unreadable rulings file before the fold: "cannot read"
    is never "nothing ruled", and a spooled result waits for a pass on a machine that still
    knows what the queue rejected."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        t = self.tmp.name
        # The cache-policy dance the split tests use: both gates sit behind
        # _caches_ready(), so both caches must be lit -- from a throwaway root, with the
        # registry put back exactly as it was found.
        saved = {k: os.environ.get(k) for k in list(os.environ)
                 if k.startswith("NETRADIO_") and ("CACHE" in k or
                                                   k in ("NETRADIO_CACHE_ROOT",
                                                         "NETRADIO_DOWNLOAD_ROOT",
                                                         "NETRADIO_DISK_MAX_PCT",
                                                         "NETRADIO_CACHE_EVENTS_DAYS"))}
        for k in saved:
            os.environ.pop(k, None)
        os.environ["NETRADIO_CACHE_ROOT"] = os.path.join(t, "policy-root")
        import cache_budget
        registry = dict(cache_budget._REGISTRY), dict(cache_budget._STATS)

        def restore_env_and_registry():
            cache_budget._REGISTRY.clear()
            cache_budget._REGISTRY.update(registry[0])
            cache_budget._STATS.clear()
            cache_budget._STATS.update(registry[1])
            for k, v in saved.items():
                os.environ[k] = v
        self.addCleanup(restore_env_and_registry)
        harvest.register_caches()           # re-read: both caches lit under the tmp root
        self.assertIsNotNone(harvest._keep_dir())
        # Every path the two entry points touch before their gate -- and, on a dropped
        # gate, the first thing after it -- is re-pointed into the throwaway directory.
        self._paths = (harvest.STATE_DIR, harvest.WRITER_LOCK, harvest.RULINGS,
                       harvest.STATE, harvest.QUEUE, harvest.CACHE, harvest.KEEP,
                       collector.STATE_DIR, collector.STATE, collector.QUEUE,
                       collector.JOBS, collector.RESULTS, collector.KEEP,
                       harvester.JOBS, harvester.RESULTS)
        harvest.STATE_DIR = os.path.join(t, "harvest")
        harvest.WRITER_LOCK = os.path.join(t, "writer.lock")
        harvest.RULINGS = os.path.join(t, "rulings.json")       # never written: the gates' case
        harvest.STATE = os.path.join(t, "h-state.json")
        harvest.QUEUE = os.path.join(t, "h-queue.json")
        harvest.CACHE = os.path.join(t, "cache")
        harvest.KEEP = os.path.join(t, "keep")
        collector.STATE_DIR = os.path.join(t, "harvest")
        collector.STATE = os.path.join(t, "state.json")
        collector.QUEUE = os.path.join(t, "queue.json")
        collector.JOBS = os.path.join(t, "jobs")
        collector.RESULTS = os.path.join(t, "results")
        collector.KEEP = os.path.join(t, "keep")
        harvester.JOBS = collector.JOBS                      # the spool's two names, one dir
        harvester.RESULTS = collector.RESULTS

        def restore_paths():
            (harvest.STATE_DIR, harvest.WRITER_LOCK, harvest.RULINGS,
             harvest.STATE, harvest.QUEUE, harvest.CACHE, harvest.KEEP,
             collector.STATE_DIR, collector.STATE, collector.QUEUE,
             collector.JOBS, collector.RESULTS, collector.KEEP,
             harvester.JOBS, harvester.RESULTS) = self._paths
        self.addCleanup(restore_paths)
        # A real record on the spool, written by the spool's own writer: an ok result the
        # fold would take, so the refusal has to come before the fold, not instead of it.
        harvester.submit_result(URL, ok=True)

    def test_the_loop_stands_down_without_folding(self):
        # A pass that cannot read the rulings file must propose NOTHING -- not even a
        # result already sitting on the spool -- and the fold is mocked to prove the
        # ordering: reached, it fails the test instead of scoring on amnesia.
        with unittest.mock.patch.object(
                    collector, "queries",
                    lambda state=None: (_ for _ in ()).throw(
                        AssertionError("the gate must come before the query set is read"))), \
                unittest.mock.patch.object(
                    collector, "collect_once",
                    lambda *a, **k: (_ for _ in ()).throw(
                        AssertionError("the fold must not run on an unreadable rulings "
                                       "file"))), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            collector.run()
        text = out.getvalue()
        self.assertIn("the rulings file", text)
        self.assertIn(harvest.RULINGS, text, "the refusal names the file, so a hand-run can "
                                             "tell WHICH file is missing")
        self.assertIn("standing down without folding", text)
        self.assertEqual(os.listdir(collector.RESULTS), [harvest._sig_key(URL) + ".json"],
                         "the spool keeps its record: nothing was folded, nothing was lost")
        self.assertFalse(os.path.exists(collector.STATE),
                         "the gate wrote no state of its own -- the stand-down is the print")

    def test_the_one_shot_pass_refuses_before_the_fold(self):
        # --once is the cron entry point, and it scores the spool too: the same gate, the
        # same ordering, at its own entry point.
        with unittest.mock.patch.dict(os.environ, {"NETRADIO_COLLECTOR": "on"}), \
                unittest.mock.patch.object(sys, "argv", ["collector.py", "--once"]), \
                unittest.mock.patch.object(
                    collector, "queries",
                    lambda state=None: (_ for _ in ()).throw(
                        AssertionError("the gate must come before the query set is read"))), \
                unittest.mock.patch.object(
                    collector, "collect_once",
                    lambda *a, **k: (_ for _ in ()).throw(
                        AssertionError("the fold must not run on an unreadable rulings "
                                       "file"))), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            collector.main()
        text = out.getvalue()
        self.assertIn("the rulings file", text)
        self.assertIn(harvest.RULINGS, text, "the refusal names the file, so a hand-run can "
                                             "tell WHICH file is missing")
        self.assertIn("not folding", text)
        self.assertEqual(os.listdir(collector.RESULTS), [harvest._sig_key(URL) + ".json"],
                         "the spool keeps its record: nothing was folded, nothing was lost")
        self.assertFalse(os.path.exists(collector.STATE),
                         "the gate wrote no state of its own -- the stand-down is the print")
