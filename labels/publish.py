#!/usr/bin/python3
"""publish.py — one-command, hard-gated publish of hand labels (ROADMAP G5 / A4).

Takes valid edited `<stem>.labels.tsv` files all the way to an open pull request with no
manual sub-steps: validate → sort → branch → commit → push → open PR. The whole thing is
**all-or-nothing**: if any target fails validation, NOTHING is committed or pushed.

`main` is PR-only (branch protection; a direct push is rejected with GH013), so publish no
longer commits on your branch and pushes `main`. Instead it proposes the sorted labels on a
fresh `labels/publish-YYYYMMDD-HHMMSS` branch cut from `origin/main`, pushes THAT branch, and
opens a PR with `gh pr create`. It **never pushes to main** and **never merges** — a human
reviews and merges the PR. If `gh` is missing (or the PR can't be opened), the branch is still
pushed and publish prints the exact compare URL to open the PR by hand.

The commit/push/PR all happen in a **throwaway git worktree** under `.worktree/` (the same
pattern `scripts/tracklist_sync.sh` uses), so the **invoking checkout is left untouched** —
its branch, index, and (except for the in-place sort, which is the tool's normal job and is
what the PR proposes) working tree are unchanged. That makes publish safe to run from the live
`main` checkout the harvester supervisor runs out of.

The hard gate (Proposal D) refuses to publish on:
  * bad-syntax labels — any row `sort_tsv.py` reports as unrecognized grammar;
  * incomplete / unverified tracks — a sync line missing its `verified` tag, a `file end`
    missing `COMPLETE`, a missing `LABELTRACK` marker (when the file uses them), or a file
    with no start-sync / no `file end … COMPLETE` at all (e.g. a half-labelled tail capture);
  * seed/engine files — `*.starter.labels.tsv` (seed-only) and `*.auto.labels.tsv` (engine)
    are never hand-published.
On any of these it exits non-zero, names the offending file/line, and pushes nothing.

The gate REUSES `sort_tsv.py --test` (which already flags grammar/verified/COMPLETE/LABELTRACK
issues) instead of re-implementing the grammar, plus a completeness check for the required
start-sync + `file end … COMPLETE` anchors.

Sheet refresh: the sheet imports from `origin/main`, so the labels are NOT on main until the
PR is merged — firing the refresh at publish time would refresh nothing. Publish therefore
DEFERS the refresh and prints the post-merge reminder. After the PR is merged, run
`publish.py --refresh-only` (POSTs `NETRADIO_SHEET_WEBHOOK`, whose `doPost` calls
`GithubImport()`; if unset, prints the manual "Reload Data" reminder).

Stdlib only. Build/test here; never run against prod from CI.
"""

import argparse
import datetime
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SORT_TSV = os.path.join(HERE, "sort_tsv.py")

# stderr lines from sort_tsv.py that mean "do not publish"
_GATE_SIGNAL = re.compile(r"^\s*(WARNING|NOTICE|ERROR|Warning):")

_PR_BODY = ("Automated label publish (labels/publish.py): validated by the hard gate "
            "(sort_tsv.py --test) and sorted.\n\n"
            "After merging, run `python3 labels/publish.py --refresh-only` to refresh the "
            "sheet from origin/main (or click 'Reload Data').")


def _stem(path):
    base = os.path.basename(path)
    for suffix in (".starter.labels.tsv", ".auto.labels.tsv", ".labels.tsv", ".labels.txt"):
        if base.endswith(suffix):
            return base[: -len(suffix)]
    return os.path.splitext(base)[0]


def resolve_target(arg, labels_dir):
    """Map a stem or path to its label file.

    A path (or explicit `.txt`/`.tsv`) is used as-is. A bare stem prefers a freshly
    exported `<stem>.labels.txt` (the Audacity export — `sort_tsv.py` will convert it)
    when present, else the existing `<stem>.labels.tsv`.
    """
    if os.sep in arg or arg.endswith(".tsv") or arg.endswith(".txt"):
        return arg if os.path.isabs(arg) else os.path.join(os.getcwd(), arg)
    txt = os.path.join(labels_dir, arg + ".labels.txt")
    if os.path.isfile(txt):
        return txt
    return os.path.join(labels_dir, arg + ".labels.tsv")


def published_path(path):
    """The on-disk path after `sort_tsv.py` runs: a `.txt` export is renamed to `.tsv`."""
    return path[:-3] + "tsv" if path.endswith("txt") else path


def _completeness_errors(path, text):
    """Block files lacking the anchors that mark a hand-verified, complete capture."""
    stem = _stem(path)
    errors = []
    if not re.search(r"(?im)^\S+\s+\S+\s+file (?:start )?sync:.*%s" % re.escape(stem), text):
        errors.append("%s: no `file [start] sync: %s…` anchor — track not positioned"
                      % (os.path.basename(path), stem))
    if not re.search(r"(?im)file end:\s*%s.*COMPLETE" % re.escape(stem), text):
        errors.append("%s: no `file end: %s.wav … COMPLETE` — capture not finished"
                      % (os.path.basename(path), stem))
    return errors


def gate_errors(path, python=None):
    """Return the list of gate failures for one target ([] => publishable)."""
    name = os.path.basename(path)
    if name.endswith(".starter.labels.tsv"):
        return ["%s: starter files are seed-only and are never published" % name]
    if name.endswith(".auto.labels.tsv"):
        return ["%s: engine (.auto) files are never hand-published" % name]
    if not os.path.isfile(path):
        return ["%s: file not found" % path]

    errors = []
    # (a) reuse sort_tsv.py --test: any WARNING/NOTICE/ERROR is a gate failure
    proc = subprocess.run([python or sys.executable, SORT_TSV, path, "--test"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                          text=True, check=False)
    for line in proc.stderr.splitlines():
        if _GATE_SIGNAL.match(line):
            errors.append("%s: %s" % (name, line.strip()))

    # (b) completeness anchors
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        errors.extend(_completeness_errors(path, handle.read()))
    return errors


def validate_targets(paths, python=None):
    """{path: [errors]} for every target (all-or-nothing decision input)."""
    return {p: gate_errors(p, python=python) for p in paths}


def _run(cmd, dry_run, cwd=None):
    """The mutation seam: print, then run (unless dry-run). Tests monkeypatch this so no
    real push/PR ever happens. Read-only queries go through `_capture`, not here."""
    print("$ " + " ".join(cmd))
    if dry_run:
        return 0
    return subprocess.run(cmd, cwd=cwd, check=False).returncode


def _capture(cmd, cwd=None):
    """Run a read-only git query and return its stdout (empty string on any failure)."""
    try:
        proc = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, text=True, check=False)
    except OSError:
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _branch_name(now=None):
    """A fresh publish branch: labels/publish-YYYYMMDD-HHMMSS (UTC)."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return "labels/publish-" + now.strftime("%Y%m%d-%H%M%S")


def _unique_branch(name, root):
    """`name`, or `name-2`/`name-3`/… — whichever first claims NO existing local or remote ref.

    Second-resolution timestamps are not unique on their own (a rapid retry, or a leftover
    branch from an interrupted run), and colliding would reset-and-delete a branch this run
    does not own. Both checks are read-only; the remote check degrades to local-only offline
    (ls-remote returns nothing — the push would fail later anyway)."""
    candidate, n = name, 1
    while (_capture(["git", "rev-parse", "--verify", "refs/heads/" + candidate], cwd=root)
           or _capture(["git", "ls-remote", "--heads", "origin", candidate], cwd=root)):
        n += 1
        candidate = "%s-%d" % (name, n)
    return candidate


def _repo_root_for(staged, dry_run):
    """The git repo the sorted files live in (the invoking checkout). Falls back to this
    script's own repo, and — only for dry-run display — to the file's own directory."""
    for cwd in (os.path.dirname(os.path.abspath(staged[0])), HERE):
        root = _capture(["git", "rev-parse", "--show-toplevel"], cwd=cwd)
        if root:
            return root
    return os.path.dirname(os.path.abspath(staged[0])) if dry_run else None


def _github_slug(root):
    """`owner/repo` parsed from origin's URL (https or ssh), or "" if not GitHub."""
    url = _capture(["git", "remote", "get-url", "origin"], cwd=root)
    m = re.search(r"github\.com[:/]+([^/]+/[^/]+?)(?:\.git)?/?$", url)
    return m.group(1) if m else ""


def _print_compare_url(branch, root):
    """The exact 'open this PR by hand' instructions when gh can't."""
    slug = _github_slug(root)
    if slug:
        print("  open a PR:  https://github.com/%s/compare/main...%s?expand=1"
              % (slug, branch))
    else:
        print("  origin has no GitHub URL — open a PR from branch %s onto main by hand"
              % branch)
    print("  (base: main   head: %s)" % branch)


def _open_pr_request(branch, message, dry_run, cwd, root):
    """Open the PR with gh; on gh-absent/failure, print the compare URL and instructions.

    Returns True if `gh pr create` succeeded, False if we fell back to manual instructions.
    The branch is already pushed either way, so a False return is NOT a publish failure.
    """
    title = message.splitlines()[0]
    if shutil.which("gh") is not None:
        rc = _run(["gh", "pr", "create", "--base", "main", "--head", branch,
                   "--title", title, "--body", _PR_BODY], dry_run, cwd=cwd)
        if rc == 0:
            return True
        sys.stderr.write("gh pr create failed — the branch is pushed; open the PR by hand:\n")
    else:
        print("NOTE: `gh` not found — the branch is pushed; open the PR by hand:")
    _print_compare_url(branch, root)
    return False


def _open_pr(staged, branch, message, dry_run):
    """Propose the sorted `staged` files on `branch` via a throwaway worktree off origin/main,
    then push the branch and open a PR to main. The invoking checkout is never touched (no
    checkout/add/commit happens in it). Returns exit code (0 => branch pushed + PR handled)."""
    root = _repo_root_for(staged, dry_run)
    if not root:
        sys.stderr.write("publish: the labels are not inside a git repo — cannot open a PR\n")
        return 1

    wtdir = os.path.join(root, ".worktree")
    if not dry_run:
        os.makedirs(wtdir, exist_ok=True)
        wt = tempfile.mkdtemp(prefix="publish.", dir=wtdir)
    else:
        wt = os.path.join(wtdir, "publish-DRYRUN")

    # Base the branch on the freshest main; a failed fetch is non-fatal (fall back to the
    # last-known origin/main — the PR may then need a rebase, which the reviewer handles).
    if _run(["git", "fetch", "origin", "main"], dry_run, cwd=root) != 0:
        sys.stderr.write("publish: could not fetch origin/main (using last-known ref)\n")

    # `-b`, not `-B`: the caller made the name unique, so an existing branch here is a true
    # race — fail loudly rather than reset (and later delete) a branch this run does not own.
    if _run(["git", "worktree", "add", "-q", "-b", branch, wt, "origin/main"],
            dry_run, cwd=root) != 0:
        sys.stderr.write("publish: could not create the publish worktree — nothing pushed\n")
        return 1

    try:
        rels = []
        for src in staged:
            rel = os.path.relpath(os.path.abspath(src), root)
            rels.append(rel)
            if not dry_run:
                dst = os.path.join(wt, rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copyfile(src, dst)
        if _run(["git", "add"] + rels, dry_run, cwd=wt) != 0:
            return 1
        if _run(["git", "commit", "-m", message], dry_run, cwd=wt) != 0:
            sys.stderr.write("git commit failed (nothing to commit?) — nothing pushed\n")
            return 1
        if _run(["git", "push", "-u", "origin", branch], dry_run, cwd=wt) != 0:
            sys.stderr.write("git push failed — branch not on the remote; nothing to PR\n")
            return 1
        _open_pr_request(branch, message, dry_run, cwd=wt, root=root)
        return 0
    finally:
        # Tidy the throwaway worktree + local branch; the pushed remote branch stays for the PR.
        _run(["git", "worktree", "remove", "--force", wt], dry_run, cwd=root)
        _run(["git", "worktree", "prune"], dry_run, cwd=root)
        _run(["git", "branch", "-D", branch], dry_run, cwd=root)


def trigger_refresh(dry_run=False):
    """POST to the Apps Script Web App if configured, else print the manual reminder."""
    url = os.environ.get("NETRADIO_SHEET_WEBHOOK")
    if not url:
        print("NOTE: NETRADIO_SHEET_WEBHOOK unset — click 'Reload Data' on the File Analysis "
              "tab to refresh the sheet (deploy Code.js as a Web App + set the URL to automate).")
        return False
    print("POST %s  (triggers GithubImport via doPost)" % url)
    if dry_run:
        return True
    import urllib.request
    req = urllib.request.Request(url, data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 (trusted URL from env)
        print("sheet refresh: HTTP %s" % getattr(resp, "status", resp.getcode()))
    return True


def _print_refresh_deferred():
    """The sheet imports from origin/main, so it can't refresh until the PR is merged."""
    print("NOTE: the labels are in a PR, not yet on main — the sheet imports from origin/main, "
          "so it is NOT refreshed now. After the PR is MERGED, run:")
    print("    python3 labels/publish.py --refresh-only")
    print("  (or click 'Reload Data' on the sheet).")


def publish(paths, message, dry_run=False, refresh=True, python=None, branch=None):
    """Validate ALL targets, then (if clean) sort → branch → commit → push → open PR.

    The sort runs in place in the invoking checkout (so local state matches what the PR
    proposes, as before), but the commit/push/PR happen in a throwaway worktree — the
    invoking checkout's branch and index are untouched and it is never pushed to main.
    `refresh` controls only whether the post-merge refresh reminder is printed (the refresh
    itself is always deferred — see `--refresh-only`). Returns exit code.
    """
    results = validate_targets(paths, python=python)
    failures = {p: errs for p, errs in results.items() if errs}
    if failures:
        sys.stderr.write("REFUSING TO PUBLISH — %d of %d file(s) failed the gate "
                         "(nothing pushed):\n" % (len(failures), len(paths)))
        for p in paths:
            for err in results[p]:
                sys.stderr.write("  %s\n" % err)
        return 1

    # all valid: sort each (a .txt export is converted to .tsv here) in the invoking tree,
    # then propose the POST-conversion .tsv paths on a fresh branch via a worktree.
    # `--no-next`: publish is non-interactive with no terminal, so sort_tsv must not try to prep
    # the next file (its TTY guard would skip it anyway; this is explicit belt-and-braces, and
    # keeps publish from spending time on a hints run mid-publish).
    py = python or sys.executable
    staged = [published_path(p) for p in paths]
    for p in paths:
        if _run([py, SORT_TSV, p, "--no-next"], dry_run) != 0:
            sys.stderr.write("sort_tsv failed on %s — aborting before the PR\n" % p)
            return 1

    branch = branch or _branch_name()
    root = _repo_root_for(staged, dry_run)
    if root:
        # second-resolution names collide on rapid retries / leftover branches; a collision
        # would have the worktree machinery reset-and-delete a branch this run does not own
        branch = _unique_branch(branch, root)
    rc = _open_pr(staged, branch, message, dry_run)
    if rc != 0:
        return rc
    if refresh:
        _print_refresh_deferred()
    print("published %d file(s) on branch %s — merge the PR to land them on main."
          % (len(paths), branch))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("targets", nargs="*", help="stems or <stem>.labels.tsv paths to publish")
    parser.add_argument("--labels-dir", default=HERE, help="dir holding the .labels.tsv files")
    parser.add_argument("-m", "--message", default=None, help="commit message")
    parser.add_argument("--dry-run", action="store_true",
                        help="validate and show what would happen; no write/commit/push/PR/refresh")
    parser.add_argument("--no-refresh", action="store_true",
                        help="skip printing the post-merge refresh reminder")
    parser.add_argument("--refresh-only", action="store_true",
                        help="fire ONLY the sheet refresh (run this AFTER the publish PR is merged)")
    parser.add_argument("--check", action="store_true",
                        help="run only the gate (validate); exit non-zero if any target fails")
    args = parser.parse_args(argv)

    if args.refresh_only:
        if args.targets:
            parser.error("--refresh-only takes no targets (it only refreshes the sheet)")
        trigger_refresh(dry_run=args.dry_run)
        return 0

    if not args.targets:
        parser.error("no targets given (or pass --refresh-only to refresh after a merge)")

    paths = [resolve_target(t, args.labels_dir) for t in args.targets]

    if args.check:
        results = validate_targets(paths)
        ok = True
        for p in paths:
            if results[p]:
                ok = False
                for err in results[p]:
                    sys.stderr.write("  %s\n" % err)
            else:
                print("OK   %s" % os.path.basename(p))
        return 0 if ok else 1

    message = args.message or ("labels: publish " + ", ".join(_stem(p) for p in paths))
    return publish(paths, message, dry_run=args.dry_run, refresh=not args.no_refresh)


if __name__ == "__main__":
    sys.exit(main())
