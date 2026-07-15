"""Tests for the ACRCloud/AudD music-ID tool (no network, no keys, no ffmpeg).

The HTTP layer (`_post`) and the audio carving are stubbed, so these pin the parts that are
easy to get subtly wrong: the window placement, the per-service multipart file-field name (a
real bug -- AudD rejects anything but `file` with error 700), the ACRCloud HMAC signature,
response parsing into hits, and -- importantly -- that a service ERROR surfaces instead of
masquerading as "no match".
"""

import base64
import hashlib
import hmac
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))

import identify_by_api as m  # noqa: E402


class WindowPlacement(unittest.TestCase):
    def test_interior_avoids_both_edges(self):
        w = m.window_starts(200.0, 4, length=12.0)
        self.assertEqual(len(w), 4)
        self.assertGreaterEqual(w[0], 10.0)              # past the head edge
        self.assertLessEqual(w[-1] + 12.0, 190.0)        # ends before the tail edge
        self.assertTrue(all(w[i] < w[i + 1] for i in range(3)))  # spread, ascending

    def test_single_window_is_centred(self):
        self.assertAlmostEqual(m.window_starts(200.0, 1, length=12.0)[0], (18.0 + 170.0) / 2, 1)

    def test_too_short_clip_falls_back_to_one(self):
        w = m.window_starts(10.0, 4, length=12.0)
        self.assertEqual(len(w), 1)
        self.assertEqual(w[0], 0.0)                      # whole (short) clip, clamped to 0


class MultipartFieldName(unittest.TestCase):
    def test_acrcloud_uses_sample_audd_uses_file(self):
        _ct, acr = m._multipart({"access_key": "k"}, b"WAV", file_field="sample")
        self.assertIn(b'name="sample"; filename="sample.wav"', acr)
        _ct, audd = m._multipart({"api_token": "t"}, b"WAV", file_field="file")
        self.assertIn(b'name="file"; filename="sample.wav"', audd)   # regression: not `sample`
        self.assertIn(b"WAV", audd)
        self.assertTrue(audd.rstrip().endswith(b"--"))               # closing boundary


class AcrCloudSignature(unittest.TestCase):
    def test_signature_is_the_documented_hmac_sha1(self):
        captured = {}

        def fake_post(url, ctype, body, timeout=45):
            captured["body"] = body
            return {"status": {"code": 1001}}          # no result -> no hits, no raise

        m._post = fake_post
        m.acrcloud_identify(b"WAV", ("host", "AK", "SECRET"), now=1700000000)
        # the signature the request must carry, per ACRCloud's documented string-to-sign
        to_sign = "\n".join(["POST", "/v1/identify", "AK", "audio", "1", "1700000000"])
        want = base64.b64encode(
            hmac.new(b"SECRET", to_sign.encode(), hashlib.sha1).digest()).decode()
        self.assertIn(want.encode(), captured["body"])        # it went out in the multipart body


class ResponseParsing(unittest.TestCase):
    def tearDown(self):
        # restore in case a test swapped _post
        import importlib
        importlib.reload(m)

    def test_acrcloud_returns_every_music_hit(self):
        m._post = lambda *a, **k: {"status": {"code": 0}, "metadata": {"music": [
            {"title": "T1", "artists": [{"name": "A1"}], "album": {"name": "Alb"},
             "label": "Lbl", "release_date": "1998-01-01", "score": 90, "external_metadata": {}},
            {"title": "T2", "artists": [{"name": "A2"}], "external_metadata": {}}]}}
        hits = m.acrcloud_identify(b"WAV", ("h", "k", "s"))
        self.assertEqual([h["title"] for h in hits], ["T1", "T2"])     # all hits, not just #1
        self.assertEqual(hits[0]["year"], "1998")
        self.assertEqual(hits[0]["service"], "ACRCloud")

    def test_acrcloud_1001_is_no_match_not_an_error(self):
        m._post = lambda *a, **k: {"status": {"code": 1001, "msg": "No result"}}
        self.assertEqual(m.acrcloud_identify(b"WAV", ("h", "k", "s")), [])

    def test_acrcloud_auth_error_is_raised_not_masked(self):
        m._post = lambda *a, **k: {"status": {"code": 3001, "msg": "Invalid access key"}}
        with self.assertRaises(RuntimeError):
            m.acrcloud_identify(b"WAV", ("h", "k", "s"))

    def test_audd_success_result(self):
        m._post = lambda *a, **k: {"status": "success", "result": {
            "artist": "Skyjuice", "title": "The Rope-a-Dope", "album": "No Categories",
            "label": "Ubiquity", "release_date": "1998-01-01", "song_link": "https://lis.tn/x"}}
        hits = m.audd_identify(b"WAV", "tok")
        self.assertEqual(len(hits), 1)
        self.assertEqual((hits[0]["artist"], hits[0]["year"], hits[0]["service"]),
                         ("Skyjuice", "1998", "AudD"))

    def test_audd_null_result_is_no_match(self):
        m._post = lambda *a, **k: {"status": "success", "result": None}
        self.assertEqual(m.audd_identify(b"WAV", "tok"), [])

    def test_audd_error_is_raised_not_masked(self):
        # the exact failure the raw test caught: error 700 was being swallowed as "no match"
        m._post = lambda *a, **k: {"status": "error",
                                   "error": {"error_code": 700, "error_message": "no file"}}
        with self.assertRaises(RuntimeError):
            m.audd_identify(b"WAV", "")


class ServiceResolution(unittest.TestCase):
    def setUp(self):
        for k in ("ACRCLOUD_HOST", "ACRCLOUD_ACCESS_KEY", "ACRCLOUD_ACCESS_SECRET",
                  "AUDD_API_TOKEN"):
            os.environ.pop(k, None)

    def test_no_creds_no_services(self):
        self.assertEqual(m.resolve_services("all"), [])

    def test_only_configured_services_appear(self):
        os.environ["AUDD_API_TOKEN"] = "tok"
        names = [n for n, _ in m.resolve_services("all")]
        self.assertEqual(names, ["AudD"])
        os.environ.update(ACRCLOUD_HOST="h", ACRCLOUD_ACCESS_KEY="k", ACRCLOUD_ACCESS_SECRET="s")
        self.assertEqual({n for n, _ in m.resolve_services("all")}, {"AudD", "ACRCloud"})
        self.assertEqual([n for n, _ in m.resolve_services("audd")], ["AudD"])


class Formatting(unittest.TestCase):
    def test_fmt_hit_reads_cleanly(self):
        line = m._fmt_hit(m._hit("AudD", artist="A", title="T", album="Alb", year="1998"))
        self.assertIn("AudD", line)
        self.assertIn("A - T", line)
        self.assertIn("1998", line)


if __name__ == "__main__":
    unittest.main()
