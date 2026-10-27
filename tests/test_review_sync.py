"""DP-22 §6.7: commit + push the review notes to this repository's standing branch.

The §6.7 invariants pinned here:
  * a plumbing commit contains EXACTLY `labels/review/*.review.tsv` (+ the summary pattern, and
    the one exact file `data/rulings.json` — nothing else in data/) and the correct parent chain
    (first parent = main; subsequent = the previous branch commit);
  * the checkout's live working tree, index, and branch are NEVER touched;
  * strays in labels/review/ are never swept in, and the pathspec PHYSICALLY cannot reach
    the authoritative labels/*.labels.tsv root files
    (a changed label file never rides a review commit — invariant 9);
  * no change -> no commit;
  * an offline push fails SOFT (recorded, never raises, never forces) and retries once online;
  * post-merge recreate fails CLOSED unless the PR is verifiably MERGED (open, absent,
    gh-failure, rejected remote delete, failed fetch all refuse with both refs intact);
  * dark unless the checkout is the ROOT of a git work tree and not a linked worktree;
    NETRADIO_REVIEW_SYNC=0 is the kill switch; with no override the checkout is the one the
    module sits in;
  * `make review-sync-automerge ARGS=--dry-run` runs and merges nothing.

Fully offline + deterministic: each test builds its own tmp "checkout" git repo + a LOCAL bare
origin and points review_sync at it via NETRADIO_REVIEW_SYNC_REPO. gh is never invoked for
real — the `_run_gh` seam is faked where a PR/merge state is needed.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import review_sync


def _git(repo, *args, check=True):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, check=check)


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _tree_files(repo, ref):
    r = _git(repo, "ls-tree", "-r", "--name-only", ref)
    return set(x for x in r.stdout.splitlines() if x)


class ReviewSyncBase(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        self._run_git = review_sync._run_git
        self._run_gh = review_sync._run_gh
        review_sync._last_error = {}
        review_sync._logged = {}
        self._echo = review_sync.ECHO
        review_sync.ECHO = False             # quiet; TestFailuresReachTheLog turns it on
        review_sync._last_ok = {}
        review_sync._last_commit = None
        review_sync._last_push = 0.0
        review_sync._last_commit_pass = None

        self.root = tempfile.mkdtemp(prefix="rsync_")
        self.origin = os.path.join(self.root, "origin.git")
        self.work = os.path.join(self.root, "checkout")
        subprocess.run(["git", "init", "-q", "--bare", self.origin], check=True)
        subprocess.run(["git", "init", "-q", "-b", "main", self.work], check=True)
        _git(self.work, "config", "user.email", "t@example.com")
        _git(self.work, "config", "user.name", "T")
        # main starts with a LABEL file and no labels/review/ at all, so a first
        # commit_once ADDS review files — and the label is the invariant-9 canary
        _write(os.path.join(self.work, "README.md"), "checkout\n")
        _write(os.path.join(self.work, "labels", "d376-395.labels.tsv"), "0\t0\tfile start\n")
        _git(self.work, "add", "-A")
        _git(self.work, "commit", "-q", "-m", "init main")
        _git(self.work, "remote", "add", "origin", self.origin)
        _git(self.work, "push", "-q", "origin", "main")

        os.environ["NETRADIO_REVIEW_SYNC_REPO"] = self.work
        os.environ["NETRADIO_REVIEW_SYNC"] = "1"
        os.environ["GIT_MAIN_COMMIT_OK"] = "True"    # a global hook blocks main commits; tests seed main
        os.environ.pop("NETRADIO_REVIEW_SYNC_BRANCH", None)
        os.environ.pop("NETRADIO_REVIEW_SYNC_BASE", None)
        os.environ.pop("NETRADIO_REVIEW_SYNC_REMOTE", None)

    def tearDown(self):
        review_sync.ECHO = self._echo
        review_sync._run_git = self._run_git
        review_sync._run_gh = self._run_gh
        os.environ.clear()
        os.environ.update(self._env)
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)

    # --- helpers -------------------------------------------------------------------------------
    def _seed_review(self, name="d376-395.orig072.review.tsv",
                     text="0.000000\t0.000000\tnote REVIEW session s1: assumed-rate "
                          "rate 1.000000 from 1 pair 2026-08-06\n"):
        _write(os.path.join(self.work, "labels", "review", name), text)


class TestCommitPlumbing(ReviewSyncBase):
    def test_commit_contains_exactly_pathspec_and_excludes_strays(self):
        self._seed_review()
        self._seed_review("d000-018.orig010.review.tsv")
        # things that must NEVER be committed, sharing the labels/review/ directory:
        _write(os.path.join(self.work, "labels", "review", "random-stray.json"), "{}")
        _write(os.path.join(self.work, "labels", "review", "scratch.txt"), "stray\n")

        sha = review_sync.commit_once()
        self.assertTrue(sha and len(sha) == 40, "expected a commit sha, got %r" % sha)

        added = _tree_files(self.work, "align-review-data") - _tree_files(self.work, "main")
        self.assertEqual(added, {
            "labels/review/d376-395.orig072.review.tsv",
            "labels/review/d000-018.orig010.review.tsv",
        })
        allfiles = _tree_files(self.work, "align-review-data")
        self.assertNotIn("labels/review/random-stray.json", allfiles)
        self.assertNotIn("labels/review/scratch.txt", allfiles)

    def test_pathspec_physically_cannot_reach_labels(self):
        # invariant 9: every pattern is rooted under one of the two DP-24 subdirs, or is the
        # one exact rulings file (no wildcard, not under labels/) ...
        for p in review_sync.PATHSPEC:
            self.assertTrue(p.startswith("labels/review/")
                            or p.startswith("labels/summary/")
                            or p == "data/rulings.json", p)
            self.assertNotIn("..", p)
        # ... never a bare directory, and never a pattern that could match the
        # authoritative root files (labels/*.labels.tsv)
        self.assertNotIn("labels/review/", review_sync.PATHSPEC)
        self.assertNotIn("labels/summary/", review_sync.PATHSPEC)
        import fnmatch
        for p in review_sync.PATHSPEC:
            self.assertFalse(fnmatch.fnmatch("labels/d376-395.labels.tsv", p), p)
        # ... and a changed/added LABEL file never rides a review commit
        self._seed_review()
        _write(os.path.join(self.work, "labels", "d376-395.labels.tsv"),
               "0\t0\tfile start EDITED\n")
        _write(os.path.join(self.work, "labels", "new-capture.labels.tsv"), "0\t0\tx\n")
        review_sync.commit_once()
        def _hand_labels(ref):
            # the AUTHORITATIVE files: under labels/ but NOT in the two DP-24
            # subdirs review_sync owns (labels/review/, labels/summary/)
            return {f for f in _tree_files(self.work, ref)
                    if f.startswith("labels/")
                    and not f.startswith(("labels/review/", "labels/summary/"))}
        self.assertEqual(_hand_labels("align-review-data"), _hand_labels("main"),
                         "hand labels must carry over from main UNTOUCHED — "
                         "never staged by review_sync")
        r = _git(self.work, "diff", "main", "align-review-data", "--",
                 "labels/", ":(exclude)labels/review/", ":(exclude)labels/summary/")
        self.assertEqual(r.stdout.strip(), "", "no hand-label content may differ from main")

    def test_rulings_file_is_committed_and_nothing_else_in_data(self):
        # main ships data/rulings.json (initially `{}`) beside other data files
        _write(os.path.join(self.work, "data", "rulings.json"), "{}")
        _write(os.path.join(self.work, "data", "track-sources.csv"), "a,b\n")
        _git(self.work, "add", "-A")
        _git(self.work, "commit", "-q", "-m", "ship the rulings file")
        # its writer rewrites it in place; the atomic-write tmp, an edit to a tracked sibling
        # and a new untracked file all sit in data/ at the same moment
        _write(os.path.join(self.work, "data", "rulings.json"), '{\n "u0123": "listened"\n}')
        _write(os.path.join(self.work, "data", "rulings.json.4242.tmp"), '{"partial')
        _write(os.path.join(self.work, "data", "track-sources.csv"), "a,b\nEDITED\n")
        _write(os.path.join(self.work, "data", "new.json"), "{}")

        sha = review_sync.commit_once()
        self.assertTrue(sha and len(sha) == 40, "expected a commit sha, got %r" % sha)
        changed = _git(self.work, "diff", "--name-only", "main", "align-review-data").stdout.split()
        self.assertEqual(changed, ["data/rulings.json"])
        self.assertEqual(_git(self.work, "show", "align-review-data:data/rulings.json").stdout,
                         '{\n "u0123": "listened"\n}')
        # the hand labels still ride over from main untouched
        self.assertEqual(_git(self.work, "diff", "main", "align-review-data", "--",
                              "labels/").stdout, "")

    def test_a_missing_rulings_file_is_never_committed_as_a_deletion(self):
        # an independent review's finding: a deleted-on-disk rulings file must not delete it
        # on the branch (and so, once merged, on main)
        path = os.path.join(self.work, "data", "rulings.json")
        _write(path, "{}")
        _git(self.work, "add", "-A")
        _git(self.work, "commit", "-q", "-m", "ship the rulings file")
        _write(path, '{\n "u0123": "listened"\n}')
        self.assertTrue(review_sync.commit_once())       # the branch now carries the new bytes
        os.unlink(path)
        self._seed_review()                               # something else to commit
        self.assertTrue(review_sync.commit_once())
        self.assertEqual(_git(self.work, "show", "align-review-data:data/rulings.json").stdout,
                         '{\n "u0123": "listened"\n}')
        # with nothing else changed, the missing file alone makes no commit at all
        self.assertIsNone(review_sync.commit_once())

    def test_parent_chain(self):
        self._seed_review()
        c1 = review_sync.commit_once()
        main_sha = _git(self.work, "rev-parse", "main").stdout.strip()
        p1 = _git(self.work, "rev-parse", "align-review-data^").stdout.strip()
        self.assertEqual(p1, main_sha, "first commit's parent must be main")

        self._seed_review(text="0.000000\t0.000000\tnote REVIEW session s1: assumed-rate "
                               "rate 1.000000 from 1 pair 2026-08-06\n"
                               "5.000000\t5.000000\tnote REVIEW s1: orig072 1.000000 | x\n")
        c2 = review_sync.commit_once()
        self.assertNotEqual(c1, c2)
        p2 = _git(self.work, "rev-parse", "align-review-data^").stdout.strip()
        self.assertEqual(p2, c1, "second commit must chain onto the first")

    def test_checkout_working_tree_and_index_untouched(self):
        # a dirty, uncommitted edit to a NON-review file — live work in progress
        _write(os.path.join(self.work, "README.md"), "locally edited, uncommitted\n")
        self._seed_review()
        before_status = _git(self.work, "status", "--porcelain").stdout
        before_head = _git(self.work, "symbolic-ref", "--short", "HEAD").stdout.strip()

        review_sync.commit_once()

        self.assertEqual(_git(self.work, "symbolic-ref", "--short", "HEAD").stdout.strip(),
                         "main")
        self.assertEqual(before_head, "main")
        self.assertEqual(_git(self.work, "status", "--porcelain").stdout, before_status)
        self.assertEqual(
            _git(self.work, "rev-parse", "--verify", "align-review-data",
                 check=False).returncode, 0)

    def test_no_change_no_commit(self):
        self._seed_review()
        c1 = review_sync.commit_once()
        self.assertIsNotNone(c1)
        c2 = review_sync.commit_once()               # nothing changed on disk
        self.assertIsNone(c2)
        tip = _git(self.work, "rev-parse", "align-review-data").stdout.strip()
        self.assertEqual(tip, c1, "branch tip must not move when nothing changed")
        self.assertNotIn("commit", review_sync._last_error)

    def test_no_review_dir_yet_is_quietly_nothing(self):
        # a fresh checkout with no labels/review/ at all: the unmatched pathspec must not
        # make commit_once fatal (git add fatals on an unmatched pathspec)
        sha = review_sync.commit_once()
        self.assertIsNone(sha)
        self.assertNotIn("commit", review_sync._last_error)


class TestFailuresReachTheLog(ReviewSyncBase):
    """A commit failure is printed once per distinct message, stays in status() until the
    next success, and the success prints that it cleared."""

    def setUp(self):
        super().setUp()
        review_sync.ECHO = True

    def _failing_update_ref(self, message):
        real = self._run_git

        def run(argv, **kw):
            if "update-ref" in argv:
                return subprocess.CompletedProcess(argv, 1, "", message)
            return real(argv, **kw)
        review_sync._run_git = run

    def _commit(self):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            sha = review_sync.commit_once()
        return sha, out.getvalue()

    def test_a_commit_failure_is_logged_once_then_cleared(self):
        self._failing_update_ref("fatal: cannot lock ref: index.lock exists")
        self._seed_review()
        sha, out = self._commit()
        self.assertIsNone(sha)
        self.assertIn("review-sync: commit failing", out)
        self.assertIn("index.lock", out)
        self.assertIn("index.lock", review_sync.status()["errors"]["commit"]["detail"])
        _sha, again = self._commit()                 # the same failure: no second line
        self.assertEqual(again, "")
        self._failing_update_ref("fatal: disk full")  # a new message is news
        _sha, other = self._commit()
        self.assertIn("disk full", other)
        review_sync._run_git = self._run_git
        sha, cleared = self._commit()
        self.assertIsNotNone(sha)
        self.assertIn("review-sync: commit working again", cleared)
        self.assertNotIn("commit", review_sync.status()["errors"])
        _sha, quiet = self._commit()                 # nothing changed: nothing to say
        self.assertEqual(quiet, "")


class TestPush(ReviewSyncBase):
    def test_push_then_no_unpushed(self):
        self._seed_review()
        review_sync.commit_once()
        self.assertTrue(review_sync.has_unpushed())
        self.assertTrue(review_sync.push())
        self.assertFalse(review_sync.has_unpushed())
        self.assertEqual(_git(self.origin, "rev-parse", "align-review-data").stdout.strip(),
                         _git(self.work, "rev-parse", "align-review-data").stdout.strip())

    def test_offline_push_soft_fails_then_recovers(self):
        self._seed_review()
        review_sync.commit_once()
        _git(self.work, "remote", "set-url", "origin", os.path.join(self.root, "nope.git"))
        self.assertFalse(review_sync.push(), "offline push must return False, not raise")
        self.assertIn("push", review_sync._last_error)
        self.assertTrue(review_sync.has_unpushed(), "commits accumulate locally while offline")
        _git(self.work, "remote", "set-url", "origin", self.origin)
        self.assertTrue(review_sync.push())
        self.assertNotIn("push", review_sync._last_error)
        self.assertFalse(review_sync.has_unpushed())

    def test_push_never_forces(self):
        # a diverged remote branch (someone else's commit) must make push FAIL, not clobber
        self._seed_review()
        review_sync.commit_once()
        review_sync.push()
        other = os.path.join(self.root, "other")
        subprocess.run(["git", "clone", "-q", self.origin, other], check=True)
        _git(other, "config", "user.email", "o@example.com")
        _git(other, "config", "user.name", "O")
        _git(other, "checkout", "-q", "align-review-data")
        _write(os.path.join(other, "review", "d999.orig001.review.tsv"), "0\t0\tx\n")
        _git(other, "add", "-A")
        _git(other, "commit", "-q", "-m", "other machine")
        _git(other, "push", "-q", "origin", "align-review-data")
        remote_tip = _git(self.origin, "rev-parse", "align-review-data").stdout.strip()
        self._seed_review(text="0\t0\tchanged\n")
        review_sync.commit_once()                     # local diverges
        self.assertFalse(review_sync.push(), "non-fast-forward must fail soft")
        self.assertEqual(_git(self.origin, "rev-parse", "align-review-data").stdout.strip(),
                         remote_tip, "the remote tip must survive — never force-pushed")

    def test_no_origin_is_not_an_error(self):
        self._seed_review()
        review_sync.commit_once()
        _git(self.work, "remote", "remove", "origin")
        self.assertFalse(review_sync.push())
        self.assertNotIn("push", review_sync._last_error)


class TestPostMerge(ReviewSyncBase):
    def _simulate_squash_merge(self):
        """Land the review data on main+origin as one commit, the way a squash-merge
        by hand would."""
        self._seed_review()
        review_sync.commit_once()
        review_sync.push()
        _git(self.work, "add", "-A", "--", "labels/review/d376-395.orig072.review.tsv")
        env = dict(os.environ, GIT_MAIN_COMMIT_OK="True")
        subprocess.run(["git", "-C", self.work, "commit", "-q", "-m",
                        "data: sync align-review-data (squashed)"],
                       check=True, env=env, capture_output=True, text=True)
        _git(self.work, "push", "-q", "origin", "main")

    def test_recreate_after_merge(self):
        self._simulate_squash_merge()
        review_sync._run_gh = lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="MERGED\n", stderr="")
        note = review_sync.post_merge()
        self.assertIn("recreated", note)
        origin_main = _git(self.work, "rev-parse", "origin/main").stdout.strip()
        rd = _git(self.work, "rev-parse", "align-review-data").stdout.strip()
        self.assertEqual(rd, origin_main, "the branch must be recreated at the new main")
        ahead = _git(self.work, "rev-list", "origin/main..align-review-data").stdout.strip()
        self.assertEqual(ahead, "", "recreated branch must carry nothing beyond main")

    def test_post_merge_refuses_open_pr_even_when_fully_pushed(self):
        self._seed_review()
        review_sync.commit_once()
        review_sync.push()
        review_sync._run_gh = lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="OPEN\n", stderr="")
        with self.assertRaises(RuntimeError):
            review_sync.post_merge()
        self.assertEqual(
            _git(self.work, "rev-parse", "--verify", "align-review-data",
                 check=False).returncode, 0)
        r = _git(self.work, "ls-remote", "--heads", "origin", "align-review-data")
        self.assertIn("align-review-data", r.stdout)

    def test_post_merge_refuses_when_gh_cannot_tell(self):
        self._seed_review()
        review_sync.commit_once()
        review_sync.push()
        review_sync._run_gh = lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout="", stderr="boom")
        with self.assertRaises(RuntimeError):
            review_sync.post_merge()
        self.assertEqual(
            _git(self.work, "rev-parse", "--verify", "align-review-data",
                 check=False).returncode, 0)

    def test_post_merge_refuses_when_remote_delete_is_rejected(self):
        self._simulate_squash_merge()
        review_sync._run_gh = lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="MERGED\n", stderr="")
        real_run = review_sync._run_git

        def deny_delete(cmd, **kw):
            if "--delete" in cmd:
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="remote: protected")
            return real_run(cmd, **kw)
        review_sync._run_git = deny_delete
        try:
            before = _git(self.work, "rev-parse", "align-review-data").stdout.strip()
            with self.assertRaises(RuntimeError):
                review_sync.post_merge()
            self.assertEqual(_git(self.work, "rev-parse", "align-review-data").stdout.strip(),
                             before)
            r = _git(self.work, "ls-remote", "--heads", "origin", "align-review-data")
            self.assertIn("align-review-data", r.stdout)
        finally:
            review_sync._run_git = real_run

    def test_post_merge_survives_an_already_deleted_remote_branch(self):
        self._simulate_squash_merge()
        review_sync._run_gh = lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="MERGED\n", stderr="")
        _git(self.work, "push", "origin", "--delete", "align-review-data")
        note = review_sync.post_merge()
        self.assertIn("recreated", note)

    def test_post_merge_refuses_when_fetch_fails(self):
        self._simulate_squash_merge()
        review_sync._run_gh = lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="MERGED\n", stderr="")
        real_run = review_sync._run_git

        def deny_fetch(cmd, **kw):
            if "fetch" in cmd:
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="network down")
            return real_run(cmd, **kw)
        review_sync._run_git = deny_fetch
        try:
            before = _git(self.work, "rev-parse", "align-review-data").stdout.strip()
            with self.assertRaises(RuntimeError):
                review_sync.post_merge()
            self.assertEqual(_git(self.work, "rev-parse", "align-review-data").stdout.strip(),
                             before)
        finally:
            review_sync._run_git = real_run


class TestAutoMerge(ReviewSyncBase):
    """`make review-sync-automerge` merges a rulings-only PR and leaves every other one to be
    merged by hand (decision made 2026-10-08). gh is faked; post_merge is stubbed
    (TestPostMerge covers it)."""

    def _gh(self, pr=None, view_rc=0, view_err="", merge_rc=0):
        calls = []

        def fake(argv, **k):
            calls.append(argv)
            if argv[1:3] == ["pr", "view"]:
                if view_rc:
                    return subprocess.CompletedProcess(argv, view_rc, stdout="", stderr=view_err)
                return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(pr), stderr="")
            if argv[1:3] == ["pr", "merge"]:
                return subprocess.CompletedProcess(argv, merge_rc, stdout="", stderr="merge refused")
            raise AssertionError("unexpected gh call %r" % (argv,))
        review_sync._run_gh = fake
        recreated = []
        orig = review_sync.post_merge
        review_sync.post_merge = lambda dry_run=False: recreated.append(1) or "recreated"
        self.addCleanup(setattr, review_sync, "post_merge", orig)
        return calls, recreated

    @staticmethod
    def _pr(*paths, state="OPEN"):
        return {"state": state, "baseRefName": "main", "headRefOid": "abc123", "url": "https://x/pr/1",
                "files": [{"path": p} for p in paths]}

    def _merges(self, calls):
        return [c for c in calls if c[1:3] == ["pr", "merge"]]

    def test_rulings_only_pr_is_merged_at_the_checked_head_then_recreated(self):
        calls, recreated = self._gh(self._pr("data/rulings.json"))
        out = review_sync.auto_merge()
        merges = self._merges(calls)
        self.assertEqual(len(merges), 1)
        self.assertIn("--squash", merges[0])
        self.assertEqual(merges[0][merges[0].index("--match-head-commit") + 1], "abc123")
        self.assertEqual(recreated, [1])
        self.assertIn("merged", out)

    def test_a_pr_with_notes_is_left_to_merge_by_hand(self):
        for paths in (("data/rulings.json", "labels/review/a.orig001.review.tsv"),
                      ("labels/summary/a.summary.tsv",)):
            calls, recreated = self._gh(self._pr(*paths))
            out = review_sync.auto_merge()
            self.assertEqual(self._merges(calls), [])
            self.assertEqual(recreated, [])
            self.assertIn("left to merge by hand", out)
            # the recreate hint is the full command, with this checkout's own path
            self.assertIn("make -C %s review-sync-postmerge" % self.work, out)

    def test_no_open_pr_is_nothing_to_do(self):
        calls, recreated = self._gh(view_rc=1, view_err="no pull requests found for branch")
        self.assertIn("nothing to merge", review_sync.auto_merge())
        calls, recreated = self._gh(self._pr("data/rulings.json", state="MERGED"))
        self.assertIn("nothing to merge", review_sync.auto_merge())
        self.assertEqual(self._merges(calls), [])

    def test_a_pr_with_no_files_is_never_merged(self):
        calls, _ = self._gh(self._pr())
        self.assertIn("left to merge by hand", review_sync.auto_merge())
        self.assertEqual(self._merges(calls), [])

    def test_dry_run_merges_nothing(self):
        calls, recreated = self._gh(self._pr("data/rulings.json"))
        self.assertIn("would squash-merge", review_sync.auto_merge(dry_run=True))
        self.assertEqual(self._merges(calls), [])
        self.assertEqual(recreated, [])

    def test_a_refused_merge_raises_and_recreates_nothing(self):
        _, recreated = self._gh(self._pr("data/rulings.json"), merge_rc=1)
        with self.assertRaises(RuntimeError):
            review_sync.auto_merge()
        self.assertEqual(recreated, [])

    def test_a_failed_recreate_after_the_merge_names_the_merge_and_the_fix(self):
        calls, _ = self._gh(self._pr("data/rulings.json"))

        def boom(dry_run=False):
            raise RuntimeError("fetch failed")
        review_sync.post_merge = boom
        with self.assertRaises(RuntimeError) as cm:
            review_sync.auto_merge()
        self.assertEqual(len(self._merges(calls)), 1)
        self.assertIn("merged", str(cm.exception))
        self.assertIn("make -C %s review-sync-postmerge" % self.work, str(cm.exception))

    def test_a_pr_into_another_base_or_with_no_head_is_not_merged(self):
        pr = self._pr("data/rulings.json")
        pr["baseRefName"] = "other"
        calls, _ = self._gh(pr)
        self.assertIn("nothing to merge", review_sync.auto_merge())
        pr = self._pr("data/rulings.json")
        del pr["headRefOid"]
        calls, _ = self._gh(pr)
        self.assertIn("no head commit", review_sync.auto_merge())
        self.assertEqual(self._merges(calls), [])

    def test_the_cli_verb_exits_1_on_failure_and_refuses_when_dark(self):
        self._gh(view_rc=1, view_err="HTTP 502")
        self.assertEqual(review_sync.main(["auto-merge"]), 1)
        self._gh(view_rc=1, view_err="no pull requests found for branch")
        self.assertEqual(review_sync.main(["auto-merge"]), 0)
        orig = review_sync.enabled
        review_sync.enabled = lambda: False
        self.addCleanup(setattr, review_sync, "enabled", orig)
        self.assertEqual(review_sync.main(["auto-merge"]), 1)

    def test_gh_failure_raises(self):
        self._gh(view_rc=1, view_err="HTTP 502")
        with self.assertRaises(RuntimeError):
            review_sync.auto_merge()


class TestSyncCommand(ReviewSyncBase):
    def test_sync_commits_pushes_then_opens_one_pr(self):
        order = []
        orig_commit = review_sync.commit_once
        orig_push = review_sync.push
        orig_pr = review_sync.open_or_refresh_pr

        def fake_commit(*a, **k):
            order.append("commit"); return "d" * 40

        def fake_push(*a, **k):
            order.append("push"); return True

        def fake_pr():
            order.append("pr"); return "https://example/pr/1", True

        review_sync.commit_once = fake_commit
        review_sync.push = fake_push
        review_sync.open_or_refresh_pr = fake_pr
        # commit/push are faked, so no real branch exists: say "one commit ahead" so this test
        # stays about ORDER and the converged short-circuit is exercised on its own below.
        orig_ahead = review_sync.commits_ahead
        review_sync.commits_ahead = lambda: 1
        import contextlib
        import io
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                rc = review_sync.main(["sync"])
        finally:
            review_sync.commit_once = orig_commit
            review_sync.push = orig_push
            review_sync.open_or_refresh_pr = orig_pr
            review_sync.commits_ahead = orig_ahead
        self.assertEqual(rc, 0)
        self.assertEqual(order, ["commit", "push", "pr"])

    def test_nudge_burst_coalesces_to_one_commit_per_interval(self):
        # §6.7 invariant 3: the fsynced append acked the note; a burst of nudges
        # (one per note) must become ONE commit per commit interval, never one
        # commit per note. The clock is PINNED LOW (fresh-boot conditions: CI
        # runners start with monotonic() near zero) so this also deterministically
        # covers the 0.0-sentinel regression — with 0.0 as "never", the first
        # pass was deferred a whole interval on any freshly booted machine.
        import types
        self._seed_review()
        calls = []
        orig_commit = review_sync.commit_once
        orig_time = review_sync.time
        review_sync.commit_once = lambda *a, **k: calls.append(1)
        review_sync.time = types.SimpleNamespace(monotonic=lambda: 1.0,
                                                 sleep=orig_time.sleep)
        try:
            review_sync._last_commit_pass = None
            for _ in range(5):                      # five rapid nudge-driven passes
                review_sync._tick()
        finally:
            review_sync.commit_once = orig_commit
            review_sync.time = orig_time
        self.assertEqual(len(calls), 1,
                         "exactly one immediate first pass, then coalesced")

    def test_sync_reports_converged_instead_of_failing(self):
        # The real bug: `align-review-data` had nothing main did not, `gh pr create` refused,
        # and the raised failure exited the sync non-zero — masking the other steps' success.
        # Converged is a SUCCESS, and no gh call is made at all.
        # exactly the post-merge state: the branch was recreated from main and no new notes
        # have landed since, so there is nothing to commit and nothing to PR.
        _git(self.work, "update-ref", "refs/heads/" + review_sync.branch(),
             _git(self.work, "rev-parse", "main").stdout.strip())

        def no_gh(argv, **k):
            raise AssertionError("converged must not ask gh for a PR: %r" % argv)

        review_sync._run_gh = no_gh
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = review_sync.main(["sync"])
        self.assertEqual(rc, 0)
        self.assertIn("converged", buf.getvalue())

    def test_the_pr_title_and_body_carry_no_action_point_number(self):
        # The review-data PR is public; a private task number means nothing to a reader
        # there (decision made 2026-10-10).
        orig_ahead = review_sync.commits_ahead
        review_sync.commits_ahead = lambda: 1
        made = []

        def fake_gh(argv, **k):
            if argv[1:3] == ["pr", "list"]:
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            made.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout="https://example/pr/1\n", stderr="")

        review_sync._run_gh = fake_gh
        import contextlib, io
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                review_sync.main(["sync"])
        finally:
            review_sync.commits_ahead = orig_ahead
        self.assertEqual(len(made), 1, "one pr create")
        argv = made[0]
        title = argv[argv.index("--title") + 1]
        body = argv[argv.index("--body") + 1]
        self.assertEqual(title, "data: sync align-review-data (review notes)")
        self.assertNotIn("AP-", title + body)
        self.assertNotIn("§", body, "a private plan's section number")
        # the body is posted to the public repository: no machine path
        self.assertNotIn(self.work, body)
        self.assertIn("make review-sync-automerge", body)
        self.assertIn("make review-sync-postmerge", body)

    def test_sync_treats_githubs_no_commits_refusal_as_converged(self):
        # Same case reached with a STALE local origin/main: the count says "ahead", GitHub
        # compares the real tips and refuses. Still converged, still rc 0.
        orig_ahead = review_sync.commits_ahead
        review_sync.commits_ahead = lambda: 1

        def fake_gh(argv, **k):
            if argv[1:3] == ["pr", "list"]:
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(
                argv, 1, stdout="",
                stderr="pull request create failed: GraphQL: No commits between main and "
                       "align-review-data (createPullRequest)")

        review_sync._run_gh = fake_gh
        import contextlib, io
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                rc = review_sync.main(["sync"])
        finally:
            review_sync.commits_ahead = orig_ahead
        self.assertEqual(rc, 0)
        self.assertIn("converged", buf.getvalue())

    def test_commits_ahead_counts_against_the_pushed_base(self):
        self.assertEqual(review_sync.commits_ahead(), 0, "no branch yet is nothing to PR")
        self._seed_review()
        review_sync.commit_once()
        self.assertEqual(review_sync.commits_ahead(), 1)

    def test_commits_ahead_falls_back_to_the_remote_branch(self):
        # A fresh clone, or a post_merge that deleted the local ref and then failed to recreate
        # it, leaves the commits on the REMOTE branch only. Reading the local ref alone reported
        # "converged" over unmerged data — the one direction this must never get wrong.
        self._seed_review()
        review_sync.commit_once()
        review_sync.push()
        self.assertEqual(review_sync.commits_ahead(), 1)
        _git(self.work, "update-ref", "-d", review_sync.branch_ref())
        self.assertIsNone(review_sync._rev(review_sync.branch_ref()), "local ref really is gone")
        self.assertEqual(review_sync.commits_ahead(), 1,
                         "the remote branch still has a commit main does not")

    def test_a_dry_run_says_what_the_real_run_would_do(self):
        # commit_once(dry_run=True) does not move the branch ref, so the ahead-count still
        # reflects the state BEFORE the commit. Reported as-is, a dry run claimed "would commit"
        # and "converged, no PR needed" together, while the real run opens a PR.
        self._seed_review()

        def no_gh(argv, **k):
            raise AssertionError("a dry run must not touch gh: %r" % argv)

        review_sync._run_gh = no_gh
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = review_sync.main(["sync", "--dry-run"])
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("would commit, then open/refresh", out)
        self.assertNotIn("converged", out)

    def test_a_converged_dry_run_still_says_converged(self):
        _git(self.work, "update-ref", "refs/heads/" + review_sync.branch(),
             _git(self.work, "rev-parse", "main").stdout.strip())

        def no_gh(argv, **k):
            raise AssertionError("a dry run must not touch gh: %r" % argv)

        review_sync._run_gh = no_gh
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = review_sync.main(["sync", "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertIn("(dry-run) converged", buf.getvalue())

    def test_open_or_refresh_pr_reuses_existing(self):
        def fake_gh(argv, **k):
            if argv[1:3] == ["pr", "list"]:
                return subprocess.CompletedProcess(argv, 0, stdout="https://example/pr/9\n", stderr="")
            raise AssertionError("must not create a PR when one is open: %r" % argv)

        review_sync._run_gh = fake_gh
        url, created = review_sync.open_or_refresh_pr()
        self.assertEqual(url, "https://example/pr/9")
        self.assertFalse(created)


class TestEnabledGating(ReviewSyncBase):
    def test_enabled_at_the_checkouts_git_root(self):
        self.assertTrue(review_sync.enabled())

    def test_kill_switch(self):
        os.environ["NETRADIO_REVIEW_SYNC"] = "0"
        self.assertFalse(review_sync.enabled())
        self.assertIsNone(review_sync.start())

    def _assert_all_mutations_refuse(self):
        """Every mutating entry point — direct AND CLI — must refuse while dark."""
        self._seed_review()
        self.assertIsNone(review_sync.commit_once(), "commit_once must be a no-op")
        self.assertNotEqual(
            _git(self.work, "rev-parse", "--verify", "align-review-data",
                 check=False).returncode, 0, "no ref may be created while dark")
        self.assertFalse(review_sync.push())
        with self.assertRaises(RuntimeError):
            review_sync.post_merge()
        import contextlib
        import io
        for verb in ("commit", "push", "sync", "post-merge"):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = review_sync.main([verb])
            self.assertEqual(rc, 1, verb)
            self.assertIn("dark", buf.getvalue())

    def test_kill_switch_gates_every_mutating_entry_point(self):
        # the P1 case: an operator who explicitly disabled review-sync must not be
        # able to create refs or push through the Make targets anyway
        os.environ["NETRADIO_REVIEW_SYNC"] = "0"
        self._assert_all_mutations_refuse()

    def test_non_root_checkout_gates_every_mutating_entry_point(self):
        sub = os.path.join(self.work, "labels", "review")
        os.makedirs(sub, exist_ok=True)
        os.environ["NETRADIO_REVIEW_SYNC_REPO"] = sub
        self._seed_review()
        self.assertIsNone(review_sync.commit_once())
        self.assertFalse(review_sync.push())
        with self.assertRaises(RuntimeError):
            review_sync.post_merge()
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(review_sync.main(["sync"]), 1)
        # and the ENCLOSING repo got no ref either — the whole point of the guard
        self.assertNotEqual(
            _git(self.work, "rev-parse", "--verify", "align-review-data",
                 check=False).returncode, 0)

    def test_disabled_outside_a_git_repo(self):
        nongit = tempfile.mkdtemp(prefix="rsync_nogit_")
        try:
            os.environ["NETRADIO_REVIEW_SYNC_REPO"] = nongit
            self.assertFalse(review_sync.enabled())
        finally:
            import shutil
            shutil.rmtree(nongit, ignore_errors=True)

    def test_disabled_when_repo_is_a_subdir_not_the_root(self):
        # the strict root check: a subdir INSIDE a work tree must stay dark, or review
        # commits would land in whatever repo happens to enclose the path
        sub = os.path.join(self.work, "labels", "review")
        os.makedirs(sub, exist_ok=True)
        os.environ["NETRADIO_REVIEW_SYNC_REPO"] = sub
        self.assertFalse(review_sync.enabled())

    def test_with_no_override_the_repo_is_the_checkout_the_module_sits_in(self):
        os.environ.pop("NETRADIO_REVIEW_SYNC_REPO", None)
        self.assertEqual(os.path.realpath(review_sync.repo_dir()), os.path.realpath(REPO))
        self.assertEqual(review_sync.postmerge_command(),
                         "make -C %s review-sync-postmerge" % review_sync.repo_dir())

    def test_with_no_override_it_commits_into_its_own_checkout(self):
        # the module copied into the scratch checkout's scripts/ and run there with no
        # override: the commit lands on that checkout's branch
        os.makedirs(os.path.join(self.work, "scripts"))
        import shutil
        shutil.copy2(os.path.join(REPO, "scripts", "review_sync.py"),
                     os.path.join(self.work, "scripts", "review_sync.py"))
        self._seed_review()
        env = {k: v for k, v in os.environ.items() if not k.startswith("NETRADIO_REVIEW_SYNC")}
        code = ("import sys; sys.path.insert(0, %r); import review_sync; "
                "sys.exit(review_sync.main(['commit']))" % os.path.join(self.work, "scripts"))
        r = subprocess.run([sys.executable, "-c", code], cwd=self.root, env=env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("labels/review/d376-395.orig072.review.tsv",
                      _tree_files(self.work, "align-review-data"))
        self.assertEqual(_git(self.work, "status", "--porcelain", "--", "labels/").stdout.strip(),
                         "?? labels/review/", "the working tree is left as it was")

    def test_a_linked_worktree_is_dark_and_write_free(self):
        # a worktree shares the branch refs: a commit from it would put its own copies of the
        # review files and the rulings onto the standing branch
        wt = os.path.join(self.root, "wt")
        _git(self.work, "worktree", "add", "-q", "--detach", wt, "main")
        _write(os.path.join(wt, "labels", "review", "d376-395.orig072.review.tsv"), "stale\n")
        os.environ["NETRADIO_REVIEW_SYNC_REPO"] = wt
        self.assertTrue(review_sync.is_linked_worktree())
        self.assertFalse(review_sync.enabled())
        self.assertIsNone(review_sync.commit_once())
        self.assertFalse(review_sync.push())
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(review_sync.main(["sync"]), 1)
        self.assertIn("linked worktree", buf.getvalue())
        self.assertNotEqual(
            _git(self.work, "rev-parse", "--verify", "align-review-data",
                 check=False).returncode, 0, "no ref may be created from a worktree")


class TestMakeTargets(ReviewSyncBase):
    """The Makefile's review-sync targets, run for real against the scratch checkout, with a
    stand-in `gh` on PATH that answers and records every call."""

    FAKE_GH = """#!/bin/sh
printf '%s\n' "$*" >> "$GH_LOG"
case "$1 $2" in
  "pr view") printf '%s\n' '{"state": "OPEN", "baseRefName": "main", "headRefOid": "abc123", "url": "https://example/pr/1", "files": [{"path": "data/rulings.json"}]}' ;;
  *) echo "unexpected gh call: $*" >&2; exit 1 ;;
esac
"""

    def test_automerge_dry_run_runs_and_merges_nothing(self):
        bindir = os.path.join(self.root, "bin")
        os.makedirs(bindir)
        gh = os.path.join(bindir, "gh")
        with open(gh, "w") as fh:
            fh.write(self.FAKE_GH)
        os.chmod(gh, 0o755)
        log = os.path.join(self.root, "gh.log")
        env = dict(os.environ, PATH=bindir + os.pathsep + os.environ.get("PATH", ""),
                   GH_LOG=log, NETRADIO_REVIEW_SYNC_REPO=self.work, NETRADIO_REVIEW_SYNC="1")
        r = subprocess.run(["make", "-s", "-C", REPO, "review-sync-automerge", "ARGS=--dry-run"],
                           capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("would squash-merge https://example/pr/1", r.stdout)
        with open(log) as fh:
            calls = fh.read().splitlines()
        self.assertTrue(calls and all(c.startswith("pr view") for c in calls), calls)
        self.assertNotEqual(
            _git(self.work, "rev-parse", "--verify", "align-review-data",
                 check=False).returncode, 0, "a dry run creates no ref")

    def test_the_targets_run_the_scripts_under_scripts(self):
        with open(os.path.join(REPO, "Makefile"), encoding="utf-8") as fh:
            mk = fh.read()
        for target, verb in (("review-sync:", "sync"), ("review-sync-automerge:", "auto-merge"),
                             ("review-sync-postmerge:", "post-merge")):
            self.assertIn(target, mk)
            self.assertIn("scripts/review_sync.py %s $(ARGS)" % verb, mk)


if __name__ == "__main__":
    unittest.main()
