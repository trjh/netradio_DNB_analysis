"""The harvester launcher (`scripts/run_harvester.sh`), exercised without a harvester.

Every test here runs the real script against a THROWAWAY repo root: a temp directory with
`scripts/run_harvester.sh` copied in and a stand-in interpreter at `.venv/bin/python` that
sleeps instead of running anything. Nothing in this file starts `harvest.py`, touches the
signature bucket or reaches the network -- the launcher's job is process bookkeeping, and
process bookkeeping is what is checked.

The stand-in matters in one non-obvious way: the launcher decides whether a pid is still
"its" process by looking for `harvest.py` on that process's command line (pids are reused,
and a pidfile outlives a reboot). A stand-in invoked as `<fake> scripts/harvest.py --run`
carries exactly that command line, so the real check runs, unmodified.
"""

import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "scripts", "run_harvester.sh")

# A stand-in interpreter that stays up and dies on the first SIGTERM.
FAKE_PY = "#!/bin/sh\n# stand-in for the venv interpreter -- never runs the harvester\nsleep 30\n"

# A stand-in that LINGERS after SIGTERM, the way a real run does while it finishes the
# candidate in hand and writes its state. `stop` has to wait for it.
FAKE_PY_SLOW = (
    "#!/bin/sh\n"
    "trap 'sleep 2; exit 0' TERM\n"
    "while true; do sleep 0.2; done\n"
)


class LauncherTestCase(unittest.TestCase):
    """A temp repo root with the launcher in it, and nothing else."""

    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="run-harvester-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        os.makedirs(os.path.join(self.root, "scripts"))
        self.script = os.path.join(self.root, "scripts", "run_harvester.sh")
        shutil.copy2(SCRIPT, self.script)
        self.state_dir = os.path.join(self.root, ".harvest")
        self.pidfile = os.path.join(self.state_dir, "harvester.pid")
        self.log = os.path.join(self.state_dir, "harvest.log")
        self._started = []
        self.addCleanup(self._kill_started)

    # -- helpers ---------------------------------------------------------------------

    def fake_interpreter(self, body=FAKE_PY, path=".venv/bin/python"):
        full = os.path.join(self.root, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as fh:
            fh.write(body)
        os.chmod(full, 0o755)
        return full

    def run_cmd(self, *args, **kw):
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": self.root}
        env.update(kw.pop("env", {}))
        return subprocess.run([self.script, *args], capture_output=True, text=True,
                              env=env, timeout=120, **kw)

    def start(self, **kw):
        out = self.run_cmd("start", **kw)
        if os.path.exists(self.pidfile):
            self._started.append(self.read_pid())
        return out

    def read_pid(self):
        with open(self.pidfile) as fh:
            return int(fh.read().strip())

    def alive(self, pid):
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def _kill_started(self):
        for pid in self._started:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

    def spawn_stranger(self):
        """A live process that is NOT a harvester, and its pid."""
        proc = subprocess.Popen(["sleep", "60"])
        self.addCleanup(proc.wait)     # cleanups run last-added-first: kill, then reap
        self.addCleanup(proc.kill)
        return proc.pid

    def write_pidfile(self, pid):
        os.makedirs(self.state_dir, exist_ok=True)
        with open(self.pidfile, "w") as fh:
            fh.write("%s\n" % pid)


class StartTests(LauncherTestCase):

    def test_start_launches_and_records_the_pid(self):
        self.fake_interpreter()
        out = self.start()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(os.path.exists(self.pidfile))
        self.assertTrue(self.alive(self.read_pid()))
        self.assertIn("harvester UP", out.stdout)

    def test_a_stale_pidfile_is_cleared_and_the_start_goes_ahead(self):
        """The ordinary aftermath of a crash or a reboot: a pid that names nothing."""
        self.fake_interpreter()
        dead = subprocess.Popen(["true"])
        dead.wait()
        self.write_pidfile(dead.pid)
        out = self.start()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("stale pidfile", out.stdout)
        self.assertNotEqual(self.read_pid(), dead.pid)
        self.assertTrue(self.alive(self.read_pid()))

    def test_a_recycled_pid_does_not_pass_for_a_harvester(self):
        """A pidfile pointing at a live process that is something else is still stale."""
        self.fake_interpreter()
        stranger = self.spawn_stranger()
        self.write_pidfile(stranger)
        down = self.run_cmd("status")
        self.assertIn("harvester DOWN", down.stdout)
        out = self.start()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotEqual(self.read_pid(), stranger)

    def test_a_second_instance_is_refused_and_the_first_is_left_alone(self):
        self.fake_interpreter()
        first = self.start()
        self.assertEqual(first.returncode, 0, first.stderr)
        pid = self.read_pid()
        again = self.run_cmd("start")
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("already running", again.stderr)
        self.assertEqual(self.read_pid(), pid)
        self.assertTrue(self.alive(pid))

    def test_a_missing_interpreter_refuses_before_anything_is_written(self):
        out = self.run_cmd("start")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("make venv", out.stderr)
        self.assertFalse(os.path.exists(self.pidfile))


class EnvFileTests(LauncherTestCase):
    """The repo's .env is read, and its absence is not an error."""

    def test_the_env_file_supplies_the_interpreter(self):
        fake = self.fake_interpreter(path="elsewhere/python")   # NOT at .venv/bin/python
        with open(os.path.join(self.root, ".env"), "w") as fh:
            fh.write("NETRADIO_PYTHON=%s\n" % fake)
        out = self.start()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn(fake, out.stdout)

    def test_no_env_file_is_a_normal_state(self):
        self.assertFalse(os.path.exists(os.path.join(self.root, ".env")))
        self.fake_interpreter()
        out = self.start()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stderr, "")


class StopTests(LauncherTestCase):

    def test_stop_waits_for_the_exit_before_it_returns(self):
        self.fake_interpreter(body=FAKE_PY_SLOW)
        self.assertEqual(self.start().returncode, 0)
        pid = self.read_pid()
        began = time.time()
        out = self.run_cmd("stop")
        took = time.time() - began
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("stopped", out.stdout)
        self.assertFalse(self.alive(pid), "stop returned while the process was still up")
        self.assertFalse(os.path.exists(self.pidfile))
        self.assertGreaterEqual(took, 1.5, "stop did not wait for the lingering exit")

    def test_stop_with_nothing_running_is_quiet_and_clears_the_pidfile(self):
        dead = subprocess.Popen(["true"])
        dead.wait()
        self.write_pidfile(dead.pid)
        out = self.run_cmd("stop")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("not running", out.stdout)
        self.assertFalse(os.path.exists(self.pidfile))

    def test_stop_never_withdraws_a_registration_that_is_not_the_one_it_stopped(self):
        """`stop` takes no lock — it is the recovery verb, and a leaked lock must never be
        able to block it — so it withdraws the pidfile only when the file still names the
        pid it just stopped. Without that, a start completing inside stop's force-kill
        window loses its registration, and the harvester it launched becomes one that
        `status` calls DOWN and `stop` says is not running.

        The window is reproduced here rather than argued about: with the wait set to 0 the
        force-kill path runs, and its `sleep 1` is when the pidfile is overwritten.
        """
        self.fake_interpreter(body=FAKE_PY_SLOW)      # lingers on SIGTERM, so it is force-killed
        self.assertEqual(self.start().returncode, 0)
        done = []

        def stopper():
            done.append(self.run_cmd("stop", env={"NETRADIO_HARVEST_STOP_WAIT_S": "0"}))

        t = threading.Thread(target=stopper)
        t.start()
        time.sleep(0.3)                               # inside stop's post-kill second
        self.write_pidfile(424242)                    # "another start just registered"
        t.join(timeout=60)

        self.assertEqual(len(done), 1)
        self.assertEqual(done[0].returncode, 0, done[0].stderr)
        self.assertTrue(os.path.exists(self.pidfile),
                        "stop deleted a registration that was not the one it stopped")
        self.assertEqual(self.read_pid(), 424242)

    def test_restart_stops_the_old_process_and_starts_a_new_one(self):
        self.fake_interpreter()
        self.assertEqual(self.start().returncode, 0)
        first = self.read_pid()
        out = self.run_cmd("restart")
        self.assertEqual(out.returncode, 0, out.stderr)
        second = self.read_pid()
        self._started.append(second)
        self.assertNotEqual(second, first)
        self.assertFalse(self.alive(first))
        self.assertTrue(self.alive(second))


class StatusTests(LauncherTestCase):

    def test_status_reports_down_with_no_state_and_no_ledger(self):
        out = self.run_cmd("status")
        self.assertEqual(out.returncode, 1)          # "down" is the exit code, not a fault
        self.assertIn("harvester DOWN", out.stdout)
        self.assertIn("no state file yet", out.stdout)
        self.assertIn("absent (nothing signed yet)", out.stdout)
        self.assertEqual(out.stderr, "")

    def test_status_prints_the_pid_and_the_phase(self):
        self.fake_interpreter()
        os.makedirs(self.state_dir, exist_ok=True)
        with open(os.path.join(self.state_dir, "state.json"), "w") as fh:
            fh.write('{"analyzed": 3, "session": {"phase": "waiting on example.test", '
                     '"until": 0}, "current": null}')
        self.assertEqual(self.start().returncode, 0)
        out = self.run_cmd("status")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("harvester UP (pid %d)" % self.read_pid(), out.stdout)
        self.assertIn("phase:  waiting on example.test", out.stdout)

    def test_an_absent_ledger_is_never_an_error(self):
        """Until the signing pass has run once there is no ledger, and that is normal:
        nothing signed yet, no candidates. It may not stop a start or fail a status."""
        self.fake_interpreter()
        started = self.start()
        self.assertEqual(started.returncode, 0, started.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.state_dir, "ledger.json")))
        self.assertIn("ledger: absent (nothing signed yet)", started.stdout)
        out = self.run_cmd("status")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("ledger: absent (nothing signed yet)", out.stdout)
        self.assertNotIn("error", (out.stdout + out.stderr).lower())

    def test_a_ledger_that_is_present_is_reported_as_present(self):
        os.makedirs(self.state_dir, exist_ok=True)
        with open(os.path.join(self.state_dir, "ledger.json"), "w") as fh:
            fh.write("{}\n")
        out = self.run_cmd("status")
        self.assertIn("ledger: present (3 bytes)", out.stdout)


class LogRotationTests(LauncherTestCase):

    def _log_over_the_cap(self, cap):
        os.makedirs(self.state_dir, exist_ok=True)
        with open(self.log, "w") as fh:
            fh.write("first generation\n" * cap)

    def test_the_log_rotates_one_generation_over_the_cap(self):
        self.fake_interpreter()
        self._log_over_the_cap(64)
        out = self.start(env={"NETRADIO_HARVEST_LOG_MAX_BYTES": "512"})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("rotating", out.stdout)
        with open(self.log + ".1") as fh:
            self.assertIn("first generation", fh.read())
        with open(self.log) as fh:
            self.assertNotIn("first generation", fh.read())

    def test_rotation_keeps_exactly_one_generation(self):
        self.fake_interpreter()
        env = {"NETRADIO_HARVEST_LOG_MAX_BYTES": "512"}
        self._log_over_the_cap(64)
        self.assertEqual(self.start(env=env).returncode, 0)
        self.run_cmd("stop", env=env)
        with open(self.log, "w") as fh:
            fh.write("second generation\n" * 64)
        self.assertEqual(self.start(env=env).returncode, 0)
        with open(self.log + ".1") as fh:
            self.assertIn("second generation", fh.read())
        self.assertFalse(os.path.exists(self.log + ".2"))

    def test_a_log_under_the_cap_is_left_alone(self):
        self.fake_interpreter()
        os.makedirs(self.state_dir, exist_ok=True)
        with open(self.log, "w") as fh:
            fh.write("keep me\n")
        out = self.start(env={"NETRADIO_HARVEST_LOG_MAX_BYTES": "1048576"})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn("rotating", out.stdout)
        self.assertFalse(os.path.exists(self.log + ".1"))
        with open(self.log) as fh:
            self.assertIn("keep me", fh.read())


class ConcurrentStartTests(LauncherTestCase):
    """Two starts at once must resolve to one harvester, and one pidfile that names it.

    The pidfile cannot arbitrate this by itself: both starts look, both see nothing, both
    launch, and the second one's pidfile write buries the first one's. `harvest.py` would
    then refuse the loser at its own flock -- no data is ever corrupted -- but the loser's
    cleanup could carry off the WINNER's registration, leaving a harvester running for
    weeks that `status` calls DOWN and `stop` cannot stop. The lock directory is what makes
    that impossible; these two tests are its ends.
    """

    def _live_stand_ins(self):
        """Every live process whose command line names this test's temp root."""
        out = subprocess.run(["ps", "-A", "-o", "pid=,command="],
                             capture_output=True, text=True).stdout
        return [ln for ln in out.splitlines()
                if self.root in ln and "harvest.py" in ln]

    def test_a_start_is_refused_while_another_holds_the_lock(self):
        self.fake_interpreter()
        os.makedirs(os.path.join(self.state_dir, "harvester.start.lock"))
        out = self.run_cmd("start")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("another start is in flight", out.stderr)
        self.assertFalse(os.path.exists(self.pidfile))

    def test_two_starts_at_once_leave_one_harvester_and_one_pidfile(self):
        self.fake_interpreter()
        results = []
        guard = threading.Lock()

        def go():
            out = self.run_cmd("start")
            with guard:
                results.append(out)

        threads = [threading.Thread(target=go) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(results), 2)
        winners = [r for r in results if r.returncode == 0]
        self.assertEqual(len(winners), 1,
                         "exactly one start should succeed: %s" % [r.stderr for r in results])
        # The invariant that matters: whatever is up is what the pidfile names, so `status`
        # can see it and `stop` can stop it.
        self.assertTrue(os.path.exists(self.pidfile), "the live harvester lost its pidfile")
        pid = self.read_pid()
        self._started.append(pid)
        self.assertTrue(self.alive(pid))
        self.assertEqual(self.run_cmd("status").returncode, 0)
        # And no second one was left running beside it.
        self.assertEqual(len(self._live_stand_ins()), 1, self._live_stand_ins())


class KnobTests(LauncherTestCase):
    """A knob that names a guarantee may not disable it by being misspelt."""

    def test_a_units_suffixed_log_cap_refuses_the_run(self):
        self.fake_interpreter()
        out = self.start(env={"NETRADIO_HARVEST_LOG_MAX_BYTES": "10MB"})
        self.assertEqual(out.returncode, 2)
        self.assertIn("NETRADIO_HARVEST_LOG_MAX_BYTES must be a plain number", out.stderr)
        self.assertFalse(os.path.exists(self.pidfile))

    def test_a_units_suffixed_stop_wait_refuses_the_run(self):
        self.fake_interpreter()
        out = self.start(env={"NETRADIO_HARVEST_STOP_WAIT_S": "30s"})
        self.assertEqual(out.returncode, 2)
        self.assertIn("NETRADIO_HARVEST_STOP_WAIT_S must be a plain number", out.stderr)
        self.assertFalse(os.path.exists(self.pidfile))

    def test_a_bad_knob_does_not_quietly_skip_the_rotation(self):
        """The failure this guards against: `test -ge` on a non-number exits 2, the `||`
        branch reads that as "under the cap", and the log is never rotated again."""
        self.fake_interpreter()
        os.makedirs(self.state_dir, exist_ok=True)
        with open(self.log, "w") as fh:
            fh.write("first generation\n" * 64)
        out = self.start(env={"NETRADIO_HARVEST_LOG_MAX_BYTES": "1KB"})
        self.assertEqual(out.returncode, 2)
        self.assertFalse(os.path.exists(self.log + ".1"))
        with open(self.log) as fh:
            self.assertIn("first generation", fh.read())


class UsageTests(LauncherTestCase):

    def test_an_unknown_verb_prints_the_usage_and_fails(self):
        out = self.run_cmd("frobnicate")
        self.assertEqual(out.returncode, 2)
        self.assertIn("usage:", out.stderr)

    def test_help_prints_the_usage(self):
        out = self.run_cmd("help")
        self.assertEqual(out.returncode, 0)
        self.assertIn("start|stop|restart|status|help", out.stdout)


if __name__ == "__main__":
    unittest.main()
