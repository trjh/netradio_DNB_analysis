#!/usr/bin/env python3
"""`make env-check`: the NETRADIO_* variables this repo reads, against `.env.example` and `.env`.

    python3 scripts/env_check.py                 # this repo's .env.example and .env
    python3 scripts/env_check.py --example-only  # .env.example alone (what CI runs)
    python3 scripts/env_check.py --repo /path    # another checkout (the tests use a scratch one)

Two reports, and a non-zero exit on either (PLAN_data_tiering.md §2.6):

  stale    a name set in `.env` (or listed in `.env.example`) that no code reads: a variable
           the code retired, or a typo that silently does nothing.
  missing  a name the code reads that `.env.example` does not carry, so a new machine cannot
           learn it exists.

"Reads" is a grep at run time over the repo's `.py`, `.sh` and `Makefile` sources (tests,
`Archive/` and `.worktree/` excluded) for `NETRADIO_[A-Z0-9_]+`, skipping whole-line comments.
A name in a docstring or an inline comment still counts as read; the check is against stale
names, not a proof of use. The cache registry's
family, `NETRADIO_<NAME>_CACHE_{GB,HEADROOM_MB,MAX_AGE_DAYS,DIR}`, is built from a pattern in
`cache_budget.py`, so every member is expanded from `cache_budget.NAMES` rather than grepped.

The same file lives in both repos; it finds `cache_budget.py` at the repo root (player) or under
`scripts/` (analysis).
"""

import argparse
import os
import re
import sys

NAME_RE = re.compile(r"NETRADIO_[A-Z0-9_]+")
SOURCE_SUFFIXES = (".py", ".sh")
SOURCE_NAMES = ("Makefile",)
EXCLUDED_DIRS = {"Archive", ".worktree", "tests", ".venv", ".git", "__pycache__", "tmp",
                 "tmp_o", "tmp_t", "tmp_openclaw", "node_modules"}
# Sources that QUOTE variable names without reading them: a drawing script whose figure labels
# carry a plan's names. Relative to the repo root.
EXCLUDED_FILES = {"scripts/draw_fetch_split.py"}
CACHE_FIELDS = ("GB", "HEADROOM_MB", "MAX_AGE_DAYS", "DIR")


def repo_root(explicit=None):
    if explicit:
        return os.path.abspath(explicit)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _cache_names(root):
    """cache_budget.NAMES from this repo's copy of the module, without importing it."""
    for cand in (os.path.join(root, "cache_budget.py"), os.path.join(root, "scripts", "cache_budget.py")):
        try:
            with open(cand, encoding="utf-8") as fh:
                src = fh.read()
        except OSError:
            continue
        m = re.search(r"^NAMES\s*=\s*\((.*?)\)", src, re.S | re.M)
        if m:
            return re.findall(r'"([a-z0-9_]+)"', m.group(1))
    return []


def names_read(root):
    """Every NETRADIO_* name the repo's sources mention, plus the cache family expanded."""
    found = set()
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS and not d.startswith(".")]
        for f in files:
            if not (f.endswith(SOURCE_SUFFIXES) or f in SOURCE_NAMES):
                continue
            if os.path.relpath(os.path.join(dirpath, f), root) in EXCLUDED_FILES:
                continue
            try:
                with open(os.path.join(dirpath, f), encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                continue
            for line in text.splitlines():
                if line.lstrip().startswith("#"):     # a comment names things it does not read
                    continue
                for m in NAME_RE.findall(line):
                    if not m.endswith("_"):           # `NETRADIO_%s_CACHE_GB` leaves a fragment
                        found.add(m)
    for name in _cache_names(root):
        for field in CACHE_FIELDS:
            found.add("NETRADIO_%s_CACHE_%s" % (name.upper(), field))
    return found


def names_in_file(path):
    """The names an env file sets or lists: `VAR=`, `#VAR=`, `# VAR=` lines."""
    out = set()
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                m = re.match(r"^\s*#?\s*(NETRADIO_[A-Z0-9_]+)\s*=", line)
                if m:
                    out.add(m.group(1))
    except OSError:
        pass
    return out


def check(root, example_only=False):
    """{"stale_env", "stale_example", "missing", "read"}; the lists sorted."""
    read = names_read(root)
    example = names_in_file(os.path.join(root, ".env.example"))
    env = set() if example_only else names_in_file(os.path.join(root, ".env"))
    return {"read": sorted(read),
            "stale_env": sorted(env - read),
            "stale_example": sorted(example - read),
            "missing": sorted(read - example)}


def main(argv=None):
    ap = argparse.ArgumentParser(description="NETRADIO_* variables: read by the code vs .env.example/.env")
    ap.add_argument("--repo", help="the checkout to check (default: this one)")
    ap.add_argument("--example-only", action="store_true", help="ignore .env (CI)")
    args = ap.parse_args(argv)
    root = repo_root(args.repo)
    r = check(root, example_only=args.example_only)
    print("env-check: %s" % root)
    print("  %d NETRADIO_* names read by the code" % len(r["read"]))
    bad = False
    if r["stale_env"]:
        bad = True
        print("  STALE in .env (set, read by nothing): " + ", ".join(r["stale_env"]))
    if r["stale_example"]:
        bad = True
        print("  STALE in .env.example (listed, read by nothing): " + ", ".join(r["stale_example"]))
    if r["missing"]:
        bad = True
        print("  MISSING from .env.example (read, not listed): " + ", ".join(r["missing"]))
    if not bad:
        print("  ok: no stale names, every read name is in .env.example")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
