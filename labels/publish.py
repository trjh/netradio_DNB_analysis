#!/usr/bin/python3
"""publish.py — one-command, hard-gated publish of hand labels (ROADMAP G5 / A4).

Takes valid edited `<stem>.labels.tsv` files all the way to a refreshed sheet with no
manual sub-steps: validate → sort → commit → push → trigger the sheet refresh. The whole
thing is **all-or-nothing**: if any target fails validation, NOTHING is pushed.

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

Sheet refresh: if `NETRADIO_SHEET_WEBHOOK` (the Apps Script Web App URL, whose `doPost` calls
`GithubImport()`) is set, POST to it; otherwise print the manual "Reload Data" reminder.

Stdlib only. Build/test here; never run against prod from CI.
"""

import argparse
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SORT_TSV = os.path.join(HERE, "sort_tsv.py")

# stderr lines from sort_tsv.py that mean "do not publish"
_GATE_SIGNAL = re.compile(r"^\s*(WARNING|NOTICE|ERROR|Warning):")


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
    print("$ " + " ".join(cmd))
    if dry_run:
        return 0
    return subprocess.run(cmd, cwd=cwd, check=False).returncode


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


def publish(paths, message, dry_run=False, refresh=True, python=None):
    """Validate ALL targets, then (if clean) sort → commit → push → refresh. Returns exit code."""
    results = validate_targets(paths, python=python)
    failures = {p: errs for p, errs in results.items() if errs}
    if failures:
        sys.stderr.write("REFUSING TO PUBLISH — %d of %d file(s) failed the gate "
                         "(nothing pushed):\n" % (len(failures), len(paths)))
        for p in paths:
            for err in results[p]:
                sys.stderr.write("  %s\n" % err)
        return 1

    # all valid: sort each (a .txt export is converted to .tsv here), then commit + push.
    # Stage the POST-conversion .tsv paths, since sort_tsv renames any .txt away.
    py = python or sys.executable
    staged = [published_path(p) for p in paths]
    for p in paths:
        if _run([py, SORT_TSV, p], dry_run) != 0:
            sys.stderr.write("sort_tsv failed on %s — aborting before push\n" % p)
            return 1
    if _run(["git", "add"] + staged, dry_run, cwd=HERE) != 0:
        return 1
    if _run(["git", "commit", "-m", message], dry_run, cwd=HERE) != 0:
        sys.stderr.write("git commit failed (nothing to commit?) — not pushing\n")
        return 1
    if _run(["git", "push"], dry_run, cwd=HERE) != 0:
        return 1
    if refresh:
        trigger_refresh(dry_run)
    print("published %d file(s)." % len(paths))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("targets", nargs="+", help="stems or <stem>.labels.tsv paths to publish")
    parser.add_argument("--labels-dir", default=HERE, help="dir holding the .labels.tsv files")
    parser.add_argument("-m", "--message", default=None, help="commit message")
    parser.add_argument("--dry-run", action="store_true",
                        help="validate and show what would happen; no write/commit/push/refresh")
    parser.add_argument("--no-refresh", action="store_true", help="skip the sheet refresh step")
    parser.add_argument("--check", action="store_true",
                        help="run only the gate (validate); exit non-zero if any target fails")
    args = parser.parse_args(argv)

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
