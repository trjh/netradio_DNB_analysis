"""envfile -- the one `.env` reader for Python -- against scripts/lib/env.sh, the shell's.

Both readers run on the same fixture files, so the two grammars cannot drift: every key's value,
the first-line rule, the skipped lines, and the environment winning. The repo's `.env.example`
goes through both as well, every setting line uncommented, and must read the same in each, with
no line the shared grammar reads differently from a shell that sources the file (envfile.lint).
No real `.env` is read.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _where in (ROOT, os.path.join(ROOT, "scripts")):
    if os.path.isfile(os.path.join(_where, "envfile.py")):
        sys.path.insert(0, _where)
        break
import envfile  # noqa: E402

LIB = os.path.join(ROOT, "scripts", "lib", "env.sh")
BASH = "/bin/bash" if os.path.exists("/bin/bash") else "bash"

# Each grammar case, and what both readers must make of it.
FIXTURE = (
    "# a comment\n"
    "\n"
    "   # an indented comment\n"
    "PLAIN=one\n"
    "  SPACED  =  two words  \n"
    'DQ="quoted value"\n'
    "SQ='single'\n"
    "MIXED=\"'both'\"\n"
    "EMPTY=\n"
    "EQ=a=b=c\n"
    "TILDE=~/somewhere\n"
    "DOLLAR=$HOME/x\n"
    "SUBST=$(echo no)\n"
    "PLAIN=second line loses\n"
    "export EXPORTED=1\n"
    "NO_EQUALS_LINE\n"
    "TAIL=value   # the comment stays\n"
    "HASH=a#b\n"
    "APOS=it's\n"
    "TAB=\tpadded\t\n"
    "CRLF=windows\r\n"
    "LAST=no newline"
)
EXPECTED = {
    "PLAIN": "one", "SPACED": "two words", "DQ": "quoted value", "SQ": "single",
    "MIXED": "both", "EMPTY": "", "EQ": "a=b=c", "TILDE": "~/somewhere", "DOLLAR": "$HOME/x",
    "SUBST": "$(echo no)", "export EXPORTED": "1", "TAIL": "value   # the comment stays",
    "HASH": "a#b", "APOS": "it's", "TAB": "padded", "CRLF": "windows", "LAST": "no newline",
}

SETTING = re.compile(r"^\s*#?\s*([A-Z][A-Z0-9_]*=.*)$")


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="envfile-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = self.write("fixture.env", FIXTURE)

    def write(self, name, text):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        return path

    def sh(self, body, **env):
        base = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": self.tmp}
        base.update(env)
        script = 'set -euo pipefail\n. "%s"\n%s\n' % (LIB, body)
        r = subprocess.run([BASH, "-c", script], capture_output=True, text=True, env=base,
                           cwd=self.tmp, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def shell_value(self, path, key):
        return self.sh('env_value "$F" "$K"', F=path, K=key)

    def shell_has(self, path, key):
        return self.sh('env_has "$F" "$K" && echo yes || echo no', F=path, K=key).strip() == "yes"


class TheGrammar(Case):
    def test_envfile_reads_the_stated_grammar(self):
        self.assertEqual(envfile.read(self.path), EXPECTED)

    def test_the_shell_reader_gives_every_key_the_same_value(self):
        for key, value in envfile.read(self.path).items():
            self.assertEqual(self.shell_value(self.path, key), value, key)

    def test_both_skip_the_same_lines(self):
        for key in ("NO_EQUALS_LINE", "EXPORTED", "# a comment", ""):
            self.assertFalse(self.shell_has(self.path, key), key)
            self.assertNotIn(key, envfile.read(self.path))

    def test_value_reads_one_key(self):
        self.assertEqual(envfile.value(self.path, "PLAIN"), "one")
        self.assertEqual(envfile.value(self.path, "ABSENT"), "")
        self.assertIsNone(envfile.value(self.path, "ABSENT", None))

    def test_a_missing_file_reads_empty(self):
        missing = os.path.join(self.tmp, "absent.env")
        self.assertEqual(envfile.read(missing), {})
        self.assertEqual(self.shell_value(missing, "PLAIN"), "")
        self.assertEqual(envfile.load(missing, environ={}), [])


class TheEnvironmentWins(Case):
    def test_load_sets_what_env_load_exports(self):
        names = [k for k in EXPECTED if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", k)]
        body = 'env_load "$F"\n' + "".join(
            'printf "%%s=%%s\\n" %s "${%s-UNSET}"\n' % (k, k) for k in names)
        shell = dict(line.split("=", 1) for line in self.sh(body, F=self.path).splitlines())
        environ = {}
        added = envfile.load(self.path, environ=environ)
        self.assertEqual(environ, {k: v for k, v in shell.items() if v != "UNSET"})
        self.assertNotIn("export EXPORTED", environ, "not a name: skipped, as env_load skips it")
        self.assertEqual(sorted(added), sorted(environ))

    def test_load_never_replaces_a_set_name_even_an_empty_one(self):
        environ = {"PLAIN": "from-env", "DQ": ""}
        envfile.load(self.path, environ=environ)
        self.assertEqual((environ["PLAIN"], environ["DQ"]), ("from-env", ""))
        self.assertEqual(self.sh('env_load "$F"; printf "%s|%s" "$PLAIN" "$DQ"',
                                 F=self.path, PLAIN="from-env", DQ=""), "from-env|")

    def test_export_puts_into_a_shell_what_env_load_does(self):
        names = [k for k in EXPECTED if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", k)]
        dump = "".join('printf "%%s=%%s\\n" %s "${%s-UNSET}"\n' % (k, k) for k in names)
        via_load = self.sh('env_load "$F"\n' + dump, F=self.path, PLAIN="from-env")
        via_export = self.sh('eval "$("$PY" "$EF" --export "$F")"\n' + dump, F=self.path,
                             PLAIN="from-env", PY=sys.executable, EF=envfile.__file__)
        self.assertEqual(via_export, via_load)

    def test_get_matches_env_get(self):
        for env in ({}, {"PLAIN": "from-env"}, {"PLAIN": ""}):
            want = self.sh('env_get "$F" PLAIN', F=self.path, **env)
            self.assertEqual(envfile.get(self.path, "PLAIN", environ=env), want, env)
        self.assertEqual(envfile.get(self.path, "ABSENT", "dflt", environ={}), "dflt")


class TheLint(Case):
    def test_it_names_the_lines_a_sourced_shell_would_read_differently(self):
        found = [(n, k) for n, k, _ in envfile.lint(self.path)]
        self.assertEqual(found, [(15, "EXPORTED"), (17, "TAIL")])

    def test_it_never_reports_a_value(self):
        path = self.write("secret.env", "TOKEN=hunter2   # rotate monthly\n")
        self.assertNotIn("hunter2", repr(envfile.lint(path)))


class TheExample(Case):
    """`.env.example`, every setting line uncommented, is written in the shared grammar."""

    def uncommented(self):
        with open(os.path.join(ROOT, ".env.example"), encoding="utf-8") as fh:
            lines = [m.group(1) for m in map(SETTING.match, fh.read().splitlines()) if m]
        self.assertTrue(lines, "the example names no setting")
        return self.write("example.env", "\n".join(lines) + "\n")

    def test_no_example_line_reads_differently_from_a_sourced_shell(self):
        self.assertEqual(envfile.lint(self.uncommented()), [])

    def test_both_readers_read_the_example_alike(self):
        path = self.uncommented()
        keys = sorted(envfile.read(path))
        body = "".join('printf "%%s\\n" "$(env_value "$F" %s)"\n' % k for k in keys)
        shell = self.sh(body, F=path).split("\n")[:len(keys)]
        self.assertEqual(shell, [envfile.read(path)[k] for k in keys])


if __name__ == "__main__":
    unittest.main()
