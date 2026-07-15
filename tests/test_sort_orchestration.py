"""The one-command loop: sort_tsv finishes this file and preps the next.

Covers the pieces that turned five hand-run commands into one -- the mis-named-output warning,
`.env_vars` loading, and (the load-bearing one) the non-interactive guard that stops publish.py
from hanging on the next-file prompt when it runs sort_tsv as a subprocess with no terminal.
"""

import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr

LABELS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "labels")
sys.path.insert(0, LABELS)

import sort_tsv  # noqa: E402

SORT_TSV = os.path.join(LABELS, "sort_tsv.py")

# A complete file that also links a successor (the `file_d902-903:` row), so prep_next reaches
# the TTY guard rather than short-circuiting on "no successor found".
COMPLETE = (
    "0.000000\t0.000000\tfile start sync: d900-901.wav 0.0 verified by x\n"
    "8.000000\t8.000000\tfile_d902-903: file start sync: d902-903.wav 0.0 verified\n"
    "10.000000\t10.000000\tfile end: d900-901.wav COMPLETE\n"
)


def _stderr_of(fn, *a):
    buf = io.StringIO()
    with redirect_stderr(buf):
        fn(*a)
    return buf.getvalue()


class OutputNameWarning(unittest.TestCase):
    def test_bare_tsv_is_flagged(self):
        out = _stderr_of(sort_tsv.check_output_name, "/x/d900-901.tsv")
        self.assertIn("invisible", out)
        self.assertIn("d900-901.labels.tsv", out)

    def test_pipeline_names_are_silent(self):
        self.assertEqual(_stderr_of(sort_tsv.check_output_name, "/x/d900-901.labels.tsv"), "")
        self.assertEqual(_stderr_of(sort_tsv.check_output_name, "/x/d900-901.auto.labels.tsv"), "")
        self.assertEqual(_stderr_of(sort_tsv.check_output_name, None), "")  # stdout


class EnvVarsLoading(unittest.TestCase):
    def test_reads_values_without_clobbering_real_env(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, ".env_vars"), "w") as fh:
            fh.write("# machine paths\nNETRADIO_SOURCES_DIR=/tmp/originals\n"
                     "NETRADIO_ALREADY_SET=from_file\n\nBAD LINE NO EQUALS\n")
        os.environ.pop("NETRADIO_SOURCES_DIR", None)
        os.environ["NETRADIO_ALREADY_SET"] = "from_env"
        try:
            sort_tsv.load_env_vars(os.path.join(d, ".env_vars"))
            self.assertEqual(os.environ["NETRADIO_SOURCES_DIR"], "/tmp/originals")
            self.assertEqual(os.environ["NETRADIO_ALREADY_SET"], "from_env")  # real env wins
        finally:
            os.environ.pop("NETRADIO_SOURCES_DIR", None)
            os.environ.pop("NETRADIO_ALREADY_SET", None)

    def test_missing_file_is_not_an_error(self):
        sort_tsv.load_env_vars("/no/such/.env_vars")  # must not raise


class TheHangGuard(unittest.TestCase):
    """publish.py runs `sort_tsv.py <file>` as a subprocess with no terminal. If prep_next
    ever blocks on input() there, publish hangs forever. Drive the real binary with a
    non-TTY stdin and prove it returns promptly."""

    def _run(self, extra):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "d900-901.labels.txt")
        with open(path, "w") as fh:
            fh.write(COMPLETE)
        # stdin=DEVNULL => not a tty; timeout => a hang becomes a test failure, not a wedge
        return subprocess.run([sys.executable, SORT_TSV, path] + extra,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=60)

    def test_non_interactive_skips_the_prompt_and_returns(self):
        proc = self._run(["--no-starter"])
        self.assertEqual(proc.returncode, 0)
        self.assertIn("not a terminal", proc.stderr)
        self.assertIn("streamalign hints", proc.stderr)  # prints the command it skipped

    def test_no_next_suppresses_prep_entirely(self):
        proc = self._run(["--no-starter", "--no-next"])
        self.assertEqual(proc.returncode, 0)
        self.assertNotIn("Next file", proc.stderr)


if __name__ == "__main__":
    unittest.main()
