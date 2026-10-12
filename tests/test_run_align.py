"""scripts/run_align.sh: the status probe and the checkout guard.

The status probe must probe, and print, the host the server is bound on (with
NETRADIO_ALIGN_HOST set to a LAN address nothing answers on loopback). The guard refuses
start, stop and restart from a linked worktree, whose launcher would otherwise act on the live
server's port. No server is started: the probe is run as an extracted snippet, and the guard
runs against a throwaway root.
"""

import os
import shutil
import socket
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with open(os.path.join(HERE, *parts), encoding="utf-8") as fh:
        return fh.read()


class TheStatusProbeBehaves(unittest.TestCase):
    def _host(self, bound):
        sh = _read("scripts", "run_align.sh").split("\nstatus() {", 1)[1]
        start = sh.index("local host;")
        end = sh.index("\n", sh.index("(bound to"))
        snippet = sh[start:end]
        script = ('env_or_dotenv() { printf "%%s" "$BOUND"; }\nPORT=8766\n'
                  'probe() {\n%s\ntrue\n}\nprobe' % snippet)
        out = subprocess.run(["bash", "-c", script], env={"BOUND": bound, "PATH": os.environ["PATH"]},
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout

    def test_a_lan_bind_is_printed_and_flagged(self):
        out = self._host("192.0.2.10")
        self.assertIn("http://192.0.2.10:8766/align", out)
        self.assertIn("bound to 192.0.2.10", out)

    def test_unset_and_wildcard_binds_print_loopback_unflagged(self):
        for bound in ("", "0.0.0.0"):
            out = self._host(bound)
            self.assertIn("http://127.0.0.1:8766/align", out)
            self.assertNotIn("bound to", out)


class TheStatusProbe(unittest.TestCase):
    def test_status_probes_and_prints_the_bound_host_not_loopback(self):
        sh = _read("scripts", "run_align.sh")
        status = sh.split("\nstatus() {", 1)[1]
        self.assertIn('host="$(env_or_dotenv NETRADIO_ALIGN_HOST)"', status)
        self.assertIn('"http://$host:$PORT/api/align/sources"', status)
        self.assertNotIn("http://127.0.0.1:$PORT/api/align/sources", status)
        self.assertIn('echo "  http://$host:$PORT/align"', status)


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TheRunningHereVerb(unittest.TestCase):
    """`running-here` answers yes only for a listener this checkout started: one whose working
    directory is the checkout. A listener another checkout started on the same port is not
    ours, and `running` alone cannot tell the two apart."""

    def setUp(self):
        if not shutil.which("lsof"):
            self.skipTest("lsof is not installed: the launcher cannot see listeners")
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="run-align-here-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        os.makedirs(os.path.join(self.root, "scripts", "lib"))
        os.makedirs(os.path.join(self.root, ".git"))
        for rel in ("scripts/run_align.sh", "scripts/lib/env.sh", "scripts/lib/checkout.sh"):
            shutil.copy2(os.path.join(HERE, rel), os.path.join(self.root, rel))
        self.other = os.path.realpath(tempfile.mkdtemp(prefix="run-align-other-"))
        self.addCleanup(shutil.rmtree, self.other, ignore_errors=True)
        self.port = str(_free_port())

    def _listen(self, cwd):
        import sys
        import time
        p = subprocess.Popen([sys.executable, "-m", "http.server", self.port,
                              "--bind", "127.0.0.1"], cwd=cwd,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(p.wait)
        self.addCleanup(p.kill)
        for _ in range(50):
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", int(self.port))) == 0:
                    return
            time.sleep(0.1)
        self.fail("listener did not come up")

    def _verb(self, verb, path=None):
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": self.root,
               "NETRADIO_ALIGN_PORT": self.port}
        if path:
            env["PATH"] = path + os.pathsep + env["PATH"]
        return subprocess.run(["bash", os.path.join(self.root, "scripts", "run_align.sh"), verb],
                              capture_output=True, text=True, env=env, timeout=60).returncode

    def test_nothing_listening_is_neither(self):
        self.assertEqual(self._verb("running"), 1)
        self.assertEqual(self._verb("running-here"), 1)

    def test_this_checkouts_listener_is_here(self):
        self._listen(self.root)
        self.assertEqual(self._verb("running"), 0)
        self.assertEqual(self._verb("running-here"), 0)

    def test_another_checkouts_listener_is_running_but_not_here(self):
        self._listen(self.other)
        self.assertEqual(self._verb("running"), 0)
        self.assertEqual(self._verb("running-here"), 1)

    def test_a_listener_with_no_readable_directory_is_cannot_tell(self):
        # lsof lists the port's listener but gives no working directory for it
        fake = os.path.join(self.root, "fakebin")
        os.makedirs(fake)
        with open(os.path.join(fake, "lsof"), "w") as fh:
            fh.write('#!/bin/bash\ncase "$*" in *" -d "*) exit 1 ;; *) echo 12345 ;; esac\n')
        os.chmod(os.path.join(fake, "lsof"), 0o755)
        self.assertEqual(self._verb("running-here", path=fake), 2)


class TheLogSetting(unittest.TestCase):
    def test_the_log_path_is_read_through_the_env_chain(self):
        sh = _read("scripts", "run_align.sh")
        self.assertIn('NETRADIO_ALIGN_LOG="$(env_get "$ROOT/.env" NETRADIO_ALIGN_LOG)"', sh)
        self.assertLess(sh.index("env_get \"$ROOT/.env\" NETRADIO_ALIGN_LOG"),
                        sh.index('LOG="${NETRADIO_ALIGN_LOG:-'))


class TheCheckoutGuard(unittest.TestCase):
    """The launcher and its two libraries copied into a throwaway root; `.git` a file there
    (a linked worktree) or a directory (a main checkout)."""

    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="run-align-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        os.makedirs(os.path.join(self.root, "scripts", "lib"))
        for rel in ("scripts/run_align.sh", "scripts/lib/env.sh", "scripts/lib/checkout.sh"):
            shutil.copy2(os.path.join(HERE, rel), os.path.join(self.root, rel))
        self.port = str(_free_port())

    def _run(self, verb, **env):
        full = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": self.root,
                "NETRADIO_ALIGN_PORT": self.port}
        full.update(env)
        return subprocess.run(["bash", os.path.join(self.root, "scripts", "run_align.sh"), verb],
                              capture_output=True, text=True, env=full, timeout=60)

    def test_a_linked_worktree_is_refused_before_the_port_is_touched(self):
        with open(os.path.join(self.root, ".git"), "w") as fh:
            fh.write("gitdir: /nowhere/.git/worktrees/x\n")
        for verb in ("start", "stop", "restart"):
            r = self._run(verb)
            self.assertEqual(r.returncode, 2, verb + r.stdout + r.stderr)
            self.assertIn("REFUSING", r.stderr)
            self.assertIn("python3 scripts/align_server.py", r.stderr)
            self.assertFalse(os.path.exists(os.path.join(self.root, "logs")), verb)

    def test_the_opt_out_and_the_read_verbs_pass(self):
        with open(os.path.join(self.root, ".git"), "w") as fh:
            fh.write("gitdir: /nowhere/.git/worktrees/x\n")
        self.assertEqual(self._run("running").returncode, 1)     # nothing on the port
        r = self._run("stop", NETRADIO_ALLOW_OFF_MAIN="1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("stopped", r.stdout)

    def test_a_main_checkout_is_not_refused(self):
        os.makedirs(os.path.join(self.root, ".git"))
        r = self._run("stop")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("stopped", r.stdout)


if __name__ == "__main__":
    unittest.main()
