"""The audio bucket's settings: where the owned audio tiers live, read once per call.

    bucket = audio_bucket.bucket()       # a `bucketio.Bucket`, or None: nothing pulled or published

The bucket holds three kinds of object, each keyed by the name the code already uses for it:

    stream/master/<stem>.<wav|au>     a capture file, pulled for a decode (streamalign/audio.py)
    stream/mp3/<stem>.mp3             a capture's MP3, pulled for the align server (align_ids.py)
    stream/tracks/<NNN>.flac          a track cut out of the stream (extract_tracks.py)

Three settings name it, each its own value, never the signature bucket's:

    NETRADIO_AUDIO_BUCKET          the bucket's name; unset, the bucket is off
    NETRADIO_AUDIO_S3_ENDPOINT     an --endpoint-url for an S3-compatible provider; unset, none
    NETRADIO_AUDIO_AWS_PROFILE     an --profile for the CLI; unset, the CLI's default

`NETRADIO_AWS_CLI` (shared with the signature store) names the `aws` executable.

With no bucket named, or no `aws` CLI to be found, `bucket()` returns None and every caller
behaves as it did before there was a bucket: a missing capture is reported missing, and a cut
is written locally and published nowhere. Nothing in this repo assumes the bucket exists.

This module reads settings and names keys. Every bucket call goes through `bucketio`.
"""

import os

import bucketio

MASTER_PREFIX = "stream/master/"
MP3_PREFIX = "stream/mp3/"
TRACKS_PREFIX = "stream/tracks/"

# The capture file extensions a master object may carry, in the order a stem resolves them.
MASTER_EXTS = (".wav", ".au")

_warned = set()


def bucket():
    """The audio bucket as a `bucketio.Bucket`, or None when it is off: no bucket named, or a
    bucket named with no `aws` CLI to be found (printed once per process)."""
    name = os.environ.get("NETRADIO_AUDIO_BUCKET", "").strip()
    if not name:
        return None
    try:
        return bucketio.Bucket(name,
                               endpoint=os.environ.get("NETRADIO_AUDIO_S3_ENDPOINT", ""),
                               profile=os.environ.get("NETRADIO_AUDIO_AWS_PROFILE", ""),
                               aws_cli=os.environ.get("NETRADIO_AWS_CLI", ""))
    except bucketio.BucketError as exc:
        if str(exc) not in _warned:
            _warned.add(str(exc))
            print("audio bucket off: %s" % exc, flush=True)
        return None


def master_key(stem, listed):
    """The `stream/master/` key for `stem` in a listing (a dict keyed by full key), or None.
    `.wav` is preferred to `.au` when the bucket somehow holds both."""
    for ext in MASTER_EXTS:
        key = MASTER_PREFIX + stem + ext
        if key in listed:
            return key
    return None


def track_key(num):
    """The key of track `num`'s cut: the three-digit track number, never its title."""
    return "%s%03d.flac" % (TRACKS_PREFIX, int(num))
