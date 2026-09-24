"""The `--key` flag on `scripts/make_canary.py` prints the pool's key for a URL.

The key is the pool's one rule (docs/HARVEST_FEED.md): `u` + the first 20 hex of the
SHA-1 of the URL as given, fragment included. The harvester's canary pass re-signs the
canary's file and compares it with the signature stored under this key (`NETRADIO_CANARY_KEY` in
`.env`). `--key` is how the operator computes it from the canary's URL; the same rule
the one-line recipe in `.env.example` uses, callable from a script.

No network, no audio: the key is a pure hash of the URL string.
"""

import contextlib
import hashlib
import io
import os
import sys
import unittest
from unittest import mock

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)

try:
    import make_canary                      # noqa: E402
except Exception:                            # numpy absent -> not this test's job
    make_canary = None


@unittest.skipUnless(make_canary, "make_canary.py needs numpy -- not this test's job")
class SigKeyFor(unittest.TestCase):
    """`sig_key_for(url)` is the pool's one key rule, the same stem every signature in
    the bucket is filed under. A regression that changed the hash function, the prefix
    or the truncation would break the join between the canary's signature and the key
    the harvester reads from `.env` -- so the rule is pinned here, against the published
    contract."""

    def test_the_key_is_u_plus_the_first_20_hex_of_the_sha1(self):
        url = "https://example.invalid/watch?v=abc#t=3600,7200"
        expected = "u" + hashlib.sha1(url.encode()).hexdigest()[:20]
        self.assertEqual(make_canary.sig_key_for(url), expected)

    def test_the_key_includes_the_fragment_and_query_string(self):
        """The URL is hashed AS GIVEN -- query string and `#t=` media fragment included,
        exactly the string the feed's own record carries. Stripping the fragment would
        file the canary under a different key than the feeder used, and the harvester
        would never find it."""
        base = "https://example.invalid/watch?v=abc"
        with_fragment = base + "#t=3600,7200"
        self.assertNotEqual(make_canary.sig_key_for(base),
                            make_canary.sig_key_for(with_fragment),
                            "the fragment changes the key")

    def test_the_key_is_deterministic_for_the_same_url(self):
        url = "https://example.invalid/x"
        self.assertEqual(make_canary.sig_key_for(url), make_canary.sig_key_for(url))

    def test_different_urls_get_different_keys(self):
        self.assertNotEqual(make_canary.sig_key_for("https://a/x"),
                             make_canary.sig_key_for("https://b/x"))


@unittest.skipUnless(make_canary, "make_canary.py needs numpy -- not this test's job")
class KeyCommandLine(unittest.TestCase):
    """`make_canary.py --key <url>` prints the key and exits. It does not require
    `--out`, `--source-dir`, or any audio, and it does not run the canary build."""

    def _run(self, argv):
        out = io.StringIO()
        with mock.patch.object(sys, "argv", argv), \
             contextlib.redirect_stdout(out):
            make_canary.main()
        return out.getvalue().strip()

    def test_prints_the_key_for_the_url_and_exits(self):
        url = "https://example.invalid/watch?v=abc#t=3600,7200"
        printed = self._run(["make_canary.py", "--key", url])
        self.assertEqual(printed, make_canary.sig_key_for(url))
        self.assertTrue(printed.startswith("u"))
        self.assertEqual(len(printed), 21)      # `u` + 20 hex chars

    def test_does_not_require_out_or_source_dir(self):
        # Before the `--key` flag landed, `--out` was required. Now `--key` exits before
        # that check, so a caller printing the key needs no `--out` or `--source-dir`.
        printed = self._run(["make_canary.py", "--key", "https://x"])
        self.assertEqual(printed, make_canary.sig_key_for("https://x"))


if __name__ == "__main__":
    unittest.main()
