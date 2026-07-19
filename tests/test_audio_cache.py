"""The decoded-audio cache stays bounded (it once grew to 26 GiB, unevicted).

Two guards, both exercised here with a fake disk + a stubbed decoder (no ffmpeg, no real disk):
the cache never exceeds CACHE_MAX_FRAC of the filesystem, and nothing is written once the disk
is DISK_FULL_FRAC full. A skipped/evicted entry is always safe -- it just re-decodes.

Sizes are chosen so a `.npy`'s ~128-byte numpy header is negligible against the entry, as it is
for the real MB-sized arrays; the cap then holds ~2 entries so eviction is actually exercised.
"""

import collections
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))

import numpy as np  # noqa: E402
from streamalign import audio  # noqa: E402

Usage = collections.namedtuple("Usage", "total used free")
DISK = 400_000                       # fake filesystem size
ENTRY_FLOATS = 2000                  # -> 8000 data bytes + ~128 header per .npy


class CacheBounding(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self._saved = (audio.CACHE_DIR, audio.shutil.disk_usage, audio._ffmpeg_decode,
                       audio.CACHE_MAX_FRAC, audio.DISK_FULL_FRAC)
        audio.CACHE_DIR = self.dir
        self._free = [DISK]
        audio.shutil.disk_usage = lambda p: Usage(DISK, DISK - self._free[0], self._free[0])
        audio.CACHE_MAX_FRAC = 0.05                       # cap = 20 000 bytes (~2 entries)
        audio.DISK_FULL_FRAC = 0.95
        audio._ffmpeg_decode = lambda path, sr, mono: np.zeros(ENTRY_FLOATS, dtype="<f4")

    def tearDown(self):
        (audio.CACHE_DIR, audio.shutil.disk_usage, audio._ffmpeg_decode,
         audio.CACHE_MAX_FRAC, audio.DISK_FULL_FRAC) = self._saved

    def _npy(self):
        return [n for n in os.listdir(self.dir) if n.endswith(".npy")]

    def _cache_bytes(self):
        return sum(os.path.getsize(os.path.join(self.dir, n)) for n in self._npy())

    def _load_distinct(self, i):
        src = os.path.join(self.dir, "src%d.bin" % i)     # distinct stat -> distinct cache key
        with open(src, "wb") as h:
            h.write(b"x")
        return audio.load_audio(src)

    def test_disk_full_writes_nothing(self):
        self._free[0] = 10_000                            # 97.5% used -> above the 95% guard
        out = self._load_distinct(0)
        self.assertEqual(len(out), ENTRY_FLOATS)          # still returns the signal
        self.assertEqual(self._npy(), [])                 # but cached nothing

    def test_cache_never_exceeds_the_cap(self):
        for i in range(8):
            self._load_distinct(i)
        self.assertLessEqual(self._cache_bytes(), int(DISK * audio.CACHE_MAX_FRAC))  # <= cap
        self.assertGreaterEqual(len(self._npy()), 1)      # yet it does cache

    def test_prune_evicts_oldest_first(self):
        for i in range(3):
            p = os.path.join(self.dir, "%d.npy" % i)
            with open(p, "wb") as h:
                h.write(b"\x00" * 8000)                    # 3 * 8000 = 24 000 > 20 000 cap
            os.utime(p, (100 + i, 100 + i))               # ascending mtime: 0 oldest
        audio._prune_cache(headroom_bytes=0)
        survivors = set(self._npy())
        self.assertNotIn("0.npy", survivors)              # oldest evicted first
        self.assertIn("2.npy", survivors)                 # newest kept

    def test_a_cache_hit_returns_without_recaching(self):
        first = self._load_distinct(0)
        n = len(self._npy())
        # same source stat -> same key -> a hit, no new file
        src = os.path.join(self.dir, "src0.bin")
        second = audio.load_audio(src)
        self.assertEqual(len(first), len(second))
        self.assertEqual(len(self._npy()), n)


if __name__ == "__main__":
    unittest.main()
