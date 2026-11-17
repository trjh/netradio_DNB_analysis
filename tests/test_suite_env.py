"""The suite never reaches a real bucket: the audio bucket's settings are removed for the run.

Importing this module removes them from the environment. `python -m unittest discover -s tests`
imports every test module before it runs a single test, so the scrub is in place for every test
whichever module happens to declare it; a test that needs a bucket sets the names itself, over
`tests/bucket_fake.py`, and restores them. A shell that exported a `.env`'s lines would otherwise
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


if __name__ == "__main__":
    unittest.main()
