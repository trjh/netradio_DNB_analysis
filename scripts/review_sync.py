"""Commit + push the DP-22 review notes to a standing branch of this repository.

WHY (docs/PLAN_align_tool.md §6.7)
----------------------------------
`review_notes.py` appends §6.6 grammar rows to `labels/review/<stem>.origNNN.review.tsv` in
this checkout — fsync-before-ack, so the durable append IS the acknowledgement. Something still
has to get those files INTO git on a schedule without a human running `git commit`, and without
ever touching the checkout's working tree, index, or branch (it is a live working copy on
`main`, the one the align server runs from). It commits the repository it runs in, with git
plumbing against that checkout's own object store; its invariants are §6.7's, restated:

  1. plumbing-only commits (throwaway GIT_INDEX_FILE + read-tree/add/write-tree/commit-tree/
     update-ref) — the checkout's working tree, index and HEAD are never touched, and the standing
     `align-review-data` branch is never checked out anywhere;
  2. the pathspec is ENUMERATED — `labels/review/*.review.tsv` + `labels/summary/*.summary.tsv`
     + the one exact file `data/rulings.json` (the harvester's retired set), never a
     directory; a stray file in any of those dirs is never swept in, and a missing rulings
     file is never committed as a deletion. The rulings ride this module's cadence below (the
     coalesced commit pass, or `make review-sync`): a write of the file never triggers a
     commit itself;
  3. durable-append-before-commit: the fsynced append acked the note; the commit is a
     downstream batch (nudge on append, coalesced ~15-min commit, hourly push) — a crash
     between append and commit loses nothing;
  4. no change → no commit; parents chain commit-to-commit, first parent = this
     repository's `main`;
  5. an offline push soft-fails (recorded in status(), retried next hour) and NEVER forces;
  6. the fold into `main` is ONE squash PR (`make review-sync` opens/refreshes it and prints
     the URL). The user merges it, except one that changes only `data/rulings.json`, which
     `make review-sync-automerge` merges (auto_merge, AUTO_MERGE_FILES; decision made
     2026-10-08) — the thread itself never merges;
  7. post-merge recreate (`make review-sync-postmerge`) fails CLOSED unless the PR is
     verifiably MERGED;
  8. dark unless the checkout is the ROOT of a git work tree and not a linked worktree (a
     worktree shares the branch refs, so it would commit its own stale copies onto the
     standing branch); `NETRADIO_REVIEW_SYNC=0` is the kill switch;
  9. the pathspec physically cannot reach `labels/` — review files are not labels (§6.11); no
     configuration of this module can commit a label.

LIFECYCLE / EXPOSURE
--------------------
The committer thread rides `align_server.py`: `start()` is called at server startup (a no-op
when dark) and wakes on `nudge()` (poked by the review-note route after each fsynced append) or
on its poll interval. It runs INSIDE the align server's process, commits only to the local
clone, and its only network act is the hourly `git push` to `origin` — nothing here changes the
server's HTTP exposure posture. This repository is PUBLIC: the squash PR (invariant 6) is the
contents gate before any note reaches `main`. A failure is printed to the server log
(`logs/align.log`) once per distinct message, and again when the operation next succeeds; the
running server's `GET /api/align/sync-status` returns `status()`, errors included, and the
inspector page shows them. `python3 scripts/review_sync.py status` runs in a new process, so
its `errors` are that process's own, never the server's.
"""

import glob
import json
import os
import re
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone

# The ONLY files this module may commit — enumerated patterns, never a directory: the two
# label patterns are rooted under labels/review/ + labels/summary/ so no configuration can reach
# the authoritative labels/*.labels.tsv root files (§6.7 invariant 9, DP-24 layout), and the
# rulings file is ONE exact path (decision made 2026-09-28) — its writer rewrites it whole,
# and nothing else in data/ (the writer's `<path>.<pid>.tmp` included) is ever swept in.
PATHSPEC = [
    "labels/review/*.review.tsv",
    "labels/summary/*.summary.tsv",
    "data/rulings.json",
]

# PATHSPEC entries whose absence on disk is never committed as a deletion (see _matched_pathspec)
NEVER_DELETE = {"data/rulings.json"}

# Swappable seams for tests: every git / gh invocation passes through one
# of these. Tests point a throwaway repo at a LOCAL bare origin and fake `_run_gh`.
_run_git = subprocess.run
_run_gh = subprocess.run

NUDGE = threading.Event()           # the review-note route pokes this after a durable append
_thread = None
_stop = threading.Event()
_lock = threading.Lock()            # one commit/push pass at a time (loop + CLI + tests)

_last_error = {}                    # {op: {"at": iso, "detail": str}}
_logged = {}                        # {op: detail last printed}: one log line per distinct message
ECHO = True                         # print failures and recoveries (the CLI prints its own)
_last_ok = {}
_last_commit = None
_last_push = 0.0                    # monotonic clock of the last push ATTEMPT
_last_commit_pass = None            # monotonic clock of the last commit ATTEMPT (the coalescer);
                                    # None = never — a 0.0 sentinel would defer the first commit
                                    # on a freshly booted machine, where monotonic() is still small


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fail(op, detail):
    """Record `op`'s failure; print it once per distinct message (the server's log). `at` is
    when the failure began: it stays the first failure's time until the op next succeeds."""
    _last_error[op] = {"at": _last_error.get(op, {}).get("at") or _now(), "detail": detail}
    if _logged.get(op) != detail:
        _logged[op] = detail
        if ECHO:
            print("review-sync: %s failing -- %s" % (op, detail), flush=True)


def _ok(op):
    """Clear `op`'s failure; print the recovery once, if a failure was printed."""
    _last_error.pop(op, None)
    if _logged.pop(op, None) is not None and ECHO:
        print("review-sync: %s working again" % op, flush=True)


# commit_once stages through a temp index with a new random name each pass; a failure that names
# it would read as a new message every pass, so the name is replaced before the message is kept.
_TEMP_INDEX = re.compile(r"[^\s'\"]*review_sync_index\.[^\s'\"]*")


def _temp_index_named(detail):
    return _TEMP_INDEX.sub("<temp index>", detail)


# --- config (read lazily, so a test's environment is the one used) ------------------------------

# This module sits in scripts/; the checkout it commits is one level up.
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def repo_dir():
    """The checkout whose review files we commit: the one this module is in.
    `NETRADIO_REVIEW_SYNC_REPO` overrides (tests point it at a tmp repo)."""
    v = os.environ.get("NETRADIO_REVIEW_SYNC_REPO", "").strip()
    return os.path.expanduser(v) if v else ROOT_DIR


def postmerge_command():
    """The hint printed wherever the user has to recreate the branch by hand: the full
    command with this checkout's own path, so it is right from any directory."""
    return "make -C %s review-sync-postmerge" % repo_dir()


def branch():
    return os.environ.get("NETRADIO_REVIEW_SYNC_BRANCH",
                          "align-review-data").strip() or "align-review-data"


def base_branch():
    return os.environ.get("NETRADIO_REVIEW_SYNC_BASE", "main").strip() or "main"


def remote():
    return os.environ.get("NETRADIO_REVIEW_SYNC_REMOTE", "origin").strip() or "origin"


def _commit_interval():
    try:
        return max(30.0, float(os.environ.get("NETRADIO_REVIEW_SYNC_COMMIT_S", "900")))
    except ValueError:
        return 900.0


def _push_interval():
    try:
        return max(60.0, float(os.environ.get("NETRADIO_REVIEW_SYNC_PUSH_S", "3600")))
    except ValueError:
        return 3600.0


def _poll_interval():
    return min(300.0, _commit_interval())


def in_git_repo():
    """True only when repo_dir is itself the ROOT of a git work tree: if the override were ever
    set to a subdirectory of some other repo, a loose 'inside a work tree' test would commit
    review files into THAT repo."""
    if not repo_dir():
        return False
    r = _git("rev-parse", "--show-toplevel")
    if r.returncode != 0:
        return False
    top = (r.stdout or "").strip()
    return bool(top) and os.path.realpath(top) == os.path.realpath(repo_dir())


def enabled():
    """On when the checkout is a git work-tree root, not a linked worktree, and
    NETRADIO_REVIEW_SYNC != 0 (the kill switch). Dark otherwise. EVERY mutating
    entry point (commit_once / push / post_merge, and hence the CLI verbs) re-checks this
    predicate itself: the kill switch and the strict-root guard must hold against a direct
    CLI/Make invocation exactly as they hold against the background thread."""
    if os.environ.get("NETRADIO_REVIEW_SYNC", "1").strip() == "0":
        return False
    if is_linked_worktree():
        return False
    return in_git_repo()


def is_linked_worktree():
    """A linked worktree has a `.git` FILE; the main checkout has a directory. Read from the
    filesystem. A worktree shares the branch refs with the main checkout, so a commit from one
    would put the worktree's copies of the review files and the rulings on the standing branch."""
    git = os.path.join(repo_dir(), ".git")
    return os.path.lexists(git) and not os.path.isdir(git)


def _dark_reason():
    """Why enabled() is False, for CLI messages. None when it is actually on."""
    if os.environ.get("NETRADIO_REVIEW_SYNC", "1").strip() == "0":
        return "NETRADIO_REVIEW_SYNC=0 (kill switch)"
    if is_linked_worktree():
        return ("%s is a linked worktree; run it from the main checkout" % repo_dir())
    if not in_git_repo():
        return ("%s is not the root of a git work tree" % repo_dir())
    return None


# --- git plumbing ------------------------------------------------------------------------------

def _git(*args, index_file=None, check=False):
    """Run `git -C <checkout> <args>`. `index_file` sets GIT_INDEX_FILE so staging writes
    to a THROWAWAY index, never the checkout's real one."""
    env = None
    if index_file is not None:
        env = dict(os.environ)
        env["GIT_INDEX_FILE"] = index_file
    return _run_git(["git", "-C", repo_dir() or ".", *args],
                    capture_output=True, text=True, env=env, check=check)


def _rev(ref):
    r = _git("rev-parse", "--verify", "--quiet", ref + "^{commit}")
    out = (r.stdout or "").strip()
    return out if r.returncode == 0 and out else None


def _tree_of(commit):
    r = _git("rev-parse", "--verify", "--quiet", commit + "^{tree}")
    return (r.stdout or "").strip() if r.returncode == 0 else None


def _matched_pathspec(base):
    """The subset of PATHSPEC matching something to stage — present on disk (add/modify) OR in
    `base`'s tree (a deletion to record); git add fatals on a pathspec matching neither (a
    checkout with no labels/review/ yet). The rulings file is the exception: a deletion of it is
    NEVER recorded. Its absence on disk is an accident (a test of the supervisor's gate, a hand
    `rm`), never an intent, and a merged deletion would remove the file from `main` (an
    independent review's finding) — so when it is missing it is simply left out."""
    out = []
    for p in PATHSPEC:
        on_disk = bool(glob.glob(os.path.join(repo_dir(), p)))
        in_base = False
        if not on_disk and p in NEVER_DELETE:
            continue
        if not on_disk and base:
            r = _git("ls-tree", "-r", "--name-only", base, "--", p)
            in_base = r.returncode == 0 and bool((r.stdout or "").strip())
        if on_disk or in_base:
            out.append(p)
    return out


def branch_ref():
    return "refs/heads/" + branch()


def _remote_tracking_ref():
    return "refs/remotes/%s/%s" % (remote(), branch())


def commit_once(message=None, dry_run=False):
    """Commit the review pathspec to `refs/heads/<branch>` via plumbing, IF it differs from the
    branch tip (first commit: from `main`). Returns the new sha, or None when
    nothing changed / nothing to base off / a git step failed (recorded, never raised)."""
    global _last_commit
    with _lock:
        if not enabled():
            return None      # dark (no/killed/non-root checkout): no ref is ever written
        try:
            base = _rev(branch_ref()) or _rev(base_branch()) or _rev(remote() + "/" + base_branch())
            if not base:
                return None                      # bare/fresh repo — not our failure
            base_tree = _tree_of(base)
            fd, idx = tempfile.mkstemp(prefix="review_sync_index.")
            os.close(fd)
            try:
                rt = _git("read-tree", base, index_file=idx)
                if rt.returncode != 0:
                    raise RuntimeError("read-tree failed: " + (rt.stderr or "").strip())
                spec = _matched_pathspec(base)
                if spec:
                    add = _git("add", "-A", "--", *spec, index_file=idx)
                    if add.returncode != 0:
                        raise RuntimeError("add failed: " + (add.stderr or "").strip())
                wt = _git("write-tree", index_file=idx)
                if wt.returncode != 0:
                    raise RuntimeError("write-tree failed: " + (wt.stderr or "").strip())
                new_tree = (wt.stdout or "").strip()
            finally:
                try:
                    os.remove(idx)
                except OSError:
                    pass
            if not new_tree:
                raise RuntimeError("write-tree produced no tree")
            if new_tree == base_tree:
                _ok("commit")
                return None                      # nothing changed since the last commit
            if dry_run:
                return "(dry-run: would commit %s)" % new_tree[:12]
            msg = message or ("data: align-review-data auto-commit %s" % _now())
            parent = _rev(branch_ref()) or base
            ct = _git("commit-tree", new_tree, "-p", parent, "-m", msg)
            if ct.returncode != 0:
                raise RuntimeError("commit-tree failed: " + (ct.stderr or "").strip())
            sha = (ct.stdout or "").strip()
            ur = _git("update-ref", branch_ref(), sha)
            if ur.returncode != 0:
                raise RuntimeError("update-ref failed: " + (ur.stderr or "").strip())
            _last_commit = sha
            _last_ok["commit"] = _now()
            _ok("commit")
            return sha
        except Exception as exc:        # noqa: BLE001 — a sync failure must never crash the server
            _fail("commit", _temp_index_named(str(exc))[:200])
            return None


def has_unpushed():
    local = _rev(branch_ref())
    if not local:
        return False
    tracked = _rev(_remote_tracking_ref())
    return tracked != local


def push(dry_run=False):
    """Push `refs/heads/<branch>` to <remote>. Offline/unreachable returns False with a
    recorded error and retries next hour; NEVER raises, NEVER force-pushes."""
    global _last_push
    with _lock:
        _last_push = time.monotonic()
        if not enabled():
            return False                 # dark: nothing ever leaves the machine
        if not _rev(branch_ref()):
            return False
        rr = _git("remote", "get-url", remote())
        if rr.returncode != 0:
            return False                 # no origin — commits accumulate locally, not an error
        if dry_run:
            return True
        refspec = "%s:%s" % (branch_ref(), branch_ref())
        r = _git("push", remote(), refspec)
        if r.returncode != 0:
            detail = (r.stderr or r.stdout or "git push exited %d" % r.returncode).strip()
            _fail("push", detail[-200:])
            return False
        _last_ok["push"] = _now()
        _ok("push")
        return True


# --- the PR / post-merge helpers (gh; CLI only — the thread never merges) ------------------------

def _gh(*args):
    return _run_gh(["gh", *args], capture_output=True, text=True, cwd=repo_dir())


def commits_ahead():
    """How many commits `<branch>` has that `<base>` does not — measured against the REMOTE base
    when it is known, since that is what GitHub compares. Zero means converged: there is nothing
    to open a PR for, and asking for one anyway is an error, not a failure (see _cmd_sync)."""
    # The remote-tracking ref is the fallback, not an afterthought: a fresh clone, or a
    # post_merge that deleted the local branch and then failed to recreate it, leaves commits
    # sitting on the REMOTE branch with no local ref. Reading only the local one there would
    # report "converged" over unmerged data — the one direction this must never get wrong.
    local = _rev(branch_ref()) or _rev(_remote_tracking_ref())
    if not local:
        return 0
    base = _rev("refs/remotes/%s/%s" % (remote(), base_branch())) or _rev("refs/heads/" + base_branch())
    if not base:
        return None                      # base unknown — caller must not infer "converged"
    r = _git("rev-list", "--count", "%s..%s" % (base, local))
    if r.returncode != 0:
        return None
    try:
        return int((r.stdout or "").strip())
    except ValueError:
        return None


def open_or_refresh_pr():
    """Open ONE squash-candidate PR from <branch> → <base> in this repository, or report the
    existing open one. Returns (url, created); raises RuntimeError if gh fails."""
    ls = _gh("pr", "list", "--head", branch(), "--base", base_branch(),
             "--state", "open", "--json", "url", "--jq", ".[0].url // empty")
    if ls.returncode != 0:
        raise RuntimeError("gh pr list failed: " + (ls.stderr or "").strip())
    url = (ls.stdout or "").strip()
    if url:
        return url, False
    title = "data: sync align-review-data (review notes)"
    # The body is posted to the PUBLIC repository: it names no machine path, so the
    # recreate command is the bare target (the terminal output carries the full path).
    body = ("Accumulated `review/*.review.tsv` note files from the standing "
            "`align-review-data` branch (the whole-song review mode's review-note rows; "
            "written by `scripts/review_notes.py`, committed by "
            "`scripts/review_sync.py`), with the compiled "
            "`labels/summary/*.summary.tsv` and the rulings file `data/rulings.json`."
            "\n\nWhile this PR changes only `data/rulings.json`, "
            "`make review-sync-automerge` squash-merges it. Otherwise **squash-merge it by "
            "hand** — this repo is PUBLIC and this PR is the contents gate. After merging, "
            "run `make review-sync-postmerge` in this repository's checkout to recreate "
            "`align-review-data` from the new `main`.")
    cr = _gh("pr", "create", "--base", base_branch(), "--head", branch(),
             "--title", title, "--body", body)
    if cr.returncode != 0:
        raise RuntimeError("gh pr create failed: " + (cr.stderr or cr.stdout or "").strip())
    return (cr.stdout or "").strip(), True


def _pr_merged():
    r = _gh("pr", "view", branch(), "--json", "state,mergedAt", "--jq", ".state")
    if r.returncode != 0:
        return None
    return (r.stdout or "").strip() == "MERGED"


# The one file `make review-sync-automerge` merges without waiting for a person: the rulings are
# keys and flags with no free text, so the contents gate (invariant 6) has nothing to read in
# them (decision made 2026-10-08). A PR holding anything else — a review note, a summary —
# still waits for the user to merge it.
AUTO_MERGE_FILES = {"data/rulings.json"}


def auto_merge(dry_run=False):
    """`make review-sync-automerge`: squash-merge the open PR into the base when every
    file it changes is in AUTO_MERGE_FILES, pinned to the head gh reported (a commit pushed
    meanwhile makes the merge fail rather than slip through), then recreate the branch
    (post_merge). Returns a one-line outcome; raises RuntimeError on a gh failure, and names
    the recreate command (postmerge_command) when the merge went through but the recreate did
    not."""
    r = _gh("pr", "view", branch(), "--json", "state,baseRefName,headRefOid,files,url")
    if r.returncode != 0:
        if "no pull requests found" in (r.stderr or "").lower():
            return "no open %s PR — nothing to merge" % branch()
        raise RuntimeError("gh pr view failed: " + (r.stderr or "").strip()[-200:])
    pr = json.loads(r.stdout or "{}")
    if pr.get("state") != "OPEN" or pr.get("baseRefName") != base_branch():
        return "no open %s -> %s PR — nothing to merge" % (branch(), base_branch())
    files = {f.get("path") for f in pr.get("files") or []}
    if not files or not files <= AUTO_MERGE_FILES:
        return ("left to merge by hand: %s changes %s; merge it, then %s"
                % (pr.get("url"), ", ".join(sorted(f for f in files if f)) or "no files",
                   postmerge_command()))
    head = pr.get("headRefOid")
    if not head:
        return "left to merge by hand: gh reported no head commit for %s" % pr.get("url")
    if dry_run:
        # A dry run neither commits nor pushes, so this is the PR as GitHub has it now; the real
        # run pushes first, and a note pushed then would leave the PR to merge by hand.
        return ("(dry-run) would squash-merge %s (%s only, as the PR stands before this run's "
                "push)" % (pr.get("url"), ", ".join(sorted(files))))
    m = _gh("pr", "merge", branch(), "--squash", "--match-head-commit", head)
    if m.returncode != 0:
        raise RuntimeError("gh pr merge failed: " + (m.stderr or m.stdout or "").strip()[-200:])
    try:
        recreated = post_merge()
    except Exception as exc:            # noqa: BLE001
        raise RuntimeError("merged %s; recreating %s FAILED (%s) — run %s"
                           % (pr.get("url"), branch(), str(exc)[:160], postmerge_command()))
    return "merged %s (%s only); %s" % (pr.get("url"), ", ".join(sorted(files)), recreated)


def post_merge(dry_run=False):
    """After the user squash-merges the PR: delete + recreate `align-review-data` from the new
    `main`. FAILS CLOSED: refuses (RuntimeError) unless gh POSITIVELY reports the PR
    merged; a rejected remote delete with the branch still alive also refuses, with both refs
    intact."""
    with _lock:
        if not enabled():
            raise RuntimeError("review-sync is dark — %s" % _dark_reason())
        local = _rev(branch_ref())
        if not local:
            return "no local %s branch — nothing to recreate" % branch()
        if _pr_merged() is not True:
            raise RuntimeError(
                "refusing to recreate %s: the %s -> %s PR is not verifiably MERGED (it is open, "
                "absent, or gh could not tell). Merge it first — `make review-sync` prints the "
                "URL." % (branch(), branch(), base_branch()))
        fr = _git("fetch", remote(), base_branch())
        if fr.returncode != 0:
            raise RuntimeError("fetch of %s failed — refusing to recreate %s from a possibly-"
                               "stale base: %s"
                               % (base_branch(), branch(), (fr.stderr or "").strip()[-160:]))
        new_main = _rev(remote() + "/" + base_branch()) or _rev(base_branch())
        if not new_main:
            raise RuntimeError("cannot resolve %s to recreate %s from" % (base_branch(), branch()))
        if dry_run:
            return "(dry-run) would delete %s (local+remote) and recreate it at %s" % (
                branch(), new_main[:12])
        dr = _git("push", remote(), "--delete", branch())
        if dr.returncode != 0:
            lr = _git("ls-remote", "--heads", remote(), branch())
            if lr.returncode != 0 or (lr.stdout or "").strip():
                raise RuntimeError("could not delete remote %s (and it still exists) — refusing "
                                   "to reset the local branch: %s"
                                   % (branch(), (dr.stderr or "").strip()[-160:]))
        _git("update-ref", "-d", branch_ref())
        ur = _git("update-ref", branch_ref(), new_main)
        if ur.returncode != 0:
            raise RuntimeError("failed to recreate %s at %s: %s"
                               % (branch(), new_main[:12], (ur.stderr or "").strip()))
        return "recreated %s at %s (%s)" % (branch(), new_main[:12], base_branch())


# --- the background auto-committer (rides align_server) ------------------------------------------

def nudge():
    """Ask the loop for an immediate pass (the review-note route calls this after an append)."""
    NUDGE.set()


def _tick(force_push=False):
    """One pass. Commits are COALESCED to the commit interval (§6.7 invariant 3:
    the fsynced append already acked the note, so a burst of nudges — one per
    note — becomes one commit per interval, not one commit per note); pushes ride
    their own hourly interval."""
    global _last_push, _last_commit_pass
    if _last_commit_pass is None \
            or (time.monotonic() - _last_commit_pass) >= _commit_interval():
        _last_commit_pass = time.monotonic()
        commit_once()
    if force_push or (time.monotonic() - _last_push) >= _push_interval():
        if has_unpushed():
            push()
        else:
            _last_push = time.monotonic()


def _loop():
    global _last_push, _last_commit_pass
    _last_push = 0.0                     # first iteration catches up any unpushed commits
    _last_commit_pass = None             # ...and commits anything already on disk
    while not _stop.is_set():
        NUDGE.wait(timeout=_poll_interval())
        NUDGE.clear()
        if _stop.is_set():
            break
        try:
            _tick()
            _ok("loop")
        except Exception as exc:        # noqa: BLE001 — the committer must never die
            # commit_once and push record their own failures; this is anything else a pass
            # raised (a ref read, the remote-tracking check), recorded and printed the same way.
            _fail("loop", ("%s: %s" % (type(exc).__name__, exc))[:200])
            time.sleep(1.0)


def running():
    return _thread is not None and _thread.is_alive()


def start():
    """Start the committer thread; a no-op (None) when dark, so callers needn't check."""
    global _thread
    if not enabled() or running():
        return None
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="review-sync", daemon=True)
    _thread.start()
    return _thread


def stop():
    global _thread
    _stop.set()
    NUDGE.set()
    if _thread is not None:
        _thread.join(timeout=5.0)
    _thread = None


def status():
    return {"enabled": enabled(), "repo": repo_dir(), "branch": branch(),
            "remote": remote(), "last_commit": _last_commit,
            "unpushed": has_unpushed() if in_git_repo() else False,
            "last_ok": dict(_last_ok), "errors": dict(_last_error)}


# --- CLI (`make review-sync` / `make review-sync-postmerge`) ------------------------------------

def _cmd_sync(dry_run):
    """The `make review-sync` trigger: final commit → push → open/refresh ONE squash PR in this
    repository → print its URL and STOP. `make review-sync-automerge` then merges a
    rulings-only PR (auto_merge); any other PR the user merges."""
    sha = commit_once(dry_run=dry_run)
    if sha is None and "commit" in _last_error:
        print("review-sync: commit FAILED — %s" % _last_error["commit"]["detail"])
        return 1
    print("review-sync: %s" % ("committed " + sha if sha else "no change to commit"))
    if not push(dry_run=dry_run):
        if "push" in _last_error:
            print("review-sync: push FAILED — %s (branch left local; re-run when online)"
                  % _last_error["push"]["detail"])
            return 1
        print("review-sync: nothing to push (no origin, or already current)")
    else:
        print("review-sync: pushed %s to %s" % (branch(), remote()))
    ahead = commits_ahead()
    if dry_run:
        # `commit_once(dry_run=True)` deliberately does not move the branch ref, so the count
        # above still reflects the state BEFORE the commit this run would have made. Reporting
        # the count as-is made a dry run say "would commit …" and "converged, no PR needed" in
        # the same breath, while the identical real run opens a PR. Answer for the real run.
        if sha:
            print("review-sync: (dry-run) would commit, then open/refresh the %s → %s PR"
                  % (branch(), base_branch()))
        elif ahead == 0:
            print("review-sync: (dry-run) converged — %s has no commits %s does not; no PR needed"
                  % (branch(), base_branch()))
        else:
            print("review-sync: (dry-run) would open/refresh the %s → %s PR"
                  % (branch(), base_branch()))
        return 0
    if ahead == 0:
        # Converged, not broken: `gh pr create` REFUSES a PR with no commits between the
        # branches, and that refusal used to surface as a failed sync that masked the
        # other steps' success. Nothing to sync is a successful outcome.
        print("review-sync: converged — %s has no commits %s does not; no PR needed"
              % (branch(), base_branch()))
        return 0
    try:
        url, created = open_or_refresh_pr()
    except Exception as exc:            # noqa: BLE001
        if "no commits between" in str(exc).lower():
            # The same converged case the count above catches, reached when the local
            # `<remote>/<base>` ref is stale: GitHub compared the real tips and found nothing.
            print("review-sync: converged — %s has no commits %s does not; no PR needed"
                  % (branch(), base_branch()))
            return 0
        print("review-sync: could not open the PR — %s" % (str(exc)[:200]))
        return 1
    print("review-sync: %s PR: %s" % ("opened" if created else "existing", url))
    print("review-sync: make review-sync-automerge merges it if it changes only "
          "data/rulings.json; otherwise merge it by hand (squash), then: %s"
          % postmerge_command())
    return 0


def main(argv=None):
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    dry_run = "--dry-run" in argv
    argv = [a for a in argv if a != "--dry-run"]
    cmd = argv[0] if argv else "status"
    if cmd in ("commit", "push", "sync", "post-merge", "auto-merge") and not enabled():
        # every mutating verb refuses loudly when dark — the kill switch and the
        # strict-root guard hold for `make review-sync` exactly as for the thread
        print("review-sync: dark — %s" % _dark_reason())
        return 1
    if cmd == "commit":
        sha = commit_once(dry_run=dry_run)
        print(sha or "no change")
        return 0
    if cmd == "push":
        print("pushed" if push(dry_run=dry_run) else "not pushed")
        return 0
    if cmd == "sync":
        global ECHO
        echo, ECHO = ECHO, False         # sync prints each failure itself, once
        try:
            return _cmd_sync(dry_run)
        finally:
            ECHO = echo
    if cmd == "post-merge":
        try:
            print("review-sync: " + post_merge(dry_run=dry_run))
            return 0
        except Exception as exc:            # noqa: BLE001
            print("review-sync: post-merge FAILED — %s" % (str(exc)[:200]))
            return 1
    if cmd == "auto-merge":
        try:
            print("review-sync: " + auto_merge(dry_run=dry_run))
            return 0
        except Exception as exc:            # noqa: BLE001
            print("review-sync: auto-merge FAILED — %s" % (str(exc)[:200]))
            return 1
    if cmd == "status":
        print(json.dumps(status(), indent=2))
        return 0
    print("usage: review_sync.py [commit|push|sync|post-merge|auto-merge|status] [--dry-run]")
    return 2


if __name__ == "__main__":
    import sys
    # the checkout's .env (NETRADIO_REVIEW_SYNC_*), read the way the align server reads it;
    # here and not in main(), so a test calling main() never loads a real .env
    from align_server import load_env
    load_env()
    sys.exit(main())
