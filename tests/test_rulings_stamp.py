"""The rulings file's stamp (`scripts/rulings_stamp.py`) and the gate `harvest.py --run` makes.

The stamp, `.harvest/rulings-stamp.json`, is written by the process that writes the rulings
file: the SHA-256 of the exact bytes of `data/rulings.json`, the time and the writer. With
`NETRADIO_RULINGS_STAMP=require` a start is refused unless the stamp names the bytes the file
holds now; off (the default), nothing is read. Every test here uses a throwaway directory and
never reads or writes the committed rulings file or the checkout's `.harvest/`. The launcher's
half is in tests/test_run_harvester.py.
"""

import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)

import rulings_stamp  # noqa: E402

try:
    import harvest
except Exception:                           # librosa/numba not installed -> not this test's job
    harvest = None

RULES = b'{\n "u0123456789abcdef0123": "not_a_match"\n}\n'
ON = {"NETRADIO_RULINGS_STAMP": "require"}


class Files(unittest.TestCase):
    """A throwaway rulings file and stamp path."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rulings-stamp-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.rulings = os.path.join(self.tmp, "rulings.json")
        self.stamp = os.path.join(self.tmp, rulings_stamp.STAMP_NAME)
        with open(self.rulings, "wb") as fh:
            fh.write(RULES)

    def write_stamp(self, obj=None, raw=None):
        if raw is None:
            obj = obj if obj is not None else {
                "sha256": hashlib.sha256(RULES).hexdigest(),
                "written_at": "2026-10-27T03:00:00Z",
                "writer": {"pid": 1, "host": "h", "program": "p"}}
            raw = json.dumps(obj)
        with open(self.stamp, "w") as fh:
            fh.write(raw)

    def check(self, environ=ON):
        return rulings_stamp.check(self.rulings, self.stamp, environ)


class TheCheck(Files):

    def test_a_matching_stamp_passes(self):
        self.write_stamp()
        self.assertIsNone(self.check())

    def test_only_the_hash_is_read(self):
        """Not the age, not the pid: a writer that has restarted since still vouches."""
        self.write_stamp({"sha256": hashlib.sha256(RULES).hexdigest(),
                          "written_at": "1999-01-01T00:00:00Z",
                          "writer": {"pid": 999999999}})
        self.assertIsNone(self.check())

    def test_an_absent_stamp_refuses(self):
        why = self.check()
        self.assertIn("absent", why)
        self.assertIn(self.stamp, why)
        self.assertIn("write it again", why)

    def test_an_unreadable_stamp_refuses(self):
        for raw in ("{torn", "", "[1, 2]", '"abc"', json.dumps({"sha256": None}),
                    json.dumps({"sha256": "ABC"}),
                    json.dumps({"sha256": hashlib.sha256(RULES).hexdigest().upper()})):
            with self.subTest(raw=raw):
                self.write_stamp(raw=raw)
                why = self.check()
                self.assertIsNotNone(why)
                self.assertIn("unreadable", why)
                self.assertIn("write it again", why)

    def test_other_bytes_refuse(self):
        """A merge or a pull put another copy of the file in place: one byte is enough."""
        self.write_stamp()
        with open(self.rulings, "ab") as fh:
            fh.write(b" ")
        why = self.check()
        self.assertIn("has changed since its last write", why)
        self.assertIn(hashlib.sha256(RULES).hexdigest(), why)
        self.assertIn("write it again", why)

    def test_an_absent_rulings_file_refuses(self):
        self.write_stamp()
        os.unlink(self.rulings)
        self.assertIn("the rulings file (%s) is absent" % self.rulings, self.check())

    def test_off_reads_nothing(self):
        # No stamp at all, and still no refusal: a run with rulings kept by hand is not blocked.
        for environ in ({}, {"NETRADIO_RULINGS_STAMP": ""}, {"NETRADIO_RULINGS_STAMP": "off"},
                        {"NETRADIO_RULINGS_STAMP": " off "}):
            with self.subTest(environ=environ):
                self.assertIsNone(self.check(environ))

    def test_a_misspelt_setting_refuses(self):
        for value in ("requre", "on", "1", "Require"):
            with self.subTest(value=value):
                why = self.check({"NETRADIO_RULINGS_STAMP": value})
                self.assertIn("NETRADIO_RULINGS_STAMP", why)
                self.assertIn(repr(value), why)


class TheCommandLine(unittest.TestCase):
    """`rulings_stamp.py` alone: exit 0 when off, 2 and one line on stderr when refused."""

    def run_it(self, env):
        full = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        full.update(env)
        return subprocess.run([sys.executable, os.path.join(SCRIPTS, "rulings_stamp.py")],
                              capture_output=True, text=True, env=full, timeout=60)

    def test_off_exits_zero(self):
        out = self.run_it({})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stderr, "")
        self.assertEqual(out.stdout.count("\n"), 1)
        self.assertIn("off", out.stdout)

    def test_env_file_value_reads_the_launchers_way(self):
        tmp = tempfile.mkdtemp(prefix="rulings-env-")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "env")
        with open(path, "w") as fh:
            fh.write("# c\nOTHER=1\n  NETRADIO_RULINGS_STAMP = \"require\" \nNETRADIO_RULINGS_STAMP=off\n")
        self.assertEqual(rulings_stamp.env_file_value(path), "require")
        self.assertIsNone(rulings_stamp.env_file_value(os.path.join(tmp, "none")))

    def test_a_refusal_exits_two(self):
        out = self.run_it({"NETRADIO_RULINGS_STAMP": "no-such-mode"})
        self.assertEqual(out.returncode, rulings_stamp.EXIT_REFUSED)
        self.assertEqual(rulings_stamp.EXIT_REFUSED, 2)
        self.assertIn("refusing to start the harvester", out.stderr)


@unittest.skipIf(harvest is None, "harvest.py needs the librosa venv (.venv) — skipping")
class TheRunGate(Files):
    """`harvest.py --run` makes the same check at its start, so a hand start is covered too."""

    def setUp(self):
        super().setUp()
        names = ("STATE", "LEDGER", "WRITER_LOCK", "RULINGS", "STATE_DIR")
        saved = {n: getattr(harvest, n) for n in names}
        self.addCleanup(lambda: [setattr(harvest, n, v) for n, v in saved.items()])
        harvest.STATE = os.path.join(self.tmp, "state.json")
        harvest.LEDGER = os.path.join(self.tmp, "ledger.json")
        harvest.WRITER_LOCK = os.path.join(self.tmp, "writer.lock")
        harvest.RULINGS = self.rulings
        harvest.STATE_DIR = self.tmp          # the stamp is read from here

    def run_gate(self, environ, queries=lambda state=None: []):
        out = io.StringIO()
        with unittest.mock.patch.dict(os.environ, environ), \
             unittest.mock.patch.object(harvest, "queries", queries), \
             contextlib.redirect_stdout(out):
            code = harvest.run(None)
        return code, out.getvalue()

    def test_a_mismatch_refuses_with_exit_two(self):
        self.write_stamp({"sha256": "0" * 64})
        code, text = self.run_gate(ON, lambda state=None: (_ for _ in ()).throw(
            AssertionError("the refusal must come before the query set is read")))
        self.assertEqual(code, 2)
        self.assertIn("has changed since its last write", text)

    def test_an_absent_stamp_refuses_with_exit_two(self):
        code, text = self.run_gate(ON)
        self.assertEqual(code, 2)
        self.assertIn("absent", text)

    def test_a_match_lets_the_run_past_the_gate(self):
        self.write_stamp()
        code, text = self.run_gate(ON)
        self.assertNotIn("refusing to start", text)
        self.assertIn("nothing to search for", text, "the run went on to the next gates")

    def test_off_lets_the_run_past_with_no_stamp(self):
        code, text = self.run_gate({"NETRADIO_RULINGS_STAMP": "off"})
        self.assertNotIn("refusing to start", text)
        self.assertIn("nothing to search for", text)


if __name__ == "__main__":
    unittest.main()
