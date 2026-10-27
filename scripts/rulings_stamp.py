#!/usr/bin/env python3
"""The rulings file's stamp: does `data/rulings.json` hold the bytes its writer last wrote?

The harvester acts on the rulings file (`data/rulings.json`, the retired set). The file is
committed, so a merge or a pull can put an older committed copy back in place, and the
harvester would then search with rulings its writer has since changed. The process that writes
the rulings file can also write a stamp beside the harvester's state,
`.harvest/rulings-stamp.json`, a JSON object:

    {"sha256": "<lower-case hex SHA-256 of the exact bytes of data/rulings.json>",
     "written_at": "YYYY-MM-DDTHH:MM:SSZ",
     "writer": {"pid": ..., "host": ..., "program": ...}}

The stamp is this machine's fact about this machine's file, so it lives in the gitignored
`.harvest/` and never travels with the repo. `written_at` and `writer` are information only:
the check reads the hash and nothing else (not the age, not the pid; the writer may have
restarted since, and the hash is what proves the file is the one written).

The check is OPT-IN, by the setting `NETRADIO_RULINGS_STAMP`:

* unset, empty or `off` -- no check. A harvester run with rulings kept by hand, and no process
  that writes a stamp, starts as before.
* `require` -- a start is refused when the stamp is absent, unreadable, not an object or
  without a valid `sha256`, when the rulings file is absent, or when the file's hash differs
  from the stamp's.
* anything else -- refused too, naming the setting: a misspelt setting must not turn the check
  off without a word.

`scripts/run_harvester.sh start` (and `restart`, before it stops anything) runs this file, and
`harvest.py --run` makes the same check at its start. A refusal exits 2, the code the harvester
gives a missing file or setting.

    .venv/bin/python scripts/rulings_stamp.py      # exit 0: off or matching; exit 2: refused

Run by itself it reads the checkout's `.env` for the setting (the file wins, as it does for the
launcher) and prints one line on success saying which it was.

Standard library only, so the launcher can run it cheaply before it starts anything.
"""

import hashlib
import json
import os
import re
import sys

SETTING = "NETRADIO_RULINGS_STAMP"
STAMP_NAME = "rulings-stamp.json"
EXIT_REFUSED = 2          # the harvester's code for a missing file or setting

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RULINGS = os.path.join(_ROOT, "data", "rulings.json")
STAMP = os.path.join(_ROOT, ".harvest", STAMP_NAME)

_HEX64 = re.compile(r"[0-9a-f]{64}")

_WHAT_TO_DO = ("Have the process that writes the rulings file write it again (it writes the "
               "stamp with it), then start again. If no such process runs on this machine, "
               "set %s=off in .env." % SETTING)


def mode(environ=None):
    """`require` or `off`, from the setting. Raises ValueError, naming the setting and the
    value, for anything else."""
    value = (os.environ if environ is None else environ).get(SETTING, "").strip()
    if value in ("", "off"):
        return "off"
    if value == "require":
        return "require"
    raise ValueError("%s must be `require` or `off` (or unset) -- got: %r. Not starting: a "
                     "misspelt setting must not turn the rulings check off."
                     % (SETTING, value))


def refusal(rulings=RULINGS, stamp=STAMP):
    """Why the rulings file and its stamp do not agree, as one line naming what to do, or None
    when the stamp names the exact bytes the file holds now."""
    try:
        with open(stamp, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return ("the rulings file's stamp (%s) is absent: nothing has vouched for %s on this "
                "machine. %s" % (stamp, rulings, _WHAT_TO_DO))
    except (OSError, ValueError) as exc:
        return ("the rulings file's stamp (%s) is unreadable (%s). %s"
                % (stamp, exc.__class__.__name__, _WHAT_TO_DO))
    want = data.get("sha256") if isinstance(data, dict) else None
    if not isinstance(want, str) or not _HEX64.fullmatch(want):
        return ("the rulings file's stamp (%s) is unreadable: it is not a JSON object with a "
                "lower-case hex `sha256`. %s" % (stamp, _WHAT_TO_DO))
    try:
        with open(rulings, "rb") as fh:
            have = hashlib.sha256(fh.read()).hexdigest()
    except FileNotFoundError:
        return "the rulings file (%s) is absent. %s" % (rulings, _WHAT_TO_DO)
    except OSError as exc:
        return ("the rulings file (%s) is unreadable (%s). %s"
                % (rulings, exc.__class__.__name__, _WHAT_TO_DO))
    if have != want:
        return ("the rulings file (%s) has changed since its last write (a merge or a pull?): "
                "its SHA-256 is %s, the stamp (%s) names %s. %s"
                % (rulings, have, stamp, want, _WHAT_TO_DO))
    return None


def check(rulings=RULINGS, stamp=STAMP, environ=None):
    """The start gate: None when the check is off or passes, else the refusal's one line."""
    try:
        if mode(environ) == "off":
            return None
    except ValueError as exc:
        return str(exc)
    return refusal(rulings, stamp)


def env_file_value(path=None, key=SETTING):
    """`key`'s value in the checkout's `.env`, read the way the launcher's `.env` reader does
    (trimmed lines, `#` comments, split on the first `=`, quotes stripped, first line wins),
    or None when the file or the key is absent."""
    path = path or os.path.join(_ROOT, ".env")
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return None
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = (part.strip() for part in line.split("=", 1))
        if name == key:
            return value.strip('"').strip("'")
    return None


def main():
    environ = dict(os.environ)
    from_file = env_file_value()
    if from_file is not None:
        environ[SETTING] = from_file          # the file wins, as the launcher's load does
    why = check(environ=environ)
    if why:
        print("refusing to start the harvester: " + why, file=sys.stderr)
        return EXIT_REFUSED
    print("rulings stamp check: " + ("matches" if mode(environ) == "require"
                                     else "off (%s unset or off)" % SETTING))
    return 0


if __name__ == "__main__":
    sys.exit(main())
