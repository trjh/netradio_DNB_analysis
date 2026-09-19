#!/usr/bin/env python3
"""Check a `.env` file against the variables the code reads.

    python3 scripts/env_check.py [.env]        # make env-check

Reports two lists:

  * **set but unread** -- every `NETRADIO_*` name the file sets that no code reads: a stale
    name, a retired rule's variable, or a typo. These make the check fail (exit 1).
  * **read but unset** -- every `NETRADIO_*` name the code reads that the file leaves unset, with
    the default the code falls back to where the code states one as a literal. Information
    only. For a cache's family the default shown is what the registration sets -- the cap
    and age limit the registering code passes, which a bare name cannot state; caches whose
    registration departs from the generic cap (4 GB) or age (none) are named in the tool's
    own table so the list states the default the code really uses. The wrapper and test
    hand-off names (`INTERNAL`) are left out of this list; they still count as read.

Only names are printed, never values: a `.env` holds credentials and machine paths.

"Read by the code" is found by scanning the repo's Python, shell and Makefile sources (never
`tests/`, `Archive/` or a worktree): in Python, a string literal that is exactly a
`NETRADIO_*` name; elsewhere, a name on a line that is not a comment. A name, a default or a
cache registration mentioned only in prose (a docstring, a comment) is not read. To those the
scan adds the cache family `NETRADIO_<NAME>_CACHE_{GB,HEADROOM_MB,MAX_AGE_DAYS,DIR}` for every
cache the code registers (`cache_budget.register("<name>", ...)`). A file ending `.example`
counts a commented-out `# NETRADIO_X=` line as set, because an example documents a name even
where it leaves it off.
"""

import ast
import io
import os
import re
import sys
import tokenize

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIRS = {".git", ".worktree", ".venv", "Archive", "tests", "node_modules", "__pycache__",
             ".context"}
SOURCE_EXT = (".py", ".sh", ".swift")
# Names the code reads that are no `.env` setting -- hand-offs a wrapper or a test sets for
# itself -- so they never belong in the informational list. They still count as read: a `.env`
# that sets one is not flagged, because the code really does read it.
#   NETRADIO_TRACKLIST_SYNC_REEXEC   the sync script's re-exec guard, set by itself
#   NETRADIO_ANALYSIS_REPO           set by the Makefile's sync recipes (always this checkout)
INTERNAL = ("NETRADIO_TRACKLIST_SYNC_REEXEC", "NETRADIO_ANALYSIS_REPO")
CACHE_SUFFIXES = {"GB": "the cap the registration sets (4 GB where it sets none)",
                  "HEADROOM_MB": "the headroom the registration sets (0 unless it sets one)",
                  "MAX_AGE_DAYS": "the age limit the registration sets (none unless it sets one)",
                  "DIR": "the directory the registration names "
                         "($NETRADIO_CACHE_ROOT/<name> where it names none)"}
# What each registered cache's own settings are, where they depart from the policy's generic
# cap (4 GB) or age (none). The code scan cannot evaluate a registration's arguments (they
# are expressions, not literals), so the caches this repo registers are named here with the
# default the code really falls back to -- the registration's own. Keep it in step with the
# registrations; the generic text above covers every cache not named.
CACHE_SETTINGS = {
    "chroma": {"MAX_AGE_DAYS": "14 days, the registration's age limit"},
    "candidates": {"GB": "0.25 GB / 250 MB, the registration's cap",
                   "MAX_AGE_DAYS": "30 days, the registration's age limit"},
    "stream_tracks": {"GB": "2 GB, the registration's cap",
                      "MAX_AGE_DAYS": "14 days, the registration's age limit"},
}

NAME = re.compile(r"\bNETRADIO_[A-Z0-9_]*[A-Z0-9]\b")
WHOLE_NAME = re.compile(r"^NETRADIO_[A-Z0-9_]*[A-Z0-9]$")
DEFAULT = re.compile(r"""["'](NETRADIO_[A-Z0-9_]+)["']\s*,\s*("[^"\n]*"|'[^'\n]*'|-?[0-9][0-9.]*)""")
SHELL_DEFAULT = re.compile(r"\$\{(NETRADIO_[A-Z0-9_]+):-([^}\n]*)\}")
REGISTER = re.compile(r"""cache_budget\.register\(\s*["']([a-z_]+)["']""")
SET_LINE = re.compile(r"^\s*(?:export\s+)?(NETRADIO_[A-Z0-9_]+)\s*=")
EXAMPLE_LINE = re.compile(r"^\s*#?\s*(?:export\s+)?(NETRADIO_[A-Z0-9_]+)=")


def sources(root=None):
    """Every source file whose `NETRADIO_*` names count as read."""
    root = root or ROOT
    me = os.path.abspath(__file__)
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for fname in sorted(files):
            path = os.path.join(dirpath, fname)
            if os.path.abspath(path) == me:
                continue
            if fname.endswith(SOURCE_EXT) or fname == "Makefile":
                yield path


def _code_lines(path, text):
    """The file's lines with the prose blanked out: comment lines and `#` tails everywhere,
    docstrings in Python too. The default and register regexes read only what is left, so a
    name, a default or a registration stated in prose never counts."""
    lines = text.splitlines()
    if not path.endswith(".py"):
        return [line.split(" #")[0] for line in lines
                if not line.lstrip().startswith(("#", "//"))]
    try:
        tree = ast.parse(text)
        toks = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (SyntaxError, IndentationError, tokenize.TokenError):
        return lines
    blank = {}                                   # line number -> [(col, col) to blank]
    for tok in toks:
        if tok.type == tokenize.COMMENT:
            blank.setdefault(tok.start[0], []).append((tok.start[1], tok.end[1]))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        first = node.body[0] if node.body else None
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            blank.setdefault(first.lineno, []).append((first.col_offset, first.end_col_offset))
            for i in range(first.lineno + 1, first.end_lineno + 1):
                blank.setdefault(i, []).append((0, len(lines[i - 1])))
    out = []
    for i, line in enumerate(lines, 1):
        for start, end in blank.get(i, []):
            line = line[:start] + " " * max(0, end - start) + line[end:]
        out.append(line)
    return out


def _names_in(path, text):
    """The `NETRADIO_*` names a source file reads, leaving out prose that only mentions one."""
    if path.endswith(".py"):
        text = "\n".join(_code_lines(path, text))
        out = set()
        try:
            for tok in tokenize.generate_tokens(io.StringIO(text).readline):
                if tok.type != tokenize.STRING:
                    continue
                try:
                    value = ast.literal_eval(tok.string)
                except (ValueError, SyntaxError):
                    continue            # an f-string: no whole name in it
                if isinstance(value, str) and WHOLE_NAME.match(value):
                    out.add(value)
        except (tokenize.TokenError, SyntaxError):
            return set(NAME.findall(text))
        return out
    out = set()
    for line in _code_lines(path, text):
        out.update(NAME.findall(line))
    return out


def read_names(root=None):
    """{name: default or None} for every `NETRADIO_*` name the code reads."""
    names, caches = {}, set()
    for path in sources(root):
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except (OSError, UnicodeDecodeError):
            continue
        for name in _names_in(path, text):
            names.setdefault(name, None)
        code = "\n".join(_code_lines(path, text))
        for rx in (DEFAULT, SHELL_DEFAULT):
            for name, value in rx.findall(code):
                value = value.strip("\"'")
                if WHOLE_NAME.match(value):
                    continue            # two names side by side in a list, not a default
                # A default only ever attaches to a name the code READS: `# was: get("X", "5")`
                # in prose must not make X read, nor lend it a default.
                if name in names and names[name] is None and value:
                    names[name] = value
        caches.update(REGISTER.findall(code))
    for cache in caches:
        for suffix, default in CACHE_SUFFIXES.items():
            name = "NETRADIO_%s_CACHE_%s" % (cache.upper(), suffix)
            if names.get(name) is None:
                names[name] = CACHE_SETTINGS.get(cache, {}).get(suffix, default).replace("<name>", cache)
    return names


def env_value(env_file, name):
    """The raw value the file gives `name`, or ""."""
    line = EXAMPLE_LINE if env_file.endswith(".example") else SET_LINE
    try:
        with open(env_file, encoding="utf-8") as fh:
            for text in fh:
                m = line.match(text)
                if m and m.group(1) == name:
                    return text[m.end():].split(" #")[0].strip().strip("\"'")
    except OSError:
        pass
    return ""


def set_names(env_file):
    """The `NETRADIO_*` names the file sets (a `.example` file: also the commented-out ones)."""
    line = EXAMPLE_LINE if env_file.endswith(".example") else SET_LINE
    out = set()
    with open(env_file, encoding="utf-8") as fh:
        for text in fh:
            m = line.match(text)
            if m:
                out.add(m.group(1))
    return out


def check(env_file, root=None):
    """(set but unread, [(read but unset, default)])."""
    read = read_names(root)
    have = set_names(env_file)
    unread = sorted(have - set(read))
    unset = sorted((n, read[n]) for n in set(read) - have
                   if n not in INTERNAL)
    return unread, unset


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    env_file = argv[0] if argv else os.path.join(ROOT, ".env")
    if not os.path.isfile(env_file):
        print("env-check: %s not found" % env_file)
        return 2
    unread, unset = check(env_file)
    print("env-check: %s" % env_file)
    print("\nset but read by no code (%d):" % len(unread))
    for name in unread:
        print("  %s" % name)
    print("\nread by the code but unset (%d), with the default:" % len(unset))
    for name, default in unset:
        print("  %-44s %s" % (name, default if default is not None else "(no default stated in the code)"))
    print("\n  (also left out: the wrapper and test hand-off names: %s)"
          % ", ".join(INTERNAL))
    return 1 if unread else 0


if __name__ == "__main__":
    sys.exit(main())
