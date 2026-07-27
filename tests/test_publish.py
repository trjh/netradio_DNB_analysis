"""Tests for the A4 publish gate + branch/PR flow (labels/publish.py).

The hard gate is the safety-critical part: valid hand labels pass; bad-syntax,
unverified, not-COMPLETE, missing-anchor, and seed/engine files are refused with a
clear per-file message and a non-zero (zero-push) exit.

The branch/PR machinery is exercised two ways with NO network and NO real push:
  * --dry-run (prints the plan, touches nothing); and
  * a real throwaway git repo whose `_run` seam runs the LOCAL git ops for real
    (worktree add / add / commit) but no-ops the network + cleanup ops (fetch /
    push / gh / worktree-remove / branch-delete). That lets us assert the commit
    lands on the new branch ref and the invoking checkout is left untouched.
"""

import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

LABELS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "labels")
sys.path.insert(0, LABELS_DIR)

import publish  # noqa: E402


COMPLETE_FILE = [
    "0.000000\t0.000000\tfile start sync: d999-000.wav 100.000000 verified d998-999",
    "10.000000\t10.000000\torig069 sync: 0",
    "20.000000\t25.000000\tfile end: d999-000.wav COMPLETE",
]


def write(dir_, stem, lines):
    path = os.path.join(dir_, stem + ".labels.tsv")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    return path


class GateTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_complete_verified_file_passes(self):
        path = write(self.dir, "d999-000", COMPLETE_FILE)
        self.assertEqual(publish.gate_errors(path), [])

    def test_unverified_sync_is_blocked(self):
        lines = list(COMPLETE_FILE)
        lines[0] = "0.000000\t0.000000\tfile start sync: d999-000.wav 100.000000"  # no `verified`
        path = write(self.dir, "d999-000", lines)
        errs = publish.gate_errors(path)
        self.assertTrue(any("verified" in e for e in errs), errs)

    def test_missing_complete_is_blocked(self):
        lines = list(COMPLETE_FILE)
        lines[2] = "20.000000\t25.000000\tfile end: d999-000.wav"  # no COMPLETE
        path = write(self.dir, "d999-000", lines)
        errs = publish.gate_errors(path)
        self.assertTrue(errs)
        self.assertTrue(any("COMPLETE" in e for e in errs), errs)

    def test_no_file_end_anchor_is_blocked(self):
        path = write(self.dir, "d999-000", COMPLETE_FILE[:2])  # drop the file end row
        errs = publish.gate_errors(path)
        self.assertTrue(any("not finished" in e for e in errs), errs)

    def test_bad_syntax_row_is_blocked(self):
        lines = list(COMPLETE_FILE) + ["30.000000\t30.000000\tthis is not valid grammar"]
        path = write(self.dir, "d999-000", lines)
        errs = publish.gate_errors(path)
        self.assertTrue(any("WARNING" in e or "Unrecognized" in e for e in errs), errs)

    def test_starter_and_auto_files_refused(self):
        sp = os.path.join(self.dir, "d999-000.starter.labels.tsv")
        ap = os.path.join(self.dir, "d999-000.auto.labels.tsv")
        for p, lines in ((sp, COMPLETE_FILE), (ap, COMPLETE_FILE)):
            with open(p, "w", encoding="utf-8") as h:
                h.write("\n".join(lines) + "\n")
        self.assertTrue(any("seed-only" in e for e in publish.gate_errors(sp)))
        self.assertTrue(any("never hand-published" in e for e in publish.gate_errors(ap)))


class AllOrNothingTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_one_bad_file_blocks_the_whole_publish(self):
        good = write(self.dir, "d999-000", COMPLETE_FILE)
        bad = write(self.dir, "d999-001", COMPLETE_FILE[:2])  # missing file end
        rc = publish.publish([good, bad], "msg", dry_run=True)
        self.assertEqual(rc, 1)  # zero-push exit because one target failed

    def test_all_valid_dry_run_succeeds_without_pushing(self):
        good = write(self.dir, "d999-000", COMPLETE_FILE)
        rc = publish.publish([good], "msg", dry_run=True, refresh=False)
        self.assertEqual(rc, 0)


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_stem_maps_to_labels_tsv_when_no_txt(self):
        p = publish.resolve_target("d336-355", "/x/labels")
        self.assertEqual(p, os.path.join("/x/labels", "d336-355.labels.tsv"))

    def test_stem_prefers_fresh_txt_export(self):
        # the normal post-Audacity-export case: only <stem>.labels.txt exists
        write(self.dir, "d336-355", COMPLETE_FILE)
        txt = os.path.join(self.dir, "d336-355.labels.tsv")
        os.rename(txt, txt[:-3] + "txt")  # -> d336-355.labels.txt
        self.assertEqual(publish.resolve_target("d336-355", self.dir),
                         os.path.join(self.dir, "d336-355.labels.txt"))

    def test_published_path_maps_txt_to_tsv(self):
        self.assertEqual(publish.published_path("/a/d999-000.labels.txt"),
                         "/a/d999-000.labels.tsv")
        self.assertEqual(publish.published_path("/a/d999-000.labels.tsv"),
                         "/a/d999-000.labels.tsv")

    def test_explicit_txt_dry_run_stages_the_tsv(self):
        import io
        from contextlib import redirect_stdout
        path = write(self.dir, "d999-000", COMPLETE_FILE)
        txt = path[:-3] + "txt"
        os.rename(path, txt)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = publish.publish([txt], "msg", dry_run=True, refresh=False)
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("git add", out)
        self.assertIn("d999-000.labels.tsv", out)        # stages the converted name
        self.assertNotIn("git add %s" % txt, out)        # not the vanished .txt


class BranchNameTests(unittest.TestCase):
    def test_branch_name_shape(self):
        import datetime
        when = datetime.datetime(2026, 7, 27, 9, 8, 7, tzinfo=datetime.timezone.utc)
        self.assertEqual(publish._branch_name(when), "labels/publish-20260727-090807")

    def test_branch_name_is_utc_and_defaulted(self):
        name = publish._branch_name()
        self.assertRegex(name, r"^labels/publish-\d{8}-\d{6}$")


# A `_run` seam that runs the LOCAL git ops for real (so refs actually move) but no-ops the
# network + cleanup ops (so nothing leaves the machine and the branch/worktree survive for
# assertions). Records every command it is handed.
_LOCAL_REAL = (("git", "worktree", "add"), ("git", "add"), ("git", "commit"))
_NOOP_PREFIX = (("git", "fetch"), ("git", "push"), ("git", "worktree", "remove"),
                ("git", "worktree", "prune"), ("git", "branch"))


class _SeamRun:
    def __init__(self, gh_rc=0, gh_present=True):
        self.calls = []
        self.gh_rc = gh_rc
        self.gh_present = gh_present

    def __call__(self, cmd, dry_run, cwd=None):
        self.calls.append(list(cmd))
        if cmd[0] == "gh":
            return self.gh_rc
        for pref in _NOOP_PREFIX:
            if tuple(cmd[: len(pref)]) == pref:
                return 0
        for pref in _LOCAL_REAL:
            if tuple(cmd[: len(pref)]) == pref:
                return subprocess.run(cmd, cwd=cwd, check=False).returncode
        # sort_tsv (and anything else) runs for real too
        return subprocess.run(cmd, cwd=cwd, check=False).returncode

    def branch_arg(self):
        for cmd in self.calls:
            if cmd[:3] == ["git", "worktree", "add"]:
                return cmd[cmd.index("-B") + 1]
        return None


def _git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, text=True, check=False).stdout.strip()


class BranchPRFlowTests(unittest.TestCase):
    """Real-git tests: commit lands on the new branch, invoking checkout untouched."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.origin = os.path.join(self.tmp, "origin.git")
        self.repo = os.path.join(self.tmp, "work")
        subprocess.run(["git", "init", "--bare", "-b", "main", self.origin], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["git", "init", "-b", "main", self.repo], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for k, v in (("user.email", "t@e.st"), ("user.name", "Test"),
                     ("commit.gpgsign", "false")):
            _git(self.repo, "config", k, v)
        os.makedirs(os.path.join(self.repo, "labels"))
        # seed origin/main with an empty commit so the worktree has a base to cut from
        _git(self.repo, "commit", "--allow-empty", "-m", "root")
        _git(self.repo, "remote", "add", "origin", self.origin)
        _git(self.repo, "push", "-u", "origin", "main")
        _git(self.repo, "fetch", "origin", "main")
        # present a GitHub-looking origin so the compare-URL fallback can build a slug (the
        # origin/main tracking ref is already fetched; the seam no-ops any push/fetch to it)
        _git(self.repo, "remote", "set-url", "origin",
             "https://github.com/trjh/netradio_DNB_analysis.git")
        # the invoking checkout sits on a labelling branch, NOT main
        _git(self.repo, "checkout", "-q", "-b", "work")
        self.label = write(os.path.join(self.repo, "labels"), "d999-000", COMPLETE_FILE)
        self._orig_run = publish._run

    def tearDown(self):
        publish._run = self._orig_run

    def _publish(self, **kw):
        seam = _SeamRun(**kw)
        publish._run = seam
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = publish.publish([self.label], "labels: publish d999-000",
                                 branch="labels/publish-20260727-000000", refresh=False)
        return rc, seam, buf.getvalue()

    def test_commit_lands_on_new_branch_not_invoking_branch(self):
        rc, seam, _ = self._publish()
        self.assertEqual(rc, 0)
        branch = seam.branch_arg()
        self.assertEqual(branch, "labels/publish-20260727-000000")
        # the label file IS in the branch's tree...
        self.assertIn("labels/d999-000.labels.tsv",
                      _git(self.repo, "ls-tree", "-r", "--name-only", branch))
        # ...and NOT reachable from the invoking branch `work`
        contains = _git(self.repo, "branch", "--contains", _git(self.repo, "rev-parse", branch))
        self.assertNotIn("work", contains)

    def test_invoking_checkout_untouched(self):
        head_before = _git(self.repo, "rev-parse", "HEAD")
        rc, _, _ = self._publish()
        self.assertEqual(rc, 0)
        # current branch unchanged, HEAD unchanged, index clean (no staged label)
        self.assertEqual(_git(self.repo, "rev-parse", "--abbrev-ref", "HEAD"), "work")
        self.assertEqual(_git(self.repo, "rev-parse", "HEAD"), head_before)
        self.assertEqual(_git(self.repo, "diff", "--cached", "--name-only"), "")

    def test_push_is_the_branch_never_main(self):
        _, seam, _ = self._publish()
        pushes = [c for c in seam.calls if c[:2] == ["git", "push"]]
        self.assertTrue(pushes)
        for c in pushes:
            self.assertIn("labels/publish-20260727-000000", c)
            self.assertNotIn("main", c)

    def test_gh_success_opens_pr_no_fallback(self):
        _, seam, out = self._publish(gh_rc=0)
        self.assertTrue(any(c[:2] == ["gh", "pr"] for c in seam.calls))
        self.assertNotIn("open a PR by hand", out)

    def test_gh_absent_prints_compare_url_and_still_succeeds(self):
        orig_which = publish.shutil.which
        publish.shutil.which = lambda name: None if name == "gh" else orig_which(name)
        try:
            rc, seam, out = self._publish()
        finally:
            publish.shutil.which = orig_which
        self.assertEqual(rc, 0)  # branch pushed; only the PR wrapper is missing
        self.assertFalse(any(c[:1] == ["gh"] for c in seam.calls))
        self.assertIn("compare/main...labels/publish-20260727-000000", out)

    def test_validation_failure_does_no_git_at_all(self):
        bad = write(os.path.join(self.repo, "labels"), "d999-001", COMPLETE_FILE[:2])
        seam = _SeamRun()
        publish._run = seam
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = publish.publish([self.label, bad], "msg", refresh=False)
        self.assertEqual(rc, 1)
        self.assertEqual(seam.calls, [])  # no sort, no worktree, no commit, no push, no PR


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self._orig_refresh = publish.trigger_refresh
        self._orig_run = publish._run
        self.refresh_calls = []
        publish.trigger_refresh = lambda dry_run=False: self.refresh_calls.append(dry_run)

    def tearDown(self):
        publish.trigger_refresh = self._orig_refresh
        publish._run = self._orig_run

    def test_publish_defers_refresh_and_prints_reminder(self):
        good = write(self.dir, "d999-000", COMPLETE_FILE)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = publish.publish([good], "msg", dry_run=True, refresh=True)
        self.assertEqual(rc, 0)
        self.assertEqual(self.refresh_calls, [])       # NOT fired at publish time
        self.assertIn("--refresh-only", buf.getvalue())  # reminder printed

    def test_refresh_only_flag_fires_the_refresh(self):
        rc = publish.main(["--refresh-only", "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.refresh_calls, [True])   # fired exactly once

    def test_refresh_only_rejects_targets(self):
        with self.assertRaises(SystemExit):
            publish.main(["--refresh-only", "d999-000"])


if __name__ == "__main__":
    unittest.main()
