"""DP-22 §6.8 — the align server's whole-file review-audio routes.

Hermetic: real HTTP against an in-process align server on an ephemeral port,
temp dirs for the MP3/sources roots. No ffmpeg anywhere — there is nothing to
transcode: wv-only originals are REFUSED (§6.8 step 3, decision made 2026-08-11).
Covers: identifier validation (traversal → 404), Range semantics (shared
`rangeserve.serve`, the /audio contract), the §6.8 resolution tiers including
the wv-only refusal, the size cap, read-only + auth gates.
"""

import base64
import http.client
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import align_inspect
import align_server
import review_audio

MP3_BYTES = b"ID3mp3-bytes-of-the-capture-stream-file!"


class RouteCase(unittest.TestCase):
    """A live align server over temp MP3/sources dirs (per-test env patch)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="review-audio-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.mp3_dir = os.path.join(self.tmp, "mp3")
        self.src_dir = os.path.join(self.tmp, "sources")
        os.makedirs(self.mp3_dir)
        os.makedirs(self.src_dir)
        env = mock.patch.dict(os.environ, {
            "NETRADIO_MP3_DIR": self.mp3_dir,
            "NETRADIO_SOURCES_DIR": self.src_dir,
            "NETRADIO_LABELS_DIR": "",
            "NETRADIO_AUTH_USER": "",
            "NETRADIO_AUTH_PASS": "",
            "NETRADIO_READ_ONLY": "",
            "NETRADIO_FFPROBE": "",
        })
        env.start()
        self.addCleanup(env.stop)
        root = mock.patch.object(align_inspect, "ROOT_DIR", "")   # no checkout: DSP dark
        root.start()
        self.addCleanup(root.stop)
        self.write(self.mp3_dir, "d019-040.mp3", MP3_BYTES)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), align_server.AlignHandler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.shutdown)

    @staticmethod
    def write(root, name, data):
        with open(os.path.join(root, name), "wb") as handle:
            handle.write(data)

    def req(self, path, headers=None, method="GET"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp, data


class TestStreamRoute(RouteCase):

    def test_serves_the_capture_mp3_in_full(self):
        resp, data = self.req("/api/align/audio/stream/d019-040")
        self.assertEqual(resp.status, 200)
        self.assertEqual(data, MP3_BYTES)
        self.assertEqual(resp.getheader("Content-Type"), "audio/mpeg")
        self.assertEqual(resp.getheader("Accept-Ranges"), "bytes")
        self.assertEqual(resp.getheader("X-Review-Source"), "d019-040.mp3")

    def test_unknown_stem_404(self):
        resp, _ = self.req("/api/align/audio/stream/d999-000")
        self.assertEqual(resp.status, 404)

    def test_traversal_attempts_404(self):
        # outside the MP3 dir, hidden behind dots/encodings — identifiers only
        secret = os.path.join(self.tmp, "secret.mp3")
        self.write(self.tmp, "secret.mp3", b"never served")
        self.assertTrue(os.path.exists(secret))
        for evil in ("../secret", "..%2Fsecret", "%2e%2e/secret",
                     "d019-040/../d019-040", "/etc/passwd"):
            resp, _ = self.req("/api/align/audio/stream/" + evil)
            self.assertEqual(resp.status, 404, evil)

    def test_symlinked_stem_escaping_the_root_404s(self):
        # a stem whose FILE resolves outside the MP3 dir must not be listed/served
        self.write(self.tmp, "outside.mp3", b"outside bytes")
        os.symlink(os.path.join(self.tmp, "outside.mp3"),
                   os.path.join(self.mp3_dir, "leak.mp3"))
        resp, _ = self.req("/api/align/audio/stream/leak")
        self.assertEqual(resp.status, 404)

    def test_oversize_refused_413(self):
        with mock.patch.object(review_audio, "MAX_REVIEW_BYTES", 10):
            resp, data = self.req("/api/align/audio/stream/d019-040")
        self.assertEqual(resp.status, 413)
        self.assertIn(b"size cap", data)


class TestSourceRevisionHeader(RouteCase):
    """X-Review-Source-Rev: the resolved file's mtime_ns-size revision token —
    rangeserve carries no ETag/Last-Modified (its own docstring), so this header
    is what lets a caching client (the DP-22 Mac app) revalidate; it must change
    on an in-place re-rip even when the byte size does not."""

    def test_rev_header_names_the_resolved_files_mtime_and_size(self):
        resp, _ = self.req("/api/align/audio/stream/d019-040")
        st = os.stat(os.path.join(self.mp3_dir, "d019-040.mp3"))
        self.assertEqual(resp.getheader("X-Review-Source-Rev"),
                         "%d-%d" % (st.st_mtime_ns, st.st_size))

    def test_same_size_in_place_rerip_changes_the_rev(self):
        resp1, _ = self.req("/api/align/audio/stream/d019-040")
        rev1 = resp1.getheader("X-Review-Source-Rev")
        # rewrite the SAME bytes (size unchanged), bump mtime — the re-rip case
        # the size-only key could not see
        path = os.path.join(self.mp3_dir, "d019-040.mp3")
        self.write(self.mp3_dir, "d019-040.mp3", MP3_BYTES)
        st = os.stat(path)
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 7_000_000_000))
        resp2, _ = self.req("/api/align/audio/stream/d019-040")
        rev2 = resp2.getheader("X-Review-Source-Rev")
        self.assertNotEqual(rev1, rev2)
        self.assertEqual(resp1.getheader("X-Review-Source"),
                         resp2.getheader("X-Review-Source"))
        self.assertEqual(rev1.split("-")[-1], rev2.split("-")[-1])  # same size

    def test_rev_header_present_on_the_orig_route_too(self):
        self.write(self.src_dir, "072-song.flac", b"fLaC" + b"\0" * 64)
        resp, _ = self.req("/api/align/audio/orig/72")
        st = os.stat(os.path.join(self.src_dir, "072-song.flac"))
        self.assertEqual(resp.getheader("X-Review-Source-Rev"),
                         "%d-%d" % (st.st_mtime_ns, st.st_size))


class TestRangeSemantics(RouteCase):
    """The shared Range contract (rangeserve), on the align server's route."""

    PATH = "/api/align/audio/stream/d019-040"

    def test_partial_206(self):
        resp, data = self.req(self.PATH, {"Range": "bytes=3-7"})
        self.assertEqual(resp.status, 206)
        self.assertEqual(data, MP3_BYTES[3:8])
        self.assertEqual(resp.getheader("Content-Range"),
                         "bytes 3-7/%d" % len(MP3_BYTES))
        self.assertEqual(resp.getheader("Content-Length"), "5")
        self.assertEqual(resp.getheader("X-Review-Source"), "d019-040.mp3")

    def test_open_ended_range(self):
        resp, data = self.req(self.PATH, {"Range": "bytes=10-"})
        self.assertEqual(resp.status, 206)
        self.assertEqual(data, MP3_BYTES[10:])

    def test_suffix_range_is_the_last_n_bytes(self):
        resp, data = self.req(self.PATH, {"Range": "bytes=-4"})
        self.assertEqual(resp.status, 206)
        self.assertEqual(data, MP3_BYTES[-4:])

    def test_beyond_eof_is_416_with_star_form(self):
        resp, _ = self.req(self.PATH, {"Range": "bytes=%d-" % (len(MP3_BYTES) + 5)})
        self.assertEqual(resp.status, 416)
        self.assertEqual(resp.getheader("Content-Range"),
                         "bytes */%d" % len(MP3_BYTES))

    def test_if_range_forces_the_whole_file(self):
        # no validators are issued, so any If-Range must degrade to a full 200
        resp, data = self.req(self.PATH, {"Range": "bytes=3-7",
                                          "If-Range": '"some-etag"'})
        self.assertEqual(resp.status, 200)
        self.assertEqual(data, MP3_BYTES)

    def test_multi_range_degrades_to_200(self):
        resp, data = self.req(self.PATH, {"Range": "bytes=0-1,4-5"})
        self.assertEqual(resp.status, 200)
        self.assertEqual(data, MP3_BYTES)


class TestOrigResolution(RouteCase):

    def test_unknown_or_malformed_number_404(self):
        for ref in ("7", "007x", "..", "../7", "7/../7", ""):
            resp, _ = self.req("/api/align/audio/orig/" + ref)
            self.assertEqual(resp.status, 404, ref)

    def test_tier_a_flac_sibling_wins(self):
        self.write(self.src_dir, "072-song.wv", b"wavpack-master")
        self.write(self.src_dir, "072-song.flac", b"fLaCsibling-bytes")
        resp, data = self.req("/api/align/audio/orig/72")
        self.assertEqual(resp.status, 200)
        self.assertEqual(data, b"fLaCsibling-bytes")
        self.assertEqual(resp.getheader("Content-Type"), "audio/flac")
        self.assertEqual(resp.getheader("X-Review-Source"), "072-song.flac")

    def test_tier_b_playable_original_served_directly(self):
        self.write(self.src_dir, "031-tune.mp3", b"mp3-original")
        resp, data = self.req("/api/align/audio/orig/31")
        self.assertEqual(resp.status, 200)
        self.assertEqual(data, b"mp3-original")
        self.assertEqual(resp.getheader("Content-Type"), "audio/mpeg")
        self.assertEqual(resp.getheader("X-Review-Source"), "031-tune.mp3")

    def test_tier_b_m4a_content_type(self):
        self.write(self.src_dir, "032-tune.m4a", b"m4a-original")
        resp, _ = self.req("/api/align/audio/orig/32")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader("Content-Type"), "audio/mp4")

    def test_wv_only_is_refused_503_naming_the_sync(self):
        # §6.8 tier (c), decision made 2026-08-11: no transcode fallback exists —
        # a wv-only original is refused with the remedy spelled out
        self.write(self.src_dir, "040-only.wv", b"wv-only-master")
        resp, data = self.req("/api/align/audio/orig/40")
        self.assertEqual(resp.status, 503)
        self.assertIn(b"wv-only", data)
        self.assertIn(b"DP-23", data)
        self.assertIn(b"FLAC sibling", data)
        self.assertIn(b"040-only.wv", data)

    def test_wav_only_is_refused_the_same_way(self):
        # wav/aif are lossless masters too — same tier, same refusal
        self.write(self.src_dir, "041-only.wav", b"wav-only-master")
        resp, data = self.req("/api/align/audio/orig/41")
        self.assertEqual(resp.status, 503)
        self.assertIn(b"wv-only", data)

    def test_refusal_lifts_the_moment_the_flac_sibling_appears(self):
        # the remedy actually works: after the DP-23 sync writes the sibling,
        # the same request serves it (tier a) with no restart or cache involved
        self.write(self.src_dir, "042-late.wv", b"wv-master")
        self.assertEqual(self.req("/api/align/audio/orig/42")[0].status, 503)
        self.write(self.src_dir, "042-late.flac", b"flac-arrived")
        resp, data = self.req("/api/align/audio/orig/42")
        self.assertEqual(resp.status, 200)
        self.assertEqual(data, b"flac-arrived")

    def test_tiers_a_and_b_need_no_tooling_at_all(self):
        # pure file serving: flac sibling + playable original, nothing spawned
        self.write(self.src_dir, "043-a.wv", b"wv")
        self.write(self.src_dir, "043-a.flac", b"flac-bytes")
        self.write(self.src_dir, "044-b.mp3", b"mp3-bytes")
        self.assertEqual(self.req("/api/align/audio/orig/43")[0].status, 200)
        self.assertEqual(self.req("/api/align/audio/orig/44")[0].status, 200)

    def test_ambiguous_number_picks_first_sorted_and_names_it(self):
        self.write(self.src_dir, "050-bbb.mp3", b"second-sorted")
        self.write(self.src_dir, "050-aaa.mp3", b"first-sorted")
        resp, data = self.req("/api/align/audio/orig/50")
        self.assertEqual(resp.status, 200)
        self.assertEqual(data, b"first-sorted")
        self.assertEqual(resp.getheader("X-Review-Source"), "050-aaa.mp3")

    def test_oversize_original_refused_413(self):
        self.write(self.src_dir, "051-big.flac", b"x" * 64)
        with mock.patch.object(review_audio, "MAX_REVIEW_BYTES", 10):
            resp, data = self.req("/api/align/audio/orig/51")
        self.assertEqual(resp.status, 413)
        self.assertIn(b"size cap", data)

    def test_unicode_filename_serves_with_a_percent_encoded_header(self):
        # header values are latin-1 on the wire; a unicode original name must be
        # percent-encoded, never crash the handler mid-response (review iter 1)
        name = "052-café—mix.mp3"
        self.write(self.src_dir, name, b"unicode-named-mp3")
        resp, data = self.req("/api/align/audio/orig/52")
        self.assertEqual(resp.status, 200)
        self.assertEqual(data, b"unicode-named-mp3")
        header = resp.getheader("X-Review-Source")
        self.assertEqual(header, urllib.parse.quote(name))
        self.assertEqual(urllib.parse.unquote(header), name)

    def test_directory_named_like_an_original_is_a_404(self):
        # a stray DIRECTORY must never be picked as a candidate (review iter 1):
        # it would 200 and then die opening the "file"
        os.makedirs(os.path.join(self.src_dir, "053-not-a-file.mp3"))
        resp, _ = self.req("/api/align/audio/orig/53")
        self.assertEqual(resp.status, 404)

    def test_directory_candidate_never_shadows_the_real_file(self):
        os.makedirs(os.path.join(self.src_dir, "054-aaa-dir.mp3"))
        self.write(self.src_dir, "054-bbb-real.mp3", b"real-file")
        resp, data = self.req("/api/align/audio/orig/54")
        self.assertEqual(resp.status, 200)
        self.assertEqual(data, b"real-file")
        self.assertEqual(resp.getheader("X-Review-Source"), "054-bbb-real.mp3")

    def test_no_sources_dir_is_a_404(self):
        with mock.patch.dict(os.environ, {"NETRADIO_SOURCES_DIR":
                                          os.path.join(self.tmp, "nope")}):
            resp, _ = self.req("/api/align/audio/orig/72")
        self.assertEqual(resp.status, 404)


class TestGateOnAudioRoutes(RouteCase):

    def test_read_only_hides_the_audio_routes(self):
        with mock.patch.dict(os.environ, {"NETRADIO_READ_ONLY": "1"}):
            for path in ("/api/align/audio/stream/d019-040",
                         "/api/align/audio/orig/72"):
                resp, _ = self.req(path)
                self.assertEqual(resp.status, 404, path)

    def test_auth_required_when_configured(self):
        with mock.patch.dict(os.environ, {"NETRADIO_AUTH_USER": "u",
                                          "NETRADIO_AUTH_PASS": "p"}):
            resp, _ = self.req("/api/align/audio/stream/d019-040")
            self.assertEqual(resp.status, 401)
            good = {"Authorization":
                    "Basic " + base64.b64encode(b"u:p").decode("ascii")}
            resp, data = self.req("/api/align/audio/stream/d019-040", good)
            self.assertEqual(resp.status, 200)
            self.assertEqual(data, MP3_BYTES)


if __name__ == "__main__":
    unittest.main()
