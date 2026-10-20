"""The align server: routing + dark-mode behaviour over real HTTP.

The DSP boundary itself (align_inspect) is fully covered in test_align; this
exercises the thin server around it: page route, sources route,
compute routes relaying the validator's answers, 404s. Runs against a live
in-process server on an ephemeral port, with no checkout configured (dark).
"""

import ast
import http.client
import json
import os
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import align_inspect
import align_server

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestStandaloneServer(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # `align_server._read_only()` reads the environment on every request, so an ambient
        # NETRADIO_READ_ONLY would gate this whole class behind 403s. Declare the posture here;
        # the tests that are ABOUT read-only patch it back on for themselves.
        cls.env = mock.patch.dict(os.environ, {"NETRADIO_MP3_DIR": "",
                                               "NETRADIO_LABELS_DIR": "",
                                               "NETRADIO_READ_ONLY": ""})
        cls.env.start()
        cls.root = mock.patch.object(align_inspect, "ROOT_DIR", "")   # no checkout: dark
        cls.root.start()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), align_server.AlignHandler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.env.stop()
        cls.root.stop()

    def _req(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path, body=body,
                     headers={"Content-Type": "application/json"} if body else {})
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, data

    def test_page_served_at_root_and_align(self):
        for path in ("/", "/align", "/align/"):
            status, data = self._req("GET", path)
            self.assertEqual(status, 200, path)
            self.assertIn(b"sample inspector", data)

    def test_sources_reports_dark_when_unconfigured(self):
        status, data = self._req("GET", "/api/align/sources")
        self.assertEqual(status, 200)
        payload = json.loads(data)
        self.assertFalse(payload["enabled"])
        self.assertEqual(payload["streams"], [])
        # DP-22: the placed collection is always present, [] when dark
        self.assertEqual(payload["placed"], [])

    def test_compute_routes_relay_the_dark_404(self):
        for path in ("/api/align/slice", "/api/align/refine", "/api/align/context",
                     "/api/align/overview"):
            status, data = self._req("POST", path, body=json.dumps({"stem": "x"}))
            self.assertEqual(status, 404, path)
            self.assertIn("not configured", json.loads(data)["error"])

    def test_bad_json_body_is_a_400(self):
        status, data = self._req("POST", "/api/align/slice", body="{nope")
        self.assertEqual(status, 400)

    def test_unknown_routes_404(self):
        self.assertEqual(self._req("GET", "/api/other")[0], 404)
        self.assertEqual(self._req("POST", "/api/align/other")[0], 404)


class TestLogLines(unittest.TestCase):
    def test_stdout_is_line_buffered_before_main(self):
        # The launcher's log is a file: without this, prints reach it in blocks.
        with open(os.path.join(REPO, "scripts", "align_server.py"), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        block = next((n for n in tree.body if isinstance(n, ast.If)
                      and ast.unparse(n.test) == "__name__ == '__main__'"), None)
        self.assertIsNotNone(block, "align_server.py has no __main__ block")
        self.assertEqual([ast.unparse(s) for s in block.body],
                         ["sys.stdout.reconfigure(line_buffering=True)", "main()"])


class TestPaths(unittest.TestCase):
    """The module sits in scripts/: the checkout root is one level up, and the pages
    and the .env are the checkout's, not scripts/'s."""

    def test_root_and_public_dir_are_the_checkouts(self):
        self.assertEqual(os.path.realpath(align_server.ROOT_DIR), os.path.realpath(REPO))
        self.assertEqual(os.path.realpath(align_server.PUBLIC_DIR),
                         os.path.realpath(os.path.join(REPO, "public")))
        self.assertTrue(os.path.isfile(os.path.join(align_server.PUBLIC_DIR, "align.html")))
        self.assertTrue(os.path.isfile(os.path.join(align_server.PUBLIC_DIR, "review.html")))
        self.assertEqual(os.path.realpath(align_inspect.repo_root()), os.path.realpath(REPO))

    def test_labels_default_to_the_checkouts_labels(self):
        with mock.patch.dict(os.environ, {"NETRADIO_LABELS_DIR": ""}), \
                mock.patch.object(align_inspect, "ROOT_DIR", "/some/checkout"):
            self.assertEqual(align_server._labels_dir(), "/some/checkout/labels")
        with mock.patch.dict(os.environ, {"NETRADIO_LABELS_DIR": "/elsewhere"}):
            self.assertEqual(align_server._labels_dir(), "/elsewhere")


class TestGate(unittest.TestCase):
    """Basic Auth (when configured) and read-only mode gate every route."""

    def _server(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), align_server.AlignHandler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.shutdown)
        return httpd.server_address[1]

    def _req(self, port, path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", path, headers=headers or {})
        resp = conn.getresponse()
        resp.read()
        conn.close()
        return resp.status

    def test_auth_required_when_configured(self):
        import base64
        with mock.patch.dict(os.environ, {"NETRADIO_AUTH_USER": "u",
                                          "NETRADIO_AUTH_PASS": "p",
                                          "NETRADIO_READ_ONLY": ""}):
            port = self._server()
            self.assertEqual(self._req(port, "/align"), 401)
            self.assertEqual(self._req(port, "/api/align/sources"), 401)
            good = {"Authorization": "Basic " + base64.b64encode(b"u:p").decode()}
            self.assertEqual(self._req(port, "/align", good), 200)

    def test_read_only_hides_everything(self):
        with mock.patch.dict(os.environ, {"NETRADIO_READ_ONLY": "1",
                                          "NETRADIO_AUTH_USER": "",
                                          "NETRADIO_AUTH_PASS": ""}):
            port = self._server()
            for path in ("/align", "/api/align/sources"):
                self.assertEqual(self._req(port, path), 404, path)


class TestRunAlignScript(unittest.TestCase):
    """DP-17: the server's launcher exists, parses, and has the shape
    the docs promise (own pidfile, logs/ log, the four verbs, .env port chain)."""

    SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "scripts", "run_align.sh")

    def setUp(self):
        with open(self.SCRIPT, "r", encoding="utf-8") as handle:
            self.src = handle.read()

    def test_executable_and_valid_bash(self):
        import subprocess
        self.assertTrue(os.access(self.SCRIPT, os.X_OK), "must be chmod +x")
        proc = subprocess.run(["bash", "-n", self.SCRIPT], capture_output=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_launches_the_align_server_from_scripts(self):
        self.assertIn('"$PYTHON" scripts/align_server.py', self.src)
        self.assertIn("NETRADIO_ALIGN_PORT", self.src)
        self.assertIn("8766", self.src)

    def test_own_pidfile_and_log(self):
        self.assertIn(".align.pid", self.src)
        self.assertIn("logs/align.log", self.src)

    def test_the_four_verbs(self):
        for verb in ("start", "stop", "restart", "status"):
            self.assertIn(verb, self.src)
        self.assertNotIn("tunnel", self.src)   # exposure changes are NOT this script's job

    def test_status_probe_sends_basic_auth_when_configured(self):
        # the status probe used to print "DSP: unknown" against a perfectly
        # healthy server whenever Basic Auth was on, because it never sent
        # credentials; now NETRADIO_AUTH_USER/PASS (env, else .env) ride on curl
        self.assertIn("NETRADIO_AUTH_USER", self.src)
        self.assertIn("NETRADIO_AUTH_PASS", self.src)
        self.assertIn('curl_auth=(-u "$user:$pass")', self.src)
        # bash-3.2-safe empty-array expansion under set -u
        self.assertIn('${curl_auth[@]+"${curl_auth[@]}"}', self.src)

    def test_status_reports_401_as_up_not_unknown(self):
        # a 401 proves the server is up -- it must never read as "unknown"
        self.assertIn("UP (auth required)", self.src)
        self.assertNotIn("Basic Auth on, or still starting", self.src)

    def test_status_reads_dsp_state_from_the_json(self):
        # when reachable, the DSP verdict comes from the server's own answer
        self.assertIn('"enabled": true', self.src)
        self.assertIn("DSP: enabled", self.src)
        self.assertIn("DSP: NOT CONFIGURED", self.src)


if __name__ == "__main__":
    unittest.main()
