"""scripts/post_pull_restart.sh, .githooks/ and `make hooks`: hermetic, under /bin/bash.

Each case builds a scratch "origin" and a scratch clone holding copies of the real script, the
hooks and the checkout library, with a stub launcher that logs its calls. The stub answers
`running` from a marker file, so no real server is touched. HOME is a scratch directory so the
user's own git config never reaches the run.
"""

import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASH = "/bin/bash" if os.path.exists("/bin/bash") else "bash"

STUB = """#!/bin/bash
echo "{name} $*" >> "$STUB_LOG"
env | grep -E '^GIT|git-core' >> "$STUB_ENV"
case "$1" in
  running) [ -e "$STUB_STATE/{name}.up" ] ;;
  restart) [ ! -e "$STUB_STATE/{name}.fail" ] ;;
esac
"""


class HookCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="netradio-test-hook-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = os.path.join(self.tmp, "home")
        self.state = os.path.join(self.tmp, "state")
        os.makedirs(self.home)
        os.makedirs(self.state)
        self.log = os.path.join(self.tmp, "calls.log")
        open(self.log, "w").close()
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("NETRADIO_", "GIT_"))}
        self.env.update(HOME=self.home, STUB_LOG=self.log, STUB_ENV=os.path.join(self.tmp, "env.log"), STUB_STATE=self.state,
                        GIT_CONFIG_NOSYSTEM="1")
        self.origin = os.path.join(self.tmp, "origin")
        self.root = os.path.join(self.tmp, "root")
        self.git("init", "-q", "-b", "main", self.origin, cwd=self.tmp)
        self.write(self.origin, "scripts/align_server.py", "v1\n")
        self.write(self.origin, "public/align.html", "v1\n")
        self.write(self.origin, "README.md", "v1\n")
        self.write(self.origin, "scripts/post_pull_restart.sh", None, copy="scripts/post_pull_restart.sh")
        self.write(self.origin, "scripts/lib/checkout.sh", None, copy="scripts/lib/checkout.sh")
        self.write(self.origin, "scripts/lib/env.sh", None, copy="scripts/lib/env.sh")
        for h in ("post-merge", "pre-commit", "chain-global.sh"):
            self.write(self.origin, ".githooks/" + h, None, copy=".githooks/" + h)
        self.write(self.origin, "scripts/run_align.sh", STUB.format(name="run_align"))
        self.commit(self.origin, "one")
        self.git("clone", "-q", self.origin, self.root, cwd=self.tmp)
        self.git("config", "user.email", "t@example.com", cwd=self.root)
        self.git("config", "user.name", "t", cwd=self.root)
        self.git("config", "core.hooksPath", os.path.join(self.root, ".githooks"), cwd=self.root)

    # --- helpers --------------------------------------------------------------------------------
    def git(self, *args, cwd, check=True):
        return subprocess.run(["git", *args], cwd=cwd, env=self.env, check=check,
                              capture_output=True, text=True)

    def write(self, repo, rel, body, copy=None):
        path = os.path.join(repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if copy:
            shutil.copy(os.path.join(ROOT, copy), path)
        else:
            with open(path, "w") as f:
                f.write(body)
        if rel.endswith(".sh") or rel.startswith(".githooks/"):
            os.chmod(path, 0o755)

    def commit(self, repo, msg):
        self.git("add", ".", cwd=repo)
        self.git("-c", "user.email=t@example.com", "-c", "user.name=t", "-c",
                 "core.hooksPath=/dev/null", "commit", "-q", "-m", msg, cwd=repo)

    def upstream(self, rel, body="v2\n"):
        self.write(self.origin, rel, body)
        self.commit(self.origin, "change " + rel)

    def up(self, name, fail=False):
        open(os.path.join(self.state, name + ".up"), "w").close()
        if fail:
            open(os.path.join(self.state, name + ".fail"), "w").close()

    def pull(self, cwd=None, **extra):
        env = dict(self.env, **extra)
        return subprocess.run(["git", "pull", "-q", "--ff-only"], cwd=cwd or self.root, env=env,
                              capture_output=True, text=True)

    def calls(self):
        with open(self.log) as f:
            return [ln.strip() for ln in f if ln.strip()]

    def restarts(self):
        return [c for c in self.calls() if c.endswith(" restart")]

    def run_script(self, cwd=None, **extra):
        env = dict(self.env, **extra)
        return subprocess.run([BASH, os.path.join(cwd or self.root, "scripts/post_pull_restart.sh")],
                              cwd=cwd or self.root, env=env, capture_output=True, text=True)


class RestartTests(HookCase):
    def test_pull_with_code_change_restarts_the_align_server_via_hook(self):
        self.up("run_align")
        self.upstream("scripts/align_server.py")
        r = self.pull()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.restarts(), ["run_align restart"])
        self.assertIn("restarting the align server", r.stderr)

    def test_a_page_change_restarts_it_too(self):
        self.up("run_align")
        self.upstream("public/align.html")
        self.pull()
        self.assertEqual(self.restarts(), ["run_align restart"])

    def test_launcher_sees_no_git_hook_environment(self):
        self.up("run_align")
        self.upstream("scripts/align_server.py")
        envlog = os.path.join(self.tmp, "env.log")
        open(envlog, "w").close()
        self.pull()
        self.assertEqual(self.restarts(), ["run_align restart"])
        with open(envlog) as f:
            self.assertEqual(f.read(), "")

    def test_docs_labels_and_data_alone_do_nothing(self):
        for rel in ("README.md", "docs/notes.md", "labels/review/a.orig001.review.tsv",
                    "data/rulings.json", "Makefile"):
            with self.subTest(rel=rel):
                self.up("run_align")
                self.upstream(rel, body="changed %s\n" % rel)
                r = self.pull()
                self.assertEqual(self.restarts(), [])
                self.assertIn("no change under scripts/ or public/", r.stderr)

    def test_server_down_does_nothing(self):
        self.upstream("scripts/align_server.py")
        self.pull()
        self.assertEqual(self.restarts(), [])

    def test_worktree_does_nothing(self):
        wt = os.path.join(self.root, ".worktree", "w")
        self.git("worktree", "add", "-q", wt, "-b", "side", cwd=self.root)
        self.up("run_align")
        r = self.run_script(cwd=wt)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.calls(), [])

    def test_off_main_does_nothing(self):
        self.git("checkout", "-q", "-b", "side", cwd=self.root)
        self.up("run_align")
        self.upstream("scripts/align_server.py")
        self.git("pull", "-q", "--ff-only", "origin", "main", cwd=self.root, check=False)
        self.assertEqual(self.restarts(), [])

    def test_failed_restart_is_loud_and_nonzero(self):
        self.up("run_align", fail=True)
        self.upstream("scripts/align_server.py")
        self.pull()  # git ignores the hook's status
        r = self.run_script()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("FAILED", r.stderr)

    def test_escape_hatch(self):
        self.up("run_align")
        self.upstream("scripts/align_server.py")
        r = self.pull(NETRADIO_PULL_RESTART="0")
        self.assertEqual(self.restarts(), [])
        self.assertIn("NETRADIO_PULL_RESTART=0", r.stderr)


class ChainTests(HookCase):
    def fake_global(self, body):
        d = os.path.join(self.tmp, "globalhooks")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "pre-commit")
        with open(path, "w") as f:
            f.write(body)
        os.chmod(path, 0o755)
        with open(os.path.join(self.home, ".gitconfig"), "w") as f:
            f.write("[core]\n\thooksPath = ~/../globalhooks\n")
        return d

    def stage(self):
        self.write(self.root, "new.txt", "x\n")
        self.git("add", "new.txt", cwd=self.root)

    def test_global_pre_commit_runs_and_refusal_propagates(self):
        self.fake_global('#!/bin/bash\necho "global $*" >> "$STUB_LOG"\necho refused >&2\nexit 1\n')
        self.stage()
        r = self.git("commit", "-q", "-m", "x", cwd=self.root, check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("refused", r.stderr)
        self.assertEqual(self.calls(), ["global"])

    def test_global_pre_commit_pass_lets_commit_through(self):
        self.fake_global('#!/bin/bash\nexit 0\n')
        self.stage()
        r = self.git("commit", "-q", "-m", "x", cwd=self.root, check=False)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_no_global_hook_is_fine(self):
        self.stage()
        r = self.git("commit", "-q", "-m", "x", cwd=self.root, check=False)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_global_post_merge_runs_after_ours(self):
        d = self.fake_global("#!/bin/bash\n")
        with open(os.path.join(d, "post-merge"), "w") as f:
            f.write('#!/bin/bash\necho global-post-merge >> "$STUB_LOG"\n')
        os.chmod(os.path.join(d, "post-merge"), 0o755)
        self.up("run_align")
        self.upstream("scripts/align_server.py")
        self.pull()
        self.assertEqual(self.calls(), ["run_align running", "run_align restart",
                                        "global-post-merge"])


class MakeHooksTests(HookCase):
    def test_sets_absolute_path_from_a_worktree(self):
        self.git("config", "--unset", "core.hooksPath", cwd=self.root)
        wt = os.path.join(self.root, ".worktree", "w")
        self.git("worktree", "add", "-q", wt, "-b", "side", cwd=self.root)
        make = os.path.join(ROOT, "Makefile")
        for _ in range(2):  # idempotent
            r = subprocess.run(["make", "-f", make, "hooks"], cwd=wt, env=self.env,
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
        want = os.path.realpath(os.path.join(self.root, ".githooks"))
        got = self.git("config", "--get", "core.hooksPath", cwd=self.root).stdout.strip()
        self.assertEqual(got, want)
        self.assertTrue(os.path.isabs(got))
        self.assertIn(want, r.stdout)


if __name__ == "__main__":
    unittest.main()
