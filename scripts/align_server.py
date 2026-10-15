"""The /align inspector server: its own process, its own port.

The alignment tool runs as a process of its own (decision made 2026-07-31), on its own port,
so stopping or breaking it touches nothing else on the machine. A reverse proxy can forward
`/align` and `/api/align/` to it from another host.

It is stdlib-only: this file serves two pages (`/align`, the
per-point inspector, and `/align/review`, the DP-22 whole-song L/R review POC —
§6.9-a), five JSON routes
(sources — whose answer now carries the DP-22 `placed` collection beside `hints` —
slice, refine, context, overview) and two whole-file review-audio GETs
(DP-22 §6.8: `/api/align/audio/stream/<stem>` + `/api/align/audio/orig/<NNN>`,
Range-supported via `rangeserve.py`, resolved DSP-free in `review_audio.py`;
wv-only originals refused pending the DP-23 sync).
Every piece of DSP happens in this checkout's venv
via `align_inspect.py` (identifier-based requests, hard caps, fixed-argv subprocess
with a keep-warm worker in front of it, single-flight lock — see that module's
docstring for the full contract; the audio routes carry the same identifier/caps
contract without ever touching the venv).

Exposure model: binds loopback by default (`NETRADIO_ALIGN_HOST` to change), so the
only way in from another machine is a proxy you put in front of it, or a bind you choose.
Basic Auth is required on every route when `NETRADIO_AUTH_USER`/`NETRADIO_AUTH_PASS` are set.

At start, after `load_env`, the server puts the venv's decoded-audio cache
on the one cache policy and runs the eviction once
(`start_cache_policy()`) — so the cache's age limit and cap are applied even when nothing
else on the machine runs the policy — skipped under `NETRADIO_READ_ONLY`, so a copy started
from a worktree never evicts from a live cache.

Run:  scripts/run_align.sh start      (or python3 scripts/align_server.py; env from the
      checkout's .env; NETRADIO_ALIGN_PORT, default 8766)
"""

import base64
import json
import os
import re
import urllib.parse

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import align_inspect
import cache_budget
import rangeserve
import review_audio
import review_notes
import review_sync
import summary_compile

# This module sits in scripts/; the checkout root is one level up, and the pages are under its
# public/ directory.
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PUBLIC_DIR = os.path.join(ROOT_DIR, "public")


def load_env(path=None):
    """Minimal .env loader: KEY=VALUE lines, '#' comments, optional quotes."""
    path = path or os.path.join(ROOT_DIR, ".env")
    if not os.path.isfile(path):
        return
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _mp3_dir():
    value = os.environ.get("NETRADIO_MP3_DIR", "")
    return os.path.expanduser(value) if value else ""


def _labels_dir():
    """NETRADIO_LABELS_DIR, else the labels/ of the checkout the DSP runs in."""
    value = os.environ.get("NETRADIO_LABELS_DIR", "")
    if value:
        return os.path.expanduser(value)
    root = align_inspect.repo_root()
    return os.path.join(root, "labels") if root else ""


def _read_only():
    return os.environ.get("NETRADIO_READ_ONLY", "").strip().lower() in (
        "1", "true", "yes", "on")


class AlignHandler(BaseHTTPRequestHandler):
    server_version = "netradio-align/1"

    def _gate(self):
        """Basic Auth (when configured) + read-only, on EVERY route.

        With NETRADIO_AUTH_USER/PASS set, every request must present those
        credentials (a proxy in front passes the header through); on a read-only
        mirror the whole tool is absent — listing
        local audio or launching compute is exactly what a mirror must not do.
        Returns True when the request may proceed."""
        if _read_only():
            self.send_error(404)
            return False
        user = os.environ.get("NETRADIO_AUTH_USER", "")
        pw = os.environ.get("NETRADIO_AUTH_PASS", "")
        if not (user and pw):
            return True
        expected = "Basic " + base64.b64encode(
            ("%s:%s" % (user, pw)).encode("utf-8")).decode("ascii")
        if self.headers.get("Authorization", "") == expected:
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="netradio-align"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def _json(self, payload, status=200, headers=None):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _page(self, name):
        """Serve one of the tool's pages from public/ (no-store, so an edit shows on reload)."""
        try:
            with open(os.path.join(PUBLIC_DIR, name), "rb") as handle:
                body = handle.read()
        except OSError:
            return self.send_error(404)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._gate():
            return
        path = self.path.split("?", 1)[0]
        if path in ("/", "/align", "/align/"):
            return self._page("align.html")
        if path in ("/align/review", "/align/review/"):
            # DP-22 §6.9-a: the whole-song L/R review page (phone-first web POC)
            return self._page("review.html")
        if path == "/api/align/sources":
            return self._json({
                "enabled": align_inspect.enabled(),
                "streams": align_inspect.list_streams(_mp3_dir()),
                "originals": align_inspect.list_originals(),
                "hints": align_inspect.parse_match_hints(_labels_dir()),
                # DP-22 §6.5 source (b): the hand-placed pairs, source-tagged so the
                # review page can interleave them with the hints sets ([] when dark)
                "placed": align_inspect.list_placed(_mp3_dir()),
            })
        if path.startswith(self.AUDIO_STREAM) or path.startswith(self.AUDIO_ORIG):
            return self._serve_review_audio(path)
        return self.send_error(404)

    AUDIO_STREAM = "/api/align/audio/stream/"
    AUDIO_ORIG = "/api/align/audio/orig/"

    def _serve_review_audio(self, path):
        """The DP-22 §6.8 whole-file audio GETs — identifier-only, Range-supported,
        DSP-free (pure resolution in `review_audio.py`; wv-only pairs are REFUSED
        with a 503 naming the DP-23 sync — no transcode fallback exists). Errors
        come back as JSON; a 200 streams the resolved file through
        `rangeserve.serve` and names it in X-Review-Source."""
        if path.startswith(self.AUDIO_STREAM):
            status, result = review_audio.stream_delivery(
                path[len(self.AUDIO_STREAM):], _mp3_dir())
        else:
            ref = path[len(self.AUDIO_ORIG):]
            if not re.fullmatch(r"\d{1,4}", ref):    # a number, never a path
                return self.send_error(404)
            status, result = review_audio.orig_delivery(int(ref))
        if status != 200:
            return self._json(result, status)
        # percent-encoded: header values are latin-1 on the wire, and original
        # filenames are allowed unicode — an ASCII name passes through unchanged
        source = urllib.parse.quote(result["source"])
        headers = {"X-Review-Source": source}
        try:
            # the resolved file's revision token (mtime_ns-size — the repo's
            # name:size:mtime cache-key convention): rangeserve itself carries
            # no ETag/Last-Modified, so this is what lets a caching client (the
            # DP-22 Mac app) detect an in-place re-rip even at an unchanged size
            st = os.stat(result["path"])
            headers["X-Review-Source-Rev"] = "%d-%d" % (st.st_mtime_ns, st.st_size)
        except OSError:
            pass
        return rangeserve.serve(self, result["path"], result["ctype"],
                                extra_headers=headers)

    POST_OPS = {"/api/align/slice": "slice",
                "/api/align/refine": "refine",
                "/api/align/context": "context",
                "/api/align/overview": "overview"}
    REVIEW_NOTE_PATH = "/api/align/review-note"
    COMPILE_PATH = "/api/align/compile-summary"

    def do_POST(self):
        if not self._gate():
            return
        path = self.path.split("?", 1)[0]
        op = self.POST_OPS.get(path)
        if op is None and path not in (self.REVIEW_NOTE_PATH, self.COMPILE_PATH):
            return self.send_error(404)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"null")
        except (ValueError, OSError):
            return self._json({"error": "bad JSON body"}, 400)
        if path == self.REVIEW_NOTE_PATH:
            # DP-22 §6.6/§6.7: the fsynced append IS the ack; the standing-branch
            # commit is downstream batching, so nudge the committer and move on
            result, status = review_notes.handle_review_note(payload, _mp3_dir())
            if status == 200:
                review_sync.nudge()
            return self._json(result, status)
        if path == self.COMPILE_PATH:
            # DP-24 §3: the export button's job — compile labels/automated/ +
            # labels/review/ into labels/summary/ (whole, idempotent, fsynced);
            # A bodyless POST runs a real compile (idempotent; matches the review-note
            # route's parse). summary/ is in review_sync's PATHSPEC, so nudge the committer too
            result, status = summary_compile.handle_compile(payload)
            if status == 200 and not result.get("dry_run"):
                review_sync.nudge()
            return self._json(result, status)
        result, status = align_inspect.run_request(payload, _mp3_dir(), op=op)
        return self._json(result, status)

    def log_message(self, fmt, *args):  # quiet: one-line access log to stdout
        print("%s %s" % (self.address_string(), fmt % args))


def start_cache_policy():
    """The align server's share of the one cache policy.

    Register the venv's decoded-audio cache (align_inspect.register_cache — the same
    variable family the child reads, so cap, age and directory agree), then run the
    eviction once: the start run applies the cache's age limit and cap whatever else is
    or is not running on the machine, and this server is the one process that holds this
    registration. Both steps are skipped under NETRADIO_READ_ONLY, so an align server
    started from a worktree with a copied .env never evicts from a live cache; a no-op
    while the policy is dark (NETRADIO_CACHE_ROOT unset). Returns the run's summary, or
    None when skipped or the run failed.
    """
    if _read_only():
        return None
    align_inspect.register_cache()
    try:
        return cache_budget.run_eviction()
    except Exception as exc:   # noqa: BLE001 — an unwritable root must not kill the server
        print("cache policy run failed: %s" % exc)
        return None


def main():
    load_env()
    start_cache_policy()
    host = os.environ.get("NETRADIO_ALIGN_HOST", "127.0.0.1")
    port = int(os.environ.get("NETRADIO_ALIGN_PORT", "8766"))
    httpd = ThreadingHTTPServer((host, port), AlignHandler)
    # DP-22 §6.7: the review-note committer rides this server — a no-op when dark
    # (the checkout is not a git work-tree root, or the kill switch is set)
    review_sync.start()
    print("align inspector on http://%s:%d/align  (DSP %s; review-sync %s)"
          % (host, port,
             "enabled" if align_inspect.enabled() else
             "NOT CONFIGURED - needs .venv (make venv) and the originals "
             "(NETRADIO_SOURCES_DIR, else sources_local/)",
             "on -> branch %s" % review_sync.branch() if review_sync.enabled()
             else "dark"))
    httpd.serve_forever()


if __name__ == "__main__":
    main()
