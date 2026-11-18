"""The suite never reaches a real bucket: the audio bucket's settings are removed for the run.

Importing this module removes them from the environment. `python -m unittest discover -s tests`
imports every test module before it runs a single test, so the scrub is in place for every test
whichever module happens to declare it. A run of one module (`python -m unittest test_hints`
from tests/, say) imports only that module, so `tests/bucket_fake.py` and every test module
that reaches bucket code import this one too. A test that needs a bucket sets the names itself,
over `tests/bucket_fake.py`, and restores them. A shell that exported a `.env`'s lines would otherwise
hand a test the real bucket's name, and a pull or a publish in a test would reach it.
"""

import os
import unittest

AUDIO_BUCKET_NAMES = ("NETRADIO_AUDIO_BUCKET", "NETRADIO_AUDIO_S3_ENDPOINT",
                      "NETRADIO_AUDIO_AWS_PROFILE")

for _name in AUDIO_BUCKET_NAMES:
    os.environ.pop(_name, None)


class TestTheAudioBucketIsScrubbed(unittest.TestCase):
    def test_no_audio_bucket_setting_reaches_a_test(self):
        for name in AUDIO_BUCKET_NAMES:
            self.assertNotIn(name, os.environ, name)


class TheBucketHelperScrubsOnItsOwn(unittest.TestCase):
    def test_importing_the_bucket_fake_alone_removes_the_settings(self):
        """A run of one module never imports this file through discovery: the bucket helper
        imports it itself, so a shell that exported the bucket's name reaches no test."""
        import subprocess
        import sys
        here = os.path.dirname(os.path.abspath(__file__))
        env = dict(os.environ, NETRADIO_AUDIO_BUCKET="a-real-bucket",
                   PYTHONPATH=os.path.join(os.path.dirname(here), "scripts"))
        out = subprocess.run([sys.executable, "-c",
                              "import os, bucket_fake; "
                              "print(os.environ.get('NETRADIO_AUDIO_BUCKET'))"],
                             cwd=here, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "None")


if __name__ == "__main__":
    unittest.main()
