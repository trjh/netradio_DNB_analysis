"""scripts/full_suite_age.sh: the record of the last full-suite pass, and the OVERDUE line."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "full_suite_age.sh"


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True,
                          text=True).stdout.strip()


class FullSuiteAge(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = self._tmp.name
        git(self.repo, "init", "-q")
        git(self.repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid",
            "commit", "-q", "--allow-empty", "-m", "one")

    def run_age(self, *args, max_days=None):
        env = dict(os.environ)
        env.pop("FULL_SUITE_MAX_DAYS", None)
        if max_days is not None:
            env["FULL_SUITE_MAX_DAYS"] = str(max_days)
        out = subprocess.run(["bash", str(SCRIPT), *args], cwd=self.repo, env=env,
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout.strip()

    def test_no_record_is_overdue(self):
        self.assertTrue(self.run_age().startswith("OVERDUE"))

    def test_a_fresh_record_is_not_overdue_and_names_the_commit(self):
        self.run_age("--record")
        line = self.run_age()
        self.assertFalse(line.startswith("OVERDUE"), line)
        self.assertIn(git(self.repo, "rev-parse", "--short=7", "HEAD"), line)

    def test_a_record_older_than_the_limit_is_overdue(self):
        self.run_age("--record")
        self.assertTrue(self.run_age(max_days=0).startswith("OVERDUE"))

    def test_the_record_is_in_the_shared_git_directory(self):
        self.run_age("--record")
        self.assertTrue((Path(self.repo) / ".git" / "full-suite-run").is_file())
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")


if __name__ == "__main__":
    unittest.main()
