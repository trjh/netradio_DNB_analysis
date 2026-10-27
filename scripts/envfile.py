"""envfile -- the one `.env` reader for Python. Import it; it reads files and sets nothing until
`load` is called.

    import envfile
    envfile.read(path)              # {KEY: value}, the first line for a key winning
    envfile.value(path, key)        # KEY's value in the file, or "" (None with default=None)
    envfile.get(path, key)          # the real environment's KEY when it is set, else the file's
    envfile.load(path)              # put every key the environment does not already hold into it
    envfile.lint(path)              # [(line number, key, problem)] for lines that read oddly

From a shell (bash or zsh), to put a `.env` into the current shell:

    eval "$(python3 path/to/envfile.py --export .env)"   # `export KEY='value'` per unset name

The grammar is scripts/lib/env.sh's, exactly, and tests/test_envfile.py runs both readers on the
same files so they cannot drift. The file is never executed:
  - each line is trimmed; a blank line, a line starting with `#`, and a line with no `=` are
    skipped
  - the line splits on its first `=`, and both sides are trimmed
  - double quotes, then single quotes, are stripped from both ends of the value
  - the first line for a key wins
  - `export KEY=v` is a key named "export KEY", which nothing reads
  - a `#` after a value is part of the value: a comment goes on a line of its own
  - nothing is expanded: no `$VAR`, no command substitution, and a leading `~` stays as written
    for the reader to expand (os.path.expanduser)
A missing file reads as empty. The real environment wins over the file: `get` and `load` never
replace a name that is already set, even to an empty value.
"""

import os
import re
import shlex
import sys

# What awk's [[:space:]] matches, and no more: str.strip() with no argument also strips Unicode
# spaces that the shell reader keeps.
_SPACE = " \t\n\v\f\r"
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def pairs(path):
    """Each line's (key, value), in file order, duplicates included."""
    out = []
    try:
        # newline="\n": only a line feed ends a line, as for awk; a carriage return is trimmed.
        with open(path, encoding="utf-8", errors="surrogateescape", newline="\n") as fh:
            for line in fh:
                line = line.strip(_SPACE)
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                value = value.strip(_SPACE).strip('"').strip("'")
                out.append((key.strip(_SPACE), value))
    except (OSError, UnicodeError):
        return []
    return out


def read(path):
    """{KEY: value} from the file; the first line for a key wins."""
    out = {}
    for key, value in pairs(path):
        out.setdefault(key, value)
    return out


def value(path, key, default=""):
    """KEY's value in the file, else `default`."""
    return read(path).get(key, default)


def get(path, key, default="", environ=None):
    """The environment's KEY when it is set (an empty value included), else the file's, else
    `default`."""
    environ = os.environ if environ is None else environ
    if key in environ:
        return environ[key]
    return value(path, key, default)


def load(path, environ=None):
    """Put every key in the file that the environment does not already hold into it, skipping a
    key that is not a shell name (as env_load does). Returns the names it set."""
    environ = os.environ if environ is None else environ
    added = []
    for key, val in pairs(path):
        if not _NAME.match(key) or key in environ:
            continue
        environ[key] = val
        added.append(key)
    return added


def lint(path):
    """[(line number, key, problem)] for the lines this grammar reads differently from a shell
    that sources the file: an `export` prefix (the key is not read), and a value holding a
    ` #` (the comment is part of the value). Keys only, never values: a `.env` holds secrets."""
    out = []
    try:
        with open(path, encoding="utf-8", errors="surrogateescape", newline="\n") as fh:
            lines = fh.read().split("\n")
    except (OSError, UnicodeError):
        return []
    for number, line in enumerate(lines, 1):
        line = line.strip(_SPACE)
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip(_SPACE)
        exported = re.match(r"export[ \t]+(\S+)$", key)
        if exported:
            out.append((number, exported.group(1),
                        "starts with `export`, so the name is not read; drop the `export`"))
        elif re.search(r"[ \t]#", val):
            out.append((number, key,
                        "holds ` #`, which is part of the value; put the comment on its own line"))
    return out


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2 or argv[0] != "--export":
        sys.stderr.write("usage: envfile.py --export FILE\n")
        return 64
    for key, val in pairs(argv[1]):
        if _NAME.match(key) and key not in os.environ:
            os.environ[key] = val          # the first line wins, as in load
            print("export %s=%s" % (key, shlex.quote(val)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
