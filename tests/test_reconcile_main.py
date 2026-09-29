"""`reconcile_main` in scripts/tracklist_sync.sh, run for real against scratch repositories.

`make sync` fast-forwards a live checkout whose running processes keep rewriting some of its
files. `reconcile_main` sets aside exactly the files `git status` names under each listed path,
fast-forwards, and puts those files back on top of what the merge wrote. These tests pin what that
buys: the live bytes survive, and every file the live copy never touched keeps the merge's result.

The function is cut out of the script and sourced under macOS's /bin/bash 3.2 when it exists,
because that is the shell the script runs under there.
"""

import os
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SYNC_SH = os.path.join(ROOT, "scripts", "tracklist_sync.sh")
BASH = "/bin/bash" if os.path.exists("/bin/bash") else "bash"


def _git(cwd, *args):
    # a global pre-commit hook may block commits to `main`; these scratch repos seed it on purpose
    env = dict(os.environ, GIT_MAIN_COMMIT_OK="True")
    return subprocess.run(["git", "-C", cwd, "-c", "user.name=t", "-c", "user.email=t@example.com",
                           *args], check=True, capture_output=True, text=True, env=env).stdout.strip()


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class ReconcileMain(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="reconcile_")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def make_repo(self, files):
        origin = os.path.join(self.tmp, "origin.git")
        subprocess.run(["git", "init", "-q", "--bare", origin], check=True)
        subprocess.run(["git", "-C", origin, "symbolic-ref", "HEAD", "refs/heads/main"], check=True)
        self.pusher = os.path.join(self.tmp, "pusher")
        subprocess.run(["git", "clone", "-q", origin, self.pusher], check=True, capture_output=True)
        _git(self.pusher, "symbolic-ref", "HEAD", "refs/heads/main")
        self.advance(files, "seed")
        live = os.path.join(self.tmp, "live")
        subprocess.run(["git", "clone", "-q", origin, live], check=True, capture_output=True)
        return live

    def advance(self, files, msg="upstream"):
        for rel, text in files.items():
            _write(os.path.join(self.pusher, rel), text)
        _git(self.pusher, "add", "-A")
        _git(self.pusher, "commit", "-q", "-m", msg)
        _git(self.pusher, "push", "-q", "origin", "main")

    def reconcile(self, repo, *paths):
        m = re.search(r"^reconcile_main\(\) \{.*?^\}\n", _read(SYNC_SH), re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(m)
        fn = os.path.join(self.tmp, "reconcile_main.sh")
        _write(fn, m.group(0))
        body = ("set -euo pipefail; say() { printf '%s\\n' \"$*\"; }; DRY=false; BLOCKED=\"\"; "
                '. "$1"; shift; reconcile_main "$@"; printf "BLOCKED=[%s]\\n" "$BLOCKED"')
        r = subprocess.run([BASH, "-c", body, "bash", fn, repo, "live", *paths],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def at_origin(self, live):
        return _git(live, "rev-parse", "HEAD") == _git(live, "rev-parse", "origin/main")

    def status(self, live):
        return _git(live, "status", "--porcelain", "--untracked-files=all")

    def test_an_upstream_edit_to_a_clean_sibling_stands(self):
        live = self.make_repo({"labels/review/b.tsv": "b1\n", "code.txt": "one\n"})
        self.advance({"labels/review/b.tsv": "b2\n", "code.txt": "two\n"})
        _write(os.path.join(live, "labels/review/c.tsv"), "c-live\n")     # untracked
        out = self.reconcile(live, "labels/review")
        self.assertIn("BLOCKED=[]", out)
        self.assertTrue(self.at_origin(live))
        self.assertEqual(_read(os.path.join(live, "labels/review/b.tsv")), "b2\n")
        self.assertEqual(_read(os.path.join(live, "labels/review/c.tsv")), "c-live\n")
        self.assertEqual(self.status(live), "?? labels/review/c.tsv")
        self.assertFalse(os.path.exists(os.path.join(live, "labels/review.reconcile-bak")))

    def test_an_upstream_delete_of_a_clean_sibling_stays_deleted(self):
        live = self.make_repo({"labels/review/gone.tsv": "g\n", "labels/review/a.tsv": "a1\n",
                               "code.txt": "one\n"})
        os.remove(os.path.join(self.pusher, "labels/review/gone.tsv"))
        self.advance({"code.txt": "two\n"})
        _write(os.path.join(live, "labels/review/a.tsv"), "a1\nlive\n")
        out = self.reconcile(live, "labels/review")
        self.assertIn("BLOCKED=[]", out)
        self.assertFalse(os.path.exists(os.path.join(live, "labels/review/gone.tsv")))
        self.assertEqual(self.status(live), "M labels/review/a.tsv")

    def test_a_non_ascii_name_round_trips_and_an_added_one_arrives(self):
        live = self.make_repo({"labels/summary/café one.tsv": "v1\n", "code.txt": "one\n"})
        self.advance({"labels/summary/café one.tsv": "v2\n",
                      "labels/summary/naïve.tsv": "n1\n", "code.txt": "two\n"})
        _write(os.path.join(live, "labels/summary/café one.tsv"), "live\n")
        out = self.reconcile(live, "labels/summary")
        self.assertIn("BLOCKED=[]", out)
        self.assertEqual(_read(os.path.join(live, "labels/summary/café one.tsv")), "live\n")
        self.assertEqual(_read(os.path.join(live, "labels/summary/naïve.tsv")), "n1\n")

    def test_a_failed_fast_forward_restores_every_listed_path(self):
        live = self.make_repo({"data/rulings.json": "{}\n", "labels/review/a.tsv": "a1\n",
                               "code.txt": "one\n"})
        self.advance({"data/rulings.json": '{"k": 1}\n', "labels/review/b.tsv": "b1\n",
                      "code.txt": "two\n"})
        _write(os.path.join(live, "data/rulings.json"), '{"k": 1, "j": 2}\n')
        _write(os.path.join(live, "labels/review/a.tsv"), "a1\nlive\n")
        _write(os.path.join(live, "labels/review/b.tsv"), "b-live\n")      # untracked
        _write(os.path.join(live, "code.txt"), "locally edited\n")           # unlisted: blocks
        out = self.reconcile(live, "data/rulings.json", "labels/review")
        self.assertIn("fast-forward FAILED", out)
        self.assertFalse(self.at_origin(live))
        for rel, text in (("data/rulings.json", '{"k": 1, "j": 2}\n'),
                          ("labels/review/a.tsv", "a1\nlive\n"),
                          ("labels/review/b.tsv", "b-live\n")):
            self.assertEqual(_read(os.path.join(live, rel)), text, rel)
        for bak in ("data/rulings.json.reconcile-bak", "labels/review.reconcile-bak"):
            self.assertFalse(os.path.exists(os.path.join(live, bak)), bak)

    def test_a_backup_that_cannot_be_put_back_strands_nothing_else(self):
        live = self.make_repo({"labels/review/a.tsv": "a1\n", "labels/summary/s.tsv": "s1\n",
                               "code.txt": "one\n"})
        self.advance({"labels/review/sub": "now a file\n", "code.txt": "two\n"})
        _write(os.path.join(live, "labels/review/sub/x.tsv"), "x-live\n")   # untracked
        _write(os.path.join(live, "labels/summary/s.tsv"), "s-live\n")
        out = self.reconcile(live, "labels/review", "labels/summary")
        self.assertIn("could not put back labels/review/sub/x.tsv", out)
        self.assertIn("BLOCKED=[ live]", out)
        self.assertEqual(_read(os.path.join(live, "labels/summary/s.tsv")), "s-live\n")
        self.assertEqual(_read(os.path.join(live, "labels/review.reconcile-bak/sub/x.tsv")),
                         "x-live\n")

    def test_a_leftover_backup_is_never_overwritten(self):
        live = self.make_repo({"labels/review/a.tsv": "a1\n", "code.txt": "one\n"})
        self.advance({"code.txt": "two\n"})
        _write(os.path.join(live, "labels/review.reconcile-bak/a.tsv"), "stranded\n")
        _write(os.path.join(live, "labels/review/a.tsv"), "a1\nlive\n")
        out = self.reconcile(live, "labels/review")
        self.assertIn("BLOCKED=[ live]", out)
        self.assertFalse(self.at_origin(live))
        self.assertEqual(_read(os.path.join(live, "labels/review.reconcile-bak/a.tsv")),
                         "stranded\n")


if __name__ == "__main__":
    unittest.main()
